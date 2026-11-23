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
from unittest import mock

from fixtures import REPO, FakeGitHubSource, issue

from zaxbygraph import snapshot
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
        """A degraded production oracle is caught BEHAVIOURALLY: with
        `labels` dropped from `_REPO_BEARING_TABLES`, the ownership scan no
        longer sees a repo only that table carries — so the schema-derived
        comparison fails. This mutates the real constant instead of doing
        set arithmetic on a hand-built degraded copy (cubic finding)."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            conn = connect(Path(tdn) / "history.db")
            conn.row_factory = sqlite3.Row
            try:
                init_schema(conn)
                conn.execute(
                    "INSERT INTO labels(repo, number, name)"
                    " VALUES ('only/here', 1, 'x')"
                )
                conn.commit()
                real = snapshot._repo_owners(conn)
                self.assertIn(
                    "only/here", real,
                    "the real oracle must see a repo only labels carries",
                )
                degraded = tuple(
                    t for t in snapshot._REPO_BEARING_TABLES if t != "labels"
                )
                with mock.patch(
                    "zaxbygraph.snapshot._REPO_BEARING_TABLES", degraded
                ):
                    mutated = snapshot._repo_owners(conn)
                self.assertNotIn(
                    "only/here", mutated,
                    "a dropped member must make its rows invisible",
                )
                # ...and the guard's schema-derived comparison is what catches
                # that omission (labels is in the initialized schema).
                self.assertNotEqual(set(degraded), _repo_bearing_tables(conn))
            finally:
                conn.close()


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
