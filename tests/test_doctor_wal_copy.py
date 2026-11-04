"""Issue #52: doctor must read crash-damaged (un-checkpointed WAL) candidates
faithfully — inspect reports the true corpus, consolidation adopts it in
full, and the candidate's own directory is never touched.

Base behavior pinned here (RED at f28e4b4): doctor.py copies candidates with
shutil.copy2 (main file only), so a killed writer's committed -wal frames
vanish — the candidate is reported absent/empty while doctor claims ok, and
--consolidate replaces the store's richer corpus with the truncated copy.
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from zaxbygraph.db import connect, init_schema  # noqa: E402
from zaxbygraph.store import ingest_item  # noqa: E402

REPO = "probe/clinic"

WRITER = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, {src!r})
from zaxbygraph.db import connect, init_schema
from zaxbygraph.store import ingest_item

db, lo, hi, mode, repo, stamp = (
    Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]),
    sys.argv[4], sys.argv[5], sys.argv[6],
)
conn = connect(db)
init_schema(conn)

def raw(n):
    return {{
        "id": 5000 + n, "number": n, "node_id": "I_%d" % n,
        "title": "t%d" % n, "body": "", "state": "open",
        "user": {{"login": "alice", "html_url": "https://github.com/alice"}},
        "labels": [], "comments": 0,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:01:00Z",
        "closed_at": None, "locked": False,
        "html_url": "https://github.com/probe/clinic/issues/%d" % n,
        "url": "https://api.github.com/repos/probe/clinic/issues/%d" % n,
    }}

conn.execute("BEGIN IMMEDIATE")
for n in range(lo, hi + 1):
    ingest_item(
        conn, repo, raw(n), pull_raw=None, issue_comments=[],
        review_comments=[], reviews=[], files=[], include_patches=False,
    )
conn.execute(
    "UPDATE sync_state SET last_full_sync_at = ?, full_sync_pending = 0,"
    " issues_since = ? WHERE repo = ?",
    (stamp, stamp, repo),
)
conn.commit()
if mode == "kill":
    os._exit(9)
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
conn.close()
"""


def _rmtree(path: Path) -> None:
    for _ in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        gc.collect()
        time.sleep(0.2)


def _child_env() -> dict:
    env = dict(os.environ)
    parts = [str(ROOT / "src")]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _spawn_writer(db: Path, lo: int, hi: int, mode: str, stamp: str) -> int:
    proc = subprocess.run(
        [sys.executable, "-c", WRITER.format(src=str(ROOT / "src")),
         str(db), str(lo), str(hi), mode, REPO, stamp],
        capture_output=True, env=_child_env(), cwd=str(ROOT), timeout=180,
    )
    if proc.stderr:
        sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
    return proc.returncode


def _parse_envelope(stdout: bytes) -> dict | None:
    text = stdout.decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    try:
        return json.loads("\n".join(lines))
    except ValueError:
        return None


def _dir_state(directory: Path) -> dict:
    state = {}
    for path in sorted(directory.iterdir()):
        if path.is_file():
            state[path.name] = path.read_bytes()
    return state


class _DoctorWalCase(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="zgt5253-doctorwal-"))
        self.addCleanup(_rmtree, self.scratch)
        self.stamp = "2026-02-01T00:00:00Z"

    def _run_doctor(self, store: Path, scan_dir: Path, consolidate: bool) -> dict:
        argv = [
            sys.executable, "-m", "zaxbygraph", "doctor",
            "--repo", REPO, "--db", str(store), "--scan", str(scan_dir),
            "--format", "json",
        ]
        if consolidate:
            argv.append("--consolidate")
        proc = subprocess.run(
            argv, capture_output=True, env=_child_env(), cwd=str(ROOT), timeout=180,
        )
        self.stderr = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, self.stderr)
        envelope = _parse_envelope(proc.stdout)
        self.assertIsNotNone(envelope, self.stderr)
        return envelope

    def _killed_writer_candidate(self, scan_dir: Path, lo: int, hi: int) -> Path:
        candidate = scan_dir / "history.db"
        self.assertEqual(_spawn_writer(candidate, 1, 5, "clean", self.stamp), 0)
        self.assertEqual(_spawn_writer(candidate, 6, 10, "kill", self.stamp), 9)
        wal = candidate.with_name(candidate.name + "-wal")
        self.assertTrue(wal.exists() and wal.stat().st_size > 0)
        return candidate


class InspectSeesWalCommittedRowsTests(_DoctorWalCase):
    def test_killed_writer_candidate_reports_true_count(self) -> None:
        scan_dir = self.scratch / "scan"
        scan_dir.mkdir()
        self._killed_writer_candidate(scan_dir, 1, 10)
        store = self.scratch / "store" / "history.db"
        store.parent.mkdir(parents=True)
        envelope = self._run_doctor(store, scan_dir, consolidate=False)
        entries = {
            os.path.normcase(str(row.get("path", ""))): row
            for row in (envelope.get("data") or {}).get("scanned") or []
        }
        candidate = scan_dir / "history.db"
        entry = entries.get(os.path.normcase(str(candidate)))
        self.assertIsNotNone(entry, "crash-damaged candidate absent from scanned")
        self.assertEqual(entry["items"], 10, entry)
        self.assertTrue(entry["complete"], entry)


