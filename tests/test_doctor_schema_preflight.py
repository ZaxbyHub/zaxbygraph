"""Issue #56 frozen acceptance checks C10-C12: the consolidation preflight's
COLUMN-level and VALUE-level divergence shapes and the migration-time fold
backstop, surfaced by the plan critic's round-2 and round-3 probes. Authored
independently of any fix as a frozen spec: every test is RED at base
80ea01c6ae202ea03373cc26ad9ff8cc561fe39a for the reason its docstring
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


def _seed_store(db: Path, numbers: tuple[int, ...], stamp: str) -> None:
    conn = connect(db)
    init_schema(conn)
    for n in numbers:
        ingest_item(conn, REPO, _item(n), pull_raw=None, issue_comments=[],
                    review_comments=[], reviews=[], files=[],
                    include_patches=False)
    conn.execute(
        "UPDATE sync_state SET last_full_sync_at = ?, full_sync_pending = 0,"
        " issues_since = ? WHERE repo = ?", (stamp, stamp, REPO))
    conn.commit()
    conn.close()


def _drop_actors_column(db: Path, column: str) -> None:
    """Rebuild `actors` without `column` (ALTER TABLE DROP COLUMN is avoided:
    the CREATE text carries comments)."""
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE actors RENAME TO actors_old")
    keep = [r[1] for r in conn.execute("PRAGMA table_info(actors_old)")
            if r[1] != column]
    defs = ["login TEXT PRIMARY KEY" if c == "login" else f"{c} TEXT"
            for c in keep]
    conn.execute(f"CREATE TABLE actors ({', '.join(defs)})")
    collist = ", ".join(keep)
    conn.execute(
        f"INSERT INTO actors ({collist}) SELECT {collist} FROM actors_old")
    conn.execute("DROP TABLE actors_old")
    conn.commit()
    conn.close()


def _make_flag_nullable_and_null(db: Path, multi: bool = True,
                                 text: bool = False) -> None:
    """Rebuild `sync_state` with `include_patches` holding a fabricated
    value no genuine store can produce — NULL, or (with text=True) a TEXT
    value in the INTEGER column. With multi=True a second casing is added
    so the fold's multi-row path runs; with multi=False the single
    already-lowercase row skips the fold's body and reaches the adoption
    INSERT instead."""
    conn = sqlite3.connect(str(db))
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sync_state'"
    ).fetchone()[0]
    lines = ddl.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().startswith("include_patches"):
            lines[i] = ln.replace("NOT NULL DEFAULT 0", "")
    conn.execute("ALTER TABLE sync_state RENAME TO sync_state_old")
    conn.execute("\n".join(lines))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sync_state_old)")]
    collist = ", ".join(cols)
    conn.execute(
        f"INSERT INTO sync_state ({collist}) SELECT {collist} FROM sync_state_old")
    conn.execute("DROP TABLE sync_state_old")
    if multi:
        shared = ("issues_since, last_full_sync_at, last_incr_sync_at,"
                  " last_error, full_sync_pending, item_count,"
                  " comment_count, edge_count")
        conn.execute(
            f"INSERT INTO sync_state(repo, {shared})"
            f" SELECT 'ACME/FORGEGATE', {shared} FROM sync_state"
            " WHERE repo = ?", (REPO,))
    if text:
        # one casing keeps the integer, the other carries the TEXT value:
        # max() over the pair is a TypeError, and a single-cased TEXT value
        # would be written into the store's INTEGER column as-is
        conn.execute(
            "UPDATE sync_state SET include_patches = 'yes'"
            " WHERE repo = ?", ("ACME/FORGEGATE" if multi else REPO,))
    else:
        conn.execute("UPDATE sync_state SET include_patches = NULL")
    conn.commit()
    conn.close()


def _divergent_v1_null_flag(db: Path) -> None:
    """A genuine v1 store (the frozen V1_SCHEMA_SQL) whose sync_state is
    rebuilt with include_patches NULLABLE and holds NULL in BOTH casings:
    the migration-time fold's shape, reachable only through init_schema."""
    conn = sqlite3.connect(str(db))
    conn.executescript(V1_SCHEMA_SQL)
    conn.execute("PRAGMA user_version = 1")
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sync_state'"
    ).fetchone()[0]
    lines = ddl.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().startswith("include_patches"):
            lines[i] = ln.replace("NOT NULL DEFAULT 0", "")
    conn.execute("ALTER TABLE sync_state RENAME TO sync_state_old")
    conn.execute("\n".join(lines))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sync_state_old)")]
    collist = ", ".join(cols)
    conn.execute(
        f"INSERT INTO sync_state ({collist}) SELECT {collist} FROM sync_state_old")
    conn.execute("DROP TABLE sync_state_old")
    conn.execute(
        "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
        " include_patches) VALUES ('ACME/FORGEGATE', '2026-01-02T00:00:00Z',"
        " '2026-01-02T00:00:00Z', NULL)")
    conn.execute(
        "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
        " include_patches) VALUES ('acme/forgegate', '2026-01-01T00:00:00Z',"
        " '2026-01-01T00:00:00Z', NULL)")
    conn.execute(
        "INSERT INTO items(id, repo, number, kind, title, state, updated_at,"
        " raw_json) VALUES (1, 'acme/forgegate', 1, 'issue', 'one', 'open',"
        " '2026-01-01T00:00:00Z', '{}')")
    conn.commit()
    conn.close()


class _SchemaPreflightCase(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="zgt56-preflight-"))
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


