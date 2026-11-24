"""Issue #62: `export --out`'s alias check must refuse a `--out` it cannot
inspect, and keep its three existing outcomes.

Base behaviour pinned here (RED at 0c3168d): `_reject_database_alias` gated the
sole hard-link check behind `os.path.exists(out_path) and os.path.exists(cand)`.
`os.path.exists` is `genericpath.exists` -- `try: os.stat(path) except (OSError,
ValueError): return False` -- and it swallows UNCONDITIONALLY on 3.11 through
3.14 (3.11 via the pure-Python `genericpath.exists`, 3.12+ via the C builtin
`_path_exists`). So the `and` short-circuited to False for any per-file stat
fault, `os.path.samefile` was never reached, and the export proceeded.

The consequence, corrected against the guard's own stale docstring: an executed
probe at the anchor showed `write_corpus` writes a temp file and `os.replace`s
it, so a skipped hard-link guard does NOT truncate the store. What it does is
write the corpus to a path the guard forbids and silently break the hard link.
Both are asserted here against the real `write_corpus`, not just the guard.

The oracle is modelled, not inherited: `os.path.exists` is replaced by a faithful
re-implementation of that same body and the fault is injected at `os.stat`,
because on 3.12+ `os.path.exists` is a C builtin that bypasses a patched
`os.stat`.
"""
from __future__ import annotations

import os
import sqlite3
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
from zaxbygraph.export import (  # noqa: E402
    ExportError,
    _reject_database_alias,
    write_corpus,
)
from zaxbygraph.sync import sync_repo  # noqa: E402

FAULT = OSError(5, "Input/output error")


def _norm(p) -> str:
    return os.path.normcase(os.fspath(p))


_real_stat = os.stat


def _swallowing_exists(path, *a, **kw):
    """genericpath.exists verbatim: an OSError answers False, never raises."""
    try:
        os.stat(path)
    except (OSError, ValueError):
        return False
    return True


_real_realpath = os.path.realpath


def _make_realpath_fault(path):
    """A candidate whose realpath cannot be resolved.

    The candidate-side leg had no covering test: deleting its try/except left
    34 export tests green, while deleting the sibling stat leg failed two. This
    is that leg's pin."""
    target = _norm(path)

    def _faulting_realpath(p, *a, **kw):
        if _norm(p) == target:
            raise FAULT
        return _real_realpath(p, *a, **kw)

    return _faulting_realpath


def _make_stat_fault(path):
    target = _norm(path)

    def _faulting_stat(p, *a, **kw):
        if _norm(p) == target:
            raise FAULT
        return _real_stat(p, *a, **kw)

    return _faulting_stat


def _fault_patches(faulted: Path):
    return [
        mock.patch("os.stat", _make_stat_fault(faulted)),
        mock.patch("os.path.exists", _swallowing_exists),
    ]


def _build_store(td: Path) -> Path:
    db = td / "history.db"
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


