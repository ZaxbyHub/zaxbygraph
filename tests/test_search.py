from __future__ import annotations

from fixtures import REPO, TempDBTest, issue
from zaxbygraph.query import search


class SearchQualityTests(TempDBTest):
    """Issue #4 acceptance checks: search must answer natural-language
    queries instead of silently returning zero hits.

    The annotation-field names asserted in AC5 are part of the contract: an
    item surfaced by a comment-only hit carries `matching_comments` (count of
    that item's comments matching the query) and `comment_snippet` (a snippet
    from one matching comment that contains the matched term).
    """

    def _seed_corpus(self) -> None:
        """The 4-row corpus from 02-reproduction.md, one row per defect signal.

        #1 is the natural-language target (title match, updated 01-03); #2
        matches "memory" ONLY in its body and is strictly the NEWEST row (the
        recency-vs-relevance signal); #3 is filler; #4 contains only derived
        reconnect forms ("reconnection"/"reconnecting", never "reconnect").
        """
        self.src.add_issue(
            issue(
                1,
                title="WebSocket reconnect leaks memory",
                body="Every reconnection attempt leaks a timer; reconnecting twice doubles it.",
                updated_at="2026-01-03T00:00:00Z",
            )
        )
        self.src.add_issue(
            issue(
                2,
                title="Init hangs on empty repo",
                body="Memory grows each time the init path waits on an empty repository listing.",
                updated_at="2026-01-09T00:00:00Z",
            )
        )
        self.src.add_issue(
            issue(
                3,
                title="Sync state looks healthy after crash",
                body="last_error was swallowed so sync looked complete after the crash.",
                updated_at="2026-01-06T00:00:00Z",
            )
        )
        self.src.add_issue(
            issue(
                4,
                title="Timer cleanup on socket teardown",
                body="reconnection and reconnecting both drop the timer.",
                updated_at="2026-01-04T00:00:00Z",
            )
        )
        self.sync()

    def test_natural_language_query_finds_relevant_item(self) -> None:
        """AC1: a natural-language query over a corpus that contains a
        relevant item returns that item FIRST, not zero hits."""
        self._seed_corpus()
        data = search(self.conn, "memory leak in websocket reconnect", repo=REPO)
        titles = [i["title"] for i in data["items"]]
        self.assertEqual(titles[:1], ["WebSocket reconnect leaks memory"])

    def test_bm25_title_weighting_beats_recency(self) -> None:
        """AC2: between two items matching a term, the one matching in its
        TITLE ranks first even though its updated_at is strictly older than
        the body-only match (recency ordering provably puts the body-only
        item first on the buggy base).

        Fixture constraint, verified against the FTS5 bm25 mechanics: when a
        term matches MORE than half the corpus its IDF collapses toward zero
        and title-vs-body weighting becomes indistinguishable. So this is a
        6-item corpus where EXACTLY TWO items match "memory" -- one in the
        title (older), one only in the body (newest) -- and the other four
        rows share no stem with the term (porter stems "memory" and
        "memories" together, so no memori-* filler either).
        """
        self.src.add_issue(
            issue(
                21,
                title="Memory spike during sync",
                body="unrelated notes",
                updated_at="2026-01-01T00:00:00Z",
            )
        )
        self.src.add_issue(
            issue(
                22,
                title="Unrelated heading",
                body="a memory usage note",
                updated_at="2026-02-01T00:00:00Z",
            )
        )
        self.src.add_issue(
            issue(23, title="Init hangs on empty repo", body="the listing path waits forever", updated_at="2026-01-05T00:00:00Z")
        )
        self.src.add_issue(
            issue(24, title="Timer cleanup on socket teardown", body="drop the timer on teardown", updated_at="2026-01-06T00:00:00Z")
        )
        self.src.add_issue(
            issue(25, title="Sync state looks healthy after crash", body="last_error was swallowed", updated_at="2026-01-07T00:00:00Z")
        )
        self.src.add_issue(
            issue(26, title="Rename helpers for clarity", body="mechanical rename only", updated_at="2026-01-08T00:00:00Z")
        )
        self.sync()
        data = search(self.conn, "memory", repo=REPO)
        titles = [i["title"] for i in data["items"]]
        self.assertEqual(len(titles), 2)  # exactly the two items match the term
        self.assertEqual(titles[0], "Memory spike during sync")
        # a snippet is still returned for every hit, highlighting the matched
        # term (the existing « » markers) in the column it matched.
        self.assertIn("«Memory»", data["items"][0]["snippet"])

    def test_porter_stemming(self) -> None:
        """AC3: `search "reconnect"` matches an item whose text contains only
        the derived forms "reconnection"/"reconnecting" (never the literal)."""
        self._seed_corpus()
        data = search(self.conn, "reconnect", repo=REPO)
        numbers = [i["number"] for i in data["items"]]
        self.assertIn(4, numbers)

    def test_fallback_reports_mode_and_corpus_size(self) -> None:
        """AC4: (a) when no item contains every token, the OR fallback still
        returns the relevant items and reports matched_mode "any"; (b) when
        the tokens co-occur and the AND page is already full, matched_mode is
        "all"; (c) when nothing matches, total_matches 0 and corpus_items N
        distinguish "no hits in N items" from an empty corpus."""
        self._seed_corpus()
        # (a) "memory" (items 1, 2) and "teardown" (item 4) never co-occur,
        # so a strict AND of both tokens can match nothing.
        fb = search(self.conn, "memory teardown", repo=REPO)
        self.assertIn("matched_mode", fb)
        self.assertEqual(fb["matched_mode"], "any")
        nums = [i["number"] for i in fb["items"]]
        self.assertIn(1, nums)
        self.assertIn(4, nums)
        # (b) both tokens co-occur in item 1's title; with limit=1 the AND
        # pass already fills the page, so no fallback ran.
        co = search(self.conn, "websocket memory", repo=REPO, limit=1)
        self.assertEqual(co.get("matched_mode"), "all")
        self.assertTrue(co["items"])
        # (c) nothing matches "quokka" anywhere in the 4-item corpus.
        zm = search(self.conn, "quokka", repo=REPO)
        self.assertEqual(zm.get("total_matches"), 0)
        self.assertEqual(zm.get("corpus_items"), 4)

    def test_comment_hits_merge_into_parent_item(self) -> None:
        """AC5: a term appearing only inside a comment of item #N surfaces #N
        exactly once in the items list, annotated with the matching-comment
        count (`matching_comments`) and a comment snippet containing the term
        (`comment_snippet`) -- not stranded in a separate unranked list."""
        self.src.add_issue(issue(7, title="Crash during shutdown", body="stack trace attached"))
        self.src.add_issue(issue(8, title="Unrelated noise", body="nothing to see"))
        self.src.comment_on(7, "the zephyr build fails on this exact test")
        self.sync()
        data = search(self.conn, "zephyr", repo=REPO)
        numbers = [i["number"] for i in data["items"]]
        self.assertIn(7, numbers)
        self.assertEqual(numbers.count(7), 1)
        row = next(i for i in data["items"] if i["number"] == 7)
        self.assertGreaterEqual(row.get("matching_comments"), 1)
        # the snippet highlights the matched term with the « » markers
        self.assertIn("«zephyr»", row.get("comment_snippet", ""))


class TokenizerParityTests(TempDBTest):
    """Guardrail: schema.sql (fresh DBs) and the v2->v3 migration DDL (rebuilt
    DBs) must agree on the porter tokenizer (issue #4 AC6/AC7)."""

    def test_fresh_db_and_migrated_db_share_tokenizer(self) -> None:
        for table in ("items_fts", "comments_fts"):
            ddl = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
            ).fetchone()[0]
            self.assertIn("porter", ddl.lower(), f"{table} must use the porter tokenizer")


if __name__ == "__main__":
    unittest.main()
