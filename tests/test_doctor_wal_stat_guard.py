"""Issue #59: a candidate whose recovery log cannot be inspected must be
reported unusable, never silently dropped from the copy.

Base behavior pinned here (RED at 2009c10): `_sidecars` answers an
uninspectable sidecar as an absent one -- `is_symlink()` then
`if not sidecar.exists(): continue`.  On 3.14 both delegate to
`os.path.islink`/`os.path.exists`, which swallow an OSError and answer
False, so a `-wal` whose stat faults is dropped from the copy sources,
`_faithful_copy` returns True, and `_MigratedCopy` adopts the main-only
image -- `_faithful_copy`'s own documented worst case, a truncated image
that REPLACES the store's richer corpus.

The interpreter split is real and was verified per-errno on this box across
the whole CI matrix. On 3.11-3.13 the three oracles are pathlib's own
implementation: they re-raise an OSError outside `_ignore_error`'s swallow set
(`ENOENT`, `ENOTDIR`, `EBADF`, `ELOOP`/`WSAELOOP`, plus Windows winerror
21/123/1921) and answer `False` for the rest. On 3.14 they delegate to
`os.path.islink`/`os.path.exists`/`os.path.isfile`, which swallow every
`OSError`. So the old shape failed open on 3.14 for every fault class, and on
3.11-3.13 for the swallowed non-absence classes -- not on 3.14 alone:

    3.11 / 3.12 / 3.13   EACCES / EIO / ENODEV / EMFILE   -> re-raised
                          ENOTDIR / EBADF / ELOOP / ENOENT -> answered False
    3.14                 every fault class                -> swallowed

so every oracle in this module is modeled EXPLICITLY rather than inherited
from the running interpreter: a "3.14" stub calls `self.stat()` /
`self.lstat()` and answers False on OSError, and a "3.11-3.13" stub calls
the same and lets the OSError propagate.  Neither depends on which pathlib
version is installed, so a check that is PRESERVING stays PRESERVING and a
check that is DISCRIMINATING stays DISCRIMINATING on every interpreter the
repo supports -- the same discipline `tests/test_snapshot.py`'s
`test_stale_wal_guard_fails_closed_on_an_uninspectable_sidecar` uses.

The fault itself is injected at `os.stat`/`os.lstat` where the fixed guard
calls them, and at `Path.stat` where the stability snapshot calls them, so
no check can pass for the wrong reason once the guard stops consulting
`Path.exists()`.
"""
from __future__ import annotations

import errno
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import zaxbygraph.doctor as doctor_mod  # noqa: E402


def _norm(p) -> str:
    """A path key that compares equal for the same file on every platform."""
    return os.path.normcase(os.fspath(p))


def _fake_mode(mode: int) -> os.stat_result:
    """A stat result carrying `mode`, for a shape this box cannot create.

    A file symlink needs `SeCreateSymbolicLinkPrivilege` on Windows, which
    the suite must not require, so the link's classification is supplied
    directly rather than materialized on disk."""
    return os.stat_result(
        (mode, 0, 0, 1, 0, 0, 0, 0.0, 0.0, 0.0))


def _swallowing_exists(self, *a, **kw):
    """3.14's `os.path.exists`: an OSError answers False, never raises.

    Modeled through `self.stat()` -- the call 3.11-3.13's own
    `Path.exists()` makes -- so the stub behaves identically whichever
    pathlib version is running instead of silently reverting to it."""
    try:
        self.stat()
    except OSError:
        return False
    return True


def _swallowing_islink(self, *a, **kw):
    """3.14's `os.path.islink`, same reasoning: `self.lstat()`, swallow."""
    try:
        return stat.S_ISLNK(self.lstat().st_mode)
    except OSError:
        return False


def _propagating_exists(self, *a, **kw):
    """3.11-3.13's `Path.exists()`: `self.stat()`, non-ignored OSError
    propagates."""
    self.stat()
    return True


def _propagating_islink(self, *a, **kw):
    """3.11-3.13's `Path.is_symlink()`: `self.lstat()`, same."""
    return stat.S_ISLNK(self.lstat().st_mode)


