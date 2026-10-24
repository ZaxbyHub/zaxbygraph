"""Non-anchored coverage for the issue #6 edge contract beyond the frozen
checks: title-only reverts, merged_by shapes, degraded re-ingest survival,
both-orders merge-close convergence, and same-pair/two-stream coexistence."""
from __future__ import annotations

import sqlite3
import unittest

from fixtures import TempDBTest, issue, pull
from zaxbygraph.store import ingest_item, insert_edge

REPO = "zaxbyhub/zaxbygraph"


class ExtraEdgeIngestTest(TempDBTest):
    def ingest(self, list_raw, *, pull_raw=None, timeline=None) -> None:
        kwargs = dict(
            pull_raw=pull_raw,
            issue_comments=[],
            review_comments=[],
            reviews=[],
            files=[],
            include_patches=False,
        )
        if timeline is not None:
            kwargs["timeline"] = timeline
        self.conn.execute("BEGIN")
        ingest_item(self.conn, REPO, list_raw, **kwargs)
        self.conn.commit()

    def edge_count(self, where: str) -> int:
        return self.count(f"SELECT COUNT(*) FROM edges WHERE {where}")

    def rows(self, sql: str) -> list[tuple]:
        return [tuple(r) for r in self.conn.execute(sql).fetchall()]


class TitleOnlyRevertsTests(ExtraEdgeIngestTest):
    def test_unique_quoted_title_match_targets_the_item(self) -> None:
        self.ingest(issue(4, title="search returns zero hits", kind="pr"))
        revert = issue(
            30,
            title='Revert "search returns zero hits"',
            kind="pr",
        )
        self.ingest(revert)
        self.assertEqual(
            self.rows(
                "SELECT dst_type, dst_id, source FROM edges "
                "WHERE rel = 'reverts' AND src_id = '30'"
            ),
            [("item", "4", "keyword")],
        )

    def test_zero_matches_store_nothing(self) -> None:
        self.ingest(issue(30, title='Revert "nothing carries this title"', kind="pr"))
        self.assertEqual(self.edge_count("rel = 'reverts'"), 0)

    def test_ambiguous_matches_store_nothing(self) -> None:
        self.ingest(issue(4, title="duplicate title", kind="pr"))
        self.ingest(issue(5, title="duplicate title", kind="pr"))
        self.ingest(issue(30, title='Revert "duplicate title"', kind="pr"))
        self.assertEqual(self.edge_count("rel = 'reverts'"), 0)

    def test_span_without_trailing_quote_is_compared_whole(self) -> None:
        # A hand-edited revert title that dropped the closing quote must not
        # silently corrupt the compared span (the conditional strip).
        self.ingest(issue(4, title='say "hello"'))
        self.ingest(issue(31, title='Revert "say "hello""', kind="pr"))
        self.assertEqual(
            self.rows(
                "SELECT dst_type, dst_id FROM edges "
                "WHERE rel = 'reverts' AND src_id = '31'"
            ),
            [("item", "4")],
        )

    def test_body_sha_takes_precedence_over_title(self) -> None:
        self.ingest(issue(4, title="sha bearer", kind="pr"))
        revert = issue(
            32,
            title='Revert "sha bearer"',
            kind="pr",
            body="This reverts commit 24bbcf41c382f429d3cd8ac98de79a83c6deaa3a",
        )
        self.ingest(revert)
        self.assertEqual(
            self.rows(
                "SELECT dst_type, dst_id, source FROM edges "
                "WHERE rel = 'reverts' AND src_id = '32'"
            ),
            [("commit", "24bbcf41c382f429d3cd8ac98de79a83c6deaa3a", "keyword")],
        )


class MergedByShapeTests(ExtraEdgeIngestTest):
    def test_rest_style_dict_shape_coerces_to_the_login(self) -> None:
        pr = issue(9, title="rest pr", kind="pr")
        self.ingest(
            pr,
            pull_raw=dict(
                pull(9, merged=True),
                merge_commit_sha="feed1234",
                merged_by={"login": "octocat", "html_url": "https://github.com/octocat"},
            ),
        )
        self.assertEqual(
            self.rows(
                "SELECT dst_type, dst_id, source FROM edges "
                "WHERE rel = 'merged_by' AND src_id = '9'"
            ),
            [("actor", "octocat", "payload")],
        )

    def test_unmerged_pr_stores_no_merged_by(self) -> None:
        pr = issue(9, title="open pr", kind="pr")
        self.ingest(pr, pull_raw=dict(pull(9), merged_by="someone"))
        self.assertEqual(self.edge_count("rel = 'merged_by'"), 0)


