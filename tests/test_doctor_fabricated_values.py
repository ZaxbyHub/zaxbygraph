"""Issue #56 frozen acceptance checks C14-C18: fabricated values the
adoption reads — a NULL or non-text `sync_state.repo`, a non-integer
`revisions.number`, a copied table that lacks a current column, a
divergent storage class in an optional TEXT state column or in
`edges.evidence`, and a candidate newer than this build. Authored
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

#: The upper casing of `REPO`, for the case-split shapes.
UPPER = "ACME/FORGEGATE"


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


def _seed_comment(db: Path) -> None:
    """Add one issue comment to the candidate, so a store-side assertion
    about a comment column is not vacuous (cubic review, PR #58)."""
    conn = connect(db)
    conn.execute(
        "INSERT INTO comments(github_id, repo, number, kind, author, body,"
        " created_at, updated_at, html_url, raw_json)"
        " VALUES (?, ?, 5, 'issue_comment', 'bob', 'hello',"
        " '2027-06-02T00:00:00Z', '2027-06-02T00:00:00Z',"
        " 'https://github.com/bob', '{}')", (7100 + 5, REPO))
    conn.commit()
    conn.close()


def _text_revisions_number_candidate(bad: Path, literal: str) -> None:
    """Add a `revisions` row whose number is the given SQL literal — a
    REAL, a numeric BLOB, or a large REAL. SQLite's dynamic typing stores
    them despite the INTEGER declaration; int() accepts REAL and numeric
    BLOB (silently localizing the revision to a WRONG item number) and a
    large REAL overflows the destination binder."""
    conn = sqlite3.connect(str(bad))
    conn.execute(
        "INSERT INTO revisions(repo, number, entity, entity_id, kind,"
        " origin, content, valid_from, valid_to)"
        f" VALUES (?, {literal}, 'item_body', '5', 'body', 'edit', 'text',"
        " '2027-06-01T00:00:00Z', '2027-06-02T00:00:00Z')", (REPO,))
    conn.commit()
    conn.close()


def _optional_text_blob_candidate(bad: Path,
                                  column: str = "rate_limit_reset_at") -> None:
    """Set the optional TEXT state column `column` to a BLOB — the fold
    carries the winner's value and the adoption writes it into the store's
    TEXT column verbatim."""
    conn = sqlite3.connect(str(bad))
    conn.execute(f"UPDATE sync_state SET {column} = x'00'"
                 " WHERE repo = ?", (REPO,))
    conn.commit()
    conn.close()


def _null_revisions_number_candidate(bad: Path) -> None:
    """Rebuild the candidate's `revisions` table with a NULLable `number`
    and insert a NULL row. No zaxbygraph build can produce this (the DDL
    declares NOT NULL), but SQLite's dynamic typing stores whatever a
    divergent schema allows, and the adoption's per-item read calls int()
    on it — int(None) raises TypeError, which is not a
    sqlite3.DatabaseError and would abort the whole command
    (implementation-review round-5 finding 2: the NULL shape was the one
    storage class the round-3 guard's conversion could raise on that no
    frozen check exercised)."""
    conn = sqlite3.connect(str(bad))
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='revisions'"
    ).fetchone()[0]
    conn.execute("ALTER TABLE revisions RENAME TO revisions_old")
    conn.execute(ddl.replace("number INTEGER NOT NULL", "number INTEGER", 1))
    keep = [r[1] for r in conn.execute("PRAGMA table_info(revisions_old)")]
    collist = ", ".join(keep)
    conn.execute(
        f"INSERT INTO revisions ({collist}) SELECT {collist} FROM revisions_old")
    conn.execute("DROP TABLE revisions_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_revisions_item"
        " ON revisions(repo, number)")
    conn.execute(
        "INSERT INTO revisions(repo, number, entity, entity_id, kind, origin,"
        " content, valid_from, valid_to) VALUES (?, NULL, 'item_body', '5',"
        " 'body', 'edit', 'text', '2027-06-01T00:00:00Z',"
        " '2027-06-02T00:00:00Z')", (REPO,))
    conn.commit()
    conn.close()


