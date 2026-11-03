"""Issue #53: the v6->v7 migration backfill must survive malformed stored
payloads instead of bricking the store.

Base behavior pinned here (RED at f28e4b4): `migrate_v6_to_v7` parses
`items.raw_json` (db.py:806) and `comments.raw_json` (db.py:869) with a bare
`json.loads`, so one malformed row aborts the chain and pins
`PRAGMA user_version` at 6 forever — `sync` and `doctor --consolidate` then
fail on that store with a bare JSON error and no repair path. Wrong-typed
fields inside valid JSON, non-list child containers and out-of-64-bit
integers brick the chain the same way (sqlite3.ProgrammingError /
TypeError / OverflowError at the binder).

The guard is a PAYLOAD-validation boundary: parse failures and
payload-shape failures degrade that row's derived columns to NULL; database
execution faults still propagate and roll back (AGENTS.md: a storage fault
is reported, not swallowed).
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from zaxbygraph.db import (  # noqa: E402
    CURRENT_USER_VERSION,
    connect,
    init_schema,
)

GOOD_ITEM = '{"id": 1, "number": 1, "title": "t", "state": "open"}'


def _v0_store(raw_item: str, raw_comment: str | None = None) -> tuple:
    """A store at the current shape holding the given payloads, rewound to
    user_version 0 so the whole migration chain re-runs on the next open."""
    td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = Path(td.name) / "history.db"
    conn = connect(db)
    init_schema(conn)
    conn.execute(
        "INSERT INTO items(id, repo, number, kind, title, body, labels_text,"
        " state, raw_json) VALUES (1, 'probe/clinic', 1, 'issue', 't', 'b',"
        " 'l', 'open', ?)",
        (raw_item,),
    )
    if raw_comment is not None:
        conn.execute(
            "INSERT INTO comments(github_id, repo, number, kind, body, raw_json)"
            " VALUES (5, 'probe/clinic', 1, 'issue_comment', 'b', ?)",
            (raw_comment,),
        )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    return td, db


class _MalformedPayloadCase(unittest.TestCase):
    def setUp(self) -> None:
        self.td, self.db = _v0_store(GOOD_ITEM, '{"id": 5, "body": "hi"}')
        self.addCleanup(self.td.cleanup)

    def _reopen(self) -> sqlite3.Connection:
        """Open the store through the full migration chain. At base the
        chain bricks on a malformed payload, which this surfaces as a clean
        failure naming the exception rather than an error."""
        try:
            conn = connect(self.db)
            self.addCleanup(conn.close)
            init_schema(conn)
        except Exception as exc:  # base behavior: the chain bricks
            self.fail(f"migration chain bricked on the stored payload: {exc!r}")
        return conn

    def _rewind(self, raw_item: str, raw_comment: str | None = None) -> None:
        self.td.cleanup()
        self.td, self.db = _v0_store(raw_item, raw_comment)
        self.addCleanup(self.td.cleanup)


class ItemPayloadTests(_MalformedPayloadCase):
    def test_malformed_item_raw_json_does_not_brick(self) -> None:
        self._rewind("this is not json")
        conn = self._reopen()
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )
        row = conn.execute(
            "SELECT reactions_total, author_association FROM items WHERE id = 1"
        ).fetchone()
        self.assertIsNone(row["reactions_total"])
        self.assertIsNone(row["author_association"])

    def test_wrong_typed_item_payload_degrades_to_null(self) -> None:
        self._rewind('{"id": 1, "number": 1, "author_association": ["OWNER"]}')
        conn = self._reopen()
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )
        row = conn.execute(
            "SELECT author_association FROM items WHERE id = 1"
        ).fetchone()
        self.assertIsNone(row["author_association"])

    def test_valid_json_non_object_degrades(self) -> None:
        self._rewind("[1, 2, 3]")
        conn = self._reopen()
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )
        row = conn.execute(
            "SELECT reactions_total FROM items WHERE id = 1"
        ).fetchone()
        self.assertIsNone(row["reactions_total"])

    def test_oversized_integer_degrades_to_null(self) -> None:
        self._rewind(
            '{"id": 1, "number": 1, "reactions": {"total_count":'
            ' 100000000000000000000000000000}}'
        )
        conn = self._reopen()
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )
        row = conn.execute(
            "SELECT reactions_total FROM items WHERE id = 1"
        ).fetchone()
        self.assertIsNone(row["reactions_total"])

    def test_non_list_assignee_container_skips_children(self) -> None:
        self._rewind('{"id": 1, "number": 1, "assignees": 7}')
        conn = self._reopen()
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM assignees").fetchone()[0], 0
        )

    def test_non_list_issue_field_container_skips_children(self) -> None:
        self._rewind('{"id": 1, "number": 1, "issue_field_values": 7}')
        conn = self._reopen()
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM issue_field_values").fetchone()[0], 0
        )


class CommentPayloadTests(_MalformedPayloadCase):
    def test_malformed_comment_raw_json_does_not_brick(self) -> None:
        self._rewind(GOOD_ITEM, "{oops")
        conn = self._reopen()
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )
        row = conn.execute(
            "SELECT author_association FROM comments WHERE github_id = 5"
        ).fetchone()
        self.assertIsNone(row["author_association"])

    def test_wrong_typed_comment_payload_degrades_to_null(self) -> None:
        self._rewind(GOOD_ITEM, '{"id": 5, "author_association": ["OWNER"]}')
        conn = self._reopen()
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )
        row = conn.execute(
            "SELECT author_association FROM comments WHERE github_id = 5"
        ).fetchone()
        self.assertIsNone(row["author_association"])


class StorageFaultTests(_MalformedPayloadCase):
    def test_database_fault_propagates_and_rolls_back(self) -> None:
        """A storage fault is reported, not swallowed (AGENTS.md): the
        migration transaction rolls back and user_version stays put."""
        import zaxbygraph.extract as extract_mod

        with mock.patch.object(
            extract_mod, "extract_listing_signals",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                conn = connect(self.db)
                try:
                    init_schema(conn)
                finally:
                    conn.close()
        conn = connect(self.db)
        try:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 6,
                "the failing migration must roll back to its pre-state"
            )
        finally:
            conn.close()

    def test_second_open_after_degradation_is_a_noop(self) -> None:
        self._rewind("this is not json")
        conn = self._reopen()
        self.addCleanup(conn.close)
        before = [tuple(r) for r in conn.execute("SELECT * FROM items")]
        conn.close()
        conn = connect(self.db)
        self.addCleanup(conn.close)
        init_schema(conn)
        after = [tuple(r) for r in conn.execute("SELECT * FROM items")]
        self.assertEqual(before, after)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )


class WholeChainGuardrailTests(unittest.TestCase):
    def test_every_raw_json_malformed_still_completes(self) -> None:
        """The guardrail: a store whose EVERY raw_json is malformed (text,
        wrong-typed, non-object, oversized) migrates to the current version
        with the payload-derived columns NULL — the chain never bricks."""
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        db = Path(td.name) / "history.db"
        conn = connect(db)
        init_schema(conn)
        payloads = [
            "this is not json",
            '{"id": 2, "number": 2, "author_association": ["OWNER"]}',
            "[1, 2, 3]",
            '{"id": 4, "number": 4, "reactions": {"total_count":'
            " 100000000000000000000000000000}}",
            '{"id": 5, "number": 5, "assignees": 7}',
        ]
        for n, payload in enumerate(payloads, start=1):
            conn.execute(
                "INSERT INTO items(id, repo, number, kind, title, state, raw_json)"
                " VALUES (?, 'probe/clinic', ?, 'issue', 't', 'open', ?)",
                (n, n, payload),
            )
            conn.execute(
                "INSERT INTO comments(github_id, repo, number, kind, body, raw_json)"
                " VALUES (?, 'probe/clinic', ?, 'issue_comment', 'b', ?)",
                (900 + n, n, "{oops"),
            )
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        conn.close()

        conn = connect(db)
        self.addCleanup(conn.close)
        try:
            init_schema(conn)
        except Exception as exc:  # base behavior: the chain bricks
            self.fail(f"migration chain bricked on malformed payloads: {exc!r}")
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )
        nulls = conn.execute(
            "SELECT COUNT(*) FROM items WHERE reactions_total IS NULL"
        ).fetchone()[0]
        self.assertEqual(nulls, len(payloads))


if __name__ == "__main__":
    unittest.main()
