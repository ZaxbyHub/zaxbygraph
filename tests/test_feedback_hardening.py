"""Review-feedback hardening tests for the issue #6 edge contract (PR #14
swarm-pr-review round). These live in a NEW module because the four
checkpoint-frozen files (test_edges.py, test_migrations.py, test_extract.py,
test_store.py) are byte-locked by the trace manifest — the counterparts these
tests strengthen live there, and the manifest anchors whole-file blobs.

Covers: the edges unique-key discriminator (source alone, not evidence),
NULL-evidence survival through the v4→v5 rebuild, the foreign closing-ref
drop under its repo-qualified spelling, and the title-only revert reverse
pass (both ingest orders converge)."""
from __future__ import annotations

import sqlite3
import unittest

from fixtures import TempDBTest, issue, pull
from test_migrations import V4_SCHEMA_SQL
from zaxbygraph.db import connect, init_schema
from zaxbygraph.store import ingest_item

REPO = "zaxbyhub/zaxbygraph"


class UniqueKeyDiscriminatorTests(unittest.TestCase):
    """PRR-025: the frozen C6 test's final insert changes source AND
    evidence, so a UNIQUE(...,evidence) key would pass it. The insert here
    differs ONLY by source."""

    def test_source_alone_permits_the_duplicate(self) -> None:
        db_path = self.dir() / "history.db"
        conn = connect(db_path)
        try:
            # Seed at the genuine v4 shape (no source column yet).
            conn.executescript(V4_SCHEMA_SQL)
            conn.execute("PRAGMA user_version = 4")
            # Seed with the genuine v4 vocabulary: rel 'closes' backfills to
            # ('closes_keyword', 'keyword') through the migration, so the
            # discriminator rows below collide/insert against a REAL
            # backfilled row.
            conn.execute(
                "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
                " confidence, evidence)"
                " VALUES (?, 'item', '3', 'closes', 'item', '1',"
                " 'EXTRACTED', 'body closing keyword')",
                (REPO,),
            )
            conn.commit()
            init_schema(conn)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5)
            # Sanity: the backfilled row really is closes_keyword/keyword.
            self.assertEqual(
                tuple(
                    conn.execute(
                        "SELECT rel, source, evidence FROM edges"
                    ).fetchone()
                ),
                ("closes_keyword", "keyword", "body closing keyword"),
            )
            # Same 7-tuple except source: must insert cleanly under the
            # source-in-key contract...
            conn.execute(
                "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
                " confidence, evidence, source)"
                " VALUES (?, 'item', '3', 'closes_keyword', 'item', '1',"
                " 'EXTRACTED', 'body closing keyword', 'timeline')",
                (REPO,),
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0], 2
            )
            # ...and a row with the backfilled source but different evidence
            # must collide, proving source — not evidence — is the
            # discriminator.
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
                    " confidence, evidence, source)"
                    " VALUES (?, 'item', '3', 'closes_keyword', 'item', '1',"
                    " 'EXTRACTED', 'other evidence', 'keyword')",
                    (REPO,),
                )
        finally:
            conn.close()

    def dir(self):
        import tempfile
        from pathlib import Path

        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        return Path(self._td.name)