def _faulting(real, target: Path):
    """`stat`/`lstat` raising EACCES for exactly `target`."""
    def faulting(p, *a, **kw):
        if _norm(p) == _norm(target):
            raise PermissionError(
                errno.EACCES, "Permission denied (fixture)")
        return real(p, *a, **kw)
    return faulting


def _candidate_with_wal_only_row(scratch: Path) -> tuple[Path, Path]:
    """A candidate whose committed frames live only in `-wal`.

    Row A is checkpointed into the main file, row B is committed and left
    in the log; the pair is snapshotted while the writer is still open, so
    a clean close cannot checkpoint B away.  A main-only copy holds 1 row
    and a main+wal copy holds 2, which is what proves B is WAL-only.
    """
    live = scratch / "live.db"
    conn = sqlite3.connect(str(live))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t(a TEXT)")
        conn.execute("INSERT INTO t VALUES('A-checkpointed')")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("INSERT INTO t VALUES('B-wal-only')")
        conn.commit()  # stays in the -wal
        snap = scratch / "snap"
        snap.mkdir()
        db = snap / "cand.db"
        shutil.copyfile(live, db)
        shutil.copyfile(Path(str(live) + "-wal"), Path(str(db) + "-wal"))
    finally:
        conn.close()
    return db, Path(str(db) + "-wal")


