"""AC7 companion coverage for issue #22 (feedback round).

The frozen suite pins the title index's *plan* (C2) and its cost effect
(C4); this module pins the upgrade path claimed by docs/schema.md: an
EXISTING store that lacks `idx_items_repo_title` gains it on its next
write-open (init_schema) with no `user_version` bump, and a read-only open
never creates it. Unfrozen by design — it extends coverage without touching
the checkpoint-anchored file.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from zaxbygraph import db

TITLE_INDEX = "idx_items_repo_title"


class TitleIndexUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "history.db"
        conn = db.connect(self.db_path)
        db.init_schema(conn)
        conn.execute(f"DROP INDEX IF EXISTS {TITLE_INDEX}")
        conn.commit()
        conn.close()

    def test_existing_store_gains_title_index_on_write_open(self) -> None:
        probe = sqlite3.connect(self.db_path)
        try:
            before = probe.execute("PRAGMA user_version").fetchone()[0]
        finally:
            probe.close()
        conn = db.connect(self.db_path)
        db.init_schema(conn)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            after = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        self.assertIn(TITLE_INDEX, names)
        self.assertEqual(before, after)

    def test_schema_notes_carry_the_sync_state_staleness_caveat(self) -> None:
        from zaxbygraph.schema_notes import COLUMN_NOTES

        for column in ("item_count", "comment_count", "edge_count"):
            note = COLUMN_NOTES["sync_state"].get(column, "")
            self.assertIn("lag", note.lower(), column)

    def test_read_only_open_never_creates_the_title_index(self) -> None:
        conn = db.connect_readonly_query(self.db_path)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
        finally:
            conn.close()
        self.assertNotIn(TITLE_INDEX, names)


if __name__ == "__main__":
    unittest.main()
