"""Issue #61: `snapshot pull`'s destination guards must refuse a destination
they cannot inspect, and keep replacing one that is merely not a store.

Base behaviour pinned here (RED at 0c3168d): `_check_destination_shape` and
`_local_watermark` both opened with `if not db_path.is_file(): return`, and both
answered every `sqlite3.Error` from their read legs with a bare `return`. So a
destination whose stat faulted read as an absent one -- skipping the multi-repo
refusal immediately before `_install`'s whole-file `os.replace` -- and a
destination that was busy or I/O-faulted read as "no local corpus" / "no other
repos", skipping the `local_newer` refusal too. The second shape fails open on
EVER interpreter, which is why a stat-only fix does not close the class.

Every oracle is modelled EXPLICITLY rather than inherited from the running
interpreter: the fault is injected at `Path.stat` for one path, and
`Path.is_file`/`Path.exists` are replaced by a 3.14 model (an OSError answers
False) or a 3.11-3.13 model (it propagates). On 3.14 `Path.is_file` delegates to
the C builtin `os.path.isfile`, which bypasses a patched `os.stat`, so a leg that
relied on the installed pathlib would change meaning between hosts.

The classifier compares the PRIMARY result code (`sqlite_errorcode & 0xFF`), never
the raw value. Python exposes SQLite's extended codes through that attribute, and
two real destination schemas prove exact comparison is wrong: a `sync_state` with
a `COLLATE` column reopened without that collation answers
`SQLITE_ERROR_MISSING_COLLSEQ` (257), and an FTS5 `sync_state` with a dropped
shadow table answers `SQLITE_CORRUPT_VTAB` (267). Both are replaced successfully
by the base `pull`. Masking with `& 0xFF` is what makes the classifier read 267
as primary 11 (`SQLITE_CORRUPT`); an exact comparison to 11 or to 1 would
misroute it, and which raw value a host reports is not something this suite
should depend on.
"""
from __future__ import annotations

import os
import sqlite3
import stat as stat_mod
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fixtures import REPO, FakeGitHubSource, issue  # noqa: E402

from zaxbygraph.db import connect, init_schema  # noqa: E402
from zaxbygraph.snapshot import (  # noqa: E402
    SnapshotError,
    _check_destination_shape,
    _local_watermark,
    _primary_code,
    publish,
    pull,
)
from zaxbygraph.sync import sync_repo  # noqa: E402

MANIFEST = {"repo": REPO, "watermark": "2027-06-01T00:00:00Z"}
FAULT = PermissionError(13, "Permission denied")


def _norm(p) -> str:
    return os.path.normcase(os.fspath(p))


_real_stat = Path.stat


def _make_stat_fault(path):
    """A `Path.stat` replacement that raises for exactly one path.

    A plain function, not a callable instance: `Path.stat = <instance>` is not a
    descriptor, so `self.stat()` would call the instance with no arguments."""
    target = _norm(path)

    def _faulting_stat(self_path, *a, **kw):
        if _norm(self_path) == target:
            raise FAULT
        return _real_stat(self_path, *a, **kw)

    return _faulting_stat


def _is_file_swallow(self, *a, **kw):
    """3.14's oracle: an OSError answers False, never raises."""
    try:
        return stat_mod.S_ISREG(self.stat().st_mode)
    except OSError:
        return False


def _is_file_reraise(self, *a, **kw):
    """3.11-3.13's oracle: an OSError outside the swallow set propagates."""
    return stat_mod.S_ISREG(self.stat().st_mode)


def _exists_swallow(self, *a, **kw):
    """3.14's `Path.exists`: an OSError answers False, never raises."""
    try:
        self.stat()
    except OSError:
        return False
    return True


def _exists_reraise(self, *a, **kw):
    """3.11-3.13's `Path.exists`: an OSError outside the swallow set propagates."""
    self.stat()
    return True


def _oracle_patches(leg: str, target: Path):
    """Every existence oracle the guarded path consults, modelled explicitly."""
    if leg == "control":
        return []
    swallow = leg == "3.14"
    return [
        mock.patch.object(Path, "stat", _make_stat_fault(target)),
        mock.patch.object(
            Path, "is_file", _is_file_swallow if swallow else _is_file_reraise),
        mock.patch.object(
            Path, "exists", _exists_swallow if swallow else _exists_reraise),
    ]


