"""Feedback-round regression tests for the issue #7 review findings.

Covers the behaviors the frozen acceptance checks do not exercise:
cursor paging on capped list tools, sql execution errors answering as the
envelope error object, the sql tool's authorizer layer, the staleness
refresh on the sql read, pipelined client requests surviving the roots
handshake, pr_overlap duplicate-number handling, and UTF-8 stdin decoding
under a non-UTF-8 Windows locale (subprocess).

This module is deliberately separate from the frozen tests/test_mcp.py
(checkpoint-byte-locked) and reuses its harness helpers.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, TempDBTest, issue, pr_file, pull, scrubbed_env
from test_mcp import (
    McpSession,
    _BlockingRunner,
    pin_env_for,
    seed_small_db,
    tool_envelope,
)
from test_paths import StoreHarness, git
from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _seed_searchable(src, n: int) -> None:
    for i in range(1, n + 1):
        src.add_issue(issue(100 + i, title=f"paging probe {i:02d}", body="needle"))


class McpCursorTests(TempDBTest):
    """Review finding 1: capped list tools must emit next_cursor whenever
    the envelope reports truncated."""

    def test_search_and_file_history_page_forward(self) -> None:
        _seed_searchable(self.src, 25)
        self.sync()
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        session = McpSession(self, cwd=str(self._td.name))
        self.addCleanup(session.stop)
        session.initialize()

        response = session.call_tool("search", {"query": "needle", "repo": REPO})
        _result, env = tool_envelope(self, response, "FB1")
        self.assertIs(env.get("ok"), True, f"FB1: page 1 must answer: {env}")
        items = env["data"]["items"]
        self.assertEqual(len(items), 20, "FB1: page 1 is limited to 20")
        cursor = env["data"].get("next_cursor")
        self.assertIsInstance(cursor, str, "FB1: a truncated page must carry next_cursor")
        self.assertEqual(cursor, "20")

        response = session.call_tool("search", {"query": "needle", "cursor": cursor, "repo": REPO})
        _result, env = tool_envelope(self, response, "FB1")
        items2 = env["data"]["items"]
        self.assertEqual(len(items2), 5, "FB1: page 2 carries the remainder")
        self.assertNotEqual(
            {i["number"] for i in items},
            {i["number"] for i in items2},
            "FB1: page 2 must not repeat page 1",
        )
        self.assertIsNone(env["data"].get("next_cursor"), "FB1: the last page has no cursor")
        session.stop()

    def test_file_history_pages_forward(self) -> None:
        for i in (1, 2, 3):
            self.src.add_pr(
                issue(20 + i, title=f"hist {i}", body="f", kind="pr", state="closed",
                      updated_at=f"2026-03-0{i}T00:00:00Z"),
                pull(20 + i, merged=True),
                files=[pr_file("src/page.py")],
            )
        self.sync()
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        session = McpSession(self, cwd=str(self._td.name))
        self.addCleanup(session.stop)
        session.initialize()

        response = session.call_tool(
            "file_history", {"path": "src/page.py", "limit": 2, "repo": REPO}
        )
        _result, env = tool_envelope(self, response, "FB1")
        entries = env["data"]["entries"]
        self.assertEqual([e["number"] for e in entries], [23, 22], "FB1: newest first")
        self.assertIs(env.get("truncated"), True, "FB1: page 1 is truncated")
        cursor = env["data"].get("next_cursor")
        self.assertEqual(cursor, "2", "FB1: cursor names the next offset")

        response = session.call_tool(
            "file_history",
            {"path": "src/page.py", "limit": 2, "cursor": cursor, "repo": REPO},
        )
        _result, env = tool_envelope(self, response, "FB1")
        entries2 = env["data"]["entries"]
        self.assertEqual([e["number"] for e in entries2], [21], "FB1: page 2 is the remainder")
        self.assertIsNone(env["data"].get("next_cursor"), "FB1: the last page has no cursor")
        session.stop()


class McpSqlErrorTests(TempDBTest):
    """Review finding 2 + authorizer-layer coverage: execution-time sql
    failures and authorizer denials answer as the envelope error object."""

    def test_runtime_sql_error_is_structured_not_internal(self) -> None:
        seed_small_db(self.db_path)
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        session = McpSession(self, cwd=str(self._td.name))
        self.addCleanup(session.stop)
        session.initialize()

        response = session.call_tool(
            "sql", {"repo": REPO, "statement": "SELECT no_such_col FROM items"}
        )
        result, env = tool_envelope(self, response, "FB2")
        self.assertIs(result.get("isError"), True, "FB2: a runtime sql error sets isError")
        self.assertIs(env.get("ok"), False, "FB2: the envelope reports ok false")
        error = env.get("error")
        self.assertIsInstance(error, dict, "FB2: the envelope carries an error object")
        self.assertEqual(error.get("code"), "runtime", f"FB2: got {error.get('code')!r}")
        self.assertTrue(str(error.get("message", "")).strip(), "FB2: message non-empty")
        self.assertTrue(str(error.get("hint", "")).strip(), "FB2: hint non-empty")
        # The session survives.
        response = session.call_tool(
            "sql", {"repo": REPO, "statement": "SELECT COUNT(*) AS c FROM items"}
        )
        result, env = tool_envelope(self, response, "FB2")
        self.assertIs(env.get("ok"), True, f"FB2: the session must survive: {env}")
        session.stop()

    def test_authorizer_denial_is_structured(self) -> None:
        """The second guard layer (connect_readonly_query's authorizer) is
        what actually executes the statement; drive it through the tool."""
        seed_small_db(self.db_path)
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        session = McpSession(self, cwd=str(self._td.name))
        self.addCleanup(session.stop)
        session.initialize()

        response = session.call_tool(
            "sql",
            {"repo": REPO, "statement": "SELECT load_extension('whatever')"},
        )
        result, env = tool_envelope(self, response, "FB2")
        self.assertIs(result.get("isError"), True, "FB2: an authorizer denial sets isError")
        self.assertIs(env.get("ok"), False, "FB2: the envelope reports ok false")
        self.assertIsInstance(env.get("error"), dict, "FB2: a structured error object")
        session.stop()


class McpSqlRefreshTests(TempDBTest):
    """Review finding 3: a stale sql read answers immediately AND takes
    the same one-locked background refresh path as every other read."""

    def test_stale_sql_read_refreshes_in_background(self) -> None:
        self.src.add_issue(issue(1, title="one", body="x"))
        self.sync()
        self.conn.execute(
            "UPDATE sync_state SET last_full_sync_at = ?, last_incr_sync_at = ?"
            " WHERE repo = ?",
            ("2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z", REPO),
        )
        self.conn.commit()
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        runner = _BlockingRunner()
        session = McpSession(
            self, cwd=str(self._td.name), stale_after_s=0, sync_runner=runner
        )
        self.addCleanup(session.stop)
        session.initialize()

        response = session.call_tool(
            "sql", {"repo": REPO, "statement": "SELECT COUNT(*) AS c FROM items"}
        )
        result, env = tool_envelope(self, response, "FB3")
        self.assertIs(env.get("ok"), True, f"FB3: the stale sql read answers: {env}")
        fresh = env.get("freshness")
        self.assertIsInstance(fresh, dict, "FB3: the envelope carries freshness")
        self.assertIs(fresh.get("refreshing"), True, "FB3: a stale sql read reports refreshing")
        self.assertTrue(
            runner.started.wait(timeout=10), "FB3: the stale sql read must start one sync"
        )
        self.assertEqual(
            runner.calls, [(str(self.db_path), REPO)], "FB3: exactly one runner call"
        )
        runner.release.set()
        session.stop()


class McpPipeliningTests(StoreHarness):
    """Review finding 4: client requests pipelined while the server awaits
    its roots/list reply must still each get exactly one response.

    The cwd is a real git checkout and the calls carry no repo argument,
    so resolution genuinely enters the roots handshake."""

    def setUp(self) -> None:
        super().setUp()
        self.checkout = self.make_repo("probe")
        git(
            "remote", "set-url", "origin", f"https://github.com/{REPO}.git",
            cwd=self.checkout,
        )
        self.db = self.root / "history.db"
        seed_small_db(self.db)
        self.set_env("ZAXBYGRAPH_DB", str(self.db))

    def test_pipelined_calls_survive_the_roots_wait(self) -> None:
        session = McpSession(self, cwd=str(self.checkout))
        self.addCleanup(session.stop)
        session.initialize()

        # Two rootless calls in a row: the second is read while the first
        # waits for its roots reply.
        id_a = session.request("tools/call", {"name": "graph_status", "arguments": {}})
        id_b = session.request("tools/call", {"name": "graph_status", "arguments": {}})
        resp_a = session.wait_response(id_a)
        resp_b = session.wait_response(id_b)
        self.assertIsInstance(resp_a.get("result"), dict, "FB4: call A answered")
        self.assertIsInstance(resp_b.get("result"), dict, "FB4: pipelined call B answered")
        env_a = json.loads(resp_a["result"]["content"][0]["text"])
        env_b = json.loads(resp_b["result"]["content"][0]["text"])
        self.assertIs(env_a.get("ok"), True, f"FB4: call A ok: {env_a}")
        self.assertIs(env_b.get("ok"), True, f"FB4: call B ok: {env_b}")
        session.stop()


class McpResolutionPinTests(TempDBTest):
    """Review question, recorded decision: `zaxbygraph mcp --repo X --db Y`
    pins the resolution; the per-call tool argument still wins."""

    def test_server_pin_resolves_without_arguments(self) -> None:
        seed_small_db(self.db_path)
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        session = McpSession(
            self, cwd=str(self._td.name), repo=REPO, db=str(self.db_path)
        )
        self.addCleanup(session.stop)
        session.initialize()
        response = session.call_tool("graph_status", {})
        _result, env = tool_envelope(self, response, "FB5")
        self.assertIs(env.get("ok"), True, f"FB5: the server pin resolves: {env}")
        self.assertEqual(env.get("repo"), REPO, "FB5: repo resolved from the pin")
        session.stop()

    def test_tool_argument_outranks_the_server_pin(self) -> None:
        seed_small_db(self.db_path)
        other = Path(self._td.name) / "other.db"
        conn = connect(other)
        init_schema(conn)
        src = type(self.src)()
        src.add_issue(issue(9, title="other corpus", body="x"))
        sync_repo(conn, src, "acme/other")
        conn.close()
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        session = McpSession(
            self, cwd=str(self._td.name), repo=REPO, db=str(self.db_path)
        )
        self.addCleanup(session.stop)
        session.initialize()
        response = session.call_tool(
            "graph_status", {"repo": "acme/other", "db": str(other)}
        )
        _result, env = tool_envelope(self, response, "FB5")
        self.assertIs(env.get("ok"), True, f"FB5: the tool argument wins: {env}")
        self.assertEqual(env.get("repo"), "acme/other", "FB5: per-call repo outranks the pin")
        session.stop()


class McpOverlapDedupeTests(TempDBTest):
    """Review nit: duplicate numbers must not produce a self-pair."""

    def test_duplicate_numbers_do_not_self_pair(self) -> None:
        self.src.add_pr(
            issue(30, title="dup pr", body="b", kind="pr", state="closed"),
            pull(30, merged=True),
            files=[pr_file("src/dup.py")],
        )
        self.src.add_pr(
            issue(31, title="other pr", body="b", kind="pr", state="closed"),
            pull(31, merged=True),
            files=[pr_file("src/other.py")],
        )
        self.sync()
        from zaxbygraph.query import pr_overlap

        collapsed = pr_overlap(self.conn, [30, 30], repo=REPO)
        self.assertEqual(collapsed["pairs"], [], "duplicates collapse; no self-pair")
        mixed = pr_overlap(self.conn, [30, 30, 31], repo=REPO)
        self.assertEqual(len(mixed["pairs"]), 1, "one distinct pair")
        self.assertEqual(mixed["pairs"][0]["shared"], [], "distinct PRs stay disjoint")


class McpStdinEncodingTests(unittest.TestCase):
    """Review finding 5: MCP frames are UTF-8, so a piped stdin on a
    non-UTF-8 Windows locale must decode them faithfully.

    The pre-fix failure mode is SILENT MOJIBAKE, not a crash: a
    locale stdin decodes with cp1252+surrogateescape, so a non-ASCII
    tool argument never raises - it round-trips corrupted and returns
    confidently wrong results. The oracle therefore asserts fidelity:
    a non-ASCII sentinel sent through a tools/call statement must
    come back intact (verified RED on revert by mutation)."""

    _SENTINEL = "丁"

    def test_stdin_decodes_utf8_frames_under_cp1252_locale(self) -> None:
        db = _REPO_ROOT / ".zcode" / "fb6-stdin.db"
        self.addCleanup(lambda: db.unlink(missing_ok=True))
        conn = connect(db)
        try:
            init_schema(conn)
            src = FakeGitHubSource()
            src.add_issue(issue(1, title="sentinel probe", body="needle"))
            sync_repo(conn, src, REPO)
        finally:
            conn.close()

        env = scrubbed_env()
        env.pop("PYTHONUTF8", None)
        env.pop("PYTHONIOENCODING", None)
        env["ZAXBYGRAPH_DB"] = str(db)
        statement = f"SELECT '{self._SENTINEL}' AS echo"
        frames = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "probe", "version": "0"},
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "sql",
                        "arguments": {"repo": REPO, "statement": statement},
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        proc = subprocess.run(
            [sys.executable, "-X", "utf8=0", "-m", "zaxbygraph", "mcp"],
            input=frames.encode("utf-8"),
            capture_output=True,
            env=env,
            cwd=str(_REPO_ROOT),
            timeout=120,
        )
        out = proc.stdout.decode("utf-8", "replace")
        echo = None
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("id") == 2 and isinstance(obj.get("result"), dict):
                env_payload = json.loads(obj["result"]["content"][0]["text"])
                rows = (env_payload.get("data") or {}).get("rows") or []
                if rows:
                    echo = rows[0][0]
                break
        self.assertIsNotNone(
            echo,
            "FB6: the sql echo must answer; stderr: "
            + proc.stderr.decode("utf-8", "replace")[-400:],
        )
        self.assertEqual(
            echo,
            self._SENTINEL,
            "FB6: a non-ASCII sentinel must survive the stdin round trip "
            f"byte-faithfully (locale mojibake would corrupt it); got {echo!r}",
        )


if __name__ == "__main__":
    unittest.main()