class ActorsColumnPreflightTests(_SchemaPreflightCase):
    def test_missing_actors_column_isolates_candidate(self) -> None:
        """AC10: a consolidation candidate whose `actors` table lacks a
        column the wholesale copy reads is isolated as unreadable and the
        scan continues — for EACH column the copy names. RED at base, GREEN
        after the fix: at base the actors copy (doctor.py:609) names `login`
        and `html_url` outside both conversion regions, so a
        table-existence check alone lets the uncaught `no such column: X`
        abort the whole command (plan critic round 2, finding 1; round 3,
        finding 2 — one leg per named column, html_url first so the frozen
        base expectation is preserved)."""
        for column in ("html_url", "login"):
            with self.subTest(column=column):
                good, bad, store, empty_cwd = self._divergent_pair(
                    f"c10-{column}",
                    lambda bad, c=column: _drop_actors_column(bad, c))
                envelope = None
                try:
                    # the divergent candidate is the freshest complete one,
                    # so it is the winner and the adoption reaches the
                    # actors copy
                    envelope = doctor_mod.doctor(empty_cwd, REPO, store,
                                                 extra_scans=[good, bad],
                                                 consolidate_flag=True)
                except sqlite3.OperationalError as exc:
                    self.fail(f"OperationalError escaped candidate isolation"
                              f" and aborted the whole command: {exc!r}")
                self._assert_isolated(envelope, good, bad)


class NullFlagPreflightTests(_SchemaPreflightCase):
    def test_null_flag_column_isolates_candidate(self) -> None:
        """AC11: a candidate whose NOT NULL flag column holds a value the
        adoption cannot faithfully take is isolated as unreadable and the
        command continues — for BOTH fabricated values (NULL and a TEXT
        value in the INTEGER column) and BOTH shapes (the multi-cased fold
        path and the single-cased adoption-INSERT path). RED at base, GREEN
        after the fix: at base the multi-cased shapes raise an uncaught
        TypeError inside the fold's max() (plan critic round 2, finding 2),
        and the single-cased shapes escape the fold's fast path — the NULL
        aborts at the adoption INSERT's NOT NULL constraint (round 3,
        finding 2) and the TEXT is silently written into the store's
        INTEGER column as-is (implementation-review round 1, finding 1) —
        so neither a fold-only fix nor a preflight-only fix can pass this
        check."""
        for multi in (True, False):
            for text in (False, True):
                with self.subTest(multi_cased=multi, text_value=text):
                    good, bad, store, empty_cwd = self._divergent_pair(
                        f"c11-{'multi' if multi else 'single'}"
                        f"-{'text' if text else 'null'}",
                        lambda bad, m=multi, t=text: _make_flag_nullable_and_null(
                            bad, multi=m, text=t))
                    envelope = None
                    try:
                        envelope = doctor_mod.doctor(
                            empty_cwd, REPO, store, extra_scans=[good, bad],
                            consolidate_flag=True)
                    except (TypeError, sqlite3.IntegrityError) as exc:
                        self.fail(f"{type(exc).__name__} escaped candidate"
                                  f" isolation and aborted the whole"
                                  f" command: {exc!r}")
                    self._assert_isolated(envelope, good, bad)
                    # a silently adopted fabricated value is the same
                    # defect as an abort: the store must not hold it. A
                    # refused adoption never creates the store at all (the
                    # preflight precedes consolidate's body), so "no store"
                    # and "a store without the value" are both clean.
                    if store.exists():
                        conn = sqlite3.connect(str(store))
                        try:
                            stored = conn.execute(
                                "SELECT include_patches FROM sync_state"
                                " WHERE repo = ?", (REPO,)).fetchall()
                        except sqlite3.OperationalError:
                            stored = []
                        finally:
                            conn.close()
                        self.assertEqual(
                            [tuple(r) for r in stored], [],
                            "a fabricated flag value reached the store:"
                            f" {stored}")


class DivergentLegacyFoldTests(_SchemaPreflightCase):
    def test_divergent_legacy_null_flag_fold_fails_closed(self) -> None:
        """AC12: when a legacy store's migration-time fold meets a present
        NULL in a NOT NULL flag column, the system fails closed and refuses
        the store instead of raising an uncaught error through the
        migration. RED at base, GREEN after the fix: at base the fold runs
        inside migrate_v1_to_v2 — before any consolidation preflight can
        run — and max() over the NULL raises TypeError (plan critic round 3,
        finding 1); after the fix the fold raises sqlite3.DatabaseError and
        the store does not advance. Genuine v1 cannot reach this path (its
        DDL declares the column NOT NULL), which is exactly why the
        divergent-legacy shape needs its own check."""
        db = _prep(self.scratch / "c12" / "history.db")
        _divergent_v1_null_flag(db)
        conn = connect(db)
        try:
            init_schema(conn)
        except sqlite3.DatabaseError:
            pass  # the fail-closed refusal (the fixed behavior)
        except TypeError as exc:
            self.fail(f"TypeError escaped the migration-time fold: {exc!r}")
        else:
            self.fail("init_schema advanced a divergent legacy store")
        finally:
            conn.close()
        # the refusal must not have advanced the store's schema version
        probe = sqlite3.connect(str(db))
        try:
            version = probe.execute("PRAGMA user_version").fetchone()[0]
        finally:
            probe.close()
        self.assertEqual(version, 1,
                         "the refused store still advanced its schema"
                         " version")