class DegradedReingestSurvivalTests(ExtraEdgeIngestTest):
    def test_link_edges_survive_a_reingest_that_cannot_rederive_them(self) -> None:
        # Full GraphQL-shaped ingest: timeline events + closing references.
        closed = {
            "type": "closed",
            "created_at": "2026-01-01T00:00:00Z",
            "actor_login": "amy",
            "commit_id": "abc1234",
            "closer_type": "pull_request",
            "closer_number": 7,
        }
        cross = {
            "type": "cross_referenced",
            "created_at": "2026-01-01T00:01:00Z",
            "actor_login": "bob",
            "source_typename": "Issue",
            "source_number": 8,
            "source_repo": "ZaxbyHub/zaxbygraph",
        }
        issue5 = issue(5, title="issue five")
        self.ingest(
            issue5,
            pull_raw=dict(
                pull(5, merged=True),
                closing_issues_references=[{"number": 9, "repo": "ZaxbyHub/zaxbygraph"}],
            ),
            timeline=[closed, cross],
        )
        before = self.rows(
            "SELECT src_type, src_id, rel, dst_type, dst_id, source FROM edges "
            "WHERE source IN ('timeline', 'closing_ref') ORDER BY rel, src_id"
        )
        self.assertGreaterEqual(len(before), 4)

        # Degraded re-ingest: no timeline, no closing references (a REST
        # source or a GraphQL node that fell back per-item). Nothing that the
        # payload cannot re-derive may be destroyed.
        self.ingest(issue5, pull_raw=pull(5, merged=True), timeline=None)
        after = self.rows(
            "SELECT src_type, src_id, rel, dst_type, dst_id, source FROM edges "
            "WHERE source IN ('timeline', 'closing_ref') ORDER BY rel, src_id"
        )
        self.assertEqual(after, before, "link edges must survive a degraded re-ingest")

        # Text/payload edges keep their freshness: a changed body retracts a
        # keyword edge on rebuild — ingest with the body, re-ingest without,
        # and the edge is gone.
        self.ingest(
            issue(3, title="issue three", body="Fixes #5"),
            timeline=None,
        )
        self.assertEqual(
            self.edge_count("rel = 'closes_keyword' AND src_id = '3'"), 1
        )
        self.ingest(
            issue(3, title="issue three", body="no keyword anymore"),
            timeline=None,
        )
        self.assertEqual(
            self.edge_count("rel = 'closes_keyword' AND src_id = '3'"), 0,
            "a retracted closing keyword must retract the edge on rebuild",
        )


class DegenerateTimelineEventTests(ExtraEdgeIngestTest):
    """Reviewer finding 1: the guard branches for events that cannot yield a
    truthful endpoint ship no fabricated edges."""

    def test_closed_event_with_no_committable_closer_stores_no_edge(self) -> None:
        # Closed manually (no commit, no PR): the payload state row is the
        # only record; no closer edge may be fabricated.
        self.ingest(
            issue(6, title="manually closed"),
            timeline=[
                {
                    "type": "closed",
                    "created_at": "2026-01-01T00:00:00Z",
                    "actor_login": "amy",
                    "commit_id": None,
                    "closer_type": None,
                    "closer_number": None,
                }
            ],
        )
        self.assertEqual(self.edge_count("rel IN ('closes', 'closed_by_commit')"), 0)

    def test_cross_referenced_event_with_missing_source_is_skipped(self) -> None:
        self.ingest(
            issue(6, title="weird timeline"),
            timeline=[
                {
                    "type": "cross_referenced",
                    "created_at": "2026-01-01T00:00:00Z",
                    "actor_login": "bob",
                    "is_cross_repository": False,
                },
                {
                    "type": "cross_referenced",
                    "created_at": "2026-01-01T00:01:00Z",
                    "actor_login": "bob",
                    "source_typename": "Issue",
                    "source_number": "not-an-int",
                    "source_repo": "ZaxbyHub/zaxbygraph",
                },
            ],
        )
        self.assertEqual(self.edge_count("rel = 'cross_referenced'"), 0)

    def test_self_referential_cross_reference_is_skipped(self) -> None:
        self.ingest(
            issue(6, title="self reference"),
            timeline=[
                {
                    "type": "cross_referenced",
                    "created_at": "2026-01-01T00:00:00Z",
                    "actor_login": "bob",
                    "source_typename": "Issue",
                    "source_number": 6,
                    "source_repo": "ZaxbyHub/zaxbygraph",
                }
            ],
        )
        self.assertEqual(self.edge_count("rel = 'cross_referenced'"), 0)


