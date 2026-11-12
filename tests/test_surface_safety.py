"""Acceptance checks for issue #71 (surface safety: bounded, interruptible,
capability-honest reads). RED at base by assertion; GREEN once fixed.

AC -> -k method map: AC1 test_sql_deadline, AC2 test_sql_byte_budget_and_bytes,
AC3 test_failed_refresh_does_not_poison_or_storm, AC4
test_no_roots_request_without_capability, AC5 test_item_is_bounded_by_default,
AC6 test_reads_can_exclude_bots, AC7 test_related_is_bounded, AC10 (new check
C24, fix-plan v3 CF-M) test_broken_pipe_is_quiet. PRESERVING:
AC8 = tests/test_query.py -k test_run_sql_negative_limit_is_clamped; AC9 =
tests/test_mcp.py -k test_stale_read_answers_now_and_refreshes_in_background.

Hermetic: FakeGitHubSource only, scratch stores under tempfile dirs, no
network, Windows-safe (no symlinks, no long paths).
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, comment, issue, pr_file, pull, review
from test_mcp import (
    McpSession,
    _QueueInput,
    _QueueOutput,
    pin_env_for,
    run_cli,
    tool_envelope,
)
from test_paths import git
from zaxbygraph.db import connect, init_schema, connect_readonly_query
from zaxbygraph.github import GitHubError
from zaxbygraph.query import run_sql
from zaxbygraph.sync import sync_repo

import zaxbygraph.mcp_server as mcp_server

#: AC1: a query that would run forever if never interrupted.
_INFINITE_SQL = "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) SELECT n FROM c"
#: Huge staleness threshold: freshness-kick behavior is NOT under test here.
_NO_REFRESH_S = 10**9
#: Failure-backoff window for background refreshes (fix-plan v3 CF-B): the
#: env seam the post-backoff retry leg drives. Inert at base — no backoff
#: exists there yet, which is exactly what makes the retry leg RED.
_REFRESH_BACKOFF_ENV = "ZAXBYGRAPH_REFRESH_BACKOFF_S"


class _Problems(list):
    """Multi-leg assertion collector: every leg runs, every failure is named by one parent-level assertion (an unambiguous RED for the -k filter)."""

    @contextlib.contextmanager
    def leg(self, label: str):
        try:
            yield
        except AssertionError as exc:
            self.append(f"{label}: {exc}")


def _seed_scratch_db(db_path: Path, seeder) -> Path:
    """Build an offline scratch store with fixtures.FakeGitHubSource."""
    conn = connect(db_path)
    init_schema(conn)
    src = FakeGitHubSource()
    seeder(src)
    sync_repo(conn, src, REPO)
    conn.close()
    return db_path


def _force_stale(db_path: Path) -> None:
    """Stamp the store's syncs far in the past so every read wants a refresh."""
    conn = connect(db_path)
    conn.execute(
        "UPDATE sync_state SET last_full_sync_at = ?, last_incr_sync_at = ? WHERE repo = ?",
        ("2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z", REPO),
    )
    conn.commit()
    conn.close()


def _sync_state_row(db_path: Path) -> dict:
    """Read the REPO sync_state row (read-only) for the isolation legs."""
    check = connect_readonly_query(db_path)
    try:
        row = check.execute(
            "SELECT last_error, full_sync_pending, last_full_sync_at, issues_since, item_count"
            " FROM sync_state WHERE repo = ?",
            (REPO,),
        ).fetchone()
    finally:
        check.close()
    return {} if row is None else {key: row[key] for key in row.keys()}


