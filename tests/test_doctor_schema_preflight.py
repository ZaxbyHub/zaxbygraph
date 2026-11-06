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


def _divergent_v1_blob_timestamp(db: Path) -> None:
    """A genuine v1 store whose case-split group carries a BLOB
    `last_full_sync_at` on one casing — a divergent storage class in a TEXT
    column, reachable only through init_schema's migration fold."""
    conn = sqlite3.connect(str(db))
    conn.executescript(V1_SCHEMA_SQL)
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
        " include_patches) VALUES ('ACME/FORGEGATE', '2026-01-02T00:00:00Z',"
        " x'00', 0)")
    conn.execute(
        "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
        " include_patches) VALUES ('acme/forgegate', '2026-01-01T00:00:00Z',"
        " '2026-01-01T00:00:00Z', 0)")
    conn.execute(
        "INSERT INTO items(id, repo, number, kind, title, state, updated_at,"
        " raw_json) VALUES (1, 'acme/forgegate', 1, 'issue', 'one', 'open',"
        " '2026-01-01T00:00:00Z', '{}')")
    conn.commit()
    conn.close()


def _assert_legacy_fold_fails_closed(db: Path, label: str) -> None:
    """Drive init_schema over a divergent legacy store: the migration-time
    fold must refuse (sqlite3.DatabaseError) and the store must not
    advance — never raise an uncaught TypeError through the migration."""
    conn = connect(db)
    try:
        init_schema(conn)
    except sqlite3.DatabaseError:
        pass  # the fail-closed refusal (the fixed behavior)
    except TypeError as exc:
        self_fail = f"TypeError escaped the migration-time fold ({label}): {exc!r}"
        raise AssertionError(self_fail) from exc
    else:
        raise AssertionError(
            f"init_schema advanced a divergent legacy store ({label})")
    finally:
        conn.close()
    probe = sqlite3.connect(str(db))
    try:
        version = probe.execute("PRAGMA user_version").fetchone()[0]
    finally:
        probe.close()
    if version != 1:
        raise AssertionError(
            f"the refused store still advanced its schema version ({label}):"
            f" {version}")


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
                    # preflight precedes consolidate's body), so the
                    # structural assertion is that the store was never
                    # created (swarm-pr-review N-11: the previous
                    # `if store.exists():` guard wrapped a query that could
                    # never run).
                    self.assertFalse(
                        store.exists(),
                        "the refusal path created the store before refusing,"
                        " so 'no fabricated flag reached the store' is no"
                        " longer structural")


def _blob_timestamp_candidate(bad: Path) -> None:
    """Add the second casing of a case-split pair whose
    `last_full_sync_at` is a BLOB — a divergent storage class in a TEXT
    column, which makes the fold's max() compare bytes against str."""
    conn = sqlite3.connect(str(bad))
    shared = ("issues_since, last_full_sync_at, last_incr_sync_at,"
              " last_error, full_sync_pending, item_count,"
              " comment_count, edge_count")
    conn.execute(
        f"INSERT INTO sync_state(repo, {shared})"
        f" SELECT 'ACME/FORGEGATE', {shared} FROM sync_state"
        " WHERE repo = ?", (REPO,))
    conn.execute(
        "UPDATE sync_state SET last_full_sync_at = x'00'"
        " WHERE repo = 'ACME/FORGEGATE'")
    conn.commit()
    conn.close()


def _affinity_divergent_candidate(bad: Path) -> None:
    """Rebuild `sync_state` with `issues_since` carrying NO declared type
    (a schema that lost its affinity) and store an INTEGER watermark —
    which makes the scan's winner ranking compare int against str."""
    conn = sqlite3.connect(str(bad))
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sync_state'"
    ).fetchone()[0]
    lines = []
    for ln in ddl.splitlines():
        stripped = ln.strip()
        if stripped.startswith("issues_since"):
            ln = ln.replace("TEXT", "")  # drop the declared affinity
        lines.append(ln)
    conn.execute("ALTER TABLE sync_state RENAME TO sync_state_old")
    conn.execute("\n".join(lines))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sync_state_old)")]
    collist = ", ".join(cols)
    conn.execute(
        f"INSERT INTO sync_state ({collist}) SELECT {collist} FROM sync_state_old")
    conn.execute("DROP TABLE sync_state_old")
    conn.execute("UPDATE sync_state SET issues_since = 12345")
    conn.commit()
    conn.close()


