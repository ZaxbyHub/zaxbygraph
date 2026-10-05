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
import tempfile
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
        page1 = {i["number"] for i in items}
        page2 = {i["number"] for i in items2}
        self.assertEqual(
            page1 & page2,
            set(),
            "FB1: page 2 must be disjoint from page 1 (a repeating cursor passes a mere not-equal check)",
        )
        self.assertEqual(
            len(page1 | page2),
            25,
            "FB1: the two pages must cover exactly the 25 seeded items",
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
        self.addCleanup(runner.release.set)  # release before tempdir teardown
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
        self.assertEqual(
            session.unexpected,
            [],
            "FB4: every frame must be an answer to exactly one request",
        )
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

    def test_tool_db_argument_is_not_honored(self) -> None:
        """Superseded decision (review round 2, finding F2): the per-call
        `db` tool argument was removed — a model-reachable path is a
        filesystem oracle and, via sync, an arbitrary directory-creation
        primitive. The database is operator-pinned only; a client that
        sends `db` anyway gets the operator-pinned database."""
        seed_small_db(self.db_path)
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        session = McpSession(
            self, cwd=str(self._td.name), repo=REPO, db=str(self.db_path)
        )
        self.addCleanup(session.stop)
        session.initialize()
        response = session.call_tool(
            "graph_status",
            {"repo": "acme/other", "db": str(self.db_path)},
        )
        _result, env = tool_envelope(self, response, "FB5")
        self.assertIs(env.get("ok"), False, f"FB5: db must be ignored: {env}")
        self.assertEqual(
            Path(str(env.get("db"))),
            self.db_path,
            "FB5: resolution must land on the operator-pinned database",
        )
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
        self._fb6_td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._fb6_td.cleanup)
        db = Path(self._fb6_td.name) / "fb6-stdin.db"
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
        # Pin the hostile regime deterministically: a cp1252 stdin decodes
        # the U+4E01 sentinel's 0x81 byte as mojibake on every platform,
        # instead of trusting the ambient ANSI code page (review F15).
        env["PYTHONIOENCODING"] = "cp1252"
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


