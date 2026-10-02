from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fixtures import REPO, FakeGitHubSource, TempDBTest, issue, pr_file, pull
from zaxbygraph.cli import cmd_sync
from zaxbygraph.store import ingest_item as real_ingest_item
from zaxbygraph.query import item, status
from zaxbygraph.sync import (
    SyncError,
    _pid_alive,
    acquire_sync_lock,
    sync_repo,
)  # noqa: F401 - _pid_alive is the production helper (PRR-011): the
# suite must exercise the exact fail-closed semantics the lock ships with.


class SyncTests(TempDBTest):
    def test_full_then_incremental_since(self) -> None:
        self.src.add_issue(issue(1, title="one", updated_at="2026-01-01T00:00:00Z"))
        self.src.add_issue(issue(2, title="two", updated_at="2026-01-02T00:00:00Z"))
        first = self.sync()
        self.assertEqual(first["ingested"], 2)
        self.assertEqual(first["issues_since"], "2026-01-02T00:00:00Z")
        second = self.sync()
        # inclusive since refetches #2
        self.assertEqual(second["ingested"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 2)

    def test_comment_only_update_advances_watermark(self) -> None:
        self.src.add_issue(issue(1, title="one", updated_at="2026-01-01T00:00:00Z", comments=0))
        self.src.add_issue(issue(2, title="two", updated_at="2026-01-02T00:00:00Z", comments=0))
        self.sync()
        before = self.conn.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.src.comment_on(1, "ping")
        result = self.sync()
        after = self.conn.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertGreater(after, before)
        self.assertGreaterEqual(result["ingested"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM comments"), 1)

    def test_429_on_extra_get_leaves_earlier_items(self) -> None:
        self.src.add_issue(issue(1, title="one", updated_at="2026-01-01T00:00:00Z", comments=0))
        self.src.add_issue(issue(2, title="two", updated_at="2026-01-02T00:00:00Z", comments=0))
        self.src.add_pr(
            issue(3, title="three", updated_at="2026-01-03T00:00:00Z", comments=0, kind="pr"),
            pull(3, changed_files=1),
            files=[pr_file("src/a.py")],
        )
        self.src.fail_after(1)  # first extra fetch is get_pull(#3)
        with self.assertRaises(SyncError):
            self.sync()
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 2)
        since = self.conn.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertEqual(since, "2026-01-02T00:00:00Z")
        err = self.conn.execute(
            "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertIn("429", err)

    def test_force_clears_watermark(self) -> None:
        self.src.add_issue(issue(1, title="one", updated_at="2026-01-01T00:00:00Z"))
        self.sync()
        result = self.sync(force=True)
        self.assertTrue(result["full"])
        self.assertEqual(result["ingested"], 1)

    def test_skip_comments_get_when_zero(self) -> None:
        self.src.add_issue(issue(1, title="one", comments=0))
        self.sync()
        self.assertEqual(self.src.extra_fetches, 0)

    def test_skip_files_get_when_changed_zero(self) -> None:
        self.src.add_pr(
            issue(3, title="pr", comments=0, kind="pr"),
            pull(3, changed_files=0),
            files=[pr_file("src/ghost.py")],
        )
        before = self.src.extra_fetches
        self.sync()
        # pull + reviews + review comments; not files
        self.assertEqual(self.src.extra_fetches - before, 3)
        self.assertEqual(self.count("SELECT COUNT(*) FROM pr_files"), 0)

    def test_releases_replace_transactional(self) -> None:
        self.src.add_issue(issue(1, title="one"))
        self.src.releases = [
            {
                "id": 1,
                "tag_name": "v0.1.0",
                "name": "v0.1.0",
                "body": "first",
                "draft": False,
                "prerelease": False,
                "author": {"login": "alice"},
                "created_at": "2026-01-01T00:00:00Z",
                "published_at": "2026-01-01T00:00:00Z",
                "html_url": "https://github.com/acme/forgegate/releases/tag/v0.1.0",
            }
        ]
        self.sync()
        self.src.releases = [
            {
                "id": 2,
                "tag_name": "v0.2.0",
                "name": "v0.2.0",
                "body": "second",
                "draft": False,
                "prerelease": False,
                "author": {"login": "alice"},
                "created_at": "2026-02-01T00:00:00Z",
                "published_at": "2026-02-01T00:00:00Z",
                "html_url": "https://github.com/acme/forgegate/releases/tag/v0.2.0",
            }
        ]
        self.sync(force=True)
        tags = [r[0] for r in self.conn.execute("SELECT tag_name FROM releases").fetchall()]
        self.assertEqual(tags, ["v0.2.0"])

    def test_jsonl_writes_events_in_dir(self) -> None:
        self.src.add_issue(issue(1, title="one", comments=0))
        dest = Path(self._td.name) / "jsonl"
        self.sync(jsonl_path=dest)
        text = (dest / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"resource": "item"', text)
        self.assertTrue(dest.is_dir())

    def test_duplicate_comment_and_pr_file_from_pagination_do_not_raise(self) -> None:
        self.src.add_pr(
            issue(3, title="three", comments=1, kind="pr"),
            pull(3, changed_files=1),
            files=[pr_file("src/a.py")],
        )
        self.src.comment_on(3, "hello")
        self.src.duplicate_comments = True
        self.src.duplicate_pr_files = True
        result = self.sync()
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM comments"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM pr_files"), 1)

    def test_sqlite_error_mid_sync_records_last_error_and_is_resumable(self) -> None:
        self.src.add_issue(issue(1, title="one", updated_at="2026-01-01T00:00:00Z"))
        self.src.add_issue(issue(2, title="two", updated_at="2026-01-02T00:00:00Z"))
        calls = {"n": 0}

        def fake_ingest(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("disk I/O error")
            return real_ingest_item(*a, **kw)

        with patch("zaxbygraph.sync.ingest_item", side_effect=fake_ingest):
            with self.assertRaises(SyncError):
                self.sync()
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 1)
        since = self.conn.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertEqual(since, "2026-01-01T00:00:00Z")
        err = self.conn.execute(
            "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertIn("disk I/O error", err)

    def test_cli_reports_mid_sync_sqlite_error_as_clean_exit(self) -> None:
        self.src.add_issue(issue(1, title="one", updated_at="2026-01-01T00:00:00Z"))
        self.src.add_issue(issue(2, title="two", updated_at="2026-01-02T00:00:00Z"))
        calls = {"n": 0}

        def fake_ingest(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("disk I/O error")
            return real_ingest_item(*a, **kw)

        args = argparse.Namespace(
            repo=REPO,
            db=str(self.db_path),
            force=False,
            include_patches=False,
            jsonl=None,
            jsonl_flag=False,
            format="json",
        )
        with patch("zaxbygraph.sync.ingest_item", side_effect=fake_ingest), patch(
            "zaxbygraph.cli.GhApiSource", return_value=self.src
        ):
            code = cmd_sync(args)
        self.assertEqual(code, 1)

    def test_successful_sync_clears_last_error(self) -> None:
        self.conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_error) VALUES (?, ?, ?)",
            (REPO, "2099-01-01T00:00:00Z", "old 429"),
        )
        self.conn.commit()
        self.sync()
        err = self.conn.execute(
            "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertIsNone(err)


class SyncStateTests(TempDBTest):
    """State-truthfulness tests from issue #1 (last_error, complete, slugs)."""

    def test_unexpected_exception_records_last_error(self) -> None:
        self.src.add_issue(issue(1))

        class BoomSource(FakeGitHubSource):
            def list_issues(self, since):
                raise RuntimeError("reader thread died")

        with self.assertRaises(RuntimeError):
            sync_repo(self.conn, BoomSource(), REPO)
        err = self.conn.execute(
            "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertIsNotNone(err)
        self.assertIn("reader thread died", err)

    def test_keyboard_interrupt_mid_item_records_last_error(self) -> None:
        self.src.add_issue(issue(1, updated_at="2026-01-01T00:00:10Z"))

        # Interrupt INSIDE the per-item transaction (after BEGIN IMMEDIATE,
        # before commit): KeyboardInterrupt bypasses the `except Exception`
        # rollback guard, so the outer handler must roll the open transaction
        # back before recording, or the recorder itself would fail.
        with patch("zaxbygraph.sync.ingest_item", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.sync()
        err = self.conn.execute(
            "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertIsNotNone(err)
        self.assertIn("KeyboardInterrupt", err)
        # the connection is usable after the open transaction was rolled back
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM sync_state WHERE repo = ?", (REPO,)), 1
        )
        # and a later clean run completes
        result = self.sync()
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 1)

    def test_resumed_full_sync_reports_complete(self) -> None:
        self.src.add_issue(issue(1, updated_at="2026-01-01T00:00:10Z"))  # no extra GETs
        self.src.add_pr(
            issue(2, updated_at="2026-01-02T00:00:00Z", kind="pr"), pull(2)
        )
        self.src.fail_after(1)  # first extra GET is item 2's get_pull
        with self.assertRaises(SyncError):
            self.sync()
        state = status(self.conn, REPO)["repos"][0]
        self.assertFalse(state["complete"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 1)

        self.src.fail_after(None)  # clean resume
        self.sync()
        state = status(self.conn, REPO)["repos"][0]
        self.assertTrue(state["complete"])
        row = self.conn.execute(
            "SELECT last_full_sync_at, full_sync_pending FROM sync_state WHERE repo = ?",
            (REPO,),
        ).fetchone()
        self.assertIsNotNone(row["last_full_sync_at"])
        self.assertEqual(row["full_sync_pending"], 0)

    def test_force_interruption_marks_pending(self) -> None:
        self.src.add_issue(issue(1, updated_at="2026-01-01T00:00:10Z"))
        self.sync()  # complete corpus
        self.assertTrue(status(self.conn, REPO)["repos"][0]["complete"])

        self.src.add_pr(
            issue(2, updated_at="2026-01-02T00:00:00Z", kind="pr"), pull(2)
        )
        self.src.fail_after(1)
        with self.assertRaises(SyncError):
            self.sync(force=True)  # force nulls the watermark, then interrupts
        state = status(self.conn, REPO)["repos"][0]
        self.assertFalse(state["complete"])

        self.src.fail_after(None)
        self.sync()  # resume without --force completes the forced full sync
        state = status(self.conn, REPO)["repos"][0]
        self.assertTrue(state["complete"])

    def test_slug_case_folds(self) -> None:
        self.src.add_issue(issue(1))
        sync_repo(self.conn, self.src, "ZaxbyHub/ForgeGate")
        sync_repo(self.conn, self.src, "zaxbyhub/forgegate")
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM sync_state WHERE repo LIKE 'zaxbyhub/%'"),
            1,
        )
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM sync_state WHERE repo != 'zaxbyhub/forgegate'"),
            0,
        )
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM items WHERE repo != 'zaxbyhub/forgegate'"),
            0,
        )
        # lookups fold too
        self.assertIsNotNone(item(self.conn, 1, repo="ZaxbyHub/ForgeGate"))

    def test_jsonl_setup_failure_records_last_error_and_reports(self) -> None:
        # A sidecar setup failure is a failed sync: it must record last_error
        # (leaving complete false), and the CLI must report a result object,
        # never a traceback (review round 1 finding, sync.py jsonl block).
        self.src.add_issue(issue(1))
        blocker = Path(self._td.name) / "blocker"
        blocker.write_text("in the way", encoding="utf-8")
        args = argparse.Namespace(
            repo=REPO,
            db=str(self.db_path),
            force=False,
            include_patches=False,
            jsonl=str(blocker / "sub"),  # mkdir fails: blocker is a file
            jsonl_flag=False,
            format="json",
        )
        out = io.StringIO()
        with patch("zaxbygraph.cli.GhApiSource", return_value=self.src):
            with redirect_stdout(out):
                code = cmd_sync(args)
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("blocker", payload["error"])
        row = self.conn.execute(
            "SELECT last_error, full_sync_pending FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()
        self.assertIsNotNone(row["last_error"])
        self.assertEqual(row["full_sync_pending"], 1)
        self.assertFalse(status(self.conn, REPO)["repos"][0]["complete"])

    def test_prologue_failure_records_last_error(self) -> None:
        # A fault in the prologue (force reset / state-row ensure / pending
        # marker) is still a failed sync: it must not escape with state
        # looking clean (review round 2 finding, lock-landing experiment).
        self.src.add_issue(issue(1))
        with patch(
            "zaxbygraph.sync._ensure_state_row",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            with self.assertRaises(SyncError):
                self.sync()
        row = self.conn.execute(
            "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("database is locked", row["last_error"])
        self.assertFalse(status(self.conn, REPO)["repos"][0]["complete"])


# ==== issue-trace 2-worktree-global-store: acceptance append (AC5) ====
# Appended by .agents/issue-traces/2-worktree-global-store/repro/patches/
# patch_test_sync.py -- append-only; every class above is untouched and every
# import needed below is restated here (no header edits).
import io
import json
import socket
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fixtures import REPO, TempDBTest, issue, pr_file, pull
from zaxbygraph.cli import main



class SyncLockTests(TempDBTest):
    """Sync-lock acceptance tests (issue #2 AC5): one sync per repo at a
    time, enforced by <db>.sync.lock (join / --wait / stale takeover)."""

    def test_real_contention_joins_and_pid_semantics(self) -> None:
        """PRR-008 regression: a REAL range-lock holder (not a decoy
        payload file) must join, never raise; production _pid_alive
        semantics pinned (PRR-011)."""
        lock = acquire_sync_lock(self.db_path)
        try:
            self.assertIsNotNone(lock)
            # Real contention must JOIN (return None), never raise - the
            # PRR-008 round-1 regression turned this into PermissionError.
            self.assertIsNone(acquire_sync_lock(self.db_path))
        finally:
            lock.release_owned()
        # After release, acquiring again must succeed (lock was freed).
        lock2 = acquire_sync_lock(self.db_path)
        self.assertIsNotNone(lock2)
        lock2.release_owned()
        self.assertFalse(_pid_alive(self.dead_pid()))
        self.assertTrue(_pid_alive(os.getpid()))

    def test_fault_with_unobservable_holder_raises(self) -> None:
        """PRR-008 regression (reviewer round-5): a range-lock failure with
        NO observable holder - a persistent environmental fault, or a lock
        held empty well past the re-probe pause - must RAISE, never
        silently report joined:true. The bounded re-probe resolves the
        genuine pre-payload window (a real holder writes its payload
        within it); this shape does not clear."""
        from zaxbygraph.sync import _FAULT_REPROBE_PAUSE, _lock_range

        fd = os.open(str(self.lock_path()), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            _lock_range(fd)  # hold the range lock; payload stays absent
            with patch("zaxbygraph.sync._FAULT_REPROBE_PAUSE", 0):
                with self.assertRaises(OSError):
                    acquire_sync_lock(self.db_path)
        finally:
            os.close(fd)

    def test_monkeypatched_fault_raises_not_joins(self) -> None:
        """The original PRR-008 repro: _lock_range fails (any OSError) with
        no observable holder -> acquire_sync_lock RAISES, never returns a
        silent joined:true."""
        with patch(
            "zaxbygraph.sync._lock_range", side_effect=PermissionError(13, "injected")
        ):
            with self.assertRaises(OSError):
                acquire_sync_lock(self.db_path)

    def test_empty_lock_file_with_held_range_lock_joins(self) -> None:
        """Critic round-4 shape: a holder that owns the range lock and HAS
        written its payload is contention - the second sync joins. (The
        empty pre-payload window is covered by the fault-raise test.)"""
        from zaxbygraph.sync import _lock_range, _write_payload_locked

        fd = os.open(str(self.lock_path()), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            _lock_range(fd)  # hold the range lock
            # Write the payload THROUGH the locked fd: Windows region locks
            # block writes from any other handle, even in this process.
            _write_payload_locked(
                fd, {"pid": os.getpid(), "host": socket.gethostname(), "started_at": "t"}
            )
            self.assertIsNone(acquire_sync_lock(self.db_path))
        finally:
            os.close(fd)

    def lock_path(self):
        return Path(str(self.db_path) + ".sync.lock")

    def write_lock(self, pid, host):
        path = self.lock_path()
        path.write_text(
            json.dumps({"pid": pid, "host": host, "started_at": "2026-10-02T00:00:00Z"}),
            encoding="utf-8",
        )
        self.addCleanup(lambda: path.unlink(missing_ok=True))

    def cli_sync(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with patch("zaxbygraph.cli.GhApiSource", return_value=self.src):
                code = main(
                    ["sync", "--repo", REPO, "--db", str(self.db_path),
                     "--format", "json", *extra]
                )
        return code, out.getvalue(), err.getvalue()

    def spawn_holder(self):
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )

    def dead_pid(self):
        """A provably-dead pid (retried in the unlikely event of pid reuse)."""
        for _ in range(5):
            proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
            proc.wait()
            if not _pid_alive(proc.pid):
                return proc.pid
        self.fail("could not obtain a provably-dead pid")

    def test_concurrent_sync_does_not_double_fetch(self):
        self.src.add_issue(issue(1, title="one"))
        self.src.add_pr(
            issue(2, title="two", kind="pr", state="closed"),
            pull(2, changed_files=1),
            files=[pr_file("src/a.py")],
        )

        # --- holder is a live process on this host: join with zero calls ---
        holder = self.spawn_holder()
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertTrue(_pid_alive(holder.pid), "holder child died prematurely")
        self.write_lock(holder.pid, socket.gethostname())
        code, out, err = self.cli_sync()
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIs(data["ok"], True)
        self.assertIs(data.get("joined"), True)
        self.assertEqual(self.src.extra_fetches, 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 0)
        self.assertTrue(_pid_alive(holder.pid), "a joining sync must not kill the holder")
        # Round-2 critic blocker 3: the join path must NOT wipe the live
        # holder's payload (a wiped payload would let a third sync run).
        payload = json.loads(self.lock_path().read_text(encoding="utf-8"))
        self.assertEqual(payload["pid"], holder.pid, "join clobbered the holder's payload")
        self.assertEqual(payload["host"], socket.gethostname())

        # --- holder on a different host: never stolen, treated as held ---
        self.write_lock(holder.pid, "foreign-host-" + socket.gethostname())
        code, out, err = self.cli_sync()
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIs(data["ok"], True)
        self.assertIs(data.get("joined"), True)

        # --- foreign host with a DEAD pid: still never stolen (PRR-012) ---
        dead_foreign = self.dead_pid()
        self.assertFalse(_pid_alive(dead_foreign))
        self.write_lock(dead_foreign, "foreign-host-" + socket.gethostname())
        code, out, err = self.cli_sync()
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIs(data["ok"], True)
        self.assertIs(data.get("joined"), True)
        self.assertEqual(self.src.extra_fetches, 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 0)
        code, out, err = self.cli_sync()
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIs(data["ok"], True)
        self.assertIs(data.get("joined"), True)
        self.assertEqual(self.src.extra_fetches, 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 0)

        # --- stale holder (dead pid, same host): recovered; sync proceeds ---
        dead = self.dead_pid()
        self.assertFalse(_pid_alive(dead))
        self.write_lock(dead, socket.gethostname())
        code, out, err = self.cli_sync()
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIs(data["ok"], True)
        self.assertIsNot(data.get("joined"), True)
        self.assertEqual(data.get("ingested"), 2)
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 2)
        self.assertGreater(self.src.extra_fetches, 0)

        # --- --wait blocks for the lock instead of joining ---------------
        self.write_lock(holder.pid, socket.gethostname())
        self.assertTrue(_pid_alive(holder.pid))

        def release():
            time.sleep(0.6)
            holder.kill()
            # Deliberately NO unlink (PRR-012): the waiter must observe the
            # dead same-host payload and take over under the range lock.

        releaser = threading.Thread(target=release)
        releaser.start()
        try:
            started = time.monotonic()
            code, out, err = self.cli_sync("--wait")
            elapsed = time.monotonic() - started
        finally:
            releaser.join()
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIs(data["ok"], True)
        self.assertIsNot(data.get("joined"), True)
        self.assertGreaterEqual(elapsed, 0.45, "--wait returned before the holder died")
        self.assertLess(elapsed, 30.0, "--wait spun far too long")
        # Idempotent re-sync of the same corpus: still exactly the 2 items.
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 2)