class AffinityDivergenceTests(_SchemaPreflightCase):
    def test_divergent_storage_class_isolates_candidate(self) -> None:
        """AC13: when a candidate's `sync_state` TEXT column holds a
        divergent storage class — a BLOB timestamp in one casing, an
        INTEGER watermark in a column that lost its TEXT affinity, or a
        BLOB timestamp on a SINGLE-cased winner — the system does NOT
        abort the scan and does NOT adopt the fabricated value: the
        candidate the adoption reaches is refused as unreadable, and the
        remaining candidates are still scanned. RED at base, GREEN after
        the fix: at base the multi-cased BLOB raises an uncaught TypeError
        inside the fold's max(), the INTEGER watermark raises an uncaught
        TypeError at the scan's winner ranking — before consolidate is
        even called — and the single-cased BLOB winner skips the fold's
        fast path and is silently ADOPTED with the BLOB written into the
        store's TEXT column (implementation-review round-2 finding 1 and
        round-3 finding 1: the preflight's TEXT-column loop is the only
        layer that reaches that shape, so it needs its own leg)."""
        # shape A: the BLOB timestamp reaches the fold's aggregation
        good, bad, store, empty_cwd = self._divergent_pair(
            "c13-blob", _blob_timestamp_candidate)
        with self.subTest(shape="multi_cased_blob_timestamp"):
            envelope = None
            try:
                envelope = doctor_mod.doctor(empty_cwd, REPO, store,
                                             extra_scans=[good, bad],
                                             consolidate_flag=True)
            except TypeError as exc:
                self.fail(f"TypeError escaped candidate isolation and"
                          f" aborted the whole command: {exc!r}")
            self._assert_isolated(envelope, good, bad)

        # shape B: the INTEGER watermark reaches the winner ranking first;
        # the divergent candidate is reported and simply not adopted, and
        # the healthy sibling is still the winner
        good, bad, store, empty_cwd = self._divergent_pair(
            "c13-affinity", _affinity_divergent_candidate)
        with self.subTest(shape="integer_watermark_non_winner"):
            envelope = None
            try:
                envelope = doctor_mod.doctor(empty_cwd, REPO, store,
                                             extra_scans=[good, bad],
                                             consolidate_flag=True)
            except TypeError as exc:
                self.fail(f"TypeError escaped candidate isolation and"
                          f" aborted the whole command: {exc!r}")
            rows = {os.path.normcase(str(row.get("path", ""))): row
                    for row in envelope.get("scanned") or []}
            self.assertIn(os.path.normcase(str(bad)), rows,
                          "the divergent candidate vanished from the report")
            self.assertIn(os.path.normcase(str(good)), rows,
                          "the remaining candidates were not scanned")
            self.assertTrue(envelope.get("ok"), envelope)
            self.assertEqual(
                os.path.normcase(str(envelope.get("adopted") or "")),
                os.path.normcase(str(good)),
                "the healthy sibling was not adopted")

        # shape C: the single-cased BLOB winner — the fold's fast path
        # skips it and the ranking never compares it, so only the
        # preflight's TEXT-column loop can refuse it
        bad = _prep(self.scratch / "c13-single-blob" / "history.db")
        _seed_store(bad, (7, 8), "2027-04-01T00:00:00Z")
        conn = sqlite3.connect(str(bad))
        conn.execute("UPDATE sync_state SET last_full_sync_at = x'00'"
                     " WHERE repo = ?", (REPO,))
        conn.commit()
        conn.close()
        store = _prep(self.scratch / "c13-single-blob-store" / "history.db")
        empty_cwd = _prep(self.scratch / "c13-single-blob" / "cwd")
        # A healthy sibling, so "the remaining candidates were still
        # scanned" is a real assertion rather than a self-comparison
        # (cubic review, PR #58).
        good = _prep(self.scratch / "c13-single-blob-good" / "history.db")
        _seed_store(good, (3, 4), "2026-12-01T00:00:00Z")
        with self.subTest(shape="single_cased_blob_winner"):
            envelope = None
            try:
                envelope = doctor_mod.doctor(empty_cwd, REPO, store,
                                             extra_scans=[bad, good],
                                             consolidate_flag=True)
            except TypeError as exc:
                self.fail(f"TypeError escaped candidate isolation and"
                          f" aborted the whole command: {exc!r}")
            self._assert_isolated(envelope, good, bad)
            # The refusal precedes any store write, so the store is never
            # created — the structural form of "the fabricated BLOB must not
            # have reached the store" (swarm-pr-review N-11: the previous
            # `if store.exists():` guard wrapped a query that could never
            # run).
            self.assertFalse(
                store.exists(),
                "the refusal path created the store before refusing, so 'no"
                " divergent storage class reached the store' is no longer"
                " structural")

    def test_unrelated_repo_corruption_does_not_refuse_candidate(self) -> None:
        """AC13 (swarm-pr-review N-09): the storage-class counts are scoped
        to the repo the adoption copies (`WHERE lower(repo) = ?`, exactly
        `_copy_table`'s predicate), so corruption in a repo the user is NOT
        consolidating does not refuse the candidate. RED at the round-6
        head: a table-wide count refused a multi-repo candidate for a BLOB
        `items.title` belonging to `other/repo`, while base adopted such
        candidates — a behavior narrowing the PR introduced. `actors` has no
        repo column and IS copied wholesale, so it stays whole-table."""
        bad = _prep(self.scratch / "c13-multi-repo" / "history.db")
        _seed_store(bad, (7, 8), "2027-04-01T00:00:00Z")
        conn = sqlite3.connect(str(bad))
        # a healthy row for the target repo, plus a BLOB title for a
        # DIFFERENT repo — a shape a multi-repo legacy DB legitimately has
        conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, state,"
            " updated_at, raw_json) VALUES (900, 'other/repo', 1, 'issue',"
            " x'00', 'open', '2027-04-01T00:00:00Z', '{}')")
        conn.commit()
        conn.close()
        good = _prep(self.scratch / "c13-multi-repo-good" / "history.db")
        _seed_store(good, (3, 4), "2026-12-01T00:00:00Z")
        store = _prep(self.scratch / "c13-multi-repo-store" / "history.db")
        empty_cwd = _prep(self.scratch / "c13-multi-repo" / "cwd")
        envelope = None
        try:
            envelope = doctor_mod.doctor(empty_cwd, REPO, store,
                                         extra_scans=[bad, good],
                                         consolidate_flag=True)
        except Exception as exc:  # noqa: BLE001 - any escape is a failure
            self.fail(f"{type(exc).__name__} escaped candidate isolation and"
                      f" aborted the whole command: {exc!r}")
        entry = {os.path.normcase(str(row.get("path", ""))): row
                 for row in envelope.get("scanned") or []}.get(
                     os.path.normcase(str(bad)))
        self.assertFalse(
            entry is not None and entry.get("unreadable"),
            f"corruption in an unrelated repo refused the candidate:"
            f" {entry}")
        # the candidate is the freshest complete one, so it is adopted and
        # the OTHER repo's rows are simply not copied
        self.assertTrue(envelope.get("adopted"), envelope)
        if store.exists():
            conn = sqlite3.connect(str(store))
            try:
                titles = conn.execute(
                    "SELECT typeof(title) FROM items WHERE repo = ?",
                    (REPO,)).fetchall()
                foreign = conn.execute(
                    "SELECT COUNT(*) FROM items WHERE repo = 'other/repo'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(
                [tuple(r) for r in titles],
                [("text",), ("text",)],
                f"the adopted rows are not all text: {titles}")
            self.assertEqual(foreign, 0,
                             "an unrelated repo's rows were copied into the"
                             f" store: {foreign}")


