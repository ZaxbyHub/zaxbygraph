"""Edge-case pins for the issue #4 search rewrite (non-frozen supplements to
tests/test_search.py): empty and stopword-only queries, output-shape pins,
exact comment counts beyond the page size, the fallback superset property,
total_matches semantics, and genuine fresh-shape vs migrated tokenizer
parity."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, TempDBTest, issue
from zaxbygraph.db import connect, init_schema
from zaxbygraph.query import build_match_queries, search


class SearchEdgeTests(TempDBTest):
    def seed_corpus(self) -> None:
        # default updated_at (early Jan 1) keeps comment_on()'s synthetic
        # bump ahead of the watermark, so the second sync re-fetches item 3
        self.src.add_issue(
            issue(1, title="WebSocket reconnect leaks memory",
                  body="Every reconnection attempt leaks a timer.")
        )
        self.src.add_issue(
            issue(2, title="Init hangs on empty repo", body="Memory grows each time.")
        )
        self.src.add_issue(
            issue(3, title="Unrelated filler", body="nothing relevant here")
        )
        self.sync()

    def test_empty_query_is_honest_zero_hits(self) -> None:
        self.seed_corpus()
        for raw in ("", "   "):
            data = search(self.conn, raw, repo=REPO)
            self.assertEqual(data["items"], [])
            self.assertEqual(data["matched_mode"], "all")
            self.assertEqual(data["total_matches"], 0)
            self.assertEqual(data["corpus_items"], 3)

    def test_all_stopword_query_does_not_broaden_or_crash(self) -> None:
        self.seed_corpus()
        data = search(self.conn, "the of and", repo=REPO)
        # every token is a stopword, so broadening cannot change the match
        # set and the strict pass answers: pin the exact shape, not just the
        # presence of the fields
        self.assertEqual(data["matched_mode"], "all")
        self.assertEqual(data["items"], [])
        self.assertEqual(data["total_matches"], 0)
        self.assertEqual(data["corpus_items"], 3)

    def test_index_stale_false_on_fresh_zero_hit(self) -> None:
        """index_stale polarity pin: a fresh (current-schema) DB that also
        returns zero hits must report False — the flag tracks schema age,
        not hit count."""
        self.seed_corpus()
        data = search(self.conn, "quokka", repo=REPO)
        self.assertEqual(data["total_matches"], 0)
        self.assertEqual(data["index_stale"], False)

    def test_corpus_items_counts_whole_store_without_repo_filter(self) -> None:
        """repo=None (no --repo resolved / explicit empty) scopes
        corpus_items to the whole store, not one repo."""
        import tempfile as _tf

        from zaxbygraph.db import connect as _connect
        from zaxbygraph.db import init_schema as _init
        from zaxbygraph.sync import sync_repo as _sync

        td = _tf.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        db_path = Path(td.name) / "multi.db"
        conn = _connect(db_path)
        self.addCleanup(conn.close)
        _init(conn)
        src = FakeGitHubSource()
        for n in (1, 2):
            src.add_issue(issue(n, title=f"widget memory {n}", body="x"))
        _sync(conn, src, "acme/widget")
        src2 = FakeGitHubSource()
        other = issue(1, title="other repo filler", body="no match")
        other["id"] = 9101  # distinct item id: ids collide across repos by fixture
        src2.add_issue(other)
        _sync(conn, src2, "other/repo")
        whole = search(conn, "memory")
        self.assertEqual(whole["corpus_items"], 3)
        self.assertEqual(whole["total_matches"], 2)
        scoped = search(conn, "memory", repo="acme/widget")
        self.assertEqual(scoped["corpus_items"], 2)
        self.assertEqual(scoped["total_matches"], 2)

    def test_no_separate_comments_section(self) -> None:
        self.seed_corpus()
        self.src.comment_on(3, "a zephyr note")
        self.sync()
        data = search(self.conn, "zephyr", repo=REPO)
        self.assertNotIn("comments", data)
        self.assertEqual([i["number"] for i in data["items"]], [3])

    def test_matching_comments_exact_beyond_page_size(self) -> None:
        self.seed_corpus()
        for i in range(7):
            self.src.comment_on(3, f"zephyr follow-up number {i}")
        self.sync()
        # the page shows one item, but the count is the full comment tally
        data = search(self.conn, "zephyr", repo=REPO, limit=1)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["matching_comments"], 7)
        self.assertIn("«zephyr»", data["items"][0]["comment_snippet"])

    def test_fallback_keeps_strict_hits_on_the_page(self) -> None:
        """The fallback must never evict strict (all-tokens) hits from the
        page: when the broadened pass fills the page, every strict hit keeps
        a slot ahead of broadened-only rows. The fixture makes the strict
        hit's BROADENED rank provably beyond the limit (its matches sit in a
        length-crushed long body, drowned by short-title matches of each
        single token) — under the old replace-the-page behavior the strict
        hit vanished from the default page entirely."""
        long_body = (
            "alpha beta " + "padding words to crush the length normalization " * 8
        )
        self.src.add_issue(
            issue(1, title="zzz unrelated", body=long_body,
                  updated_at="2026-01-01T00:00:00Z")
        )
        for i in range(2, 8):
            self.src.add_issue(
                issue(i, title=f"alpha padding {i}", body="nothing here",
                      updated_at=f"2026-01-{i:02d}T00:00:00Z")
            )
        for i in range(8, 14):
            self.src.add_issue(
                issue(i, title=f"beta padding {i}", body="nothing here",
                      updated_at=f"2026-01-{i:02d}T00:00:00Z")
            )
        self.sync()
        limit = 3
        from zaxbygraph.query import _run_search_pass, build_match_queries
        all_query, any_query = build_match_queries("alpha beta")
        # precondition, deterministic rather than corpus luck: the strict
        # hit's broadened rank is beyond the limit
        _, or_page = _run_search_pass(self.conn, any_query, REPO, 99)
        or_keys = [r["hit_key"] for r in or_page]
        strict_key = "acme/forgegate#1"
        self.assertIn(strict_key, or_keys)
        self.assertGreaterEqual(or_keys.index(strict_key), limit)
        # the composition fix: the strict hit survives, ahead of the
        # broadened-only rows
        data = search(self.conn, "alpha beta", repo=REPO, limit=limit)
        self.assertEqual(data["matched_mode"], "any")
        numbers = [i["number"] for i in data["items"]]
        self.assertEqual(numbers[0], 1)
        self.assertEqual(len(numbers), limit)
        self.assertEqual(len(set(numbers)), limit)
        # total_matches stays the honest broadened-set count
        self.assertEqual(data["total_matches"], 13)

    def test_labels_outrank_body(self) -> None:
        """The shipped weights (title 10, body 1, labels 3) put a label-only
        match above a body-only match — the documented order no earlier
        fixture exercised (labels_text was never populated)."""
        self.src.add_issue(
            issue(1, title="unrelated title one", body="nothing here",
                  labels=[{"name": "memory", "color": "ff0000"}],
                  updated_at="2026-01-01T00:00:00Z")
        )
        self.src.add_issue(
            issue(2, title="unrelated title two", body="a memory note in prose",
                  updated_at="2026-01-02T00:00:00Z")
        )
        for i in (3, 4, 5):
            self.src.add_issue(
                issue(i, title=f"filler {i}", body="nothing to match",
                      updated_at=f"2026-01-0{i}T00:00:00Z")
            )
        self.sync()
        data = search(self.conn, "memory", repo=REPO)
        self.assertEqual([i["number"] for i in data["items"]][:2], [1, 2])

    def test_same_item_dual_match_uses_best_score(self) -> None:
        """MIN(score) aggregation pin with a GENUINE dual-source group: item
        1 matches in its title (strong weighted score) AND via a long padded
        comment (weak score) — MIN keeps its title score, MAX would take the
        weak comment score and let the short-comment item 2 outrank it.
        Mutation-verified: MIN→MAX flips the page to [2, 1] and fails this
        test. Item 3 (comment-only) also surfaces once."""
        self.src.add_issue(
            issue(1, title="zephyr scheduler",
                  body="unrelated prose",
                  updated_at="2026-01-01T00:00:00Z")
        )
        self.src.add_issue(
            issue(2, title="unrelated two", body="nothing",
                  updated_at="2026-01-02T00:00:00Z")
        )
        self.src.add_issue(
            issue(3, title="unrelated three", body="nothing",
                  updated_at="2026-01-03T00:00:00Z")
        )
        self.src.comment_on(
            1, "zephyr " + "padding words diluting this comment's score " * 10
        )
        self.src.comment_on(3, "zephyr")
        self.sync()
        data = search(self.conn, "zephyr", repo=REPO)
        numbers = [i["number"] for i in data["items"]]
        # MIN keeps item 1's strong title score ahead of item 3's strong
        # comment score; under MAX item 1 takes its own weak comment score
        # and the page flips to [3, 1]
        self.assertEqual(numbers, [1, 3])
        self.assertEqual(numbers.count(3), 1)
        # the title hit's snippet is the item-text snippet (all_query block)
        row1 = next(i for i in data["items"] if i["number"] == 1)
        self.assertIn("zephyr", row1["snippet"].lower())
        self.assertEqual(row1["matching_comments"], 1)

    def test_total_matches_counts_merged_items_pre_limit(self) -> None:
        self.seed_corpus()
        self.src.comment_on(3, "memory pressure in the zephyr pool")
        self.sync()
        # "memory" text-matches items 1 and 2 and comment-matches item 3:
        # three distinct merged items, even with a one-row page.
        data = search(self.conn, "memory", repo=REPO, limit=1)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["total_matches"], 3)
        self.assertEqual(data["corpus_items"], 3)

    def test_build_match_queries_quotes_and_broadens(self) -> None:
        all_q, any_q = build_match_queries("memory OR 1; NEAR(a b)")
        # every token is quoted, so FTS5 operators inside user input are inert
        self.assertIn('"OR"', all_q)
        self.assertIn('"NEAR(a"', all_q)
        self.assertNotIn(" OR ", all_q)
        self.assertIn(" OR ", any_q)
        self.assertNotIn(" AND ", any_q)
        # "or" is a stopword: dropped from the broadened query
        self.assertNotIn('"OR"', any_q)

    def test_single_token_queries_report_all_mode(self) -> None:
        self.seed_corpus()
        data = search(self.conn, "memory", repo=REPO)
        self.assertEqual(data["matched_mode"], "all")

    def test_equal_scores_break_ties_by_recency(self) -> None:
        """The approved plan's tie-break: among identical bm25 scores, the
        more recently updated item ranks first. The numbers are chosen so
        lexicographic hit_key order DISAGREES with recency: #9 is newer but
        "#30" < "#9" as strings, so removing the tie-break flips the
        assertion (verified by mutation probe)."""
        later = issue(
            9,
            title="duplicate probe",
            body="identical text",
            updated_at="2026-06-01T00:00:00Z",
        )
        earlier = issue(
            30,
            title="duplicate probe",
            body="identical text",
            updated_at="2026-01-01T00:00:00Z",
        )
        self.src.add_issue(earlier)
        self.src.add_issue(later)
        self.sync()
        data = search(self.conn, "duplicate probe", repo=REPO)
        scores = [i["number"] for i in data["items"]]
        self.assertEqual(scores, [9, 30])

    def test_index_stale_signals_unmigrated_db(self) -> None:
        """A v2 database read without migrating reports index_stale: true —
        a zero-hit answer there must never read as prior-art absence (the
        issue's own failure mode, one writable-open away)."""
        from zaxbygraph import db as db_mod

        self.seed_corpus()
        self.assertEqual(search(self.conn, "memory", repo=REPO)["index_stale"], False)
        # build a real v2 DB and open it read-only, as reads do
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        v2_path = Path(td.name) / "v2.db"
        v2 = sqlite3.connect(str(v2_path))
        try:
            from test_migrations import V2_SCHEMA_SQL
        except ImportError:
            from tests.test_migrations import V2_SCHEMA_SQL
        v2.executescript(V2_SCHEMA_SQL)
        v2.execute("PRAGMA user_version = 2")
        v2.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " updated_at, raw_json) VALUES (1, 'acme/forgegate', 1, 'issue',"
            " 'Reconnection drops timers', 'reconnection drops the timer',"
            " 'open', '2026-01-01T00:00:00Z', '{}')"
        )
        v2.commit()
        v2.close()
        ro = db_mod.open_existing(v2_path)
        self.addCleanup(ro.close)
        stale = search(ro, "reconnect", repo="acme/forgegate")
        self.assertEqual(stale["index_stale"], True)
        self.assertEqual(stale["total_matches"], 0, "v2 index cannot stem-match")

    def test_title_weighting_is_load_bearing(self) -> None:
        """Pin the 10/1/3 weights behaviorally: on this corpus length
        normalization ALONE ranks the body hit first (proven with flat
        1/1/1 weights via raw SQL), so the shipped weights are what put the
        title hit first. Mutating _ITEM_BM25 to flat weights fails this."""
        long_title = (
            "memory " + "padding words to make this title much longer "
            "than any body here " * 4
        )
        corpus = [
            (1, long_title, "nothing relevant in this body at all"),
            (2, "totally unrelated title", "memory"),
            (3, "filler three", "nothing to see in here"),
            (4, "filler four", "more unrelated text"),
            (5, "filler five", "still nothing matching"),
            (6, "filler six", "and again nothing"),
        ]
        for id_, title, body in corpus:
            self.src.add_issue(issue(id_, title=title, body=body))
        self.sync()
        flat = self.conn.execute(
            "SELECT items.number FROM items_fts"
            " JOIN items ON items.id = items_fts.rowid"
            " WHERE items_fts MATCH '\"memory\"'"
            " ORDER BY bm25(items_fts, 1.0, 1.0, 1.0) ASC"
        ).fetchall()
        self.assertEqual([r[0] for r in flat], [2, 1])
        data = search(self.conn, "memory", repo=REPO)
        self.assertEqual([i["number"] for i in data["items"]], [1, 2])