class ConsolidateAdoptsWalCommittedRowsTests(_DoctorWalCase):
    def test_full_corpus_survives_consolidation(self) -> None:
        store = self.scratch / "store" / "history.db"
        store.parent.mkdir(parents=True)
        self.assertEqual(_spawn_writer(store, 1, 30, "clean", self.stamp), 0)
        scan_dir = self.scratch / "scan"
        scan_dir.mkdir()
        self._killed_writer_candidate(scan_dir, 1, 10)
        envelope = self._run_doctor(store, scan_dir, consolidate=True)
        adopted = (envelope.get("data") or {}).get("adopted")
        self.assertTrue(adopted, envelope)
        conn = sqlite3.connect(str(store))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM items WHERE repo = ?", (REPO,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 10, "store was truncated by the adopted copy")


class SourceImmutabilityTests(_DoctorWalCase):
    def test_candidate_directory_untouched(self) -> None:
        scan_dir = self.scratch / "scan"
        scan_dir.mkdir()
        candidate = self._killed_writer_candidate(scan_dir, 1, 10)
        before = _dir_state(scan_dir)
        store = self.scratch / "store" / "history.db"
        store.parent.mkdir(parents=True)
        self._run_doctor(store, scan_dir, consolidate=True)
        after = _dir_state(scan_dir)
        self.assertEqual(
            sorted(before), sorted(after), "candidate directory gained/lost files"
        )
        for name, blob in before.items():
            self.assertEqual(
                blob, after[name], f"candidate file {name} was modified"
            )
        # the candidate must still be a WAL-mode DB holding all ten rows
        conn = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
        try:
            live = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(live, 10)

    def test_clean_no_sidecar_candidate_still_reported(self) -> None:
        scan_dir = self.scratch / "scan"
        scan_dir.mkdir()
        candidate = scan_dir / "history.db"
        self.assertEqual(_spawn_writer(candidate, 1, 4, "clean", self.stamp), 0)
        store = self.scratch / "store" / "history.db"
        store.parent.mkdir(parents=True)
        envelope = self._run_doctor(store, scan_dir, consolidate=False)
        entries = {
            os.path.normcase(str(row.get("path", ""))): row
            for row in (envelope.get("data") or {}).get("scanned") or []
        }
        entry = entries.get(os.path.normcase(str(candidate)))
        self.assertIsNotNone(entry)
        self.assertEqual(entry["items"], 4, entry)


class FaithfulCopyMechanicsTests(_DoctorWalCase):
    """The two mechanics `_faithful_copy` relies on, driven in-process so an
    injected fault actually reaches the copy loop (a subprocess mock never
    does): the recovery log is copied BEFORE the main file, and a source
    that moves during the copy is REFUSED rather than accepted as faithful
    (issue #52, review PRR-016)."""

    def _killed(self, name: str = "history.db", rows: int = 10) -> Path:
        scan_dir = self.scratch / "scan"
        scan_dir.mkdir()
        return self._killed_writer_candidate(scan_dir, 1, rows)

    def test_recovery_log_is_copied_before_the_main_file(self) -> None:
        import unittest.mock as mock

        import zaxbygraph.doctor as doctor_mod

        candidate = self._killed()
        copy = self.scratch / "copy.db"
        real_copy2 = shutil.copy2
        order: list[str] = []

        def recording_copy2(src, dst, *a, **kw):
            order.append(Path(str(src)).name)
            return real_copy2(src, dst, *a, **kw)

        with mock.patch.object(doctor_mod.shutil, "copy2", recording_copy2):
            self.assertTrue(doctor_mod._faithful_copy(candidate, copy))
        self.assertEqual(
            order,
            ["history.db-wal", "history.db"],
            "the recovery log must be copied first so a racing checkpoint"
            " leaves a stale-but-consistent pair, never a torn one",
        )

    def test_source_moving_during_the_copy_is_refused(self) -> None:
        """An active writer that changes the source between the before and
        after stat makes the pair unfaithful: the candidate must be refused
        (False), not accepted as a torn image (issue #52)."""
        import unittest.mock as mock

        import zaxbygraph.doctor as doctor_mod

        candidate = self._killed()
        copy = self.scratch / "copy.db"
        real_copy2 = shutil.copy2

        def growing_copy2(src, dst, *a, **kw):
            result = real_copy2(src, dst, *a, **kw)
            if Path(str(src)).name == "history.db":
                # an active writer appending to the main file after the copy
                with open(candidate, "ab") as handle:
                    handle.write(b"\x00" * 16)
            return result

        with mock.patch.object(doctor_mod.shutil, "copy2", growing_copy2):
            self.assertFalse(doctor_mod._faithful_copy(candidate, copy))

    def test_stable_source_is_accepted(self) -> None:
        """PRESERVING floor: the stability check must not refuse a quiescent
        candidate (a killed writer's sidecars are already final)."""
        import zaxbygraph.doctor as doctor_mod

        candidate = self._killed()
        copy = self.scratch / "copy.db"
        self.assertTrue(doctor_mod._faithful_copy(candidate, copy))