class _UninspectableSidecarTest(unittest.TestCase):
    """A `-wal` (or `-journal`) whose stat raises any OSError."""

    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="walstat_"))
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.db, self.wal = _candidate_with_wal_only_row(self.scratch)
        self.copy = self.scratch / "copy.db"

    # -- the checks --------------------------------------------------------

    def test_uninspectable_sidecar_makes_the_candidate_unusable(self) -> None:
        """AC1, the 3.14-oracle leg: RED at base, GREEN after the fix.

        The 3.14 swallow is modeled explicitly on `Path.exists`/
        `Path.is_symlink`, and the fault is injected at `os.stat`/
        `os.lstat` -- the level the fixed guard calls.  At base the sidecar
        is dropped from the copy sources and the main-only image is adopted
        as faithful; after the fix the candidate is refused and nothing is
        copied."""
        real_lstat, real_stat = os.lstat, os.stat
        with mock.patch.object(
                os, "lstat", _faulting(real_lstat, self.wal)), \
             mock.patch.object(
                os, "stat", _faulting(real_stat, self.wal)), \
             mock.patch.object(Path, "exists", _swallowing_exists), \
             mock.patch.object(Path, "is_symlink", _swallowing_islink):
            sidecars = doctor_mod._sidecars(self.db)
            faithful = doctor_mod._faithful_copy(self.db, self.copy)
        self.assertIsNone(
            sidecars,
            "an uninspectable sidecar was dropped from the copy sources"
            " instead of making the candidate unusable")
        self.assertFalse(
            faithful,
            "a candidate whose recovery log could not be inspected was"
            " copied and reported faithful")
        self.assertFalse(
            self.copy.exists(),
            "the main file was copied without its recovery log")

    def test_uninspectable_sidecar_is_refused_under_the_raising_oracle(
            self) -> None:
        """AC4, the 3.11-3.13 leg: GREEN at both ends.

        With the raising oracle the guard's own `exists()`/`is_symlink()`
        propagate the fault, so the candidate is already refused at base.
        The fix must keep refusing -- by answering None rather than by
        propagating the OSError -- and must not turn this shape into
        anything other than a refused candidate."""
        real_lstat, real_stat = os.lstat, os.stat
        with mock.patch.object(
                os, "lstat", _faulting(real_lstat, self.wal)), \
             mock.patch.object(
                os, "stat", _faulting(real_stat, self.wal)), \
             mock.patch.object(Path, "exists", _propagating_exists), \
             mock.patch.object(Path, "is_symlink", _propagating_islink):
            faithful = doctor_mod._faithful_copy(self.db, self.copy)
        self.assertFalse(
            faithful,
            "the 3.11-3.13 re-raise leg stopped refusing the candidate")

    def test_uninspectable_main_file_in_the_stability_snapshot_is_refused(
            self) -> None:
        """AC3, the stat-stability migration: RED at base, GREEN after the
        fix.

        The before/after snapshots gate each source on `p.exists()` before
        `p.stat()`, so a faulted source is dropped from the comparison
        instead of failing the attempt.  The `-wal` here is healthy and
        stays in both snapshots; it is the MAIN file that the swallowing
        oracle drops from each, so the two dicts compare equal and a copy
        that never stat'd its own source is accepted.  The fault is
        injected at `Path.stat` rather than `os.stat` so `shutil.copy2`'s
        own `os.stat` still succeeds -- at base the copy genuinely
        completes, which is what makes the RED leg real rather than an
        artifact of a broken copy."""
        real_stat = Path.stat
        with mock.patch.object(Path, "stat", _faulting(real_stat, self.db)), \
             mock.patch.object(Path, "exists", _swallowing_exists):
            faithful = doctor_mod._faithful_copy(self.db, self.copy)
        self.assertFalse(
            faithful,
            "a source that could not be stat'd was dropped from the"
            " stability comparison instead of failing the attempt")
        self.assertFalse(
            self.copy.exists(),
            "the copy proceeded past a source whose stat raised")


    def test_late_sidecar_with_uninspectable_stat_is_refused(self) -> None:
        """AC5, the after-snapshot stat-stability filter: RED at base, GREEN
        after the fix.

        A sidecar a writer creates DURING the copy is absent from `before`
        and present in `after_sidecars`.  The after-snapshot gates each
        source on `p.exists()` before `p.stat()`, so a source whose stat
        faults is dropped from `after` while `before` never had it -- the two
        dicts then agree and a main-only image is accepted, reopening the
        issue #53 late-sidecar protection.  C3 pins the BEFORE filter; this
        is the AFTER one.

        The fault is keyed on `follow_symlinks`: `Path.exists()` follows, so
        it faults, while `Path.stat(follow_symlinks=False)` does not, so the
        stat-first guard still classifies the sidecar.  The 3.14 oracle is
        modeled on `Path.exists` exactly as in C1, so the leg is
        interpreter-independent rather than depending on which pathlib
        version happens to be running."""
        stashed = self.scratch / "stashed-wal"
        shutil.move(str(self.wal), stashed)  # the log appears mid-copy
        real_stat = Path.stat
        real_copy2 = doctor_mod.shutil.copy2
        state = {"created": False}

        def late_sidecar_copy2(src, dst, *a, **kw):
            result = real_copy2(src, dst, *a, **kw)
            if not state["created"] and _norm(src) == _norm(self.db):
                state["created"] = True
                shutil.move(str(stashed), self.wal)  # the writer commits
            return result

        def faulting_stat(p, *a, **kw):
            if _norm(p) != _norm(self.wal):
                return real_stat(p, *a, **kw)
            if kw.get("follow_symlinks", True):
                raise PermissionError(
                    errno.EACCES, "Permission denied (fixture)")
            return real_stat(p, *a, **kw)

        with mock.patch.object(Path, "stat", faulting_stat),                 mock.patch.object(Path, "exists", _swallowing_exists),                 mock.patch.object(
                    doctor_mod.shutil, "copy2", late_sidecar_copy2):
            faithful = doctor_mod._faithful_copy(self.db, self.copy)
        self.assertTrue(
            state["created"], "the late sidecar was never created")
        self.assertFalse(
            faithful,
            "a sidecar created during the copy whose stat could not be"
            " inspected was dropped from the stability comparison")


    def test_second_suffix_fault_is_refused(self) -> None:
        """The loop covers BOTH suffixes, and a healthy first sidecar must not
        mask an uninspectable second one. `_sidecars` walks `("-wal",
        "-journal")` and returns `None` on the first uninspectable sidecar it
        meets, so a faulted `-journal` beside a perfectly readable `-wal`
        still refuses the whole candidate -- and it must, because copying the
        pair without the rollback journal is the same truncated-image shape
        AC1 refuses.

        Pinned on the 3.14 oracle, like C1, so the leg is
        interpreter-independent."""
        # a rollback journal beside the healthy recovery log
        self.journal = Path(str(self.db) + "-journal")
        self.journal.write_bytes(b"not a real journal, only stat-ed")
        self.addCleanup(self.journal.unlink, missing_ok=True)
        real_lstat, real_stat = os.lstat, os.stat
        with mock.patch.object(
                os, "lstat", _faulting(real_lstat, self.journal)),              mock.patch.object(
                os, "stat", _faulting(real_stat, self.journal)),              mock.patch.object(Path, "exists", _swallowing_exists),              mock.patch.object(Path, "is_symlink", _swallowing_islink):
            sidecars = doctor_mod._sidecars(self.db)
            faithful = doctor_mod._faithful_copy(self.db, self.copy)
        self.assertIsNone(
            sidecars,
            "an uninspectable -journal was skipped instead of refusing the"
            " candidate")
        self.assertFalse(
            faithful,
            "a candidate whose -journal could not be inspected was copied")
        self.assertFalse(
            self.copy.exists(),
            "the main file was copied without its rollback journal")

        # Without the fault both sidecars enumerate, which proves the loop
        # really did reach the second suffix rather than stopping early.
        self.assertEqual(
            doctor_mod._sidecars(self.db),
            [self.wal, self.journal],
            "the loop stopped enumerating before the second suffix")