class FreshShapeTokenizerTests(unittest.TestCase):
    """Genuine parity check: a database built from the shipped schema.sql
    verbatim (NO init_schema, NO migrations) must stem-match exactly like a
    migrated database does — this is the only test that exercises the
    schema.sql DDL copy directly."""

    def test_fresh_shape_and_migrated_db_share_tokenizer(self) -> None:
        from zaxbygraph import db as db_mod

        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        schema_sql = (
            Path(db_mod.__file__).parent / db_mod.SCHEMA_NAME
        ).read_text(encoding="utf-8")

        # fresh shape: the schema file alone, no migration framework
        fresh_path = Path(td.name) / "fresh.db"
        raw = sqlite3.connect(str(fresh_path))
        raw.executescript(schema_sql)
        raw.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " updated_at, raw_json) VALUES (1, 'acme/forgegate', 1, 'issue',"
            " 'Timer cleanup on socket teardown',"
            " 'reconnection and reconnecting both drop the timer.', 'open',"
            " '2026-01-04T00:00:00Z', '{}')"
        )
        raw.commit()
        fresh_hits = raw.execute(
            "SELECT items.number FROM items_fts"
            " JOIN items ON items.id = items_fts.rowid"
            " WHERE items_fts MATCH '\"reconnect\"'"
        ).fetchall()
        raw.close()
        self.assertEqual(
            [r[0] for r in fresh_hits],
            [1],
            "fresh-shape schema.sql DDL must stem-match (porter)",
        )

        # migrated shape: a v2 database opened through init_schema
        from tests.test_migrations import V2_SCHEMA_SQL

        migrated_path = Path(td.name) / "migrated.db"
        v2 = sqlite3.connect(str(migrated_path))
        v2.executescript(V2_SCHEMA_SQL)
        v2.execute("PRAGMA user_version = 2")
        v2.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " updated_at, raw_json) VALUES (1, 'acme/forgegate', 1, 'issue',"
            " 'Timer cleanup on socket teardown',"
            " 'reconnection and reconnecting both drop the timer.', 'open',"
            " '2026-01-04T00:00:00Z', '{}')"
        )
        v2.commit()
        v2.close()
        conn = connect(migrated_path)
        self.addCleanup(conn.close)
        init_schema(conn)
        migrated_hits = conn.execute(
            "SELECT items.number FROM items_fts"
            " JOIN items ON items.id = items_fts.rowid"
            " WHERE items_fts MATCH '\"reconnect\"'"
        ).fetchall()
        self.assertEqual([r[0] for r in migrated_hits], [1])


if __name__ == "__main__":
    unittest.main()