class DivergentLegacyFoldTests(_SchemaPreflightCase):
    def test_divergent_legacy_null_flag_fold_fails_closed(self) -> None:
        """AC12: when a legacy store's migration-time fold meets a
        fabricated value — a present NULL in a NOT NULL flag column, or a
        divergent storage class (BLOB) in a TEXT column — the system fails
        closed and refuses the store instead of raising an uncaught error
        through the migration. RED at base, GREEN after the fix: at base
        the fold runs inside migrate_v1_to_v2 — before any consolidation
        preflight can run — and max() over the NULL raises TypeError (plan
        critic round 3, finding 1), while the BLOB timestamp raises
        TypeError from the same aggregation's type-unsafe comparison
        (implementation-review round 2, finding 1); after the fix the fold
        raises sqlite3.DatabaseError and the store does not advance.
        Genuine v1 cannot reach either path (its DDL declares the flag NOT
        NULL and the timestamps TEXT), which is exactly why the
        divergent-legacy shapes need their own check."""
        null_db = _prep(self.scratch / "c12-null" / "history.db")
        _divergent_v1_null_flag(null_db)
        with self.subTest(shape="null_flag"):
            _assert_legacy_fold_fails_closed(null_db, "null flag")

        blob_db = _prep(self.scratch / "c12-blob" / "history.db")
        _divergent_v1_blob_timestamp(blob_db)
        with self.subTest(shape="blob_timestamp"):
            _assert_legacy_fold_fails_closed(blob_db, "blob timestamp")