def _null_not_null_text_candidate(bad: Path) -> None:
    """Rebuild the candidate's `revisions` table with a NULLable
    `valid_from`/`valid_to` and insert a NULL row. No zaxbygraph build can
    produce this (the DDL declares both NOT NULL), and the candidate's own
    divergent DDL is what lets it exist — so the preflight's derived
    storage-class rule must refuse it (swarm-pr-review N-01), because the
    per-item revisions write would otherwise raise an uncaught
    sqlite3.IntegrityError that the adoption's `except BaseException` does
    not convert, losing the whole scan."""
    conn = sqlite3.connect(str(bad))
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='revisions'"
    ).fetchone()[0]
    conn.execute("ALTER TABLE revisions RENAME TO revisions_old")
    conn.execute(ddl.replace("valid_from TEXT NOT NULL", "valid_from TEXT", 1))
    keep = [r[1] for r in conn.execute("PRAGMA table_info(revisions_old)")]
    collist = ", ".join(keep)
    conn.execute(
        f"INSERT INTO revisions ({collist}) SELECT {collist} FROM revisions_old")
    conn.execute("DROP TABLE revisions_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_revisions_item"
        " ON revisions(repo, number)")
    conn.execute(
        "INSERT INTO revisions(repo, number, entity, entity_id, kind, origin,"
        " content, valid_from, valid_to) VALUES (?, 5, 'item_body', '5',"
        " 'body', 'edit', 'text', NULL, '2027-06-02T00:00:00Z')", (REPO,))
    conn.commit()
    conn.close()


def _blob_evidence_candidate(bad: Path) -> None:
    """Make the candidate case-split and add two `rel='closes'` edge rows
    for the same pair and source — one per casing — with the upper
    casing's `evidence` a BLOB. The fold's closes-group merge reads the
    evidence as text."""
    conn = sqlite3.connect(str(bad))
    shared = ("issues_since, last_full_sync_at, last_incr_sync_at,"
              " last_error, full_sync_pending, item_count,"
              " comment_count, edge_count")
    conn.execute(
        f"INSERT INTO sync_state(repo, {shared})"
        f" SELECT '{UPPER}', {shared} FROM sync_state"
        " WHERE repo = ?", (REPO,))
    conn.execute(
        "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
        " confidence, evidence, source)"
        " VALUES ('acme/forgegate', 'item', '5', 'closes', 'item', '1',"
        " 'EXTRACTED', 'body #1', 'keyword')")
    conn.execute(
        "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
        " confidence, evidence, source)"
        f" VALUES ('{UPPER}', 'item', '5', 'closes', 'item', '1',"
        " 'EXTRACTED', x'00', 'keyword')")
    conn.commit()
    conn.close()


def _legacy_blob_evidence_candidate(bad: Path) -> None:
    """Stamp the candidate at schema v5 and give it two `closes` edge rows
    for the SAME pair under the two GitHub-reported streams — issue #19's
    shape, which production ingest really produces — with the timeline
    row's `evidence` a BLOB. The v5->v6 migration's closes-pair collapse
    merges the two rows' evidence INSIDE init_schema, so no preflight can
    precede it: at base the merge raises an uncaught TypeError that every
    handler misses and the whole command aborts (probe-verified). This is
    the shape the pre-migration raw storage-class check exists for — the
    post-migration preflight never gets a turn, because the migration
    aborts first."""
    conn = sqlite3.connect(str(bad))
    conn.execute("DROP TABLE edges")
    conn.execute(
        "CREATE TABLE edges ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " repo TEXT NOT NULL, src_type TEXT NOT NULL, src_id TEXT NOT NULL,"
        " rel TEXT NOT NULL, dst_type TEXT NOT NULL, dst_id TEXT NOT NULL,"
        " confidence TEXT NOT NULL, evidence TEXT, source TEXT NOT NULL,"
        " UNIQUE (repo, src_type, src_id, rel, dst_type, dst_id, source))")
    for name, cols in (("idx_edges_src", "repo, src_type, src_id"),
                       ("idx_edges_dst", "repo, dst_type, dst_id"),
                       ("idx_edges_rel", "repo, rel")):
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON edges({cols})")
    conn.execute(
        "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
        " confidence, evidence, source)"
        " VALUES (?, 'item', '5', 'closes', 'item', '1', 'EXTRACTED',"
        " 'body #1', 'closing_ref')", (REPO,))
    conn.execute(
        "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
        " confidence, evidence, source)"
        " VALUES (?, 'item', '5', 'closes', 'item', '1', 'EXTRACTED',"
        " x'00', 'timeline')", (REPO,))
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()


