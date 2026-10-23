"""Acceptance checks for issue-trace 17-19-machine-output-closes-uniqueness
(issue #19, AC5/C5): one `closes` row per (repo, src_type, src_id, rel,
dst_type, dst_id) pair when BOTH provenance streams report the same closure,
with both provenances visible in the surviving row's evidence. Authored at
arm's length against the trace's acceptance table.

NEW-SURFACE module: it is deliberately absent at base f4c8065 so the module's
absence is the frozen base signature. Both methods are RED at head-unfixed
(today the code STORES BOTH rows, one per source) and GREEN after the fix.
Which source value survives a merge, and the exact evidence join format, are
implementer decisions — these checks assert only that exactly ONE row
remains, that its source is one of the two GitHub streams ('closing_ref' or
'timeline'), and that BOTH provenance strings stay visible in its evidence.

Everything goes through the REAL ingest path (store.ingest_item ->
rebuild_edges -> extract.edges_from_timeline / edges_from_pr_state) with the
same harness idioms as tests/test_edges.py; no direct edge construction
except the pre-seeded cache rows in the second test.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from fixtures import REPO, TempDBTest, issue, pull
from zaxbygraph.db import connect, init_schema
from zaxbygraph.store import ingest_item

#: The closure pair under test: PR 8 closes issue 5, reported once by
#: closingIssuesReferences (on the PR's payload, source 'closing_ref') and
#: once by a timeline `closed` event with a pull_request closer (on the
#: issue's timeline, source 'timeline'). Same (dst, src) pair from both
#: streams — exactly issue #19's duplicate mechanism.
PR_NUMBER = 8
ISSUE_NUMBER = 5


def _closing_ref_pull() -> dict:
    """The PR's pull payload carrying closingIssuesReferences at ISSUE_NUMBER
    in the same repo (the GraphQL bulk mapping's REST-shaped form)."""
    return dict(
        pull(PR_NUMBER),
        closing_issues_references=[{"number": ISSUE_NUMBER, "repo": REPO}],
    )


def _closed_event() -> dict:
    """The issue's timeline `closed` event reporting PR_NUMBER as the closer."""
    return {
        "type": "closed",
        "created_at": "2026-01-02T00:00:00Z",
        "actor_login": "amy",
        "commit_id": None,
        "closer_type": "pull_request",
        "closer_number": PR_NUMBER,
    }


#: The v5-shape edges DDL exactly as db.migrate_v4_to_v5 creates it: the
#: 7-column UNIQUE key INCLUDING source, and no unique index over the bare
#: closes pair — the shape that legitimately stored one row per provenance
#: stream (issue #19's duplicate mechanism).
_V5_EDGES_DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE edges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    repo       TEXT NOT NULL,
    src_type   TEXT NOT NULL,
    src_id     TEXT NOT NULL,
    rel        TEXT NOT NULL,
    dst_type   TEXT NOT NULL,
    dst_id     TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('EXTRACTED')),
    evidence   TEXT,
    source     TEXT NOT NULL,
    UNIQUE (repo, src_type, src_id, rel, dst_type, dst_id, source)
);
"""


def unique_closes_pair_index(conn) -> str | None:
    """Name of a UNIQUE PARTIAL index on edges whose key columns are exactly
    the six-column closes pair (repo, src_type, src_id, rel, dst_type,
    dst_id), or None. Checked by SHAPE via PRAGMA index_list/index_info so
    the check never hard-codes the implementer's chosen index name."""
    for row in conn.execute("PRAGMA index_list('edges')").fetchall():
        if not row["unique"] or not row["partial"]:
            continue
        cols = [
            r["name"]
            for r in conn.execute(
                'PRAGMA index_info("%s")' % row["name"].replace('"', '""')
            ).fetchall()
        ]
        if cols == ["repo", "src_type", "src_id", "rel", "dst_type", "dst_id"]:
            return str(row["name"])
    return None