class NullEvidenceMigrationTests(unittest.TestCase):
    """PRR-016: a v4 edges row with NULL evidence must survive the v4→v5
    rebuild with its evidence (NULL) and backfilled source intact."""

    def test_null_evidence_survives_the_rebuild(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "history.db"
            v4 = sqlite3.connect(str(db_path))
            try:
                v4.executescript(V4_SCHEMA_SQL)
                v4.execute("PRAGMA user_version = 4")
                v4.execute(
                    "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
                    " confidence, evidence)"
                    " VALUES (?, 'item', '2', 'mentions', 'item', '1',"
                    " 'EXTRACTED', NULL)",
                    (REPO,),
                )
                v4.commit()
            finally:
                v4.close()
            conn = connect(db_path)
            try:
                init_schema(conn)
                row = conn.execute(
                    "SELECT rel, evidence, source FROM edges"
                ).fetchone()
                self.assertEqual(tuple(row), ("mentions", None, "keyword"))
            finally:
                conn.close()


class ForeignClosingRefDropTests(TempDBTest):
    """PRR-026 counterpart: the frozen C4 assertion counts only the bare
    '77'; the invariant is that a foreign closing reference is dropped under
    ANY id spelling, including the repo-qualified form."""

    def ingest(self, list_raw, *, pull_raw=None) -> None:
        self.conn.execute("BEGIN")
        ingest_item(
            self.conn,
            REPO,
            list_raw,
            pull_raw=pull_raw,
            issue_comments=[],
            review_comments=[],
            reviews=[],
            files=[],
            include_patches=False,
        )
        self.conn.commit()

    def test_foreign_closing_ref_stores_nothing_under_any_spelling(self) -> None:
        pr8 = issue(8, title="pr eight", kind="pr")
        pull8 = dict(
            pull(8),
            closing_issues_references=[
                {"number": 77, "repo": "Other/Repo"},
            ],
        )
        self.ingest(pr8, pull_raw=pull8)
        # No edge may reference 77 bare OR repo-qualified, in either direction.
        self.assertEqual(
            self.count(
                "SELECT COUNT(*) FROM edges WHERE src_id LIKE '%77' OR dst_id LIKE '%77'"
            ),
            0,
        )
        # And no closes edge may exist from this ingest at all: the only
        # closing reference was foreign.
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM edges WHERE rel = 'closes'"), 0
        )


class TitleRevertReversePassTests(TempDBTest):
    """PRR-007: the title-only revert edge is order-independent — the target's
    ingest completes a revert PR that ingested first, mirroring the
    merge-close reverse pass."""

    def ingest(self, list_raw) -> None:
        self.conn.execute("BEGIN")
        ingest_item(
            self.conn,
            REPO,
            list_raw,
            pull_raw=None,
            issue_comments=[],
            review_comments=[],
            reviews=[],
            files=[],
            include_patches=False,
        )
        self.conn.commit()

    def target_title_rows(self) -> list[tuple]:
        return [
            tuple(r)
            for r in self.conn.execute(
                "SELECT src_id, rel, dst_id, source FROM edges WHERE rel = 'reverts'"
            ).fetchall()
        ]

    def test_target_ingested_later_completes_the_edge(self) -> None:
        revert = issue(30, title='Revert "search returns zero hits"', kind="pr")
        self.ingest(revert)
        self.assertEqual(self.target_title_rows(), [])
        self.ingest(issue(4, title="search returns zero hits", kind="pr"))
        self.assertEqual(
            self.target_title_rows(), [("30", "reverts", "4", "keyword")]
        )

    def test_target_ingested_first_uses_the_forward_path(self) -> None:
        self.ingest(issue(4, title="search returns zero hits", kind="pr"))
        self.ingest(issue(30, title='Revert "search returns zero hits"', kind="pr"))
        self.assertEqual(
            self.target_title_rows(), [("30", "reverts", "4", "keyword")]
        )

    def test_reingest_does_not_duplicate(self) -> None:
        self.ingest(issue(4, title="search returns zero hits", kind="pr"))
        self.ingest(issue(30, title='Revert "search returns zero hits"', kind="pr"))
        self.ingest(issue(4, title="search returns zero hits", kind="pr"))
        self.assertEqual(len(self.target_title_rows()), 1)

    def test_unmerged_pr_gets_no_merged_commit_edge(self) -> None:
        """PRR-003: GitHub keeps a speculative test-merge sha on open PRs
        (REST); a merged_commit edge to it would be a false fact."""
        pr = issue(9, title="open pr", kind="pr")
        self.ingest(
            pr,
        )
        self.conn.execute("BEGIN")
        ingest_item(
            self.conn,
            REPO,
            pr,
            pull_raw=dict(
                pull(9),  # merged=False → merged_at None
                merge_commit_sha="ea365a3514ac50dddd923414b95fb1d09fb99c44",
                merged_by="someone",
            ),
            issue_comments=[],
            review_comments=[],
            reviews=[],
            files=[],
            include_patches=False,
        )
        self.conn.commit()
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM edges WHERE rel = 'merged_commit'"), 0
        )
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM edges WHERE rel = 'merged_by'"), 0
        )


if __name__ == "__main__":
    unittest.main()
