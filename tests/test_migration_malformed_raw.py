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
GOOD_ITEM_SIGNALS = (
    '{"id": 1, "number": 1, "title": "t", "state": "open",'
    ' "reactions": {"total_count": 7}, "author_association": "NONE"}'
)
SECOND_ITEM_SIGNALS = (
    '{"id": 2, "number": 2, "title": "t2", "state": "open",'
    ' "reactions": {"total_count": 9}, "author_association": "NONE"}'
)


def _v0_store_multi(payloads: list[str]) -> tuple:
    """A store at the current shape holding one item per payload, rewound
    to user_version 0 so the whole migration chain re-runs on the next
    open. Every payload is valid, so the v6->v7 backfill reaches its
    per-row UPDATE for each one."""
    td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = Path(td.name) / "history.db"
    conn = connect(db)
    init_schema(conn)
    for n, payload in enumerate(payloads, start=1):
        conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, labels_text,"
            " state, raw_json) VALUES (?, 'probe/clinic', ?, 'issue', ?, 'b',"
            " 'l', 'open', ?)",
            (n, n, f"t{n}", payload),
        )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    return td, db


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

    def test_fault_after_an_observable_write_rolls_the_write_back(self) -> None:
        """The rollback is PROVEN, not assumed.

        A fault that lands on the very first row proves nothing: no UPDATE
        has executed yet, so a neutered transaction would look identical.
        Here the fault lands on the SECOND row, after the first row's
        backfill UPDATE has run inside the migration transaction. The
        first row's derived columns must therefore be back to NULL on the
        next open — the write was rolled back, not merely never attempted
        (issue #53, review PRR-013).
        """
        import zaxbygraph.extract as extract_mod

        td, db = _v0_store_multi([GOOD_ITEM_SIGNALS, SECOND_ITEM_SIGNALS])
        self.addCleanup(td.cleanup)
        real = extract_mod.extract_listing_signals
        calls = {"n": 0}

        def explode_on_second(payload):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("disk I/O error")
            return real(payload)

        with mock.patch.object(
            extract_mod, "extract_listing_signals", side_effect=explode_on_second
        ):
            with self.assertRaises(sqlite3.OperationalError):
                conn = connect(db)
                try:
                    init_schema(conn)
                finally:
                    conn.close()
        self.assertEqual(calls["n"], 2, "the fault must land after the first row")
        conn = connect(db)
        try:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 6,
                "the failing migration must roll back to its pre-state"
            )
            rows = conn.execute(
                "SELECT number, reactions_total, author_association FROM items"
                " ORDER BY number"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertIsNone(
                rows[0]["reactions_total"],
                "the first row's in-transaction UPDATE must be rolled back",
            )
            self.assertIsNone(
                rows[0]["author_association"],
                "the first row's in-transaction UPDATE must be rolled back",
            )
        finally:
            conn.close()
        # A later clean run migrates fully and backfills both rows.
        conn = connect(db)
        try:
            init_schema(conn)
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                CURRENT_USER_VERSION,
            )
            rows = conn.execute(
                "SELECT number, reactions_total FROM items ORDER BY number"
            ).fetchall()
            self.assertEqual([r["reactions_total"] for r in rows], [7, 9])
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


