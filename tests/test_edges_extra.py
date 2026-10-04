"""Non-anchored coverage for the issue #6 edge contract beyond the frozen
checks: title-only reverts, merged_by shapes, degraded re-ingest survival,
both-orders merge-close convergence, and same-pair/two-stream coexistence."""
from __future__ import annotations

import unittest

from fixtures import TempDBTest, issue, pull
from zaxbygraph.store import ingest_item

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
        pull8 = dict(
            issue(5, title="issue five"),
        )
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
        # keyword edge on rebuild.
        self.ingest(
            issue(3, title="issue three", body="Fixes #5"),
            timeline=None,
        )
        self.assertEqual(
            self.edge_count("rel = 'closes_keyword' AND src_id = '3'"), 1
        )


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
    def test_timeline_and_closing_ref_rows_coexist_for_one_pair(self) -> None:
        closed = {
            "type": "closed",
            "created_at": "2026-01-02T00:00:00Z",
            "actor_login": "amy",
            "commit_id": None,
            "closer_type": "pull_request",
            "closer_number": 8,
        }
        pr8 = issue(8, title="pr eight", kind="pr")
        pull8 = dict(
            pull(8, merged=True),
            closing_issues_references=[{"number": 5, "repo": "ZaxbyHub/zaxbygraph"}],
        )
        # The closed event lives on ISSUE 5's timeline (closer: PR 8); the
        # closing reference rides PR 8's own payload. One ingest each; the
        # pair (8 -> 5) then exists under two provenance streams.
        self.ingest(issue(5, title="issue five"), timeline=[closed])
        self.ingest(pr8, pull_raw=pull8)
        self.assertEqual(
            self.rows(
                "SELECT source, COUNT(*) FROM edges "
                "WHERE rel = 'closes' AND src_id = '8' AND dst_id = '5' "
                "GROUP BY source ORDER BY source"
            ),
            [("closing_ref", 1), ("timeline", 1)],
        )
        # Degraded re-ingests on either side keep both rows.
        self.ingest(issue(5, title="issue five"), timeline=None)
        self.ingest(pr8, pull_raw=pull(8, merged=True))
        self.assertEqual(
            self.rows(
                "SELECT source, COUNT(*) FROM edges "
                "WHERE rel = 'closes' AND src_id = '8' AND dst_id = '5' "
                "GROUP BY source ORDER BY source"
            ),
            [("closing_ref", 1), ("timeline", 1)],
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