def _newer_candidate(bad: Path) -> None:
    """Stamp the candidate's schema version above the current build's —
    a database written by a FUTURE zaxbygraph, whose extra facts this
    build cannot know."""
    from zaxbygraph.db import CURRENT_USER_VERSION

    conn = sqlite3.connect(str(bad))
    conn.execute(f"PRAGMA user_version = {CURRENT_USER_VERSION + 1}")
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
            "c14-text-number",
            lambda bad: _text_revisions_number_candidate(bad, "'abc'"))
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

        # shapes D-F: REAL / numeric BLOB / large REAL revisions.number —
        # int() accepts REAL and numeric BLOB (silently localizing the
        # revision to a WRONG item number), and a large REAL overflows the
        # destination binder (implementation-review round-4 finding 1)
        for label, literal in (("real_number", "3.5"),
                               ("blob_number", "x'3132'"),
                               ("large_real_number", "1e300")):
            with self.subTest(shape=label):
                good, bad, store, empty_cwd = self._divergent_pair(
                    f"c14-{label}",
                    lambda bad, v=literal: _text_revisions_number_candidate(bad, v))
                envelope = None
                try:
                    envelope = doctor_mod.doctor(empty_cwd, REPO, store,
                                                 extra_scans=[good, bad],
                                                 consolidate_flag=True)
                except (ValueError, TypeError, OverflowError) as exc:
                    self.fail(f"{type(exc).__name__} escaped candidate"
                              f" isolation and aborted the whole command:"
                              f" {exc!r}")
                self._assert_isolated(envelope, good, bad)

        # shape G: the NULL revisions.number — the one storage class the
        # round-3 per-item guard's int() conversion raises TypeError on
        # that no frozen check exercised until now (round-5 finding 2).
        # The preflight refuses it, so the conversion's TypeError is the
        # second layer rather than the only one.
        good, bad, store, empty_cwd = self._divergent_pair(
            "c14-null-number", _null_revisions_number_candidate)
        with self.subTest(shape="null_revisions_number"):
            envelope = None
            try:
                envelope = doctor_mod.doctor(empty_cwd, REPO, store,
                                             extra_scans=[good, bad],
                                             consolidate_flag=True)
            except TypeError as exc:
                self.fail(f"TypeError escaped candidate isolation and"
                          f" aborted the whole command: {exc!r}")
            self._assert_isolated(envelope, good, bad)

        # shape H: a NULL in a NOT NULL TEXT column the candidate's own
        # divergent DDL declares nullable (swarm-pr-review N-01). The
        # preflight's derived rule refuses it up front — the value would
        # otherwise reach the per-item revisions write and raise an
        # uncaught sqlite3.IntegrityError, which the adoption's only
        # handler (`except BaseException: rollback; raise`) does not
        # convert, so the whole scan (healthy sibling included) would be
        # lost. RED at the round-6 head: the probe aborted the command
        # with `IntegrityError: NOT NULL constraint failed:
        # revisions.valid_from`.
        good, bad, store, empty_cwd = self._divergent_pair(
            "c14-null-not-null-text", _null_not_null_text_candidate)
        with self.subTest(shape="null_in_not_null_text_column"):
            envelope = None
            try:
                envelope = doctor_mod.doctor(empty_cwd, REPO, store,
                                             extra_scans=[good, bad],
                                             consolidate_flag=True)
            except sqlite3.IntegrityError as exc:
                self.fail(f"IntegrityError escaped candidate isolation and"
                          f" aborted the whole command: {exc!r}")
            self._assert_isolated(envelope, good, bad)


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
            "c15", lambda bad: (_drop_comments_column(bad,
                                                      "author_association"),
                                _seed_comment(bad)))
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
        # the fabricated default must not have reached the store either. The
        # divergent candidate carries a comment, so a truncated adoption
        # would leave a NULL author_association row here rather than an
        # empty result (cubic review, PR #58: the assertion was vacuous
        # while the candidate held no comments).
        # The refusal precedes any store write, so the store is never
        # created — the structural form of "the fabricated default must not
        # have reached the store" (swarm-pr-review N-11: the previous
        # `if store.exists():` guard wrapped a query that could never run,
        # because the refusal is raised before `consolidate` opens the
        # destination; planting a failure inside it still passed).
        self.assertFalse(
            store.exists(),
            "the refusal path created the store before refusing, so 'no"
            " truncated image reached the store' is no longer structural")