class PositiveBackfillTests(unittest.TestCase):
    """The other direction: a VALID payload still backfills.

    Every degradation assertion in this module is NULL-direction, so an
    implementation that never writes the payload-derived columns at all
    passes the whole module (executed mutant M25; issue #53, review
    PRR-015). These legs kill that mutant: the backfill must actually
    populate the columns and child rows for well-formed payloads.
    """

    RICH_ITEM = (
        '{"id": 1, "number": 1, "title": "t", "state": "open",'
        ' "reactions": {"total_count": 7}, "milestone": {"title": "v1.0"},'
        ' "type": {"name": "Bug"}, "author_association": "NONE",'
        ' "sub_issues_summary": {"total": 2, "completed": 1},'
        ' "issue_dependencies_summary": {"blocked_by": 1, "blocking": 2,'
        ' "total_blocked_by": 3, "total_blocking": 4},'
        ' "assignees": [{"login": "carol"}, {"login": "dave"}],'
        ' "issue_field_values": [{"name": "Priority", "value": "high"}]}'
    )

    def _store(self, raw_item: str, raw_comment: str | None = None) -> tuple:
        td, db = _v0_store(raw_item, raw_comment)
        self.addCleanup(td.cleanup)
        return td, db

    def test_valid_item_payload_backfills_its_columns(self) -> None:
        _, db = self._store(self.RICH_ITEM)
        conn = connect(db)
        self.addCleanup(conn.close)
        init_schema(conn)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )
        row = conn.execute(
            "SELECT reactions_total, milestone, item_type, author_association,"
            " sub_issues_total, sub_issues_completed, dependencies_blocked_by,"
            " dependencies_blocking, dependencies_total_blocked_by,"
            " dependencies_total_blocking FROM items WHERE id = 1"
        ).fetchone()
        expected = {
            "reactions_total": 7,
            "milestone": "v1.0",
            "item_type": "Bug",
            "author_association": "NONE",
            "sub_issues_total": 2,
            "sub_issues_completed": 1,
            "dependencies_blocked_by": 1,
            "dependencies_blocking": 2,
            "dependencies_total_blocked_by": 3,
            "dependencies_total_blocking": 4,
        }
        for column, want in expected.items():
            self.assertEqual(row[column], want, f"{column} not backfilled")
        logins = sorted(
            r[0] for r in conn.execute("SELECT login FROM assignees")
        )
        self.assertEqual(logins, ["carol", "dave"], "assignee child rows not written")
        fields = {
            (r[0], r[1])
            for r in conn.execute("SELECT field_name, value FROM issue_field_values")
        }
        self.assertIn(("Priority", "high"), fields)

    def test_valid_comment_payload_backfills_its_column(self) -> None:
        _, db = self._store(
            GOOD_ITEM,
            '{"id": 5, "author_association": "COLLABORATOR", "body": "hi"}',
        )
        conn = connect(db)
        self.addCleanup(conn.close)
        init_schema(conn)
        row = conn.execute(
            "SELECT author_association FROM comments WHERE github_id = 5"
        ).fetchone()
        self.assertEqual(row["author_association"], "COLLABORATOR")

    def test_derived_columns_come_from_the_tables_not_the_payload(self) -> None:
        """comment_count and paths_text are table-derived: the backfill must
        count the stored comments and join the stored pr_files paths."""
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        db = Path(td.name) / "history.db"
        conn = connect(db)
        init_schema(conn)
        conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, labels_text,"
            " state, raw_json) VALUES (1, 'probe/clinic', 1, 'pr', 't', 'b',"
            " 'l', 'open', ?)",
            (GOOD_ITEM,),
        )
        for pk in (5, 6):
            conn.execute(
                "INSERT INTO comments(github_id, repo, number, kind, body, raw_json)"
                " VALUES (?, 'probe/clinic', 1, 'issue_comment', 'b', ?)",
                (pk, '{"id": %d, "body": "hi"}' % pk),
            )
        for path in ("src/b.py", "src/a.py"):
            conn.execute(
                "INSERT INTO pr_files(repo, number, path, status) VALUES"
                " ('probe/clinic', 1, ?, 'modified')",
                (path,),
            )
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        conn.close()

        conn = connect(db)
        self.addCleanup(conn.close)
        init_schema(conn)
        row = conn.execute(
            "SELECT comment_count, paths_text FROM items WHERE id = 1"
        ).fetchone()
        self.assertEqual(row["comment_count"], 2)
        self.assertEqual(row["paths_text"], "src/a.py src/b.py")


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
