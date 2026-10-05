from __future__ import annotations

import unittest

from fixtures import TempDBTest, issue, pull
from zaxbygraph import query
from zaxbygraph.store import ingest_item

#: Every timeline/provenance check ingests under one repo so that same-repo
#: comparisons in closing references and cross-references resolve against it.
REPO = "zaxbyhub/zaxbygraph"


class EdgeIngestTest(TempDBTest):
    """Store-level base: ingest one item at a time, caller owns the timeline.

    `timeline` is forwarded to ingest_item only when supplied, so tests that
    pin non-timeline provenance (payload/keyword sources) also exercise the
    parameter's empty default.
    """

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

    def source_of(self, *, src_type: str, src_id: str, rel: str, dst_type: str, dst_id: str) -> str:
        row = self.conn.execute(
            "SELECT source FROM edges WHERE src_type = ? AND src_id = ? AND rel = ? "
            "AND dst_type = ? AND dst_id = ?",
            (src_type, src_id, rel, dst_type, dst_id),
        ).fetchone()
        self.assertIsNotNone(
            row, f"missing edge {src_type}:{src_id} -{rel}-> {dst_type}:{dst_id}"
        )
        return row[0]


class TimelineEdgeTests(EdgeIngestTest):
    def test_closed_event_with_commit_links_pr_and_issue(self) -> None:
        # PR 7 was merged with commit 'a'; issue 9's timeline says it was
        # closed by that same commit. The commit edge and the derived closes
        # edge (closer is the source, closed item the destination) are both
        # timeline-sourced facts.
        pr7 = issue(7, title="pr seven", kind="pr")
        self.ingest(pr7, pull_raw=dict(pull(7, merged=True), merge_commit_sha="a"))
        closed = {
            "type": "closed",
            "created_at": "2026-01-01T00:00:00Z",
            "actor_login": "amy",
            "commit_id": "a",
            "closer_type": None,
            "closer_number": None,
        }
        self.ingest(issue(9, title="issue nine"), timeline=[closed])

        self.assertEqual(
            self.source_of(
                src_type="item", src_id="9", rel="closed_by_commit",
                dst_type="commit", dst_id="a",
            ),
            "timeline",
        )
        self.assertEqual(
            self.source_of(
                src_type="item", src_id="7", rel="closes",
                dst_type="item", dst_id="9",
            ),
            "timeline",
        )

        # Timeline facts are append-only: re-ingesting PR 7 alone must not
        # erase the (7 closes 9) edge recorded on issue 9's timeline.
        self.ingest(pr7, pull_raw=dict(pull(7, merged=True), merge_commit_sha="a"))
        kept = self.count(
            "SELECT COUNT(*) FROM edges WHERE src_type = 'item' AND src_id = '7' "
            "AND rel = 'closes' AND dst_type = 'item' AND dst_id = '9' "
            "AND source = 'timeline'"
        )
        self.assertEqual(kept, 1, "timeline closes edge must survive PR 7's re-ingest")

    def test_cross_referenced_same_and_foreign_repo(self) -> None:
        # A local item 42 exists: a foreign cross-reference with source number
        # 42 must not attach to it as a bare local id.
        self.ingest(issue(42, title="local forty-two"))
        timeline = [
            {
                "type": "cross_referenced",
                "created_at": "2026-01-01T00:00:00Z",
                "actor_login": "bob",
                "source_typename": "Issue",
                "source_number": 13,
                "source_repo": "ZaxbyHub/zaxbygraph",
            },
            {
                "type": "cross_referenced",
                "created_at": "2026-01-01T00:01:00Z",
                "actor_login": "bob",
                "source_typename": "PullRequest",
                "source_number": 42,
                "source_repo": "Other/Repo",
            },
        ]
        self.ingest(issue(5, title="issue five"), timeline=timeline)

        # same-repo source (case-insensitive match): plain local item id
        self.assertEqual(
            self.source_of(
                src_type="item", src_id="13", rel="cross_referenced",
                dst_type="item", dst_id="5",
            ),
            "timeline",
        )
        # foreign source: repo-qualified node id built from the GIVEN spelling
        self.assertEqual(
            self.source_of(
                src_type="item", src_id="Other/Repo#42", rel="cross_referenced",
                dst_type="item", dst_id="5",
            ),
            "timeline",
        )
        # exactly the two cross_referenced edges above; never a bare '42'
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM edges WHERE rel = 'cross_referenced'"), 2
        )
        self.assertEqual(
            self.count(
                "SELECT COUNT(*) FROM edges WHERE rel = 'cross_referenced' "
                "AND (src_id = '42' OR dst_id = '42')"
            ),
            0,
        )
        # the node resolver must tolerate the non-numeric repo-qualified id
        result = query.related(self.conn, 5, repo=REPO)
        cross = [e for e in result["edges"] if e["rel"] == "cross_referenced"]
        self.assertEqual(len(cross), 2)


