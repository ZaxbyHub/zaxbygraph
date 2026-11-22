"""Issue #88/#91 recurrence guardrail for trace 88-91 — NON-FROZEN.

Deliberately outside the checkpoint manifest. Two properties:

1. `snapshot._REPO_BEARING_TABLES` equals the set of tables a REAL initialized
   database declares a `repo` column on — derived from `sqlite_master` +
   `PRAGMA table_info`, not from a hand-maintained list, so a future
   repo-bearing table (or a dropped oracle member) fails this test.
   `issue_field_values` is the concrete member the audit issue's enumeration
   missed.
2. A completely malformed listing page records EVERY shape, pins ONE floor,
   never counts or bundles a malformed row, and the healthy rows still ingest.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, issue

from zaxbygraph.db import connect, init_schema
from zaxbygraph.snapshot import _REPO_BEARING_TABLES
from zaxbygraph.sync import sync_repo


def _repo_bearing_tables(conn: sqlite3.Connection) -> set[str]:
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ) if not r[0].startswith("sqlite_")
    }
    actual: set[str] = set()
    for table in tables:
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "repo" in columns:
            actual.add(table)
    return actual


class RepoBearingTableGuardrailTests(unittest.TestCase):
    def test_oracle_covers_every_repo_bearing_table(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            conn = connect(Path(tdn) / "history.db")
            try:
                init_schema(conn)
                actual = _repo_bearing_tables(conn)
            finally:
                conn.close()
        self.assertIn("issue_field_values", actual)
        self.assertEqual(
            set(_REPO_BEARING_TABLES), actual,
            "snapshot._REPO_BEARING_TABLES drifted from the initialized schema",
        )

    def test_a_dropped_member_is_detectable(self) -> None:
        """The comparison above bites: a degraded oracle (a missing member)
        does not equal the derived set."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            conn = connect(Path(tdn) / "history.db")
            try:
                init_schema(conn)
                actual = _repo_bearing_tables(conn)
            finally:
                conn.close()
        degraded = set(_REPO_BEARING_TABLES) - {"issue_field_values"}
        self.assertNotEqual(degraded, actual)


class CombinedMalformedPageTests(unittest.TestCase):
    def test_all_shapes_recorded_under_one_floor(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            conn = connect(Path(tdn) / "history.db")
            conn.row_factory = sqlite3.Row
            try:
                init_schema(conn)
                page = [
                    issue(1),
                    {"title": "no number"},
                    {"number": "x", "id": 5001},
                    {"number": 2, "id": "bad"},
                    "not-a-dict",
                    issue(3),
                ]

                class Page(FakeGitHubSource):
                    def list_issues(self, since):
                        yield page

                result = sync_repo(conn, Page(), REPO)
                notes = [
                    r[0] for r in conn.execute(
                        "SELECT note FROM fetch_log"
                        " WHERE note LIKE 'skipped malformed listing row:%'"
                    )
                ]
                self.assertGreaterEqual(len(notes), 4, notes)
                self.assertEqual(result["ingested"], 2)
                self.assertEqual(
                    [r[0] for r in conn.execute(
                        "SELECT number FROM items WHERE repo = ? ORDER BY number",
                        (REPO,),
                    )],
                    [1, 3],
                )
                state = conn.execute(
                    "SELECT last_error, listing_floor_active FROM sync_state"
                    " WHERE repo = ?", (REPO,)
                ).fetchone()
                self.assertEqual(state["listing_floor_active"], 1)
                self.assertIsNotNone(state["last_error"])
                self.assertLess(
                    len(conn.execute(
                        "SELECT note FROM fetch_log WHERE note IS NOT NULL"
                        " ORDER BY id DESC LIMIT 1"
                    ).fetchone()[0]),
                    200,
                    "the recorded reason stays bounded",
                )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