def _wait_jobs_settled(db_path: Path, timeout_s: float = 10.0) -> None:
    """Poll the server's job registry until no job is running for this store."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        jobs = mcp_server._jobs_snapshot(str(db_path))
        if jobs and all(job["state"] != "running" for job in jobs):
            return
        time.sleep(0.05)


@contextlib.contextmanager
def _env_backoff(seconds: str):
    """Set the failure-backoff env seam for one leg; always restore it."""
    saved = os.environ.get(_REFRESH_BACKOFF_ENV)
    os.environ[_REFRESH_BACKOFF_ENV] = seconds
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(_REFRESH_BACKOFF_ENV, None)
        else:
            os.environ[_REFRESH_BACKOFF_ENV] = saved


class SqlDeadlineTests(unittest.TestCase):
    """AC1: a sql query past a deadline (default 5s, configurable) is interrupted, answers coded `timeout` on CLI and MCP, and leaves the connection/server usable."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = _seed_scratch_db(self.root / "history.db", lambda src: src.add_issue(issue(1)))

    def test_sql_deadline(self) -> None:
        # The seam: run_sql must expose a deadline kwarg (default 5s).
        params = inspect.signature(run_sql).parameters
        self.assertIn("deadline", params, f"AC1: query.run_sql must accept a deadline= argument (default 5s, configurable) so a query that would run forever can be interrupted; parameters are {sorted(params)}")
        default = params["deadline"].default
        if isinstance(default, (int, float)):
            self.assertTrue(0 < float(default) <= 5.0, f"AC1: run_sql's deadline default must be the documented 5s (or smaller for tests); got {default!r}")

        # Seam behavior: the infinite query is interrupted with a `timeout`
        # signal, and the connection stays usable afterwards.
        outcome: dict = {}

        def _worker() -> None:
            conn = connect_readonly_query(self.db_path)
            try:
                started = time.monotonic()
                try:
                    outcome["data"] = run_sql(conn, _INFINITE_SQL, limit=5, deadline=0.5)
                except BaseException as exc:  # noqa: BLE001 - reported back
                    outcome["exc"] = exc
                outcome["elapsed"] = time.monotonic() - started
                try:
                    outcome["usable"] = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                except BaseException as exc:  # noqa: BLE001
                    outcome["usable_exc"] = exc
            finally:
                conn.close()

        thread = threading.Thread(target=_worker, daemon=True, name="sql-deadline")
        thread.start()
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive(), "AC1: run_sql(deadline=0.5) never returned on an unbounded recursive CTE -- the deadline must interrupt the query")
        self.assertLess(outcome.get("elapsed", 999), 10.0, f"AC1: run_sql(deadline=0.5) took {outcome.get('elapsed')!r}s -- the deadline must bound the query, not the default 5s")
        if "exc" in outcome:
            self.assertIn("timeout", str(outcome["exc"]).lower(), f"AC1: the interrupted query must raise a timeout-coded error; got {outcome['exc']!r}")
        else:
            data = outcome.get("data") or {}
            code = (data.get("error") or {}).get("code") or data.get("code")
            self.assertEqual(code, "timeout", f"AC1: the interrupted query result must carry error code 'timeout'; got {data!r}")
        self.assertNotIn("usable_exc", outcome, f"AC1: the connection must stay usable after the interrupted query; a later SELECT raised {outcome.get('usable_exc')!r}")

        # CLI leg: the same statement answers the coded timeout envelope.
        code, out, err = run_cli(["sql", _INFINITE_SQL, "--repo", REPO, "--db", str(self.db_path), "--format", "json"])
        self.assertNotEqual(code, 0, "AC1: a timed-out sql must exit non-zero")
        env = json.loads(out)
        self.assertIs(env.get("ok"), False, f"AC1: the CLI envelope must be a failure: {env}")
        self.assertEqual((env.get("error") or {}).get("code"), "timeout", f"AC1: the CLI sql error code must be 'timeout'; got {env.get('error')!r}")

        # MCP leg: the session survives the timeout and still answers.
        pin_env_for(self, self.root, db=self.db_path)
        session = McpSession(self, cwd=str(self.root), repo=REPO, db=str(self.db_path), stale_after_s=_NO_REFRESH_S)
        self.addCleanup(session.stop)
        session.initialize()
        reply = session.call_tool("sql", {"repo": REPO, "statement": _INFINITE_SQL, "limit": 5}, timeout=30)
        result, env = tool_envelope(self, reply, "AC1")
        self.assertIs(result.get("isError"), True, "AC1: the timed-out MCP sql is a tool error")
        self.assertEqual((env.get("error") or {}).get("code"), "timeout", f"AC1: the MCP sql error code must be 'timeout'; got {env.get('error')!r}")
        _result, env = tool_envelope(self, session.call_tool("graph_status", {"repo": REPO}), "AC1")
        self.assertIs(env.get("ok"), True, "AC1: the server must stay responsive after a sql timeout")
        session.stop()


class SqlByteBudgetTests(unittest.TestCase):
    """AC2: a result over a byte budget stops fetching (truncated:true), oversized cells are capped with an explicit marker, bytes values ride out as hex strings (never repr b'...')."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = _seed_scratch_db(self.root / "history.db", lambda src: src.add_issue(issue(1)))

    def test_sql_byte_budget_and_bytes(self) -> None:
        conn = connect_readonly_query(self.db_path)
        self.addCleanup(conn.close)

        # bytes values ride out as hex strings, never the sqlite3 repr.
        value = run_sql(conn, "SELECT randomblob(8) AS b", limit=5)["rows"][0][0]
        self.assertIsInstance(value, str, f"AC2: a bytes cell must be encoded as a hex string, never the sqlite3 bytes repr; got {type(value).__name__} {value!r}")
        self.assertFalse(value.startswith("b'") or value.startswith('b"'), f"AC2: bytes cells must never surface as repr b'...'; got {value!r}")
        self.assertRegex(value, r"^[0-9a-fA-F]{16}$", f"AC2: an 8-byte blob must encode as 16 hex characters; got {value!r}")

        # oversized cells are capped with an explicit marker.
        cell = run_sql(conn, "SELECT printf('%.60000c', 'x') AS big", limit=5)["rows"][0][0]
        self.assertIsInstance(cell, str, "AC2: the capped cell must stay a string")
        self.assertLess(len(cell), 60_000, f"AC2: a {len(cell)}-char cell was returned whole -- oversized cells must be capped by the byte budget")
        self.assertTrue(any(marker in cell for marker in ("…", "...", "truncat", "[cut")), f"AC2: a capped cell must carry an explicit truncation marker; got tail {cell[-40:]!r}")

        # the byte budget parameter stops the fetch.
        params = inspect.signature(run_sql).parameters
        budget_name = next((n for n in ("max_bytes", "byte_budget", "bytes_budget", "budget_bytes", "max_result_bytes") if n in params), None)
        self.assertIsNotNone(budget_name, f"AC2: run_sql must expose a result byte-budget parameter (one of max_bytes/byte_budget/bytes_budget/budget_bytes); parameters are {sorted(params)}")
        big = ("WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 2000) SELECT n, printf('%.600c', 'x') AS pad FROM c")
        data = run_sql(conn, big, limit=2000, **{budget_name: 50_000})
        self.assertIs(data.get("truncated"), True, f"AC2: a ~1.2MB result under a 50KB budget must stop fetching with truncated:true; got truncated={data.get('truncated')!r}")
        self.assertLess(len(data["rows"]), 2000, f"AC2: the byte budget must stop the fetch early; got all {len(data['rows'])} rows")
        serialized = sum(len(str(c)) for row in data["rows"] for c in row)
        self.assertLess(serialized, 200_000, f"AC2: the returned rows total {serialized} chars -- far over a 50KB budget; fetching must stop at the budget")


class _AlwaysFailRunner:
    """Injected background-sync seam that always fails (offline, fast)."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, db_path, repo) -> None:
        self.calls.append((str(db_path), str(repo)))
        raise RuntimeError("injected refresh failure")