class _SidecarClassificationTest(unittest.TestCase):
    """The stat-first rewrite must keep every existing classification."""

    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="walcls_"))
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.db, self.wal = _candidate_with_wal_only_row(self.scratch)
        self.copy = self.scratch / "copy.db"

    def test_healthy_sidecar_is_still_copied_in_full(self) -> None:
        """The guard must not over-refuse: an inspectable `-wal` is still
        enumerated, still copied, and the accepted image still recovers the
        writer's whole corpus."""
        self.assertEqual(
            doctor_mod._sidecars(self.db), [self.wal],
            "an inspectable sidecar stopped being enumerated")
        self.assertTrue(doctor_mod._faithful_copy(self.db, self.copy))
        conn = sqlite3.connect(str(self.copy))
        try:
            rows = [r[0] for r in conn.execute("SELECT a FROM t ORDER BY rowid")]
        finally:
            conn.close()
        self.assertEqual(
            rows, ["A-checkpointed", "B-wal-only"],
            "the recovery log's frames were dropped from the copy")

    def test_absent_sidecar_is_still_absent_not_unusable(self) -> None:
        """Only `FileNotFoundError` means absent.  A candidate with no
        recovery log at all is a healthy candidate, not an unusable one --
        the stat-first rewrite must not turn "no sidecar" into a refusal."""
        self.wal.unlink()
        self.assertEqual(
            doctor_mod._sidecars(self.db), [],
            "an absent sidecar stopped being treated as absent")
        self.assertTrue(doctor_mod._faithful_copy(self.db, self.copy))

    def test_directory_sidecar_is_still_refused(self) -> None:
        """A non-regular sidecar is not a recovery log this copy can
        faithfully take (issue #53, review PRR-022).  A DIRECTORY named
        `cand.db-wal` is the portable shape -- `exists()` answers True for
        it on every platform without needing a symlink privilege.

        Pinned STRUCTURALLY, not by outcome: `shutil.copy2` must never be
        called.  Disabling the guard would still refuse the candidate (the
        copy of a directory raises IsADirectoryError, an OSError, and the
        retry refuses), so an outcome-only assertion cannot tell a refusal
        from a refusal-after-attempting-to-copy-a-directory."""
        real = self.wal.read_bytes()
        self.wal.unlink()
        self.wal.mkdir()
        try:
            with mock.patch.object(doctor_mod.shutil, "copy2") as spy:
                self.assertFalse(doctor_mod._faithful_copy(self.db, self.copy))
            self.assertEqual(
                spy.call_count, 0,
                "the guard must refuse before any copy is attempted")
        finally:
            self.wal.rmdir()
            self.wal.write_bytes(real)

    def test_symlinked_sidecar_is_still_refused(self) -> None:
        """A symlinked sidecar is not a recovery log either -- the module's
        own docstring says so, and the dangling-symlink ordering was a
        shipped cubic-PR-#54-round-1 fix sitting directly above the code
        this issue rewrites.  GREEN at both ends.

        No real symlink is created: a file symlink needs
        `SeCreateSymbolicLinkPrivilege` on Windows, which the suite must not
        require.  The link's classification is supplied as a stat result
        instead, and the guard is pinned to consult it with
        `follow_symlinks=False` -- classifying the LINK, not its target.
        The fake `stat()` answers S_IFLNK when asked for the link and a
        regular file when the link is followed, so the named mutant
        ("classify with a link-following stat()") fails this test by
        outcome rather than by instrumentation."""
        wal = self.wal
        real_islink, real_stat = Path.is_symlink, Path.stat

        def fake_islink(p, *a, **kw):
            if _norm(p) == _norm(wal):
                return True
            return real_islink(p, *a, **kw)

        def link_stat(p, *a, **kw):
            if _norm(p) != _norm(wal):
                return real_stat(p, *a, **kw)
            if kw.get("follow_symlinks", True):
                return _fake_mode(stat.S_IFREG)  # the link was followed
            return _fake_mode(stat.S_IFLNK)

        with mock.patch.object(Path, "is_symlink", fake_islink), \
                mock.patch.object(Path, "stat", link_stat), \
                mock.patch.object(doctor_mod.shutil, "copy2") as spy:
            self.assertIsNone(
                doctor_mod._sidecars(self.db),
                "a symlinked sidecar stopped being refused")
            self.assertFalse(
                doctor_mod._faithful_copy(self.db, self.copy),
                "a symlinked sidecar stopped refusing the whole candidate")
            self.assertEqual(
                spy.call_count, 0,
                "the guard must refuse before any copy is attempted")

    def test_dangling_symlink_sidecar_is_still_refused(self) -> None:
        """The ordering the shipped cubic-PR-#54-round-1 fix pinned: a
        symlinked sidecar must be classified BEFORE anything follows it,
        because `exists()` follows symlinks and would answer False for a
        dangling one -- silently skipping the log instead of refusing it.

        GREEN at both ends.  The sidecar here does not exist as a file at
        all, so an `exists()`-first ordering skips it and the candidate is
        adopted as a main-only image; a link-first ordering refuses it.  No
        symlink privilege is needed -- the link's classification is supplied
        as a stat result, exactly as in the test above."""
        wal = self.wal
        self.wal.unlink()  # the link now dangles: exists() would say False
        real_islink, real_stat = Path.is_symlink, Path.stat

        def fake_islink(p, *a, **kw):
            if _norm(p) == _norm(wal):
                return True
            return real_islink(p, *a, **kw)

        def link_stat(p, *a, **kw):
            if _norm(p) != _norm(wal):
                return real_stat(p, *a, **kw)
            if kw.get("follow_symlinks", True):
                # The link's TARGET does not exist, so a link-following stat
                # answers FileNotFoundError -- which is what makes this test
                # bite on 3.11 as well as 3.14: 3.11's own `Path.exists()`
                # calls `self.stat()`, gets ENOENT, and `_ignore_error` turns
                # it into False, so an exists-first ordering skips the
                # dangling link.
                raise FileNotFoundError(2, "No such file", str(p))
            return _fake_mode(stat.S_IFLNK)

        with mock.patch.object(Path, "is_symlink", fake_islink), \
                mock.patch.object(Path, "stat", link_stat), \
                mock.patch.object(doctor_mod.shutil, "copy2") as spy:
            self.assertIsNone(
                doctor_mod._sidecars(self.db),
                "a dangling symlinked sidecar was skipped instead of"
                " refused -- the exists()-before-is_symlink() ordering is"
                " back")
            self.assertFalse(doctor_mod._faithful_copy(self.db, self.copy))
            self.assertEqual(
                spy.call_count, 0,
                "the guard must refuse before any copy is attempted")


if __name__ == "__main__":
    unittest.main()