def _build_store(td: Path, name: str = "history.db") -> Path:
    db = td / name
    conn = connect(db)
    init_schema(conn)
    conn.row_factory = sqlite3.Row
    src = FakeGitHubSource()
    for n in range(201, 205):
        src.add_issue(issue(n, title=f"Report {n}", body=f"body {n}",
                            created_at="2027-06-01T00:00:00Z"))
    sync_repo(conn, src, REPO)
    conn.close()
    return db


def _build_two_repo_store(td: Path) -> Path:
    merged = td / "merged.db"
    conn = connect(merged)
    init_schema(conn)
    conn.row_factory = sqlite3.Row
    src = FakeGitHubSource()
    for n in range(601, 604):
        src.add_issue(issue(n, title=f"R{n}", body="b",
                            created_at="2027-06-01T00:00:00Z"))
    sync_repo(conn, src, REPO)
    src2 = FakeGitHubSource()
    src2.add_issue(issue(901, title="Other repo", body="x",
                         created_at="2027-06-01T00:00:00Z"))
    sync_repo(conn, src2, "acme/other")
    conn.close()
    return merged


def _published(td: Path) -> Path:
    db = _build_store(td / "src")
    bare = td / "src" / "snapshot.git"
    bare.mkdir()
    import subprocess
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid",
                "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z"})
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                   check=True, capture_output=True, env=env, timeout=180)
    publish(db, str(bare), publisher="t")
    return bare


class _QueryFaultConn:
    """A read-only connection whose FIRST execute raises.

    The fault can land on the `SELECT` rather than on the open, which is the
    same fail-open through the `except sqlite3.Error` legs. The stub carries a
    real `sqlite_errorcode` so the classifier's primary-code masking is
    exercised rather than bypassed."""

    def __init__(self, code: int, message: str):
        exc = sqlite3.OperationalError(message)
        exc.sqlite_errorcode = code
        self._exc = exc

    def execute(self, *a, **kw):
        raise self._exc

    def close(self):
        pass


class OracleModelTests(unittest.TestCase):
    """The models themselves: a leg that inherited the host's oracle would
    change meaning between 3.11-3.13 and 3.14."""

    def test_swallow_model_answers_false_on_a_fault(self):
        with mock.patch.object(Path, "stat", _make_stat_fault(Path("/nope"))):
            self.assertFalse(_is_file_swallow(Path("/nope")))
            self.assertFalse(_exists_swallow(Path("/nope")))

    def test_reraise_model_propagates_a_fault(self):
        with mock.patch.object(Path, "stat", _make_stat_fault(Path("/nope"))):
            with self.assertRaises(PermissionError):
                _is_file_reraise(Path("/nope"))
            with self.assertRaises(PermissionError):
                _exists_reraise(Path("/nope"))

    def test_models_agree_on_a_readable_path(self):
        p = Path(__file__).resolve()
        self.assertTrue(_is_file_swallow(p))
        self.assertTrue(_is_file_reraise(p))
        self.assertTrue(_exists_swallow(p))
        self.assertTrue(_exists_reraise(p))


class PrimaryCodeTests(unittest.TestCase):
    """`sqlite_errorcode & 0xFF`, never the raw extended value."""

    def test_extended_codes_reduce_to_their_primary(self):
        for raw, primary in ((257, 1), (267, 11), (266, 10), (5, 5), (26, 26)):
            exc = sqlite3.OperationalError("x")
            exc.sqlite_errorcode = raw
            self.assertEqual(_primary_code(exc), primary, f"raw {raw}")

    def test_missing_attribute_is_none_not_zero(self):
        self.assertIsNone(_primary_code(sqlite3.OperationalError("x")))


