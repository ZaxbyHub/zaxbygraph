"""Issue #56 frozen acceptance checks C14-C15: fabricated values the
adoption reads — a NULL or non-text `sync_state.repo`, a non-integer
`revisions.number`, and a copied table that lacks a current column.
Authored independently of any fix as a frozen spec: every test is RED at
base 80ea01c6ae202ea03373cc26ad9ff8cc561fe39a for the reason its docstring
names, and GREEN after the fix. All fault injection is in-process;
fixtures are local SQLite/temp-dir builds — no network, no gh, no
credentials.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fixtures import REPO, issue  # noqa: E402
from test_migrations import V1_SCHEMA_SQL  # noqa: E402

import zaxbygraph.doctor as doctor_mod  # noqa: E402
from zaxbygraph.db import connect, init_schema  # noqa: E402
from zaxbygraph.store import ingest_item  # noqa: E402


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _prep(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _item(n: int) -> dict:
    payload = issue(n, title=f"t{n}", body=f"body {n}",
                    created_at="2027-06-01T00:00:00Z")
    payload["id"] = 9000 + n
    return payload


def _seed_store(db: Path, numbers: tuple[int, ...], stamp: str,
                comment: bool = False) -> None:
    conn = connect(db)
    init_schema(conn)
    for n in numbers:
        ingest_item(
            conn, REPO, _item(n), pull_raw=None,
            issue_comments=[{
                "id": 7000 + n,
                "user": {"login": "bob", "html_url": "https://github.com/bob"},
                "body": "hello",
                "created_at": "2027-06-02T00:00:00Z",
            }] if comment else [],
            review_comments=[], reviews=[], files=[], include_patches=False)
    conn.execute(
        "UPDATE sync_state SET last_full_sync_at = ?, full_sync_pending = 0,"
        " issues_since = ? WHERE repo = ?", (stamp, stamp, REPO))
    conn.commit()
    conn.close()


def _null_repo_candidate(bad: Path) -> None:
    """Add a `sync_state` row whose repo is NULL — SQLite permits NULL in a
    TEXT PRIMARY KEY on a rowid table, and the fold's grouping calls
    .lower() on it."""
    conn = sqlite3.connect(str(bad))
    conn.execute(
        "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
        " last_error, full_sync_pending, item_count, comment_count,"
        " edge_count, include_patches)"
        " VALUES (NULL, '2027-03-02T00:00:00Z', '2027-03-02T00:00:00Z',"
        " NULL, 0, 0, 0, 0, 0)")
    conn.commit()
    conn.close()


def _text_revisions_number_candidate(bad: Path) -> None:
    """Add a `revisions` row whose number is TEXT — SQLite's dynamic typing
    stores it despite the INTEGER declaration, and the per-item read calls
    int() on it."""
    conn = sqlite3.connect(str(bad))
    conn.execute(
        "INSERT INTO revisions(repo, number, entity, entity_id, kind,"
        " origin, content, valid_from, valid_to)"
        " VALUES (?, 'abc', 'item_body', '5', 'body', 'edit', 'text',"
        " '2027-06-01T00:00:00Z', '2027-06-02T00:00:00Z')", (REPO,))
    conn.commit()
    conn.close()


def _drop_comments_column(bad: Path, column: str) -> None:
    """Rebuild `comments` without `column` (ALTER TABLE DROP COLUMN is
    avoided: the table's triggers reference the dropped name)."""
    conn = sqlite3.connect(str(bad))
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='comments'"
    ).fetchone()[0]
    lines = [ln for ln in ddl.splitlines()
             if ln.strip().split()[0] != column]
    for i in range(len(lines) - 2, -1, -1):  # drop the dangling comma
        stripped = lines[i].strip()
        if stripped.startswith("(") or not stripped:
            continue
        if lines[i].rstrip().endswith(",") and lines[i + 1].strip().startswith(")"):
            lines[i] = lines[i].rstrip()[:-1]
        break
    conn.execute("ALTER TABLE comments RENAME TO comments_old")
    conn.execute("\n".join(lines))
    keep = [r[1] for r in conn.execute("PRAGMA table_info(comments_old)")
            if r[1] != column]
    collist = ", ".join(keep)
    conn.execute(
        f"INSERT INTO comments ({collist}) SELECT {collist} FROM comments_old")
    conn.execute("DROP TABLE comments_old")
    conn.commit()
    conn.close()