class AliasGuardFaultTests(unittest.TestCase):
    """AC5: a hard-linked `--out` is still refused when either path faults."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="aliasguard_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.td, ignore_errors=True))

    def _linked(self):
        db = _build_store(self.td)
        out = self.td / "linked.jsonl"
        os.link(db, out)
        return db, out

    def test_a_provable_alias_outranks_the_uninspectable_refusal(self):
        """PRR-007: hoisting `os.stat(out_path)` ahead of the alias comparison
        made a provably-aliasing `--out` answer 1 "whether it aliases the
        database is unknown" -- on a path whose answer the guard had already
        computed from the realpaths. The base answered 2 "resolves to the
        database (or a sidecar)". Only out_path's own stat faults here, so the
        alias comparison must still win.

        This is an exit-CLASS regression, not a lost refusal: both answers
        stop the export, and the store survives either way."""
        db = _build_store(self.td)
        out = Path(str(db) + "-wal")
        out.write_bytes(b"x")
        with mock.patch("os.stat", _make_stat_fault(out)):
            with self.assertRaises(ExportError) as ctx:
                _reject_database_alias(str(out), str(db))
        self.assertEqual(ctx.exception.code, "out_is_database")
        self.assertEqual(ctx.exception.exit_code, 2)
        self.assertIn("resolves to the database", ctx.exception.message)

    def test_a_provable_hard_link_outranks_a_later_candidate_fault(self):
        """PRR-007 round 2: splitting the guard into a realpath pass and a
        samefile pass reintroduced the same defect for HARD LINKS. `--out` is a
        hard link of the store -- determinable by samefile -- and a LATER
        candidate's realpath faults. The two-pass version raised the deferred
        fault before the samefile could prove the alias, answering exit 1
        "whether --out aliases it is unknown" on a path whose answer it had.

        The guard now proves every determinable alias before raising any
        inspection fault, so both orderings hold: realpath equality wins, and
        so does a hard link proven after a sibling candidate faulted."""
        db = _build_store(self.td)
        out = self.td / "linked.jsonl"
        if out.exists():
            out.unlink()
        os.link(db, out)
        with mock.patch("os.path.realpath",
                        _make_realpath_fault(str(db) + "-wal")):
            with self.assertRaises(ExportError) as ctx:
                _reject_database_alias(str(out), str(db))
        self.assertEqual(ctx.exception.code, "out_is_database")
        self.assertEqual(ctx.exception.exit_code, 2)
        self.assertIn("hard link", ctx.exception.message)

    def test_out_faulted_is_refused(self):
        db, out = self._linked()
        patches = _fault_patches(out)
        for p in patches:
            p.start()
        try:
            with self.assertRaises(ExportError) as ctx:
                _reject_database_alias(str(out), str(db))
        finally:
            # Stop the SAME patcher objects that were started: rebuilding the
            # list leaves the first pair running for the rest of the process.
            for p in reversed(patches):
                p.stop()
        self.assertEqual(ctx.exception.code, "out_is_database")
        self.assertEqual(ctx.exception.exit_code, 1)

    def test_store_faulted_is_refused(self):
        db, out = self._linked()
        patches = _fault_patches(db)
        for p in patches:
            p.start()
        try:
            with self.assertRaises(ExportError) as ctx:
                _reject_database_alias(str(out), str(db))
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(ctx.exception.code, "out_is_database")
        self.assertEqual(ctx.exception.exit_code, 1)

    def test_unfaulted_hard_link_is_refused_with_exit_two(self):
        # The knowable alias keeps the default exit code, which is what makes
        # the fault-driven exit 1 discriminable rather than decorative.
        db, out = self._linked()
        with self.assertRaises(ExportError) as ctx:
            _reject_database_alias(str(out), str(db))
        self.assertEqual(ctx.exception.code, "out_is_database")
        self.assertEqual(ctx.exception.exit_code, 2)
        self.assertIn("hard link", ctx.exception.message)

    def test_an_ordinary_distinct_out_is_allowed(self):
        db = _build_store(self.td)
        out = self.td / "corpus.jsonl"
        out.write_bytes(b"sentinel")
        _reject_database_alias(str(out), str(db))     # must not raise
        self.assertEqual(out.read_bytes(), b"sentinel")

    def test_an_absent_out_is_allowed(self):
        db = _build_store(self.td)
        out = self.td / "does-not-exist.jsonl"
        _reject_database_alias(str(out), str(db))     # nothing to alias

    def test_samefile_own_oserror_is_refused(self):
        db, out = self._linked()
        real_samefile = os.path.samefile

        def _faulting_samefile(a, b, *args, **kw):
            if _norm(a) == _norm(db) or _norm(b) == _norm(db):
                raise OSError(5, "Input/output error")
            return real_samefile(a, b, *args, **kw)

        with mock.patch("os.path.samefile", _faulting_samefile):
            with self.assertRaises(ExportError) as ctx:
                _reject_database_alias(str(out), str(db))
        self.assertEqual(ctx.exception.code, "out_is_database")
        self.assertEqual(ctx.exception.exit_code, 1)

    def test_a_null_byte_in_the_path_is_a_coded_refusal_not_a_traceback(self):
        # os.stat raises ValueError, not OSError, for a path carrying an
        # embedded null character. The guard's contract is a coded refusal, so
        # all three handlers catch ValueError alongside OSError.
        db = _build_store(self.td)
        for bad in ("a\x00b",
                    str(self.td / "x") + "\x00" + "y"):
            with self.subTest(path=bad):
                with self.assertRaises(ExportError) as ctx:
                    _reject_database_alias(str(bad), str(db))
                self.assertEqual(ctx.exception.code, "out_is_database")
                self.assertEqual(ctx.exception.exit_code, 1)

    def test_an_uninspectable_candidate_answers_the_coded_refusal(self):
        # The candidate-side realpath leg. `--out` is a perfectly ordinary
        # path here; what cannot be inspected is the candidate, so the answer
        # is the same coded refusal with exit 1, not a traceback and not a
        # silent pass.
        db = _build_store(self.td)
        out = self.td / "corpus.jsonl"
        out.write_bytes(b"sentinel")
        with mock.patch("os.path.realpath",
                        _make_realpath_fault(str(db) + "-wal")):
            with self.assertRaises(ExportError) as ctx:
                _reject_database_alias(str(out), str(db))
        self.assertEqual(ctx.exception.code, "out_is_database")
        self.assertEqual(ctx.exception.exit_code, 1)
        self.assertIn("could not be inspected", ctx.exception.message)

    def test_samefile_file_not_found_is_not_an_alias(self):
        # A path that vanished mid-check is not evidence of an alias.
        db = _build_store(self.td)
        out = self.td / "corpus.jsonl"
        out.write_bytes(b"sentinel")

        def _vanishing_samefile(a, b, *args, **kw):
            raise FileNotFoundError(2, "No such file or directory")

        with mock.patch("os.path.samefile", _vanishing_samefile):
            _reject_database_alias(str(out), str(db))   # must not raise


class EndToEndWriteTests(unittest.TestCase):
    """AC5's second clause, against the real `write_corpus` spool + replace."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="aliasguard_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.td, ignore_errors=True))

    def _run(self, which: str):
        db = _build_store(self.td)
        out = self.td / "linked.jsonl"
        os.link(db, out)
        store_before = db.read_bytes()
        out_before = out.read_bytes()
        patches = _fault_patches(out if which == "out" else db)
        for p in patches:
            p.start()
        conn = None
        try:
            conn = connect(db)
            write_corpus(conn, REPO, None, str(out), frozenset({"items"}),
                         True, str(db))
            raised = None
        except ExportError as exc:
            raised = exc
        finally:
            # Closed on BOTH paths: the refusal legs are the ones that used to
            # leak it, and a leaked handle keeps the temp dir locked on Windows.
            if conn is not None:
                conn.close()
            for p in reversed(patches):
                p.stop()
        return db, out, store_before, out_before, raised

    def test_no_corpus_is_written_when_out_is_faulted(self):
        db, out, store_before, out_before, raised = self._run("out")
        self.assertIsNotNone(raised)
        self.assertEqual(raised.code, "out_is_database")
        self.assertEqual(out.read_bytes(), out_before,
                         "the corpus was written to an aliased --out")
        self.assertEqual(db.read_bytes(), store_before)

    def test_no_corpus_is_written_when_the_store_is_faulted(self):
        db, out, store_before, out_before, raised = self._run("store")
        self.assertIsNotNone(raised)
        self.assertEqual(raised.code, "out_is_database")
        self.assertEqual(out.read_bytes(), out_before)
        self.assertEqual(db.read_bytes(), store_before)

    def test_the_store_survives_a_bypassed_guard(self):
        # The corrected consequence: `write_corpus` spools to a temp file and
        # `os.replace`s it, so a skipped guard breaks the hard link and writes
        # the corpus, but the store's bytes survive. Pinned so the guard's
        # justification cannot drift back to a truncation claim.
        db = _build_store(self.td)
        out = self.td / "linked.jsonl"
        os.link(db, out)
        store_before = db.read_bytes()
        self.assertTrue(os.path.samefile(out, db))
        with mock.patch("zaxbygraph.export._reject_database_alias",
                        lambda *a, **k: None):
            conn = connect(db)
            write_corpus(conn, REPO, None, str(out), frozenset({"items"}),
                         True, str(db))
            conn.close()
        self.assertEqual(db.read_bytes(), store_before)
        self.assertNotEqual(out.read_bytes(), store_before)
        self.assertFalse(os.path.samefile(out, db))