class _FailingListSource(FakeGitHubSource):
    """A source whose listing always fails (the offline stand-in for a dead
    upstream): a real sync_repo run over it records the failure into
    sync_state and then raises."""

    def list_issues(self, since):
        raise GitHubError("HTTP 502 bad gateway", status=502)
        yield  # pragma: no cover - generator shape only


class _RealSyncFailRunner:
    """Injected background-sync seam whose body is a REAL sync_repo call over
    a failing source — the production repo-sync entry the MCP layer wraps
    (fix-plan v3 CF-B). sync_repo FIRST mutates sync_state (set_last_error;
    a NULL-watermark store additionally gets full_sync_pending=1 the moment a
    from-scratch walk begins) and ONLY THEN raises, which is exactly the
    failure-write shape the refresh-isolation wrapper must snapshot and
    restore."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, db_path, repo) -> None:
        self.calls.append((str(db_path), str(repo)))
        conn = connect(db_path)
        try:
            init_schema(conn)
            sync_repo(conn, _FailingListSource(), repo)
        finally:
            conn.close()


class _BlockingFailRunner:
    """Injected background-sync seam: records the call, blocks until released
    (an in-flight refresh a second read must JOIN), then fails."""

    def __init__(self) -> None:
        self.calls: list = []
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, db_path, repo) -> None:
        self.calls.append((str(db_path), str(repo)))
        self.started.set()
        self.release.wait(timeout=15)
        raise RuntimeError("injected in-flight refresh failure")


class RefreshBackoffTests(unittest.TestCase):
    """AC3: a failing background refresh leaves a complete store's complete/last_error untouched, reports a refresh_failed reason, and starts no other refresh for that store until a backoff elapses."""

    def test_failed_refresh_does_not_poison_or_storm(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)

        # --- Amend legs (fix-plan v3, CF-B): production-like RED evidence ---
        problems = _Problems()

        # (a) Production-like failure: the runner is a REAL sync_repo call
        # (the repo-sync entry the MCP layer wraps) whose source fails AFTER
        # sync_repo has recorded the failure into sync_state. The
        # refresh-isolation wrapper (CF-B: snapshot the sync_state row, restore
        # it on ANY failed run) must keep a previously-complete store's state
        # UNCHANGED. RED at base because the wrapper does not exist yet: the
        # real failing run leaves last_error='HTTP 502...' behind.
        prod_db = _seed_scratch_db(root / "prod-fail.db", lambda src: src.add_issue(issue(1)))
        _force_stale(prod_db)
        prod_runner = _RealSyncFailRunner()
        with problems.leg("a: REAL sync_repo failure leaves a complete store untouched"):
            pin_env_for(self, root, db=prod_db)
            prod_session = McpSession(self, cwd=str(root), repo=REPO, db=str(prod_db), stale_after_s=0, sync_runner=prod_runner)
            self.addCleanup(prod_session.stop)
            prod_session.initialize()
            _result, env = tool_envelope(self, prod_session.call_tool("graph_status", {"repo": REPO}), "AC3")
            self.assertIs(env.get("ok"), True, f"AC3(a): the stale read must still answer: {env}")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not prod_runner.calls:
                time.sleep(0.05)
            self.assertEqual(len(prod_runner.calls), 1, "AC3(a): the stale read must drive the REAL sync_repo seam exactly once")
            _wait_jobs_settled(prod_db)
            row = _sync_state_row(prod_db)
            self.assertIsNone(row.get("last_error"), f"AC3(a): a REAL failing background refresh mutates sync_state (set_last_error) before raising; the refresh-isolation wrapper must restore the snapshot so last_error stays clean in a previously-complete store -- got {row.get('last_error')!r}")
            self.assertFalse(bool(row.get("full_sync_pending")), f"AC3(a): a failed background refresh must not mark a previously-complete store pending; sync_state={row!r}")
            self.assertIsNotNone(row.get("last_full_sync_at"), f"AC3(a): the completed full-sync stamp must survive the failed refresh; sync_state={row!r}")
            prod_session.stop()

        # (b) Previously-complete EMPTY store (complete=true, zero items, NULL
        # watermark): a failed stale refresh must keep it complete. The NULL
        # watermark is the trap — the real sync_repo marks full_sync_pending=1
        # the moment a from-scratch walk begins, so at base the failed refresh
        # flips the complete empty store incomplete.
        empty_db = _seed_scratch_db(root / "empty-complete.db", lambda src: None)
        _force_stale(empty_db)
        empty_runner = _RealSyncFailRunner()
        with problems.leg("b: failed refresh keeps a complete EMPTY store complete"):
            pre = _sync_state_row(empty_db)
            self.assertIsNone(pre.get("issues_since"), f"fixture: the empty store carries a NULL watermark; sync_state={pre!r}")
            self.assertIsNotNone(pre.get("last_full_sync_at"), f"fixture: the empty store completed a full sync (complete=true); sync_state={pre!r}")
            self.assertEqual(int(pre.get("item_count") or 0), 0, f"fixture: the empty store holds zero items; sync_state={pre!r}")
            pin_env_for(self, root, db=empty_db)
            empty_session = McpSession(self, cwd=str(root), repo=REPO, db=str(empty_db), stale_after_s=0, sync_runner=empty_runner)
            self.addCleanup(empty_session.stop)
            empty_session.initialize()
            _result, env = tool_envelope(self, empty_session.call_tool("graph_status", {"repo": REPO}), "AC3")
            self.assertIs(env.get("ok"), True, f"AC3(b): the stale read over the empty store must still answer: {env}")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not empty_runner.calls:
                time.sleep(0.05)
            self.assertEqual(len(empty_runner.calls), 1, "AC3(b): the stale read must drive the REAL sync_repo seam exactly once")
            _wait_jobs_settled(empty_db)
            row = _sync_state_row(empty_db)
            self.assertIsNone(row.get("last_error"), f"AC3(b): a failed refresh against a previously-complete EMPTY store must not write last_error; got {row.get('last_error')!r}")
            self.assertFalse(bool(row.get("full_sync_pending")), "AC3(b): the empty store's NULL watermark must not let a FAILED refresh flip full_sync_pending on -- complete must stay true after a failed stale refresh")
            self.assertIsNotNone(row.get("last_full_sync_at"), f"AC3(b): the completed full-sync stamp must survive; sync_state={row!r}")
            empty_session.stop()

        # (c) Post-backoff retry: within the backoff window a failed refresh
        # must not re-arm, and once the window elapses a new refresh MUST be
        # allowed to start (a backoff that never releases is a poison of its
        # own). RED at base via the in-window half: with no backoff machinery
        # the read inside the window storms a second refresh.
        backoff_db = _seed_scratch_db(root / "backoff.db", lambda src: src.add_issue(issue(1)))
        _force_stale(backoff_db)
        backoff_runner = _AlwaysFailRunner()
        with problems.leg("c: a new refresh is allowed once the backoff window elapses"):
            with _env_backoff("2"):
                pin_env_for(self, root, db=backoff_db)
                backoff_session = McpSession(self, cwd=str(root), repo=REPO, db=str(backoff_db), stale_after_s=0, sync_runner=backoff_runner)
                self.addCleanup(backoff_session.stop)
                backoff_session.initialize()
                _result, env = tool_envelope(self, backoff_session.call_tool("graph_status", {"repo": REPO}), "AC3")
                self.assertIs(env.get("ok"), True, f"AC3(c): the stale read must still answer: {env}")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not backoff_runner.calls:
                    time.sleep(0.05)
                self.assertEqual(len(backoff_runner.calls), 1, "AC3(c): the first stale read must start exactly one refresh")
                _wait_jobs_settled(backoff_db)
                # Still inside the 2s window: suppressed.
                backoff_session.call_tool("graph_status", {"repo": REPO})
                time.sleep(0.3)  # grace: an un-suppressed start would land by now
                self.assertEqual(len(backoff_runner.calls), 1, f"AC3(c): with {_REFRESH_BACKOFF_ENV}=2 a read still inside the backoff window must not start another refresh for this store (and once the window elapses, the next read MUST be allowed to start one); started {len(backoff_runner.calls)} times")
                # Expire the window (the expiry seam is the env-configured
                # duration itself): a new refresh must be allowed to start.
                time.sleep(2.5)
                backoff_session.call_tool("graph_status", {"repo": REPO})
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and len(backoff_runner.calls) < 2:
                    time.sleep(0.05)
                self.assertGreaterEqual(len(backoff_runner.calls), 2, "AC3(c): once the backoff window has elapsed a new refresh must be allowed to start -- a permanent backoff would starve the store forever")
                backoff_session.stop()

        # (d) Refreshing honesty (fix-plan v3 CF-B r2-f2): a read that STARTS
        # a refresh and a read that JOINS an in-flight one both report
        # freshness.refreshing:true (the preserving C9 contract — already
        # honored at base, kept here as the contrast); only a
        # backoff-SUPPRESSED read leaves refreshing unset and carries
        # refresh_failed naming the reason. The suppressed half is RED at
        # base: with no backoff the read storms a new refresh and reports
        # refreshing:true instead.
        honesty_db = _seed_scratch_db(root / "honesty.db", lambda src: src.add_issue(issue(1)))
        _force_stale(honesty_db)
        join_runner = _BlockingFailRunner()
        with problems.leg("d: started/joined report refreshing; a suppressed read carries refresh_failed"):
            with _env_backoff("600"):
                pin_env_for(self, root, db=honesty_db)
                honesty_session = McpSession(self, cwd=str(root), repo=REPO, db=str(honesty_db), stale_after_s=0, sync_runner=join_runner)
                self.addCleanup(honesty_session.stop)
                honesty_session.initialize()
                _result, env_started = tool_envelope(self, honesty_session.call_tool("graph_status", {"repo": REPO}), "AC3")
                self.assertIs(env_started.get("ok"), True, f"AC3(d): the first stale read must answer: {env_started}")
                self.assertIs((env_started.get("freshness") or {}).get("refreshing"), True, f"AC3(d): a read that STARTS a refresh must report freshness.refreshing:true; got freshness={env_started.get('freshness')!r}")
                self.assertTrue(join_runner.started.wait(timeout=10), "fixture: the first stale read started the in-flight refresh")
                _result, env_joined = tool_envelope(self, honesty_session.call_tool("graph_status", {"repo": REPO}), "AC3")
                self.assertIs(env_joined.get("ok"), True, f"AC3(d): the joined read must answer: {env_joined}")
                self.assertIs((env_joined.get("freshness") or {}).get("refreshing"), True, f"AC3(d): a read that JOINS the in-flight refresh must ALSO report freshness.refreshing:true (preserving C9); got freshness={env_joined.get('freshness')!r}")
                join_runner.release.set()
                _wait_jobs_settled(honesty_db)
                _result, env_suppressed = tool_envelope(self, honesty_session.call_tool("graph_status", {"repo": REPO}), "AC3")
                self.assertIs(env_suppressed.get("ok"), True, f"AC3(d): the backoff-suppressed read must still answer from current data: {env_suppressed}")
                fresh = env_suppressed.get("freshness") or {}
                self.assertTrue(fresh.get("refreshing") is not True and "refresh_failed" in json.dumps(env_suppressed), f"AC3(d): a backoff-SUPPRESSED read must NOT report refreshing and must carry refresh_failed naming the reason; got freshness={fresh!r}")
                honesty_session.stop()

        self.assertFalse(
            problems,
            "AC3 amend (fix-plan v3 CF-B): a failed background refresh must be isolated from the store, backed off, and honestly reported:\n- " + "\n- ".join(problems),
        )

        # --- Original legs (unchanged) ---------------------------------------
        db_path = _seed_scratch_db(root / "history.db", lambda src: src.add_issue(issue(1)))
        conn = connect(db_path)  # force staleness so every read wants refresh
        conn.execute("UPDATE sync_state SET last_full_sync_at = ?, last_incr_sync_at = ? WHERE repo = ?", ("2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z", REPO))
        conn.commit()
        conn.close()

        pin_env_for(self, root, db=db_path)
        runner = _AlwaysFailRunner()
        session = McpSession(self, cwd=str(root), repo=REPO, db=str(db_path), stale_after_s=0, sync_runner=runner)
        self.addCleanup(session.stop)
        session.initialize()

        def _read() -> dict:
            _result, env = tool_envelope(self, session.call_tool("graph_status", {"repo": REPO}), "AC3")
            return env

        env1 = _read()
        self.assertIs(env1.get("ok"), True, f"AC3: the stale read must still answer: {env1}")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not runner.calls:
            time.sleep(0.05)
        self.assertEqual(len(runner.calls), 1, "AC3: the first stale read must start exactly one refresh")
        deadline = time.monotonic() + 10  # wait for the job to settle (failed)
        while time.monotonic() < deadline:
            jobs = mcp_server._jobs_snapshot(str(db_path))
            if jobs and all(job["state"] != "running" for job in jobs):
                break
            time.sleep(0.05)

        # Reads 2 and 3 inside the backoff window: answered, no new start.
        env2 = _read()
        env3 = _read()
        time.sleep(0.3)  # grace: a buggy start would have appended by now
        self.assertEqual(len(runner.calls), 1, f"AC3: after a FAILED refresh, reads within the backoff window must not start another refresh for this store; started {len(runner.calls)} times")
        for env in (env2, env3):
            self.assertIn("refresh_failed", json.dumps(env), f"AC3: an envelope after a failed refresh must report a refresh_failed reason; got freshness={env.get('freshness')!r}")

        # The previously-complete store is not poisoned by the failed refresh.
        check = connect_readonly_query(db_path)
        try:
            row = check.execute("SELECT last_error, full_sync_pending FROM sync_state WHERE repo = ?", (REPO,)).fetchone()
        finally:
            check.close()
        self.assertIsNone(row["last_error"], f"AC3: a failed background refresh must not write last_error into a previously-complete store; got {row['last_error']!r}")
        self.assertFalse(bool(row["full_sync_pending"]), "AC3: a failed background refresh must not mark the store pending")
        session.stop()


