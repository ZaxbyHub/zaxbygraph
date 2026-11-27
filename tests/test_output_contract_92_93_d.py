"""Acceptance checks for issue-trace 92-93-output-contract-honesty
(issues #92+#93, decision D4 / AC4: MCP failure freshness and identity,
amended after plan-critic round 1).

Companion to tests/test_output_contract_92_93.py (D1-D3),
tests/test_output_contract_92_93_b.py (D5/D6/D7/D9),
tests/test_output_contract_92_93_c.py (D8) and the shared
tests/test_output_contract_92_93_e.py helpers.

Decisions pinned here (D4):

- An MCP tool failure after the store opened (get_item 99999 not_found,
  file_history unknown_path, sql bad statement) carries the store's REAL
  freshness on the SAME fresh store (synced_at equal to a success
  envelope's, age_s int, complete true) -- on LEGACY and MODERN protocol
  calls alike; a no-corpus failure keeps the null block (PRESERVING).
- No stale state: a request that fails BEFORE target resolution after an
  earlier successful request on the same session reports null freshness
  and no stale repo/db identity; two independent McpSession objects
  against different stores never see each other's identity/freshness on
  failure.
- BOTH generic-exception arms (the tools/call dispatch arm and the outer
  process arm) keep db/repo identity and real freshness when the handler
  resolved/opened the store.

Hermetic: FakeGitHubSource, tempfile stores, in-process McpSessions; no
network, no gh, no writes outside temp roots. Stdlib only; unittest.
Every failure message carries the distinctive single-line tag OC-AC4.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import zaxbygraph
import os

_WORKTREE = Path(__file__).resolve().parents[1]
if not Path(zaxbygraph.__file__).resolve().is_relative_to(_WORKTREE / "src"):
    raise AssertionError(
        f"OC-ENV: zaxbygraph resolved to {zaxbygraph.__file__}, not this"
        f" worktree's src ({_WORKTREE / 'src'}); set"
        f" PYTHONPATH=<worktree>/src{os.pathsep}<worktree>/tests"
    )

from fixtures import REPO  # noqa: E402
from test_mcp import McpSession, pin_env_for, tool_envelope  # noqa: E402
from test_output_contract_92_93_e import seed_corpus  # noqa: E402

_NO_REFRESH_S = 10**9
_OTHER_SLUG = "acme/other-store"
_MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _stamp_sync(db_path: Path, stamp: str) -> None:
    """Pin one store's sync stamps to a fixed instant (freshness values the
    two-session leg can tell apart by value)."""
    from zaxbygraph.db import connect

    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE sync_state SET last_full_sync_at = ?, last_incr_sync_at = ?",
            (stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()


class TestAc4McpFailureFreshness(unittest.TestCase):
    """D4 (AC4): MCP failures carry the opened store's real freshness with
    no cross-call or cross-session state."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db = self.root / "history.db"
        seed_corpus(self.db)
        self.other_db = self.root / "other.db"
        seed_corpus(self.other_db, _OTHER_SLUG)
        # DISTINCT fixed sync stamps per store (critic round 2, finding 3):
        # a leaked envelope is detectable by VALUE, not just by shape.
        _stamp_sync(self.db, "2026-01-01T12:00:00Z")
        _stamp_sync(self.other_db, "2025-06-01T08:30:00Z")
        pin_env_for(self, self.root, db=self.db)

        class _NoopRunner:
            def __call__(self, db_path, repo) -> None:
                return None

        self._noop = _NoopRunner
        self.session = McpSession(
            self, cwd=str(self.root), repo=REPO, db=str(self.db),
            stale_after_s=_NO_REFRESH_S, sync_runner=self._noop(),
        )
        self.addCleanup(self.session.stop)
        self.session.initialize()

    def _reference_synced_at(self, session, repo: str) -> str:
        result, env = tool_envelope(self, session.call_tool("graph_status", {"repo": repo}), "OC-AC4")
        self.assertIs(env.get("ok"), True, f"OC-AC4: the reference read must succeed: {env.get('error')!r}")
        fresh = env.get("freshness") or {}
        self.assertIsInstance(
            fresh.get("synced_at"), str, "OC-AC4: the reference freshness must be real"
        )
        return fresh["synced_at"]

    def _assert_real_freshness(self, env: dict, label: str, synced_at: str, problems: list) -> None:
        fresh = env.get("freshness") or {}
        if fresh.get("synced_at") != synced_at:
            problems.append(
                f"{label}: freshness.synced_at {fresh.get('synced_at')!r}, want the store's real {synced_at!r}"
            )
        if not isinstance(fresh.get("age_s"), int):
            problems.append(f"{label}: freshness.age_s must be an int, got {fresh.get('age_s')!r}")
        if fresh.get("complete") is not True:
            problems.append(f"{label}: freshness.complete must be true, got {fresh.get('complete')!r}")

    def test_ac4_mcp_failure_freshness(self) -> None:
        synced_at = self._reference_synced_at(self.session, REPO)
        problems: list[str] = []
        # (tool, arguments, meta, expected codes, note) -- post-open
        # failures on the SAME fresh store, legacy era.
        legs = [
            ("get_item", {"repo": REPO, "number": 99999}, None, ("not_found",), "ghost number"),
            ("file_history", {"repo": REPO, "path": "src/nothing-here.py"}, None, ("unknown_path",), "unknown path"),
            ("sql", {"repo": REPO, "statement": "SELECT * FROM nope_table"}, None, ("runtime", "no_such_table"), "bad statement"),
            # (d) the same post-open failure through a MODERN protocol call.
            ("get_item", {"repo": REPO, "number": 99999}, _MODERN_META, ("not_found",), "modern-era ghost number"),
        ]
        for tool, arguments, meta, want_codes, note in legs:
            params = {"name": tool, "arguments": arguments}
            if meta is not None:
                params["_meta"] = meta
            response = self.session.wait_response(self.session.request("tools/call", params))
            result, env = tool_envelope(self, response, "OC-AC4")
            error = env.get("error") or {}
            if env.get("ok") is not False:
                problems.append(f"{note}: ok {env.get('ok')!r}")
                continue
            if error.get("code") not in want_codes:
                problems.append(f"{note}: error.code {error.get('code')!r}, want one of {want_codes}")
            self._assert_real_freshness(env, note, synced_at, problems)
        # PRESERVING: a no-corpus failure keeps the null block.
        result, env = tool_envelope(
            self, self.session.call_tool("graph_status", {"repo": "acme/nocorpus"}), "OC-AC4"
        )
        self.assertIs(env.get("ok"), False, "OC-AC4: the no-corpus call must fail")
        fresh = env.get("freshness") or {}
        self.assertIsNone(
            fresh.get("synced_at"),
            "OC-AC4: the no-corpus failure keeps the null freshness block (PRESERVING)",
        )
        # (a) A request that fails BEFORE target resolution, after an
        # earlier successful request on the SAME session: no stale repo/db
        # identity, null freshness.
        result, env = tool_envelope(
            self, self.session.call_tool("graph_status", {"repo": "NOT A SLUG"}), "OC-AC4"
        )
        self.assertIs(env.get("ok"), False, "OC-AC4: the bad-slug call must fail")
        self.assertIsNone(
            env.get("db"),
            f"OC-AC4: a pre-resolution failure must not carry the previous"
            f" call's db identity, got {env.get('db')!r}",
        )
        self.assertIsNone(
            env.get("repo"),
            f"OC-AC4: a pre-resolution failure must not carry the previous"
            f" call's repo identity, got {env.get('repo')!r}",
        )
        fresh = env.get("freshness") or {}
        if fresh.get("synced_at") is not None or fresh.get("age_s") is not None or fresh.get("complete") is not False:
            problems.append(f"pre-resolution failure: freshness must be the null block, got {fresh!r}")
        self.assertFalse(
            problems,
            "OC-AC4: post-open MCP failures must carry the store's real"
            " freshness (legacy AND modern):\n- " + "\n- ".join(problems),
        )

    def test_ac4_two_sessions_and_generic_arms(self) -> None:
        problems: list[str] = []
        synced_at = self._reference_synced_at(self.session, REPO)
        # (b) Two independent sessions against DIFFERENT stores: a failure
        # on session B must carry B's own identity/freshness, never A's.
        pin_env_for(self, self.root, db=self.other_db)
        session_b = McpSession(
            self, cwd=str(self.root), repo=_OTHER_SLUG, db=str(self.other_db),
            stale_after_s=_NO_REFRESH_S, sync_runner=self._noop(),
        )
        self.addCleanup(session_b.stop)
        session_b.initialize()
        other_synced_at = self._reference_synced_at(session_b, _OTHER_SLUG)
        result, env_b = tool_envelope(
            self, session_b.call_tool("get_item", {"repo": _OTHER_SLUG, "number": 99999}), "OC-AC4"
        )
        self.assertIs(env_b.get("ok"), False, "OC-AC4: session B's ghost read must fail")
        if env_b.get("db") != str(self.other_db):
            problems.append(
                f"session B: db identity {env_b.get('db')!r}, want its own store {str(self.other_db)!r}"
            )
        if env_b.get("repo") != _OTHER_SLUG:
            problems.append(f"session B: repo identity {env_b.get('repo')!r}, want {_OTHER_SLUG!r}")
        if env_b.get("db") == str(self.db) or env_b.get("repo") == REPO:
            problems.append("session B: the failure leaked session A's store identity")
        self._assert_real_freshness(env_b, "session B ghost", other_synced_at, problems)

        # (c) Both generic-exception arms keep db/repo identity and real
        # freshness after the handler resolved/opened the store.
        import zaxbygraph.mcp_server as mcp_server

        # Arm 1 -- the tools/call dispatch-level generic arm: a handler that
        # resolves and opens the store, then raises RuntimeError.
        saved_handler = mcp_server._TOOL_HANDLERS["graph_status"]

        def _boom_after_open(arguments, ctx) -> None:
            slug, db_path = mcp_server._resolve_target(arguments, ctx)
            conn = mcp_server._open_corpus(slug, db_path)
            conn.close()
            raise RuntimeError("boom after open")

        mcp_server._TOOL_HANDLERS["graph_status"] = _boom_after_open
        try:
            result, env = tool_envelope(
                self, self.session.call_tool("graph_status", {"repo": REPO}), "OC-AC4"
            )
        finally:
            mcp_server._TOOL_HANDLERS["graph_status"] = saved_handler
        self.assertIs(env.get("ok"), False, "OC-AC4: the dispatch-arm fault must answer ok:false")
        self.assertEqual(
            (env.get("error") or {}).get("code"),
            "runtime",
            "OC-AC4: the dispatch-level generic arm codes 'runtime'",
        )
        if env.get("db") != str(self.db):
            problems.append(f"dispatch arm: db identity lost ({env.get('db')!r})")
        if env.get("repo") != REPO:
            problems.append(f"dispatch arm: repo identity lost ({env.get('repo')!r})")
        self._assert_real_freshness(env, "dispatch arm", synced_at, problems)

        # Arm 2 -- the outer _process generic arm: the handler SUCCEEDS but
        # the result write raises ONCE, so the fault escapes _dispatch and
        # the outer arm must answer from the recorded per-call state.
        pin_env_for(self, self.root, db=self.db)
        real_tool_result = mcp_server._tool_result
        state = {"armed": True}

        def _flaky_tool_result(stdout, req_id, env, modern):
            if state["armed"]:
                state["armed"] = False
                raise RuntimeError("result-write boom")
            return real_tool_result(stdout, req_id, env, modern)

        mcp_server._tool_result = _flaky_tool_result
        try:
            result, env = tool_envelope(
                self, self.session.call_tool("graph_status", {"repo": REPO}), "OC-AC4"
            )
        finally:
            mcp_server._tool_result = real_tool_result
        self.assertIs(env.get("ok"), False, "OC-AC4: the outer-arm fault must answer ok:false")
        self.assertEqual(
            (env.get("error") or {}).get("code"),
            "runtime",
            "OC-AC4: the outer generic arm codes 'runtime'",
        )
        if env.get("db") != str(self.db):
            problems.append(f"outer arm: db identity lost ({env.get('db')!r})")
        if env.get("repo") != REPO:
            problems.append(f"outer arm: repo identity lost ({env.get('repo')!r})")
        self._assert_real_freshness(env, "outer arm", synced_at, problems)
        # Reopen rejection (critic round 2, finding 3): with any SECOND open
        # of the store patched to raise, the failing tool call opens the
        # store exactly once and still reports the ORIGINAL freshness.
        import zaxbygraph.mcp_server as mcp_server  # noqa: F811 (same module)

        real_open = mcp_server.open_existing
        calls = {"n": 0}

        def _counting_open(path):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("second open refused")
            return real_open(path)

        mcp_server.open_existing = _counting_open
        try:
            result, env = tool_envelope(
                self, self.session.call_tool("get_item", {"repo": REPO, "number": 99999}), "OC-AC4"
            )
        finally:
            mcp_server.open_existing = real_open
        self.assertEqual(
            calls["n"], 1,
            f"OC-AC4: a failing tool call must open the store exactly once (no"
            f" reopen to stamp freshness); got {calls['n']} opens",
        )
        self.assertIs(env.get("ok"), False, "OC-AC4: the reopen-rejection leg must still fail")
        self._assert_real_freshness(env, "reopen rejection", synced_at, problems)

        # The session must have survived both armed faults.
        result, env = tool_envelope(
            self, self.session.call_tool("graph_status", {"repo": REPO}), "OC-AC4"
        )
        self.assertIs(
            env.get("ok"), True, f"OC-AC4: the session must survive the armed faults: {env.get('error')!r}"
        )
        self.assertFalse(
            problems,
            "OC-AC4: MCP failure identity/freshness must be per-call and"
            " per-store (both generic arms, two sessions):\n- " + "\n- ".join(problems),
        )

    def test_ac4_concurrent_two_sessions_no_cross_leak(self) -> None:
        # ROUND 3 (finding 6): barrier-controlled CONCURRENT two-session
        # check. Two threads, each with its OWN store and its OWN McpSession,
        # run a failing post-open get_item at the same time. The patched
        # _open_corpus releases both threads only once each session has
        # opened its own store, so both per-call records exist before
        # either failure envelope is emitted — PROCESS-GLOBAL per-call
        # state then deterministically cross-leaks (at least one envelope
        # carries the other session's identity/freshness) and must fail.
        import threading

        import zaxbygraph.mcp_server as mcp_server

        stores = (
            (REPO, self.db, "2026-01-01T12:00:00Z"),
            (_OTHER_SLUG, self.other_db, "2025-06-01T08:30:00Z"),
        )
        ready = threading.Barrier(2, timeout=60)
        opened = threading.Barrier(2, timeout=60)
        envelopes: dict[str, dict] = {}
        worker_errors: dict[str, str] = {}
        real_corpus = mcp_server._open_corpus

        def _synced_open(slug: str, db_path):
            conn = real_corpus(slug, db_path)
            opened.wait()  # both sessions hold their store open at once
            return conn

        def _worker(slug: str, db_path) -> None:
            session = McpSession(
                self, cwd=str(self.root), repo=slug, db=str(db_path),
                stale_after_s=_NO_REFRESH_S, sync_runner=self._noop(),
            )
            try:
                session.initialize()
                ready.wait()  # both sessions are initialized
                _result, env = tool_envelope(
                    self, session.call_tool("get_item", {"repo": slug, "number": 99999}), "OC-AC4"
                )
                envelopes[slug] = env
            except Exception as exc:  # recorded; asserted on the main thread
                worker_errors[slug] = f"{type(exc).__name__}: {exc}"
            finally:
                session.stop()

        mcp_server._open_corpus = _synced_open
        threads = []
        try:
            for slug, db_path, _stamp in stores:
                thread = threading.Thread(target=_worker, args=(slug, db_path), daemon=True)
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join(timeout=120)
        finally:
            mcp_server._open_corpus = real_corpus
        problems: list[str] = []
        for slug, worker_error in worker_errors.items():
            problems.append(f"worker {slug}: the concurrent leg itself must run: {worker_error}")
        for thread in threads:
            if thread.is_alive():
                problems.append("a concurrent worker thread did not finish (deadlock?)")
        for slug, db_path, stamp in stores:
            env = envelopes.get(slug)
            if env is None:
                problems.append(f"{slug}: no failure envelope collected")
                continue
            if env.get("ok") is not False:
                problems.append(f"{slug}: the concurrent ghost read must fail; ok {env.get('ok')!r}")
            if env.get("db") != str(db_path):
                problems.append(
                    f"{slug}: the concurrent failure must carry its OWN db identity"
                    f" {str(db_path)!r}; got {env.get('db')!r} (cross-leak or loss)"
                )
            if env.get("repo") != slug:
                problems.append(
                    f"{slug}: the concurrent failure must carry its OWN repo identity;"
                    f" got {env.get('repo')!r}"
                )
            fresh = env.get("freshness") or {}
            if fresh.get("synced_at") != stamp:
                problems.append(
                    f"{slug}: the concurrent failure must carry its OWN store's"
                    f" real synced_at {stamp!r}; got {fresh.get('synced_at')!r}"
                    f" (a process-global per-call state mutant must fail here)"
                )
            if fresh.get("complete") is not True:
                problems.append(f"{slug}: the concurrent failure's freshness.complete must be true")
            if not isinstance(fresh.get("age_s"), int):
                problems.append(f"{slug}: the concurrent failure's freshness.age_s must be an int")
        self.assertFalse(
            problems,
            "OC-AC4: two CONCURRENT sessions on different stores must each"
            " report their OWN identity and real freshness on a post-open"
            " failure (process-global per-call state must fail):\n- "
            + "\n- ".join(problems),
        )


if __name__ == "__main__":
    unittest.main()