def _null_repo_v1_store(db: Path) -> None:
    """A genuine v1 store with one lowercase sync_state row plus a NULL-repo
    row — the migration-time fold's grouping shape."""
    conn = sqlite3.connect(str(db))
    conn.executescript(V1_SCHEMA_SQL)
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
        " include_patches) VALUES ('acme/forgegate', '2026-01-01T00:00:00Z',"
        " '2026-01-01T00:00:00Z', 0)")
    conn.execute(
        "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
        " include_patches) VALUES (NULL, '2026-01-02T00:00:00Z',"
        " '2026-01-02T00:00:00Z', 0)")
    conn.execute(
        "INSERT INTO items(id, repo, number, kind, title, state, updated_at,"
        " raw_json) VALUES (1, 'acme/forgegate', 1, 'issue', 'one', 'open',"
        " '2026-01-01T00:00:00Z', '{}')")
    conn.commit()
    conn.close()


class _FabricatedValueCase(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="zgt56-fabricated-"))
        self.addCleanup(_rmtree, self.scratch)

    def _divergent_pair(self, tag: str, diverge):
        """(good, bad, store, empty_cwd): a healthy candidate, a freshest
        complete divergent candidate (the consolidation winner), a store
        path, and a deterministically empty non-git cwd."""
        root = self.scratch / tag
        good = _prep(root / "good" / "history.db")
        _seed_store(good, (1, 2), "2027-01-01T00:00:00Z")
        bad = _prep(root / "bad" / "history.db")
        _seed_store(bad, (5, 6), "2027-03-01T00:00:00Z")
        diverge(bad)
        return good, bad, _prep(root / "store" / "history.db"), _prep(root / "cwd")

    def _assert_isolated(self, envelope, good: Path, bad: Path) -> None:
        rows = {os.path.normcase(str(row.get("path", ""))): row
                for row in envelope.get("scanned") or []}
        self.assertIn(os.path.normcase(str(good)), rows,
                      "the remaining candidates were not scanned")
        entry = rows.get(os.path.normcase(str(bad)))
        self.assertIsNotNone(entry, "the divergent candidate vanished")
        self.assertTrue(entry.get("unreadable"), entry)
        self.assertTrue(entry.get("unreadable_reason"), entry)
        self.assertTrue(envelope.get("ok"), envelope)
        self.assertIsNone(envelope.get("adopted"), envelope)