class MergeCloseConvergenceTests(ExtraEdgeIngestTest):
    def test_both_ingest_orders_converge_on_the_same_edge(self) -> None:
        closed = {
            "type": "closed",
            "created_at": "2026-01-01T00:00:00Z",
            "actor_login": "amy",
            "commit_id": "abc1234",
            "closer_type": None,
            "closer_number": None,
        }
        pr7 = issue(7, title="pr seven", kind="pr")
        issue9 = issue(9, title="issue nine")

        # Issue-first: the forward lookup finds no PR row yet, so only
        # closed_by_commit lands; the edge completes at the PR's ingest.
        self.ingest(issue9, timeline=[closed])
        self.assertEqual(self.edge_count("rel = 'closes' AND source = 'timeline'"), 0)
        self.ingest(pr7, pull_raw=dict(pull(7, merged=True), merge_commit_sha="abc1234"))
        issue_first = self.rows(
            "SELECT src_id, rel, dst_id, evidence, source FROM edges "
            "WHERE rel = 'closes' AND source = 'timeline'"
        )
        self.assertEqual(issue_first, [("7", "closes", "9", "timeline closed event 2026-01-01T00:00:00Z", "timeline")])

        # PR-first order converges on the identical 7-tuple for a fresh DB.
        # Release the first fixture's connection and temp dir BEFORE
        # re-entering setUp, or Windows pins history.db and the temp dir
        # cleanup PermissionErrors (review round 2, PRR-023).
        self.tearDown()
        self.setUp()
        self.ingest(pr7, pull_raw=dict(pull(7, merged=True), merge_commit_sha="abc1234"))
        self.ingest(issue9, timeline=[closed])
        pr_first = self.rows(
            "SELECT src_id, rel, dst_id, evidence, source FROM edges "
            "WHERE rel = 'closes' AND source = 'timeline'"
        )
        self.assertEqual(pr_first, issue_first)

        # Re-ingesting both items keeps the edge set stable (no duplicates,
        # no self-edge, evidence not churned away).
        self.ingest(pr7, pull_raw=dict(pull(7, merged=True), merge_commit_sha="abc1234"))
        self.ingest(issue9, timeline=[closed])
        self.assertEqual(
            self.rows(
                "SELECT src_id, rel, dst_id, evidence, source FROM edges "
                "WHERE rel = 'closes' AND source = 'timeline'"
            ),
            issue_first,
        )
        self.assertEqual(self.edge_count("rel = 'closes' AND src_id = dst_id"), 0)