class StatFaultTests(unittest.TestCase):
    """AC1 / AC4: an uninspectable destination is refused, not read as absent."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="destguard_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.td, ignore_errors=True))

    def test_shape_refuses_on_both_oracle_models(self):
        dest = _build_two_repo_store(self.td)
        for leg in ("3.14", "3.11"):
            with self.subTest(leg=leg):
                with mock.patch.multiple(
                        Path,
                        stat=_make_stat_fault(dest),
                        is_file=_is_file_swallow if leg == "3.14" else _is_file_reraise,
                        exists=_exists_swallow if leg == "3.14" else _exists_reraise):
                    with self.assertRaises(SnapshotError) as ctx:
                        _check_destination_shape(dest, MANIFEST, False)
                self.assertEqual(ctx.exception.code, "dest_uninspectable")
                self.assertEqual(ctx.exception.exit_code, 1)
                self.assertIn(str(dest), ctx.exception.message)

    def test_watermark_refuses_rather_than_reading_none(self):
        dest = _build_store(self.td)
        for leg in ("3.14", "3.11"):
            with self.subTest(leg=leg):
                patches = _oracle_patches(leg, dest)
                for p in patches:
                    p.start()
                try:
                    with self.assertRaises(SnapshotError) as ctx:
                        _local_watermark(dest, REPO)
                finally:
                    for p in reversed(patches):
                        p.stop()
                self.assertEqual(ctx.exception.code, "dest_uninspectable")

    def test_end_to_end_pull_refuses_and_leaves_the_destination_byte_identical(self):
        bare = _published(self.td)
        dest = _build_two_repo_store(self.td)
        resolved = Path(dest).resolve()
        before = dest.read_bytes()
        patches = _oracle_patches("3.14", resolved)
        for p in patches:
            p.start()
        try:
            with self.assertRaises(SnapshotError) as ctx:
                pull(dest, str(bare), force=False)
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(ctx.exception.code, "dest_uninspectable")
        self.assertEqual(dest.read_bytes(), before)

    def test_force_does_not_override_the_inspection_refusal(self):
        bare = _published(self.td)
        dest = _build_two_repo_store(self.td)
        resolved = Path(dest).resolve()
        patches = _oracle_patches("3.14", resolved)
        for p in patches:
            p.start()
        try:
            with self.assertRaises(SnapshotError) as ctx:
                pull(dest, str(bare), force=True)
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(ctx.exception.code, "dest_uninspectable")


class SqliteLegTests(unittest.TestCase):
    """AC7: the `except sqlite3.Error` legs, which fail open on every
    interpreter and which a stat-only fix leaves untouched."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="destguard_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.td, ignore_errors=True))

    def _drive(self, code, message, fn):
        dest = _build_store(self.td)
        with mock.patch("zaxbygraph.snapshot._open_readonly",
                        lambda _p: _QueryFaultConn(code, message)):
            with self.assertRaises(SnapshotError) as ctx:
                fn(dest)
        self.assertEqual(ctx.exception.code, "dest_uninspectable")
        self.assertEqual(ctx.exception.exit_code, 1)

    def test_open_time_busy_is_refused(self):
        # The fault lands on the OPEN, not the SELECT: `_open_readonly` itself
        # raises, so the classifier never sees a connection. Distinct from the
        # query-time legs below, which reach a connection and fail on execute.
        dest = _build_store(self.td)

        def _faulting_open(_path):
            exc = sqlite3.OperationalError("unable to open database file")
            exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise exc

        with mock.patch("zaxbygraph.snapshot._open_readonly", _faulting_open):
            with self.assertRaises(SnapshotError) as ctx:
                _check_destination_shape(dest, MANIFEST, False)
        self.assertEqual(ctx.exception.code, "dest_uninspectable")
        self.assertEqual(ctx.exception.exit_code, 1)

    def test_query_time_busy_is_refused(self):
        self._drive(sqlite3.SQLITE_BUSY, "database is locked",
                    lambda d: _local_watermark(d, REPO))

    def test_query_time_ioerr_is_refused(self):
        self._drive(sqlite3.SQLITE_IOERR, "disk I/O error",
                    lambda d: _local_watermark(d, REPO))

    def test_extended_busy_is_refused_through_the_primary_code(self):
        # SQLITE_BUSY_SNAPSHOT (517) -> primary 5.
        self._drive(517, "database is locked",
                    lambda d: _check_destination_shape(d, MANIFEST, False))

    def test_a_real_exclusive_writer_is_refused(self):
        dest = _build_store(self.td)
        conn = sqlite3.connect(str(dest))
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            conn.close()
        writer = sqlite3.connect(str(dest), isolation_level=None, timeout=0)
        writer.execute("BEGIN EXCLUSIVE")
        self.addCleanup(writer.close)
        with self.assertRaises(SnapshotError) as ctx:
            _check_destination_shape(dest, MANIFEST, False)
        self.assertEqual(ctx.exception.code, "dest_uninspectable")

    def test_a_real_exclusive_writer_does_not_mutate_the_destination(self):
        bare = _published(self.td)
        dest = _build_store(self.td)
        conn = sqlite3.connect(str(dest))
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            conn.close()
        writer = sqlite3.connect(str(dest), isolation_level=None, timeout=0)
        writer.execute("BEGIN EXCLUSIVE")
        self.addCleanup(writer.close)
        before = dest.read_bytes()
        with self.assertRaises(SnapshotError) as ctx:
            pull(dest, str(bare), force=False)
        self.assertEqual(ctx.exception.code, "dest_uninspectable")
        self.assertEqual(dest.read_bytes(), before)