class FeedbackRound2Tests(TempDBTest):
    """Regression tests for the round-2 review findings (F1/F5/F6/F7/F8/F11/F12/F13)."""

    def seed_tool_corpus(self) -> None:
        self.src.add_issue(issue(1, title="probe one", body="needle", state="open"))
        self.src.add_pr(
            issue(3, title="probe pr", body="adds", kind="pr", state="closed"),
            pull(3, changed_files=1, merged=True),
            files=[pr_file("src/probe.py")],
        )
        self.sync()

    def test_related_depth_is_strictly_validated_and_capped(self) -> None:
        self.seed_tool_corpus()
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        session = McpSession(self, cwd=str(self._td.name))
        self.addCleanup(session.stop)
        session.initialize()
        for bad in (0, -2, "3", True, 4.5):
            response = session.call_tool("related", {"repo": REPO, "number": 1, "depth": bad})
            result, env = tool_envelope(self, response, "F11")
            self.assertIs(
                result.get("isError"), True, f"F11: depth {bad!r} must be rejected"
            )
            self.assertEqual(env.get("error", {}).get("code"), "bad_request")
        response = session.call_tool(
            "related", {"repo": REPO, "number": 1, "depth": 999}
        )
        result, _env = tool_envelope(self, response, "F11")
        self.assertIs(
            result.get("isError"), True, "F11: depth above the cap must be rejected"
        )
        session.stop()

    def test_search_limit_and_cursor_are_capped(self) -> None:
        self.seed_tool_corpus()
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        session = McpSession(self, cwd=str(self._td.name))
        self.addCleanup(session.stop)
        session.initialize()
        response = session.call_tool(
            "search", {"repo": REPO, "query": "needle", "cursor": "1000000000"}
        )
        _result, env = tool_envelope(self, response, "F1")
        self.assertIs(env.get("ok"), True, f"F1: a huge cursor must stay bounded: {env}")
        response = session.call_tool(
            "search", {"repo": REPO, "query": "needle", "limit": 100000}
        )
        _result, env = tool_envelope(self, response, "F1")
        self.assertLessEqual(
            len(env["data"]["items"]), 500, "F1: the page must stay under the ceiling"
        )
        session.stop()

    def test_pr_overlap_rejects_short_and_oversized_lists(self) -> None:
        self.seed_tool_corpus()
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        session = McpSession(self, cwd=str(self._td.name))
        self.addCleanup(session.stop)
        session.initialize()
        for numbers in ([1], []):
            response = session.call_tool(
                "pr_overlap", {"repo": REPO, "numbers": numbers}
            )
            result, env = tool_envelope(self, response, "F12")
            self.assertIs(result.get("isError"), True, f"F12: {numbers} must be rejected")
            self.assertEqual(env.get("error", {}).get("code"), "bad_request")
        session.stop()

    def test_sync_bool_arguments_are_strict(self) -> None:
        self.seed_tool_corpus()
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        session = McpSession(self, cwd=str(self._td.name), sync_runner=lambda db, repo: None)
        self.addCleanup(session.stop)
        session.initialize()
        response = session.call_tool(
            "sync", {"repo": REPO, "force": "false", "include_patches": "yes"}
        )
        result, env = tool_envelope(self, response, "F6")
        self.assertIs(result.get("isError"), True, "F6: string booleans must be rejected")
        self.assertEqual(env.get("error", {}).get("code"), "bad_request")
        self.assertEqual(
            env.get("error", {}).get("hint"), "pass force as true or false"
        )
        session.stop()

    def test_get_item_max_body_chars_is_validated(self) -> None:
        self.seed_tool_corpus()
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        session = McpSession(self, cwd=str(self._td.name))
        self.addCleanup(session.stop)
        session.initialize()
        response = session.call_tool(
            "get_item", {"repo": REPO, "number": 1, "max_body_chars": "50"}
        )
        result, env = tool_envelope(self, response, "F5")
        self.assertIs(result.get("isError"), True, "F5: a string max_body_chars must be rejected")
        self.assertEqual(env.get("error", {}).get("code"), "bad_request")
        session.stop()

    def test_tool_faults_answer_as_envelope_errors(self) -> None:
        """F4: an unexpected handler exception still answers the envelope
        contract (isError tool result), never a bare protocol error."""
        self.seed_tool_corpus()
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        session = McpSession(self, cwd=str(self._td.name))
        self.addCleanup(session.stop)
        session.initialize()
        # A directory in place of the database: open_existing raises
        # sqlite3.OperationalError, which must reach the client as an
        # envelope runtime error now that the db argument is gone this
        # is driven through the environment-resolved path only; simplest
        # in-contract probe is a repository fault: a number so large the
        # handler raises is not reachable, so drive the sqlite path via
        # a corrupted copy of the database.
        corrupt = Path(self._td.name) / "corrupt.db"
        corrupt.write_bytes(b"this is not a database" * 64)
        import os as _os

        _os.environ["ZAXBYGRAPH_DB"] = str(corrupt)
        self.addCleanup(_os.environ.pop, "ZAXBYGRAPH_DB", None)
        response = session.call_tool("graph_status", {"repo": REPO})
        result, env = tool_envelope(self, response, "F4")
        self.assertIs(result.get("isError"), True, "F4: a corrupt db is a tool error")
        self.assertIsInstance(env.get("error"), dict, "F4: structured envelope error")
        session.stop()

    def test_what_closed_reports_kind(self) -> None:
        self.seed_tool_corpus()
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        session = McpSession(self, cwd=str(self._td.name))
        self.addCleanup(session.stop)
        session.initialize()
        response = session.call_tool("what_closed", {"repo": REPO, "number": 3})
        _result, env = tool_envelope(self, response, "F13")
        self.assertEqual(
            env["data"].get("kind"), "pr", "F13: a PR number must be recognizable"
        )
        session.stop()


