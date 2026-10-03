"""Issue #4 v3-migration atomicity and fresh-chain tests (non-frozen
supplements to tests/test_migrations.py, whose blob is pinned by the frozen
C6 check)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_migrations import V2_SCHEMA_SQL  # unittest discovery import style
except ImportError:  # pragma: no cover - direct tests.* package import
    from tests.test_migrations import V2_SCHEMA_SQL
from zaxbygraph import db as db_mod
from zaxbygraph.db import CURRENT_USER_VERSION, connect, init_schema


def _seed_v2(db_path: Path) -> None:
    v2 = sqlite3.connect(str(db_path))
    v2.executescript(V2_SCHEMA_SQL)
    v2.execute("PRAGMA user_version = 2")
    v2.execute(
        "INSERT INTO items(id, repo, number, kind, title, body, state,"
        " updated_at, raw_json) VALUES (1, 'acme/forgegate', 1, 'issue',"
        " 'WebSocket reconnect leaks memory', 'every reconnect leaks a timer',"
        " 'open', '2026-01-03T00:00:00Z', '{}')"
    )
    v2.commit()
    v2.close()


class V3MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        self.db_path = Path(td.name) / "history.db"

    def test_fresh_db_ends_at_current_version_with_porter(self) -> None:
        conn = connect(self.db_path)
        self.addCleanup(conn.close)
        init_schema(conn)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )
        for table in ("items_fts", "comments_fts"):
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
            ).fetchone()[0]
            self.assertIn("porter", ddl.lower())

    def test_v3_failure_rolls_back_whole_including_ddl(self) -> None:
        """A mid-v3 failure must leave user_version 2 AND the OLD unicode61
        FTS DDL intact — this is the test that catches an executescript-style
        implicit COMMIT inside the migration (it would leak the new DDL past
        the rollback)."""
        _seed_v2(self.db_path)

        def exploding_v3(conn):
            conn.execute("DROP TRIGGER IF EXISTS items_ai")
            conn.execute("DROP TABLE IF EXISTS items_fts")
            raise RuntimeError("boom mid-migration")

        with mock.patch.object(
            db_mod,
            "MIGRATIONS",
            [(2, db_mod.migrate_v1_to_v2), (3, exploding_v3)],
        ):
            conn = connect(self.db_path)
            with self.assertRaises(RuntimeError):
                init_schema(conn)
            # the failed migration's transaction rolled back whole
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 2
            )
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'items_fts'"
            ).fetchone()[0]
            self.assertNotIn(
                "porter", ddl.lower(), "old FTS DDL must survive the rollback"
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1
            )
            conn.close()

        # a later clean run migrates fully and the corpus survives
        conn = connect(self.db_path)
        self.addCleanup(conn.close)
        init_schema(conn)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1
        )
        hits = conn.execute(
            "SELECT items.number FROM items_fts"
            " JOIN items ON items.id = items_fts.rowid"
            " WHERE items_fts MATCH '\"reconnect\"'"
        ).fetchall()
        self.assertEqual([r[0] for r in hits], [1])

    def test_real_v3_with_poisoned_tail_rolls_back_atomically(self) -> None:
        """Exercise the REAL migrate_v2_to_v3 (not a stand-in callback) with
        an injected failure after its destructive DDL: poisoning the tail of
        _FTS_PORTER_STATEMENTS drives the genuine code path, so an
        executescript-style implicit COMMIT inside it would show porter DDL
        on a second connection. Review finding F5: the mocked-callback
        variant of this test cannot catch that bug class."""
        _seed_v2(self.db_path)
        poison = ("INSERT INTO items_fts(items_fts) VALUES('not-a-valid-fts5-command')",)
        with mock.patch.object(
            db_mod, "_FTS_PORTER_STATEMENTS", db_mod._FTS_PORTER_STATEMENTS + poison
        ):
            conn = connect(self.db_path)
            with self.assertRaises(sqlite3.OperationalError):
                init_schema(conn)
            conn.close()
        # observe from a SECOND connection: the failed migration must not
        # have leaked any new-schema DDL past the rollback
        witness = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        self.addCleanup(witness.close)
        self.assertEqual(witness.execute("PRAGMA user_version").fetchone()[0], 2)
        ddl = witness.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'items_fts'"
        ).fetchone()[0]
        self.assertNotIn(
            "porter", ddl.lower(), "torn DDL state: migration leaked past rollback"
        )
        witness.close()
        # a later clean run (unpoisoned) migrates fully and stems
        conn = connect(self.db_path)
        self.addCleanup(conn.close)
        init_schema(conn)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )
        hits = conn.execute(
            "SELECT items.number FROM items_fts"
            " JOIN items ON items.id = items_fts.rowid"
            " WHERE items_fts MATCH '\"reconnect\"'"
        ).fetchall()
        self.assertEqual([r[0] for r in hits], [1])

    def test_migrated_comments_fts_carries_porter_and_stems(self) -> None:
        """Review finding F4: the migrated path's comments_fts coverage —
        its DDL must carry porter AND a stemmed comment query must match
        (stored 'reconnection', query 'reconnect'). Both mutations this
        guards (dropping porter from either DDL copy) are suite-visible
        only with these assertions on a genuine v2-seeded database."""
        v2 = sqlite3.connect(str(self.db_path))
        v2.executescript(V2_SCHEMA_SQL)
        v2.execute("PRAGMA user_version = 2")
        v2.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " updated_at, raw_json) VALUES (1, 'acme/forgegate', 1, 'issue',"
            " 'Timer cleanup on socket teardown', 'nothing stemmy here',"
            " 'open', '2026-01-04T00:00:00Z', '{}')"
        )
        v2.execute(
            "INSERT INTO comments(github_id, repo, number, kind, author, body,"
            " raw_json) VALUES (501, 'acme/forgegate', 1, 'issue_comment',"
            " 'bob', 'the reconnection attempt failed', '{}')"
        )
        v2.commit()
        v2.close()
        conn = connect(self.db_path)
        self.addCleanup(conn.close)
        init_schema(conn)
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'comments_fts'"
        ).fetchone()[0]
        self.assertIn("porter", ddl.lower())
        hits = conn.execute(
            "SELECT comments.number FROM comments_fts"
            " JOIN comments ON comments.pk = comments_fts.rowid"
            " WHERE comments_fts MATCH '\"reconnect\"'"
        ).fetchall()
        self.assertEqual([r[0] for r in hits], [1])


if __name__ == "__main__":
    unittest.main()