class NotAStoreTests(unittest.TestCase):
    """AC3: readable-but-unusable destinations are still REPLACED.

    Each layout is a real database carrying a `valuable_payload` table, driven
    through a real `pull`. The assertion that the payload is gone is the honest
    disclosure that this is whole-file replacement of data the tool does not
    own -- not proof that nothing valuable was there."""

    LAYOUTS = (
        ("no sync_state table", None),
        ("sync_state(other TEXT)", "CREATE TABLE sync_state(other TEXT)"),
        ("sync_state(repo TEXT)", "CREATE TABLE sync_state(repo TEXT)"),
        ("sync_state(issues_since TEXT)",
         "CREATE TABLE sync_state(issues_since TEXT)"),
        ("sync_state with an unregistered collation",
         "CREATE TABLE sync_state(repo TEXT COLLATE app_collation,"
         " issues_since TEXT)"),
        ("sync_state as a view over malformed JSON",
         "CREATE TABLE raw(k TEXT, v TEXT)"),
    )

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="destguard_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.td, ignore_errors=True))

    def _make(self, name, ddl):
        p = self.td / (name.replace(" ", "_").replace("(", "").replace(")", "")
                       .replace(",", "") + ".db")
        conn = sqlite3.connect(str(p))
        if "collation" in name:
            conn.create_collation(
                "app_collation",
                lambda a, b: -1 if a < b else (1 if a > b else 0))
        if ddl:
            conn.execute(ddl)
        if name == "sync_state as a view over malformed JSON":
            # A readable `sync_state` VIEW whose body evaluates json_extract on
            # a truncated document. The open succeeds and BOTH fixed literals
            # fail with SQLITE_ERROR 1 `malformed JSON` during DATA evaluation,
            # not schema resolution -- which is why the classifier preserves
            # the whole code rather than a message match.
            conn.execute("INSERT INTO raw VALUES ('a','{not-json')")
            conn.execute(
                "CREATE VIEW sync_state AS SELECT"
                " json_extract(v, '$.repo') AS repo,"
                " json_extract(v, '$.issues_since') AS issues_since FROM raw")
        conn.execute("CREATE TABLE valuable_payload(secret TEXT)")
        conn.execute("INSERT INTO valuable_payload VALUES ('do-not-lose')")
        conn.commit()
        conn.close()
        return p

    def test_every_readable_but_unusable_layout_is_replaced(self):
        bare = _published(self.td)
        for name, ddl in self.LAYOUTS:
            with self.subTest(layout=name):
                dest = self._make(name, ddl)
                pull(dest, str(bare), force=False)
                check = sqlite3.connect(str(dest))
                try:
                    payload = check.execute(
                        "SELECT count(*) FROM sqlite_master WHERE type='table'"
                        " AND name='valuable_payload'").fetchone()[0]
                    (verdict,) = check.execute("PRAGMA quick_check").fetchone()
                finally:
                    check.close()
                self.assertEqual(payload, 0, f"{name}: payload survived")
                self.assertEqual(verdict, "ok", f"{name}: {verdict!r}")

    def test_a_future_watermark_in_a_schema_incompatible_store_is_not_read(self):
        # The `WHERE repo = ?` clause needs the missing column, so BOTH fixed
        # literals fail and no watermark is readable.
        dest = self._make("sync_state(issues_since TEXT)",
                          "CREATE TABLE sync_state(issues_since TEXT)")
        conn = sqlite3.connect(str(dest))
        conn.execute("INSERT INTO sync_state VALUES ('9999-12-31T23:59:59Z')")
        conn.commit()
        conn.close()
        self.assertIsNone(_local_watermark(dest, REPO))

    def test_an_fts5_destination_with_a_dropped_shadow_table_is_replaced(self):
        bare = _published(self.td)
        dest = self.td / "fts5_sync_state.db"
        conn = sqlite3.connect(str(dest))
        conn.execute("CREATE VIRTUAL TABLE sync_state USING fts5(repo, issues_since)")
        conn.execute("INSERT INTO sync_state VALUES ('acme/other','2020-01-01T00:00:00Z')")
        conn.execute("CREATE TABLE valuable_payload(secret TEXT)")
        conn.execute("INSERT INTO valuable_payload VALUES ('do-not-lose')")
        conn.commit()
        conn.close()
        dropper = sqlite3.connect(str(dest))
        dropper.execute("DROP TABLE sync_state_data")
        dropper.commit()
        dropper.close()
        observed = None
        chk = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        try:
            chk.execute("SELECT repo FROM sync_state").fetchall()
            observed = "read cleanly"
        except sqlite3.Error as exc:
            observed = _primary_code(exc)
        finally:
            chk.close()
        # SQLITE_CORRUPT_VTAB (267) -> primary 11 (SQLITE_CORRUPT) on this host.
        # Either raw value a host reports lands in the proceed set once the
        # primary code is taken, so the layout is replaced either way.
        self.assertIn(observed, (1, 11), f"unexpected primary code {observed}")
        pull(dest, str(bare), force=False)
        check = sqlite3.connect(str(dest))
        try:
            (verdict,) = check.execute("PRAGMA quick_check").fetchone()
            payload = check.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
                " AND name='valuable_payload'").fetchone()[0]
        finally:
            check.close()
        self.assertEqual(verdict, "ok")
        # Whole-file replacement, asserted rather than assumed: the payload the
        # tool does not own is gone.
        self.assertEqual(payload, 0, "valuable_payload survived the replace")

    def test_the_malformed_json_view_fails_both_literals_with_code_one(self):
        # The layout that disproved the "only schema errors, exactly three
        # shapes" proof: the open succeeds and the failure happens during data
        # evaluation, so the classifier's whole-code preservation is what keeps
        # this layout replaceable rather than refused.
        dest = self._make("sync_state as a view over malformed JSON",
                          "CREATE TABLE raw(k TEXT, v TEXT)")
        codes = []
        for q in ("SELECT repo FROM sync_state",
                  "SELECT issues_since FROM sync_state WHERE repo = ?"):
            chk = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
            try:
                chk.execute(q, () if "?" not in q else ("x",)).fetchall()
                codes.append("ok")
            except sqlite3.Error as exc:
                codes.append(_primary_code(exc))
            finally:
                chk.close()
        self.assertEqual(codes, [sqlite3.SQLITE_ERROR, sqlite3.SQLITE_ERROR],
                         f"unexpected codes {codes}")