# ==== #90 AC2: DOS device names are refused by a portable predicate ========
#
# The guard matched only the `\\.\` and `/dev/` prefixes, so a WINDOWS device
# name in its ordinary spelling (CON, PRN, AUX, COM1-9, LPT1-9, case variants,
# extension forms such as CON.txt and CON.tar.gz, and absolute paths) passed
# the guard, spooled the corpus, and died late at os.replace with a generic
# runtime error instead of the documented out_is_database refusal.
#
# The refusal is gated on the platform (a POSIX file may legitimately be named
# CON), so this check patches os.name to make each branch reachable on every CI
# leg. Every path is built BEFORE the patch: pathlib consults os.name when a
# Path is instantiated, so a Path built under a patched value would raise
# NotImplementedError on the other platform.
#
# RED at e6c8f41: the device names pass the guard. GREEN after the fix.

def test_dos_device_names_refused() -> None:
    names = (
        "CON", "con", "CON.txt", "CON.tar.gz", "PRN", "AUX", "NUL",
        "COM1", "COM9", "LPT1", "LPT9", "CON ", "CON.",
    )
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "history.db")
        Path(db_path).write_bytes(b"store bytes")
        # Absolute and mixed-separator spellings, built before any patch.
        absolute_nul = td.rstrip("/\\") + os.sep + "NUL"
        mixed_nul = td.replace("\\", "/").rstrip("/") + "/NUL"
        with mock.patch.object(os, "name", "nt"):
            for name in names + (absolute_nul, mixed_nul):
                try:
                    _reject_database_alias(name, db_path)
                except ExportError as exc:
                    assert exc.code == "out_is_database", (
                        f"--out {name!r} must be refused with the documented"
                        f" out_is_database code; got {exc.code!r}"
                    )
                else:
                    raise AssertionError(
                        f"--out {name!r} names a DOS device path and must be"
                        " refused before the corpus is spooled"
                    )
        with mock.patch.object(os, "name", "posix"), mock.patch.object(
            os.path, "realpath", lambda path: str(path)
        ):
            # POSIX does not reserve these names: a file legitimately called
            # CON must not be refused. realpath is stubbed to an identity
            # because the HOST'S ntpath may resolve CON to the \\\\.\\CON
            # device path (observed on GitHub's windows-latest runner on
            # 3.13/3.14), which the UNCONDITIONAL device-prefix arm correctly
            # refuses regardless of os.name; a faithful POSIX simulation must
            # not inherit that host-specific resolution.
            _reject_database_alias("CON", db_path)
            _reject_database_alias(absolute_nul, db_path)


if __name__ == "__main__":
    unittest.main()