class _RootsProbeSession:
    """One in-process server whose client NEVER offers roots: any server-initiated roots/list request is recorded (and answered with an error result so the base server still finishes the call)."""

    def __init__(self, test: unittest.TestCase, **serve_kwargs) -> None:
        self.test = test
        self.stdin = _QueueInput()
        self.stdout = _QueueOutput()
        self.roots_requests = 0
        self.error: str | None = None
        self._next_id = 500
        self._thread = threading.Thread(target=self._run, kwargs=serve_kwargs, daemon=True, name="roots-probe")
        self._thread.start()

    def _run(self, **serve_kwargs) -> None:
        import traceback

        try:
            mcp_server.serve(self.stdin, self.stdout, **serve_kwargs)
        except BaseException as exc:  # noqa: BLE001
            self.error = "".join(traceback.format_exception(exc, exc, exc.__traceback__))
        finally:
            self.stdout._lines.put(None)

    def _send(self, obj: dict) -> None:
        self.stdin.send_line(json.dumps(obj))

    def _request(self, method: str, params: dict | None):
        req_id = self._next_id
        self._next_id += 1
        frame: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            frame["params"] = params
        self._send(frame)
        return req_id

    def wait_response(self, req_id, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.test.fail(f"AC4: no response with id {req_id!r} before timeout; roots/list requests seen: {self.roots_requests}")
            try:
                line = self.stdout._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                self.test.fail(f"AC4: server exited before answering id {req_id!r}: {self.error}")
            obj = json.loads(line)
            if "method" in obj and "id" in obj:
                # A server-initiated REQUEST. roots/list to a client that never
                # declared the capability is the AC4 violation; the default
                # answer is an error result so the base server still completes
                # (subclasses answer with real roots for the contrast legs).
                if obj["method"] == "roots/list":
                    self.roots_requests += 1
                    self._answer_roots_request(obj["id"])
                continue
            if obj.get("id") == req_id:
                return obj

    def _answer_roots_request(self, request_id) -> None:
        """Default answer for the no-capability probe legs: an error result."""
        self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "no roots"}})

    def initialize_with_capabilities(self, capabilities: dict) -> None:
        req_id = self._request("initialize", {"protocolVersion": "2025-06-18", "capabilities": capabilities, "clientInfo": {"name": "probe", "version": "0"}})
        self.wait_response(req_id)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def initialize_without_roots_capability(self) -> None:
        self.initialize_with_capabilities({})

    def call_graph_status(self) -> dict:
        self.initialize_without_roots_capability()
        req_id = self._request("tools/call", {"name": "graph_status", "arguments": {}})
        return self.wait_response(req_id)

    def stop(self) -> None:
        self.stdin.send_eof()
        self._thread.join(timeout=5)
        if self.error is not None:
            self.test.fail(f"AC4: serve() raised:\n{self.error}")