class SamePairTwoStreamsTests(ExtraEdgeIngestTest):
    """One closes row per (repo, dst, src) pair (issue #19): the two
    GitHub-reported streams corroborate one closure fact and merge."""

    CLOSED = {
        "type": "closed",
        "created_at": "2026-01-02T00:00:00Z",
        "actor_login": "amy",
        "commit_id": None,
        "closer_type": "pull_request",
        "closer_number": 8,
    }
    PAIR_WHERE = "rel = 'closes' AND src_id = '8' AND dst_id = '5'"

    def _merged_pair_state(self) -> tuple:
        rows = self.rows(
            "SELECT source, evidence FROM edges WHERE " + self.PAIR_WHERE
        )
        self.assertEqual(
            len(rows), 1, "exactly one closes row must survive for the pair"
        )
        source, evidence = rows[0]
        self.assertIn(
            source, ("closing_ref", "timeline"),
            "the surviving source is one of the two GitHub-reported streams",
        )
        self.assertIn("closingIssuesReferences", evidence)
        self.assertIn("timeline closed event", evidence)
        return rows[0]

    @staticmethod
    def _parts(evidence: str) -> set:
        return set(evidence.split("; "))

    def test_timeline_and_closing_ref_streams_merge_for_one_pair(self) -> None:
        pr8 = issue(8, title="pr eight", kind="pr")
        pull8 = dict(
            pull(8, merged=True),
            closing_issues_references=[{"number": 5, "repo": "ZaxbyHub/zaxbygraph"}],
        )
        # The closed event lives on ISSUE 5's timeline (closer: PR 8); the
        # closing reference rides PR 8's own payload. Either ingest order
        # converges on one row carrying both provenances (order-independent
        # up to evidence part order).
        self.ingest(issue(5, title="issue five"), timeline=[self.CLOSED])
        self.ingest(pr8, pull_raw=pull8)
        self._merged_pair_state()
        # Degraded re-ingests on either side still leave the single merged
        # row, and re-ingesting one stream alone must not drop the other
        # stream's provenance from the evidence payload.
        self.ingest(issue(5, title="issue five"), timeline=None)
        self._merged_pair_state()
        self.ingest(pr8, pull_raw=pull(8, merged=True))
        state = self._merged_pair_state()
        # ...and re-ingesting the timeline stream alone keeps one row with
        # both provenances and no churn.
        self.ingest(issue(5, title="issue five"), timeline=[self.CLOSED])
        final = self._merged_pair_state()
        self.assertEqual(
            self._parts(final[1]), self._parts(state[1]),
            "re-ingest must not churn the evidence payload",
        )

    def test_both_ingest_orders_converge_up_to_part_order(self) -> None:
        pr8 = issue(8, title="pr eight", kind="pr")
        pull8 = dict(
            pull(8, merged=True),
            closing_issues_references=[{"number": 5, "repo": "ZaxbyHub/zaxbygraph"}],
        )
        self.ingest(issue(5, title="issue five"), timeline=[self.CLOSED])
        self.ingest(pr8, pull_raw=pull8)
        issue_first = self.rows(
            "SELECT source, evidence FROM edges WHERE " + self.PAIR_WHERE
        )
        # PR-first order on a fresh DB converges on the same row content
        # (part-set equality: the evidence string is first-seen ordered).
        self.tearDown()
        self.setUp()
        self.ingest(pr8, pull_raw=pull8)
        self.ingest(issue(5, title="issue five"), timeline=[self.CLOSED])
        pr_first = self.rows(
            "SELECT source, evidence FROM edges WHERE " + self.PAIR_WHERE
        )
        self.assertEqual(len(pr_first), 1)
        self.assertEqual(len(issue_first), 1)
        self.assertEqual(pr_first[0][0], issue_first[0][0])
        self.assertEqual(self._parts(pr_first[0][1]), self._parts(issue_first[0][1]))

    def test_same_source_refresh_preserves_merged_evidence(self) -> None:
        pr8 = issue(8, title="pr eight", kind="pr")
        pull8 = dict(
            pull(8, merged=True),
            closing_issues_references=[{"number": 5, "repo": "ZaxbyHub/zaxbygraph"}],
        )
        self.ingest(issue(5, title="issue five"), timeline=[self.CLOSED])
        self.ingest(pr8, pull_raw=pull8)
        merged = self.rows(
            "SELECT evidence FROM edges WHERE " + self.PAIR_WHERE
        )[0][0]
        # A later re-ingest of ONLY the closing PR (a comment bumped
        # updated_at; the payload re-carries the closing reference) hits the
        # same-source refresh path — it must union, never replace.
        self.ingest(pr8, pull_raw=pull8)
        after = self.rows(
            "SELECT evidence FROM edges WHERE " + self.PAIR_WHERE
        )[0][0]
        self.assertIn("closingIssuesReferences", after)
        self.assertIn("timeline closed event", after)
        self.assertEqual(self._parts(after), self._parts(merged))

    def test_what_closed_reports_one_entry_for_a_merged_pair(self) -> None:
        from zaxbygraph import query

        pr8 = issue(8, title="pr eight", kind="pr")
        pull8 = dict(
            pull(8, merged=True),
            closing_issues_references=[{"number": 5, "repo": "ZaxbyHub/zaxbygraph"}],
        )
        self.ingest(issue(5, title="issue five"), timeline=[self.CLOSED])
        self.ingest(pr8, pull_raw=pull8)
        closed = query.what_closed(self.conn, 5, repo=REPO)
        self.assertEqual(
            [p["number"] for p in closed["prs"]], [8],
            "the merged pair must read back as ONE closing PR",
        )
        self.assertIn("closingIssuesReferences", closed["prs"][0]["evidence"])
        self.assertIn("timeline closed event", closed["prs"][0]["evidence"])

    def test_overcap_evidence_never_nulls_and_first_insert_is_bounded(self) -> None:
        # feedback round: an over-cap part must hard-truncate, never erase —
        # merge_evidence used to return None and insert_edge wrote NULL,
        # destroying both provenances on the survivor row
        big = "timeline closed event " + "t" * 600
        pr8 = issue(8, title="pr eight", kind="pr")
        self.ingest(issue(5, title="issue five"), timeline=[dict(self.CLOSED)])
        # simulate the pathological oversized evidence by inserting directly
        # through the store (the extractor caps event strings naturally)
        self.conn.execute("BEGIN")
        insert_edge(
            self.conn, REPO,
            ("item", "8", "closes", "item", "5", big, "timeline"),
        )
        self.conn.commit()
        self.ingest(
            pr8,
            pull_raw=dict(
                pull(8, merged=True),
                closing_issues_references=[{"number": 5, "repo": REPO}],
            ),
        )
        rows = self.rows(
            "SELECT source, evidence FROM edges WHERE " + self.PAIR_WHERE
        )
        self.assertEqual(len(rows), 1)
        source, evidence = rows[0]
        self.assertIsNotNone(evidence, "merged evidence must never become NULL")
        self.assertLessEqual(len(evidence), 512)
        self.assertIn("closingIssuesReferences", evidence)
        self.assertEqual(source, "closing_ref")
        # the first-insert path is bounded by the same cap
        self.ingest(pr8, pull_raw=None)
        bounded = self.rows(
            "SELECT length(evidence) FROM edges WHERE " + self.PAIR_WHERE
        )[0][0]
        self.assertLessEqual(bounded, 512)

    def test_other_relations_keep_two_rows_per_pair(self) -> None:
        # the documented invariant: the closes exception is closes-only —
        # another relation reported by two streams is still two facts
        mentions_a = ("item", "8", "mentions", "item", "5", "body #5", "keyword")
        mentions_b = ("item", "8", "mentions", "item", "5", "comment #5", "payload")
        self.conn.execute("BEGIN")
        insert_edge(self.conn, REPO, mentions_a)
        insert_edge(self.conn, REPO, mentions_b)
        self.conn.commit()
        self.assertEqual(
            self.edge_count("rel = 'mentions' AND src_id = '8' AND dst_id = '5'"),
            2,
            "non-closes relations keep two-rows-are-two-facts",
        )

    def test_partial_index_fires_on_bypassing_writers(self) -> None:
        pr8 = issue(8, title="pr eight", kind="pr")
        pull8 = dict(
            pull(8, merged=True),
            closing_issues_references=[{"number": 5, "repo": "ZaxbyHub/zaxbygraph"}],
        )
        self.ingest(issue(5, title="issue five"), timeline=[self.CLOSED])
        self.ingest(pr8, pull_raw=pull8)
        # After the merge the pair holds one row. A writer that bypasses
        # insert_edge's merge (the index is the schema-level guardrail) must
        # fail loudly rather than resurrect the duplicate.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
                " confidence, evidence, source)"
                " VALUES (?, 'item', '8', 'closes', 'item', '5',"
                " 'EXTRACTED', 'timeline closed event 2026-02-02T00:00:00Z', 'timeline')",
                (REPO,),
            )


class SelfCloseExclusionTests(ExtraEdgeIngestTest):
    def test_reverse_pass_never_fabricates_a_self_edge(self) -> None:
        # A PR whose own merge commit closed itself (degenerate timeline):
        # neither the forward lookup nor the reverse pass may emit
        # (7 closes 7).
        closed = {
            "type": "closed",
            "created_at": "2026-01-01T00:00:00Z",
            "actor_login": "amy",
            "commit_id": "abc1234",
            "closer_type": None,
            "closer_number": None,
        }
        pr7 = issue(7, title="pr seven", kind="pr")
        self.ingest(pr7, pull_raw=dict(pull(7, merged=True), merge_commit_sha="abc1234"))
        self.ingest(issue(7, title="pr seven", kind="pr", body="x"), timeline=[closed])
        self.assertEqual(
            self.edge_count("rel = 'closes' AND src_id = dst_id"), 0
        )
        # The closed_by_commit fact itself is still recorded.
        self.assertEqual(self.edge_count("rel = 'closed_by_commit'"), 1)


if __name__ == "__main__":
    unittest.main()