class ProductQueryRepoRequiredTests(TempDBTest):
    """F8: the three product queries reject a falsy repo (number-keyed
    lookups collapse distinct same-numbered items across repos)."""

    def test_repo_is_required(self) -> None:
        from zaxbygraph.query import file_history, pr_overlap, what_closed

        for call in (
            lambda: pr_overlap(self.conn, [1, 2], repo=None),
            lambda: file_history(self.conn, "src/x.py", repo=None),
            lambda: what_closed(self.conn, 1, repo=None),
        ):
            with self.assertRaises(ValueError):
                call()

    def test_file_history_dedupes_closed_issues(self) -> None:
        from zaxbygraph.query import file_history

        self.src.add_pr(
            issue(40, title="dual closer", body="b", kind="pr", state="closed",
                  updated_at="2026-04-01T00:00:00Z"),
            pull(40, merged=True),
            files=[pr_file("src/dual.py")],
        )
        self.src.add_issue(issue(7, title="the bug", body="x", state="closed"))
        self.sync()
        self.conn.execute(
            "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
            " confidence, evidence, source) VALUES (?,?,?,?,?,?,?,?,?)",
            (REPO, "item", "40", "closes", "item", "7", "EXTRACTED",
             "timeline closed event", "timeline"),
        )
        self.conn.commit()
        result = file_history(self.conn, "src/dual.py", repo=REPO)
        self.assertEqual(
            result["entries"][0]["closed_issues"],
            [7],
            "F7: two provenance streams for one closer must not emit [7, 7]",
        )





class FeedbackRound3Tests(TempDBTest):
    """Round-3 reviewer regressions: the generic tools/call fault branch
    must answer AND keep the session alive, and the external-join path
    must roll its registry entry back."""

    def seed_tool_corpus(self) -> None:
        self.src.add_issue(issue(1, title="probe one", body="needle", state="open"))
        self.src.add_pr(
            issue(3, title="probe pr", body="adds", kind="pr", state="closed"),
            pull(3, changed_files=1, merged=True),
            files=[pr_file("src/probe.py")],
        )
        self.sync()

    def test_generic_tool_fault_answers_and_session_survives(self) -> None:
        self.seed_tool_corpus()
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        session = McpSession(self, cwd=str(self._td.name))
        self.addCleanup(session.stop)
        session.initialize()
        # Drive the GENERIC (non-ToolError) branch: corrupt the FTS tables
        # out of the database after the corpus guard passes, so search
        # raises sqlite3.OperationalError mid-handler.
        self.conn.execute("DROP TABLE items_fts")
        self.conn.execute("DROP TABLE comments_fts")
        self.conn.commit()
        response = session.call_tool("search", {"repo": REPO, "query": "needle"})
        result, env = tool_envelope(self, response, "R3")
        self.assertIs(result.get("isError"), True, "R3: a tool fault answers isError")
        self.assertIs(env.get("ok"), False, "R3: the envelope reports ok false")
        self.assertEqual(env.get("error", {}).get("code"), "runtime")
        # The session must survive: a legal read still answers.
        response = session.call_tool("graph_status", {"repo": REPO})
        result, env = tool_envelope(self, response, "R3")
        self.assertIs(env.get("ok"), True, f"R3: the session must survive: {env}")
        session.stop()

    def test_external_join_does_not_wedge_single_flight(self) -> None:
        import threading

        from zaxbygraph.sync import acquire_sync_lock

        self.seed_tool_corpus()
        pin_env_for(self, Path(self._td.name), db=self.db_path)
        lock = acquire_sync_lock(self.db_path)
        self.assertIsNotNone(lock, "R3: the test holds the external lock")
        started_flag = threading.Event()
        session = McpSession(
            self,
            cwd=str(self._td.name),
            stale_after_s=0,
            sync_runner=lambda db, repo: started_flag.set(),
        )
        self.addCleanup(session.stop)
        session.initialize()
        # First sync joins the external lock.
        response = session.call_tool("sync", {"repo": REPO})
        _result, env = tool_envelope(self, response, "R3")
        self.assertIs(env.get("ok"), True, f"R3: the join answers: {env}")
        lock.release_owned()
        # After the external lock frees, a second sync must START (the
        # pre-fix delta left a phantom running entry that joined forever).
        response = session.call_tool("sync", {"repo": REPO})
        _result, env = tool_envelope(self, response, "R3")
        self.assertTrue(
            env.get("data", {}).get("started"),
            f"R3: single-flight must recover after the external lock frees: {env}",
        )
        self.assertTrue(started_flag.wait(timeout=10), "R3: the runner must run")
        session.stop()


if __name__ == "__main__":
    unittest.main()
