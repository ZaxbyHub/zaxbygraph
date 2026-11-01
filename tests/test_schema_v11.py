"""Schema v11 migration tests (issue #48 fetch-source persistence).

Fresh-shape vs migrated-shape column order, NULL defaults, migration
idempotence, and the reads-never-migrate boundary.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from zaxbygraph.db import CURRENT_USER_VERSION, connect, init_schema, open_existing


def _columns(db_path):
    c = sqlite3.connect(str(db_path))
    try:
        return [
            r[1]
            for r in c.execute("PRAGMA table_info(sync_state)").fetchall()
        ]
    finally:
        c.close()


def _version(db_path):
    c = sqlite3.connect(str(db_path))
    try:
        return int(c.execute("PRAGMA user_version").fetchone()[0])
    finally:
        c.close()


class SchemaV11MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        self.db = Path(td.name) / "history.db"

    def test_fresh_store_has_fetch_source_last_and_null(self):
        c = connect(self.db)
        init_schema(c)
        c.execute(
            "INSERT INTO sync_state(repo, include_patches) VALUES (?, 0)",
            ("acme/forgegate",),
        )
        c.commit()
        row = c.execute(
            "SELECT fetch_source FROM sync_state WHERE repo = ?",
            ("acme/forgegate",),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row[0], "a fresh stamp must leave fetch_source NULL")
        c.close()
        self.assertEqual(_columns(self.db)[-1], "fetch_source")
        self.assertEqual(_version(self.db), CURRENT_USER_VERSION)

    def test_v10_store_migrates_to_identical_shape(self):
        c = connect(self.db)
        init_schema(c)
        # Demote to the real v10 shape (ALTER DROP chokes on the commented
        # CREATE text): recreate the table without the v11 column.
        c.execute("DROP TABLE sync_state")
        c.execute(
            "CREATE TABLE sync_state ("
            " repo TEXT PRIMARY KEY, issues_since TEXT,"
            " last_full_sync_at TEXT, last_incr_sync_at TEXT,"
            " last_error TEXT, item_count INTEGER NOT NULL DEFAULT 0,"
            " comment_count INTEGER NOT NULL DEFAULT 0,"
            " edge_count INTEGER NOT NULL DEFAULT 0,"
            " include_patches INTEGER NOT NULL DEFAULT 0,"
            " full_sync_pending INTEGER NOT NULL DEFAULT 0,"
            " rate_limit_remaining INTEGER, rate_limit_reset_at TEXT)"
        )
        c.execute("PRAGMA user_version = 10")
        c.commit()
        c.close()
        self.assertNotIn("fetch_source", _columns(self.db))

        c = connect(self.db)
        init_schema(c)  # writable open: migrates in place
        self.assertEqual(_version(self.db), CURRENT_USER_VERSION)
        c.close()
        # Fresh and migrated shapes agree on column order.
        fresh = Path(str(self.db) + ".fresh")
        c2 = connect(fresh)
        init_schema(c2)
        c2.close()
        self.assertEqual(_columns(self.db), _columns(fresh))
        self.assertEqual(_columns(self.db)[-1], "fetch_source")

    def test_migration_is_idempotent(self):
        c = connect(self.db)
        init_schema(c)
        init_schema(c)  # second writable open: no-op
        c.close()
        self.assertEqual(_version(self.db), CURRENT_USER_VERSION)

    def test_reads_never_migrate_a_v10_store(self):
        c = connect(self.db)
        init_schema(c)
        c.execute("DROP TABLE sync_state")
        c.execute(
            "CREATE TABLE sync_state ("
            " repo TEXT PRIMARY KEY, issues_since TEXT,"
            " last_full_sync_at TEXT, last_incr_sync_at TEXT,"
            " last_error TEXT, item_count INTEGER NOT NULL DEFAULT 0,"
            " comment_count INTEGER NOT NULL DEFAULT 0,"
            " edge_count INTEGER NOT NULL DEFAULT 0,"
            " include_patches INTEGER NOT NULL DEFAULT 0,"
            " full_sync_pending INTEGER NOT NULL DEFAULT 0,"
            " rate_limit_remaining INTEGER, rate_limit_reset_at TEXT)"
        )
        c.execute("PRAGMA user_version = 10")
        c.commit()
        c.close()
        ro = open_existing(self.db)
        try:
            ro.execute("SELECT COUNT(*) FROM sync_state").fetchone()
        finally:
            ro.close()
        self.assertEqual(
            _version(self.db), 10,
            "a read-only open must never migrate the store",
        )


if __name__ == "__main__":
    unittest.main()