class _DeclaredRootsSession(_RootsProbeSession):
    """A legacy session that DECLARES capabilities.roots in initialize and
    answers the server's roots/list REQUEST with real directory roots (the
    C4 positive-contrast harness: for a DECLARED capability the roots
    round-trip is legal and its answer must resolve)."""

    def __init__(self, test, roots: list[Path], **serve_kwargs) -> None:
        super().__init__(test, **serve_kwargs)
        self._roots = list(roots)

    def _answer_roots_request(self, request_id) -> None:
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"roots": [{"uri": p.as_uri(), "name": p.name} for p in self._roots]},
            }
        )

    def call_graph_status_declared(self, arguments: dict | None = None) -> dict:
        self.initialize_with_capabilities({"roots": {"listChanged": False}})
        req_id = self._request("tools/call", {"name": "graph_status", "arguments": arguments or {}})
        return self.wait_response(req_id)


class RootsCapabilityTests(unittest.TestCase):
    """AC4: a legacy client that did NOT declare the roots capability never receives a roots/list request; resolution falls to the server pin / cwd or a structured bad_request naming repo."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = _seed_scratch_db(self.root / "history.db", lambda src: src.add_issue(issue(1)))
        pin_env_for(self, self.root)

    def test_no_roots_request_without_capability(self) -> None:
        problems = _Problems()

        # Leg 1: no pin, cwd outside any git checkout -> structured error
        # naming repo, and NO roots/list on the wire.
        session = _RootsProbeSession(self, cwd=str(self.root), stale_after_s=_NO_REFRESH_S)
        self.addCleanup(session.stop)
        with problems.leg("leg1 (no pin, non-git cwd)"):
            response = session.call_graph_status()
            self.assertEqual(session.roots_requests, 0, "the server sent a roots/list request to a client that did NOT declare the roots capability in initialize")
            body = response.get("result") if "result" in response else response.get("error")
            self.assertIsInstance(body, dict, f"the repo-less call must answer a structured result or error; got {response!r}")
            self.assertIn("repo", json.dumps(response), f"the repo-less failure must name the repo argument as the remedy; got {response!r}")
        session.stop()

        # Leg 2 (preserving contrast): the server --repo pin resolves without
        # any roots round-trip.
        session = _RootsProbeSession(self, cwd=str(self.root), repo=REPO, db=str(self.db_path), stale_after_s=_NO_REFRESH_S)
        self.addCleanup(session.stop)
        with problems.leg("leg2 (server --repo pin)"):
            session.call_graph_status()
            self.assertEqual(session.roots_requests, 0, "a pinned repo must never trigger roots/list")
        session.stop()

        # Leg 3: no pin and no repo argument, cwd IS a git checkout -> the cwd
        # origin resolves (no roots round-trip) and the call answers.
        checkout = self.root / "checkout"
        checkout.mkdir()
        git("init", "-b", "main", cwd=checkout)
        git("config", "user.email", "t@example.com", cwd=checkout)
        git("config", "user.name", "Tester", cwd=checkout)
        (checkout / "README.md").write_text("probe\n", encoding="utf-8")
        git("add", "-A", cwd=checkout)
        git("commit", "-m", "init", cwd=checkout)
        git("remote", "add", "origin", f"https://github.com/{REPO}.git", cwd=checkout)
        session = _RootsProbeSession(self, cwd=str(checkout), db=str(self.db_path), stale_after_s=_NO_REFRESH_S)
        self.addCleanup(session.stop)
        with problems.leg("leg3 (git-checkout cwd)"):
            response = session.call_graph_status()
            self.assertEqual(session.roots_requests, 0, "with no roots capability declared, resolution must fall through to the server cwd's git origin instead of asking the client for roots")
            result = response.get("result")
            self.assertIsInstance(result, dict, f"the cwd-resolved call must answer; got {response!r}")
            env = json.loads((result.get("content") or [{}])[0].get("text", "null")) or {}
            self.assertIs(env.get("ok"), True, f"the cwd-resolved read must succeed: {env}")
        session.stop()

        # Leg 4 (POSITIVE CONTRAST, fix-plan v3 CF-C / critic r2-f6): a
        # session that DID declare capabilities.roots is entitled to the
        # roots/list round-trip. Answered with the real checkout root (the
        # git checkout leg 3 built), a repo-less call must resolve THROUGH
        # that root and answer. This leg is deliberately NOT RED at base --
        # it is the positive contrast proving the capability-aware fix keeps
        # the declared-capability path resolving while legs 1 and 3 stay the
        # negative evidence.
        session = _DeclaredRootsSession(self, roots=[checkout], cwd=str(self.root), db=str(self.db_path), stale_after_s=_NO_REFRESH_S)
        self.addCleanup(session.stop)
        with problems.leg("leg4 (declared roots, positive contrast)"):
            response = session.call_graph_status_declared()
            self.assertEqual(session.roots_requests, 1, "a DECLARED roots capability earns exactly one roots/list round-trip (srv-* id), answered with the real root")
            result = response.get("result")
            self.assertIsInstance(result, dict, f"the declared-roots call must answer; got {response!r}")
            env = json.loads((result.get("content") or [{}])[0].get("text", "null")) or {}
            self.assertIs(env.get("ok"), True, f"resolution through the declared root must succeed (the repo argument is absent; the root's git origin names the store): {env}")
            self.assertEqual(env.get("repo"), REPO, f"the resolved repo must come from the declared root's checkout origin; got {env.get('repo')!r}")
        session.stop()

        # Leg 5 (contrast pair to leg 4): the explicit repo argument keeps
        # precedence over roots — no roots/list on the wire even though the
        # capability IS declared, and the call answers through the argument.
        session = _DeclaredRootsSession(self, roots=[checkout], cwd=str(self.root), db=str(self.db_path), stale_after_s=_NO_REFRESH_S)
        self.addCleanup(session.stop)
        with problems.leg("leg5 (explicit repo argument precedence, no roots request)"):
            response = session.call_graph_status_declared({"repo": REPO})
            self.assertEqual(session.roots_requests, 0, "an explicit repo argument must resolve without any roots round-trip, even for a session that declared the roots capability")
            result = response.get("result")
            self.assertIsInstance(result, dict, f"the explicit-repo call must answer; got {response!r}")
            env = json.loads((result.get("content") or [{}])[0].get("text", "null")) or {}
            self.assertIs(env.get("ok"), True, f"the explicit-repo read must succeed: {env}")
        session.stop()

        self.assertFalse(problems, "AC4: roots/list must never be sent to a client that did not declare the roots capability:\n- " + "\n- ".join(problems))


class ItemBoundingTests(unittest.TestCase):
    """AC5: CLI item and MCP get_item with DEFAULTS return a bounded body/comments/reviews, report comments_omitted/reviews_omitted, and set truncated:true when anything was cut."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

        def seed(src: FakeGitHubSource) -> None:
            src.add_pr(issue(7, title="big pr", body="x" * 100_000, kind="pr", state="open", comments=200), pull(7, changed_files=1, merged=True), files=[pr_file("src/big.py")])
            src.issue_comments[7] = [comment(600_000 + i, f"comment number {i}") for i in range(200)]
            src.issues[7]["comments"] = 200
            src.reviews[7] = [review(700_000 + i, "APPROVED", body="ok") for i in range(120)]

        self.db_path = _seed_scratch_db(self.root / "history.db", seed)

    def test_item_is_bounded_by_default(self) -> None:
        problems = _Problems()

        with problems.leg("CLI item"):
            code, out, err = run_cli(["item", "7", "--repo", REPO, "--db", str(self.db_path), "--format", "json"])
            self.assertEqual(code, 0, f"item must succeed: {err}")
            env = json.loads(out)
            data = env["data"]
            self.assertIn("comments_omitted", data, f"the default-bounded item payload must report comments_omitted; data keys are {sorted(data)}")
            self.assertGreater(int(data["comments_omitted"]), 0, "200 stored comments must trip the default bound")
            self.assertIn("reviews_omitted", data, f"the payload must report reviews_omitted; keys {sorted(data)}")
            self.assertGreater(int(data["reviews_omitted"]), 0, "120 stored reviews must trip the default bound")
            self.assertLess(len(data["comments"]), 200, "the returned comment list must be bounded")
            self.assertLess(len(data["reviews"]), 120, "the returned review list must be bounded")
            self.assertLess(len(data.get("body") or ""), 100_000, "the returned body must be bounded")
            self.assertIs(env.get("truncated"), True, f"the envelope must set truncated:true when content was cut; got {env.get('truncated')!r}")

        with problems.leg("MCP get_item"):
            pin_env_for(self, self.root, db=self.db_path)
            session = McpSession(self, cwd=str(self.root), repo=REPO, db=str(self.db_path), stale_after_s=_NO_REFRESH_S)
            self.addCleanup(session.stop)
            session.initialize()
            _result, env = tool_envelope(self, session.call_tool("get_item", {"repo": REPO, "number": 7}), "AC5")
            data = env.get("data") or {}
            self.assertIn("comments_omitted", data, f"MCP get_item defaults must bound and report comments_omitted; data keys are {sorted(data)}")
            self.assertGreater(int(data["comments_omitted"]), 0, "MCP get_item must omit comments under defaults")
            self.assertIn("reviews_omitted", data, f"MCP get_item must report reviews_omitted; keys {sorted(data)}")
            self.assertLess(len(data.get("comments") or []), 200, "MCP get_item comment list must be bounded")
            self.assertLess(len(data.get("reviews") or []), 120, "MCP get_item review list must be bounded")
            session.stop()

        self.assertFalse(problems, "AC5: item/get_item must be bounded by default and say what was cut:\n- " + "\n- ".join(problems))