class ProvenanceTests(EdgeIngestTest):
    def test_keyword_edges_are_labelled_weaker(self) -> None:
        self.ingest(issue(14, title="issue fourteen"))
        self.ingest(issue(3, title="issue three", body="Fixes #14"))
        # keyword-derived closing reference: renamed rel, weaker source
        self.assertEqual(
            self.source_of(
                src_type="item", src_id="3", rel="closes_keyword",
                dst_type="item", dst_id="14",
            ),
            "keyword",
        )
        # the structured payload edge from the very same ingest keeps its own
        # stronger source
        self.assertEqual(
            self.source_of(
                src_type="actor", src_id="alice", rel="authored",
                dst_type="item", dst_id="3",
            ),
            "payload",
        )
        # the body produced no unqualified 'closes' row
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM edges WHERE rel = 'closes' AND src_id = '3'"),
            0,
        )

    def test_closing_refs_stored_with_own_source(self) -> None:
        self.ingest(issue(5, title="issue five"))
        self.ingest(issue(9, title="issue nine"))
        pull8 = dict(
            pull(8),
            closing_issues_references=[
                {"number": 5, "repo": "ZaxbyHub/zaxbygraph"},
                {"number": 77, "repo": "Other/Repo"},
            ],
        )
        pr8 = issue(8, title="pr eight", kind="pr")
        self.ingest(pr8, pull_raw=pull8)

        # same-repo closing_issues_reference: stored under its own source
        self.assertEqual(
            self.source_of(
                src_type="item", src_id="8", rel="closes",
                dst_type="item", dst_id="5",
            ),
            "closing_ref",
        )
        # the foreign entry is dropped entirely, never localized to a local 77
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM edges WHERE src_id = '77' OR dst_id = '77'"),
            0,
        )

        # a timeline closed-event on the same PR adds a timeline-sourced closes
        # edge (closer PR 9 is the source, the closed PR 8 the destination);
        # both provenances coexist after the re-ingest
        closed = {
            "type": "closed",
            "created_at": "2026-01-02T00:00:00Z",
            "actor_login": "amy",
            "commit_id": None,
            "closer_type": "pull_request",
            "closer_number": 9,
        }
        self.ingest(pr8, pull_raw=pull8, timeline=[closed])
        self.assertEqual(
            self.source_of(
                src_type="item", src_id="8", rel="closes",
                dst_type="item", dst_id="5",
            ),
            "closing_ref",
        )
        self.assertEqual(
            self.source_of(
                src_type="item", src_id="9", rel="closes",
                dst_type="item", dst_id="8",
            ),
            "timeline",
        )


class CommitEdgeTests(EdgeIngestTest):
    def test_merged_by_commit_and_reverts(self) -> None:
        revert_sha = "24bbcf41c382f429d3cd8ac98de79a83c6deaa3a"
        merge_sha = "15948fde4a83d2918f3e91f4faca7c741adac1ee"
        pr13 = issue(
            13,
            title="pr thirteen",
            kind="pr",
            body=f"This reverts commit {revert_sha}",
        )
        self.ingest(
            pr13,
            pull_raw=dict(
                pull(13, merged=True),
                merge_commit_sha=merge_sha,
                merged_by="zaxbysauce",
            ),
        )
        self.assertEqual(
            self.source_of(
                src_type="item", src_id="13", rel="merged_by",
                dst_type="actor", dst_id="zaxbysauce",
            ),
            "payload",
        )
        self.assertEqual(
            self.source_of(
                src_type="item", src_id="13", rel="merged_commit",
                dst_type="commit", dst_id=merge_sha,
            ),
            "payload",
        )
        # reverts is keyword-derived from the body sha, not a payload fact
        self.assertEqual(
            self.source_of(
                src_type="item", src_id="13", rel="reverts",
                dst_type="commit", dst_id=revert_sha,
            ),
            "keyword",
        )


if __name__ == "__main__":
    unittest.main()
