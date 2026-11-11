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


def _make_v10_store(conn) -> None:
    """Demote an initialized store to the real v10 sync_state shape
    (ALTER DROP chokes on the commented CREATE text) and rewind the
    version stamp. Shared by the migration tests (review PRR-018)."""
    conn.execute("DROP TABLE sync_state")
    conn.execute(
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
    conn.execute("PRAGMA user_version = 10")
    conn.commit()


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
        # fetch_source is an APPENDED migration column: it arrives after
        # every v10 column. It is not asserted to be the last column —
        # v12 appends listing_floor/listing_floor_active after it, and
        # "last" would break on every later addition while saying nothing
        # the parity assertion below does not.
        self.assertGreater(
            _columns(self.db).index("fetch_source"),
            # The v10 shape ends at rate_limit_reset_at (index 11 of the
            # 12-column list), so fetch_source must sit strictly AFTER it —
            # `> 10` would admit index 11, before the last v10 column
            # (PR #76 review PRR-029).
            11,
            "fetch_source must follow every v10 column, never precede it",
        )
        self.assertEqual(_version(self.db), CURRENT_USER_VERSION)

    def test_v10_store_migrates_to_identical_shape(self):
        c = connect(self.db)
        init_schema(c)
        # Demote to the real v10 shape (shared helper, review PRR-018).
        _make_v10_store(c)
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
        self.assertGreater(
            _columns(self.db).index("fetch_source"),
            # The v10 shape ends at rate_limit_reset_at (index 11 of the
            # 12-column list), so fetch_source must sit strictly AFTER it —
            # `> 10` would admit index 11, before the last v10 column
            # (PR #76 review PRR-029).
            11,
            "fetch_source must follow every v10 column, never precede it",
        )

    def test_migration_is_idempotent(self):
        c = connect(self.db)
        init_schema(c)
        init_schema(c)  # second writable open: no-op
        c.close()
        self.assertEqual(_version(self.db), CURRENT_USER_VERSION)

    def test_reads_never_migrate_a_v10_store(self):
        c = connect(self.db)
        init_schema(c)
        _make_v10_store(c)
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