class ClosesUniquenessTest(TempDBTest):
    """Store-level ingest base (mirrors test_edges.EdgeIngestTest): one item
    at a time through the real ingest_item path, caller owns the timeline."""

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

    def ingest_pair(self) -> None:
        """Ingest the closure pair through the real path: the PR's payload
        (closingIssuesReferences) and the issue's timeline (closed event)."""
        self.ingest(
            issue(PR_NUMBER, title="pr eight", kind="pr"),
            pull_raw=_closing_ref_pull(),
        )
        self.ingest(
            issue(ISSUE_NUMBER, title="issue five", state="closed"),
            timeline=[_closed_event()],
        )

    def closes_rows(self, conn=None) -> list[tuple]:
        """Every closes row for the pair, WITHOUT source in the predicate —
        counting across sources is the whole point. Rows come back as
        (source, evidence) so callers assert the surviving provenance."""
        conn = self.conn if conn is None else conn
        return [
            (r["source"], r["evidence"] or "")
            for r in conn.execute(
                "SELECT source, evidence FROM edges "
                "WHERE repo = ? AND src_type = 'item' AND src_id = ? "
                "AND rel = 'closes' AND dst_type = 'item' AND dst_id = ?",
                (REPO, str(PR_NUMBER), str(ISSUE_NUMBER)),
            ).fetchall()
        ]

    def assert_single_merged_row(self, rows: list[tuple]) -> None:
        """The shared AC5 verdict: exactly one row, its source is one of the
        two GitHub streams, and BOTH provenance strings remain visible in its
        evidence (closingIssuesReferences AND the timeline closed event)."""
        self.assertEqual(
            len(rows),
            1,
            "CLOSES1: exactly one closes row must exist for the (pr, issue) "
            "pair regardless of source, got "
            + str(len(rows))
            + ": "
            + repr(rows),
        )
        source, evidence = rows[0]
        self.assertIn(
            source,
            ("closing_ref", "timeline"),
            "CLOSES1: the surviving row's source must be one of the two "
            "GitHub-reported streams, got " + repr(source),
        )
        self.assertIn(
            "closingIssuesReferences",
            evidence,
            "CLOSES1: the surviving row's evidence must still name the "
            "closingIssuesReferences provenance, got " + repr(evidence),
        )
        self.assertIn(
            "timeline closed event",
            evidence,
            "CLOSES1: the surviving row's evidence must still name the "
            "timeline closed-event provenance, got " + repr(evidence),
        )

    def test_both_streams_store_one_closes_row(self):
        """AC5 case (a). RED at head-unfixed (two rows stored), GREEN after
        the fix: an item pair reported by BOTH the closingIssuesReferences
        stream and a timeline closed event for the same (dst, src) ingests to
        exactly one closes row carrying both provenances."""
        self.ingest_pair()
        self.assert_single_merged_row(self.closes_rows())

    def test_preseeded_duplicates_collapse_on_reingest(self):
        """AC5 case (b). RED at head-unfixed (no v6 migration: user_version
        stays 5 and both duplicate rows survive), GREEN after the fix: a
        faithful v5 store — the v5 edges DDL (7-column UNIQUE including
        source, no unique closes-pair index), holding the two duplicate
        closes rows (one per provenance stream, distinct evidences), stamped
        PRAGMA user_version = 5 — heals through the REAL public seam
        db.init_schema: it must land at user_version 6, carry a unique
        partial index over the six-column closes pair, and leave exactly ONE
        merged row for the pair."""
        v5_path = Path(self._td.name) / "v5-store.db"
        conn = connect(v5_path)
        try:
            # v5 shape: only meta (init_schema's informational row) and the
            # v5 edges table; the migration path itself only touches edges.
            conn.executescript(_V5_EDGES_DDL)
            conn.executemany(
                "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id, "
                "confidence, evidence, source) "
                "VALUES (?, 'item', ?, 'closes', 'item', ?, 'EXTRACTED', ?, ?)",
                [
                    (REPO, str(PR_NUMBER), str(ISSUE_NUMBER),
                     "closingIssuesReferences", "closing_ref"),
                    (REPO, str(PR_NUMBER), str(ISSUE_NUMBER),
                     "timeline closed event 2031-01-01T00:00:00Z", "timeline"),
                ],
            )
            conn.execute("PRAGMA user_version = 5")
            conn.commit()

            # the public migration seam: the forward chain with the v5->v6
            # closes-pair collapse
            init_schema(conn)

            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(
                version,
                6,
                "MIGRATE1: the stamped v5 store must come out of init_schema "
                "at user_version 6, got " + str(version),
            )
            index_name = unique_closes_pair_index(conn)
            self.assertIsNotNone(
                index_name,
                "MIGRATE1: edges must carry a UNIQUE partial index over "
                "(repo, src_type, src_id, rel, dst_type, dst_id) scoped to "
                "closes pairs after the migration",
            )
            self.assert_single_merged_row(self.closes_rows(conn))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