class UnreadableCandidateTests(_DoctorWalCase):
    def test_non_database_file_reported_unusable_not_crash(self) -> None:
        scan_dir = self.scratch / "scan"
        scan_dir.mkdir()
        (scan_dir / "history.db").write_bytes(b"this is not a database at all")
        store = self.scratch / "store" / "history.db"
        store.parent.mkdir(parents=True)
        envelope = self._run_doctor(store, scan_dir, consolidate=False)
        self.assertTrue((envelope.get("data") or {}).get("ok", True))
        for row in (envelope.get("data") or {}).get("scanned") or []:
            self.assertNotEqual(
                os.path.normcase(str(row.get("path", ""))),
                os.path.normcase(str(scan_dir / "history.db")),
            )

    def test_oserror_candidate_is_skipped_not_fatal(self) -> None:
        """One candidate the OS refuses (a locked or permission-denied file)
        must be reported unusable, never abort the scan — and a good sibling
        must still be adopted (PRR-003; implementation-review finding 1:
        the base `except OSError` around the copy). Driven in-process so the
        injected fault reaches the copy loop."""
        import unittest.mock as mock

        import zaxbygraph.doctor as doctor_mod

        scan_dir = self.scratch / "scan"
        scan_dir.mkdir()
        good = scan_dir / "b"
        good.mkdir()
        self.assertEqual(
            _spawn_writer(good / "history.db", 1, 4, "clean", self.stamp), 0
        )
        locked = scan_dir / "a" / "history.db"
        locked.parent.mkdir()
        locked.write_bytes(b"x" * 64)
        real_copy2 = shutil.copy2

        def refusing_copy2(src, dst, *a, **kw):
            if os.path.normcase(str(src)) == os.path.normcase(str(locked)):
                raise PermissionError(13, "Permission denied", str(src))
            return real_copy2(src, dst, *a, **kw)

        store = self.scratch / "store" / "history.db"
        store.parent.mkdir(parents=True)
        with mock.patch.object(doctor_mod.shutil, "copy2", refusing_copy2):
            # inspect: the unreadable candidate is None, not an exception
            self.assertIsNone(doctor_mod.inspect(locked, REPO))
            entry = doctor_mod.inspect(good / "history.db", REPO)
            self.assertIsNotNone(entry)
            self.assertEqual(entry["items"], 4)
            # and the whole scan+consolidate pass still adopts the sibling
            report = doctor_mod.doctor(
                None, REPO, store, extra_scans=[scan_dir], consolidate_flag=True
            )
        self.assertTrue(report["ok"], report)
        self.assertEqual(
            os.path.normcase(str(report["adopted"])),
            os.path.normcase(str(good / "history.db")),
            report,
        )
        paths = {
            os.path.normcase(str(row.get("path", "")))
            for row in report["scanned"]
        }
        self.assertNotIn(
            os.path.normcase(str(locked)),
            paths,
            "the unreadable candidate must be skipped, not abort the scan",
        )

    def test_sidecar_vanishing_mid_copy_is_not_fatal(self) -> None:
        """A sidecar a finishing writer removes between exists() and the
        copy raises FileNotFoundError (an OSError): the copy is retried and
        the candidate still reports its true corpus."""
        import unittest.mock as mock

        import zaxbygraph.doctor as doctor_mod

        scan_dir = self.scratch / "scan"
        scan_dir.mkdir()
        candidate = self._killed_writer_candidate(scan_dir, 1, 10)
        real_copy2 = shutil.copy2
        state = {"raised": False}

        def vanishing_copy2(src, dst, *a, **kw):
            if (
                not state["raised"]
                and str(src).endswith("-wal")
                and os.path.normcase(str(src)).startswith(
                    os.path.normcase(str(scan_dir))
                )
            ):
                state["raised"] = True
                raise FileNotFoundError(2, "No such file", str(src))
            return real_copy2(src, dst, *a, **kw)

        with mock.patch.object(doctor_mod.shutil, "copy2", vanishing_copy2):
            entry = doctor_mod.inspect(candidate, REPO)
        self.assertTrue(state["raised"], "the vanish race was never exercised")
        self.assertIsNotNone(entry, "inspect aborted on the vanishing sidecar")
        self.assertEqual(entry["items"], 10, entry)


if __name__ == "__main__":
    unittest.main()