class FabricatedKeyValueTests(_FabricatedValueCase):
    def test_fabricated_sync_state_values_isolate_candidate(self) -> None:
        """AC14: when a candidate's `sync_state` carries a fabricated value
        the adoption reads — a NULL or non-text `repo` (the fold's grouping
        key), or a non-integer `revisions.number` (the per-item read) — the
        system does NOT abort the command: the candidate is isolated as
        unreadable, on the consolidation path AND through the migration
        fold. RED at base, GREEN after the fix: at base the NULL repo
        raises an uncaught AttributeError at .lower() (both paths), and the
        TEXT revisions.number raises an uncaught ValueError at int() —
        neither is a sqlite3.DatabaseError, so every handler misses them
        (implementation-review round-3 findings 2 and 3)."""
        # shape A: the NULL repo reaches the fold's grouping on the
        # consolidation path
        good, bad, store, empty_cwd = self._divergent_pair(
            "c14-null-repo", _null_repo_candidate)
        with self.subTest(shape="null_repo_candidate"):
            envelope = None
            try:
                envelope = doctor_mod.doctor(empty_cwd, REPO, store,
                                             extra_scans=[good, bad],
                                             consolidate_flag=True)
            except AttributeError as exc:
                self.fail(f"AttributeError escaped candidate isolation and"
                          f" aborted the whole command: {exc!r}")
            self._assert_isolated(envelope, good, bad)

        # shape B: the TEXT revisions.number reaches the per-item read
        good, bad, store, empty_cwd = self._divergent_pair(
            "c14-text-number", _text_revisions_number_candidate)
        with self.subTest(shape="text_revisions_number"):
            envelope = None
            try:
                envelope = doctor_mod.doctor(empty_cwd, REPO, store,
                                             extra_scans=[good, bad],
                                             consolidate_flag=True)
            except ValueError as exc:
                self.fail(f"ValueError escaped candidate isolation and"
                          f" aborted the whole command: {exc!r}")
            self._assert_isolated(envelope, good, bad)

        # shape C: the NULL repo reaches the fold inside the migration,
        # which no preflight precedes
        db = _prep(self.scratch / "c14-null-repo-v1" / "history.db")
        _null_repo_v1_store(db)
        with self.subTest(shape="null_repo_legacy_migration"):
            conn = connect(db)
            try:
                init_schema(conn)
            except sqlite3.DatabaseError:
                pass  # the fail-closed refusal (the fixed behavior)
            except AttributeError as exc:
                self.fail(f"AttributeError escaped the migration-time fold:"
                          f" {exc!r}")
            else:
                self.fail("init_schema advanced a divergent legacy store")
            finally:
                conn.close()
            probe = sqlite3.connect(str(db))
            try:
                version = probe.execute("PRAGMA user_version").fetchone()[0]
            finally:
                probe.close()
            self.assertEqual(version, 1,
                             "the refused store still advanced its schema"
                             " version")


class ColumnParityTests(_FabricatedValueCase):
    def test_missing_column_candidate_is_refused(self) -> None:
        """AC15: when a consolidation candidate's copied table lacks a
        column the current schema has, the candidate is refused as
        unreadable instead of being adopted with the store's defaults for
        the missing column — a truncated image. RED at base, GREEN after
        the fix: at base a candidate whose `comments` table lacks
        `author_association` is adopted and the store's comments carry
        NULL for it (probe-verified); after the fix the preflight's column
        parity check refuses the candidate (implementation-review round-3
        finding 1's sibling: the TEXT-column loop pins sync_state, this
        pins the copied tables)."""
        good, bad, store, empty_cwd = self._divergent_pair(
            "c15", lambda bad: _drop_comments_column(bad, "author_association"))
        envelope = None
        try:
            envelope = doctor_mod.doctor(empty_cwd, REPO, store,
                                         extra_scans=[good, bad],
                                         consolidate_flag=True)
        except Exception as exc:  # noqa: BLE001 - any escape is a failure
            self.fail(f"{type(exc).__name__} escaped candidate isolation and"
                      f" aborted the whole command: {exc!r}")
        entry = {os.path.normcase(str(row.get("path", ""))): row
                 for row in envelope.get("scanned") or []}.get(
                     os.path.normcase(str(bad)))
        self.assertTrue(
            entry is not None and entry.get("unreadable"),
            "a candidate missing a current column was adopted as a truncated"
            f" image: {entry}")
        self._assert_isolated(envelope, good, bad)
        # the fabricated default must not have reached the store either
        if store.exists():
            conn = sqlite3.connect(str(store))
            try:
                stored = conn.execute(
                    "SELECT author_association FROM comments WHERE repo = ?",
                    (REPO,)).fetchall()
            finally:
                conn.close()
            self.assertEqual(
                [tuple(r) for r in stored], [],
                "a candidate missing a current column was adopted as a"
                f" truncated image: {stored}")