class CorruptDestinationTests(unittest.TestCase):
    """AC3's other half: a genuinely CORRUPT destination is the documented
    recovery path, so the classifier must not blanket-refuse it."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="destguard_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.td, ignore_errors=True))

    def test_a_page_corrupted_destination_is_replaced(self):
        bare = _published(self.td)
        dest = _build_store(self.td)
        raw = bytearray(dest.read_bytes())
        raw[100:] = bytes(len(raw) - 100)      # header survives, pages do not
        dest.write_bytes(bytes(raw))
        code = None
        chk = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        try:
            chk.execute("SELECT repo FROM sync_state").fetchall()
        except sqlite3.Error as exc:
            code = _primary_code(exc)
        finally:
            chk.close()
        self.assertEqual(code, sqlite3.SQLITE_CORRUPT)
        pull(dest, str(bare), force=False)
        check = sqlite3.connect(str(dest))
        try:
            (verdict,) = check.execute("PRAGMA quick_check").fetchone()
        finally:
            check.close()
        self.assertEqual(verdict, "ok")


class ReadableDestinationTests(unittest.TestCase):
    """AC2: the refusals a readable destination already made stay green."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="destguard_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.td, ignore_errors=True))

    def test_multi_repo_destination_is_still_refused(self):
        bare = _published(self.td)
        dest = _build_two_repo_store(self.td)
        with self.assertRaises(SnapshotError) as ctx:
            pull(dest, str(bare), force=False)
        self.assertEqual(ctx.exception.code, "multi_repo_destination")
        self.assertEqual(ctx.exception.exit_code, 2)

    def test_a_newer_local_store_is_still_refused(self):
        bare = _published(self.td)
        dest = _build_store(self.td)
        conn = sqlite3.connect(str(dest))
        conn.execute("UPDATE sync_state SET issues_since='2099-01-01T00:00:00Z'"
                     " WHERE repo=?", (REPO,))
        conn.commit()
        conn.close()
        with self.assertRaises(SnapshotError) as ctx:
            pull(dest, str(bare), force=False)
        self.assertEqual(ctx.exception.code, "local_newer")
        self.assertEqual(ctx.exception.exit_code, 2)

    def test_a_directory_destination_is_not_given_a_new_refusal(self):
        # Legacy behaviour preserved, not made safe: `os.replace` refuses it and
        # `_install` maps that to dest_open.
        bare = _published(self.td)
        dest = self.td / "adir"
        dest.mkdir()
        with self.assertRaises(SnapshotError) as ctx:
            pull(dest, str(bare), force=False)
        self.assertEqual(ctx.exception.code, "dest_open")


if __name__ == "__main__":
    unittest.main()