class BotExclusionTests(unittest.TestCase):
    """AC6 (--no-bots CLI / exclude_bots MCP): item/search/brief omit content authored by actors with is_bot=1 and report the omission count."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

        def seed(src: FakeGitHubSource) -> None:
            src.add_issue(issue(5, title="deps", body="bump", state="open"))
            src.comment_on(5, "bump zyzzyxneedle from 1 to 2", author="dependabot[bot]")
            src.comment_on(5, "please review the bump")
            src.add_issue(issue(6, title="real work", body="mentions zyzzyxneedle", state="open"))

        self.db_path = _seed_scratch_db(self.root / "history.db", seed)
        conn = connect(self.db_path)  # pin the bot flag deterministically (AC seeds actors.is_bot=1)
        conn.execute("UPDATE actors SET is_bot = 1 WHERE login = 'dependabot[bot]'")
        conn.commit()
        conn.close()

    def test_reads_can_exclude_bots(self) -> None:
        problems = _Problems()
        base = ["--repo", REPO, "--db", str(self.db_path), "--format", "json"]

        with problems.leg("CLI item"):
            code, out, err = run_cli(["item", "5", "--no-bots", *base])
            self.assertEqual(code, 0, f"`item N --no-bots` must exist and succeed (exit {code}: {err.strip().splitlines()[:1]}); bot content must be excludable")
            data = json.loads(out)["data"]
            authors = [c.get("author") for c in data.get("comments") or []]
            self.assertFalse(any(str(a or "").endswith("[bot]") for a in authors), f"--no-bots must omit bot-authored comments; comment authors are {authors}")
            omission = next((data[k] for k in ("bots_omitted", "omitted_bots", "bot_comments_omitted") if isinstance(data.get(k), int)), None)
            self.assertIsNotNone(omission, f"--no-bots must report the omission count (a bots_omitted-style integer); data keys are {sorted(data)}")
            self.assertGreater(omission, 0, "one bot comment was omitted")

        with problems.leg("CLI search"):
            code, out, err = run_cli(["search", "zyzzyxneedle", "--no-bots", *base])
            self.assertEqual(code, 0, f"`search --no-bots` must succeed: {err}")
            numbers = [i["number"] for i in json.loads(out)["data"]["items"]]
            self.assertNotIn(5, numbers, "an item matched ONLY by a bot comment must be omitted under --no-bots")
            self.assertIn(6, numbers, "human-authored matches must stay under --no-bots")

        with problems.leg("CLI brief"):
            code, out, err = run_cli(["brief", "5", "--no-bots", *base])
            self.assertEqual(code, 0, f"`brief N --no-bots` must succeed: {err}")
            self.assertNotIn("dependabot", out, "--no-bots must omit bot-authored content from the brief packet")

        with problems.leg("MCP get_item/search"):
            pin_env_for(self, self.root, db=self.db_path)
            session = McpSession(self, cwd=str(self.root), repo=REPO, db=str(self.db_path), stale_after_s=_NO_REFRESH_S)
            self.addCleanup(session.stop)
            session.initialize()
            _result, env = tool_envelope(self, session.call_tool("get_item", {"repo": REPO, "number": 5, "exclude_bots": True}), "AC6")
            authors = [c.get("author") for c in (env.get("data") or {}).get("comments") or []]
            self.assertFalse(any(str(a or "").endswith("[bot]") for a in authors), f"MCP get_item(exclude_bots=true) must omit bot comments; authors are {authors}")
            _result, env = tool_envelope(self, session.call_tool("search", {"repo": REPO, "query": "zyzzyxneedle", "exclude_bots": True}), "AC6")
            numbers = [i["number"] for i in (env.get("data") or {}).get("items") or []]
            self.assertNotIn(5, numbers, "MCP search(exclude_bots=true) must not match through a bot comment")
            session.stop()

        self.assertFalse(problems, "AC6: reads must be able to exclude bot-authored content and report the omission:\n- " + "\n- ".join(problems))


class RelatedBoundingTests(unittest.TestCase):
    """AC7: related with a node/byte budget stops expanding and sets truncated:true."""

    def test_related_is_bounded(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)

        def seed(src: FakeGitHubSource) -> None:
            # One hub issue mentioning 100 stored neighbor issues: the one-hop
            # neighborhood is 101 item nodes joined by mention edges.
            neighbors = range(100, 200)
            src.add_issue(issue(8, title="hub", state="open", body="related " + " ".join(f"#{n}" for n in neighbors)))
            for n in neighbors:
                src.add_issue(issue(n, title=f"neighbor {n}", state="open"))

        db_path = _seed_scratch_db(root / "history.db", seed)
        code, out, err = run_cli(["related", "8", "--repo", REPO, "--db", str(db_path), "--format", "json"])
        self.assertEqual(code, 0, f"AC7: related must succeed: {err}")
        env = json.loads(out)
        data = env["data"]
        nodes = data.get("nodes") or []
        # The unbounded neighborhood is 101 item nodes (hub + 100 mentions).
        self.assertGreaterEqual(len(nodes), 2, f"AC7: fixture sanity - related must return the hub neighborhood; got {len(nodes)} nodes (mention edges missing?)")
        self.assertTrue(env.get("truncated") or data.get("truncated"), f"AC7: a {len(nodes)}-node neighborhood under the default budget must set truncated:true; envelope truncated={env.get('truncated')!r}, data keys {sorted(data)}")
        self.assertLess(len(nodes), 101, f"AC7: related returned all {len(nodes)} nodes -- a node/byte budget must stop the expansion early")


class BrokenPipeQuietTests(unittest.TestCase):
    """AC10 (new check C24, fix-plan v3 CF-M): piping any command into a
    closed pipe exits quietly — no BrokenPipeError traceback on stderr, no
    unraisable-at-shutdown output, exit 0. RED at base: the interpreter's
    shutdown flush of the dead stdout prints 'Exception ignored in ...' and
    exits 120 (Windows shape verified: OSError [Errno 22])."""

    def test_broken_pipe_is_quiet(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        db_path = _seed_scratch_db(root / "history.db", lambda src: src.add_issue(issue(1, title="pipe probe", body="needle")))

        src_dir = str(Path(__file__).resolve().parents[1] / "src")
        env = dict(os.environ)
        env["PYTHONPATH"] = src_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["ZAXBYGRAPH_HOME"] = str(root / "zhome")
        env["ZAXBYGRAPH_DB"] = str(db_path)

        with tempfile.TemporaryFile() as err_file:
            proc = subprocess.Popen(
                [sys.executable, "-m", "zaxbygraph", "status", "--repo", REPO, "--db", str(db_path), "--format", "json"],
                stdout=subprocess.PIPE,
                stderr=err_file,
                stdin=subprocess.DEVNULL,
                env=env,
                cwd=str(root),
            )
            # The reader goes away immediately — what `| head` does after it
            # exits: every child write now lands on a broken pipe.
            proc.stdout.close()
            try:
                returncode = proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
                self.fail("C24: the CLI never exited after its stdout reader went away")
            err_file.seek(0)
            stderr_text = err_file.read().decode("utf-8", "replace")

        noisy = [token for token in ("Traceback", "BrokenPipeError", "Exception ignored", "OSError") if token in stderr_text]
        self.assertFalse(noisy, f"C24: piping a command into a closed pipe must exit quietly — stderr carries {noisy} (a BrokenPipeError traceback and/or the interpreter's unraisable-at-shutdown output must never print):\n{stderr_text[:600]}")
        self.assertEqual(returncode, 0, f"C24: a broken pipe is the quiet success exit (the reader got what it wanted, as with `| head`), never a traceback/shutdown-failure exit; got exit {returncode} with stderr:\n{stderr_text[:600]}")


if __name__ == "__main__":
    unittest.main()