class OptionalTextColumnTests(_FabricatedValueCase):
    def test_optional_text_blob_is_refused(self) -> None:
        """AC16: when a candidate's optional TEXT state column
        (`rate_limit_reset_at`, `fetch_source`) holds a divergent storage
        class, the candidate is refused as unreadable instead of being
        adopted with the BLOB written into the store's TEXT column. RED at
        base, GREEN after the fix: at base the fold carries the winner's
        value untouched and the adoption writes it (probe-verified: the
        store's column reads back `blob`), contradicting the storage-class
        contract the docs promise (implementation-review round-4 finding
        2)."""
        for column in ("rate_limit_reset_at", "fetch_source"):
            with self.subTest(column=column):
                good, bad, store, empty_cwd = self._divergent_pair(
                    f"c16-{column}",
                    lambda bad, c=column: _optional_text_blob_candidate(bad, c))
                envelope = None
                try:
                    envelope = doctor_mod.doctor(empty_cwd, REPO, store,
                                                 extra_scans=[good, bad],
                                                 consolidate_flag=True)
                except Exception as exc:  # noqa: BLE001 - any escape is a failure
                    self.fail(f"{type(exc).__name__} escaped candidate"
                              f" isolation and aborted the whole command:"
                              f" {exc!r}")
                entry = {os.path.normcase(str(row.get("path", ""))): row
                         for row in envelope.get("scanned") or []}.get(
                             os.path.normcase(str(bad)))
                self.assertTrue(
                    entry is not None and entry.get("unreadable"),
                    "a divergent storage class in an optional TEXT column"
                    f" was adopted: {entry}")
                self._assert_isolated(envelope, good, bad)


class EvidenceStorageClassTests(_FabricatedValueCase):
    def test_blob_evidence_is_refused(self) -> None:
        """AC17: when a candidate's `edges.evidence` holds a divergent
        storage class, the candidate is refused as unreadable instead of
        aborting the whole command. RED at base, GREEN after the fix.

        Shape 1 (a current-version candidate): at base the fold's
        closes-group merge reads the evidence as text and raises an
        uncaught TypeError inside merge_evidence (probe-verified;
        implementation-review round-4 finding 3).

        Shape 2 (a v5 candidate — `source` inside the edges unique key):
        at base the MIGRATION's v5->v6 closes-pair collapse merges the two
        streams' evidence and aborts the same way, before any preflight
        can run (probe-verified). This is the shape the pre-migration raw
        storage-class check exists for: the post-migration preflight never
        gets a turn, because the migration aborts first."""
        good, bad, store, empty_cwd = self._divergent_pair(
            "c17", _blob_evidence_candidate)
        with self.subTest(shape="blob_evidence"):
            envelope = None
            try:
                envelope = doctor_mod.doctor(empty_cwd, REPO, store,
                                             extra_scans=[good, bad],
                                             consolidate_flag=True)
            except TypeError as exc:
                self.fail(f"TypeError escaped candidate isolation and"
                          f" aborted the whole command: {exc!r}")
            self._assert_isolated(envelope, good, bad)

        good, bad, store, empty_cwd = self._divergent_pair(
            "c17-legacy", _legacy_blob_evidence_candidate)
        with self.subTest(shape="blob_evidence_legacy_migration"):
            envelope = None
            try:
                envelope = doctor_mod.doctor(empty_cwd, REPO, store,
                                             extra_scans=[good, bad],
                                             consolidate_flag=True)
            except TypeError as exc:
                self.fail(f"TypeError escaped candidate isolation and"
                          f" aborted the whole command: {exc!r}")
            self._assert_isolated(envelope, good, bad)


class NewerCandidateTests(_FabricatedValueCase):
    def test_newer_candidate_is_refused(self) -> None:
        """AC18: when a candidate's schema is NEWER than this build
        (`user_version` above the current), the candidate is refused as
        unreadable instead of being adopted. RED at base, GREEN after the
        fix: at base such a candidate is admitted and its extra facts are
        silently dropped (the sync_state INSERT names the store's columns;
        actors/revisions extras are ignored) — the asymmetry the issue's
        claim-2 prior-art reconciliation named, resolved the way
        `snapshot` refuses a newer store (implementation-review round-4
        disproved the narrower RECORD rationale)."""
        good, bad, store, empty_cwd = self._divergent_pair(
            "c18", _newer_candidate)
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
            f"a candidate newer than this build was adopted: {entry}")
        self._assert_isolated(envelope, good, bad)
