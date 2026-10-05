"""Acceptance checks for issue-trace 7-mcp-typed-agent-surface (issue #7).

Authored at arm's length against the issue's acceptance criteria AC1-AC6
(AC7 is a recorded product decision, AC8 is the preserving leg covered by
the existing guard tests). Must ERROR at base (ModuleNotFoundError:
zaxbygraph.mcp_server) and PASS once the criteria are implemented.

Every failure carries a distinctive single-line tag (MCP1..MCP6) so a
line-wise grep can tell the checks apart.

CONTRACT THESE TESTS PIN for the implementation (zaxbygraph.mcp_server):

1. Module name: `zaxbygraph.mcp_server` (importable; its absence is the
   base-leg RED signal for c1/c2/c3/c5/c6).

2. Server entry: `serve(stdin, stdout, *, cwd=None, stale_after_s=900,
   sync_runner=None)`.
   - stdin/stdout are TEXT streams. The tests pass io.TextIOBase
     subclasses, so the server must not use fileno() or any OS-level
     stream operation; it reads requests with readline() and writes
     responses with write(...)+flush() (print(file=...) is fine).
   - Framing: newline-delimited JSON-RPC 2.0; one UTF-8 JSON object per
     line. stdout carries ONLY protocol frames; logs go to stderr.
   - serve() returns when stdin.readline() returns '' (EOF).
   - cwd: the server working directory used for repo resolution
     (default Path.cwd()). Tests pass an explicit path.
   - stale_after_s: freshness.age_s threshold triggering a background
     refresh (issue default 900; tests shrink it).
   - sync_runner: the background-sync seam. A callable
     `sync_runner(db_path, repo)` invoked exactly once per in-flight
     refresh; None means the server builds its own real runner. Tests
     always inject (the suite is offline: no gh, no network).

3. Repo resolution order: explicit `"repo"` tool argument > MCP roots
   (the server sends a roots/list REQUEST to the client on stdout and
   reads the reply line from stdin; roots are file:// directory URIs,
   each mapped to its checkout's git origin) > git origin of `cwd`.

4. Tool result shape: MCP tools/call result object whose content[0] is
   {"type": "text"} and whose text parses to the CLI JSON envelope
   {ok, db, repo, freshness{synced_at, age_s, complete[, refreshing]},
   data, truncated} (+ error{code, message, hint} on failure). Failures
   set isError: true on the tool result.

5. Background refresh: a stale read (freshness.age_s > stale_after_s)
   answers immediately from current data, reports
   freshness.refreshing: true, and starts exactly ONE background sync
   (the whole-run sync lock's one-in-flight rule: a second read while a
   refresh is running must not start another).

6. Resource: resources/list advertises uri "zaxbygraph://schema";
   resources/read returns contents[0].text parsing to the same
   {"tables": [...]} describe_schema produces for the `schema` CLI
   command on the same database.
"""

from __future__ import annotations

import io
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# fixtures FIRST (convention), then the existing zaxbygraph surface, then
# the module under test LAST so the base-leg error is the mcp_server import.
from fixtures import REPO, FakeGitHubSource, TempDBTest, issue, pr_file, pull
from test_paths import StoreHarness, db_files_under, git
from zaxbygraph.cli import main
from zaxbygraph.db import connect, init_schema
from zaxbygraph.paths import store_db_path
from zaxbygraph.sync import sync_repo
# Submodule import (not `from zaxbygraph import mcp_server`) so the base-leg
# failure is exactly: ModuleNotFoundError: No module named
# 'zaxbygraph.mcp_server'
import zaxbygraph.mcp_server as mcp_server

# AC1: the exact typed-tool surface initialize must advertise.
EXPECTED_TOOLS = {
    "graph_status",
    "search",
    "get_item",
    "related",
    "path",
    "pr_overlap",
    "file_history",
    "what_closed",
    "open_items",
    "sql",
    "sync",
}

OTHER_SLUG = "acme/other"
OTHER_ORIGIN = "https://github.com/acme/other.git"
SEEDED_TITLE = "seeded widget one"

_RESPONSE_WAIT_S = 20.0
_JOIN_WAIT_S = 5.0


class _QueueInput(io.TextIOBase):
    """stdin side the tests drive: readline() serves queued lines, '' at EOF."""

    def __init__(self) -> None:
        super().__init__()
        self._lines: queue.Queue[str] = queue.Queue()

    def send_line(self, line: str) -> None:
        self._lines.put(line + "\n")

    def send_eof(self) -> None:
        self._lines.put("")

    def readline(self, size=-1):
        line = self._lines.get()
        if line == "":
            return ""
        if size is not None and size >= 0:
            return line[:size]
        return line


class _QueueOutput(io.TextIOBase):
    """stdout side the tests read: complete lines only, non-blocking."""

    def __init__(self) -> None:
        super().__init__()
        self._lines: queue.Queue = queue.Queue()
        self._partial = ""

    def write(self, s) -> int:
        self._partial += str(s)
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._lines.put(line)
        return len(str(s))

    def flush(self) -> None:
        return None


class McpSession:
    """One in-process server instance over queue-backed text streams.

    Reactive: the test sends a request line, then reads whatever the
    server emits; whenever the server sends a roots/list REQUEST the
    session answers it from `roots_uris` (empty list by default), so the
    tests never depend on WHEN the server asks.
    """

    def __init__(self, test: unittest.TestCase, **serve_kwargs) -> None:
        self.test = test
        self.stdin = _QueueInput()
        self.stdout = _QueueOutput()
        self.roots_uris: list[str] = []
        self.lines: list[str] = []
        self.server_notifications: list[dict] = []
        self.unexpected: list[dict] = []
        self.error: str | None = None
        self.finished = threading.Event()
        self._next_id = 900
        self._thread = threading.Thread(
            target=self._run, kwargs=serve_kwargs, daemon=True, name="mcp-serve"
        )
        self._thread.start()

    def _run(self, **serve_kwargs) -> None:
        try:
            mcp_server.serve(self.stdin, self.stdout, **serve_kwargs)
        except BaseException as exc:  # surfaced by recv/wait timeouts
            import traceback

            self.error = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
        finally:
            self.stdout._lines.put(None)  # server-done sentinel
            self.finished.set()

    # -- client side -------------------------------------------------------

    def _send_obj(self, obj: dict) -> None:
        self.stdin.send_line(json.dumps(obj))

    def stop(self, *, expect_exit: bool = True) -> None:
        self.stdin.send_eof()
        joined = self._thread.join(timeout=_JOIN_WAIT_S)
        if expect_exit and self._thread.is_alive():
            self.test.fail("MCP: serve() did not return after stdin EOF")
        if self.error is not None:
            self.test.fail(f"MCP: serve() raised:\n{self.error}")

    def recv_line(self, timeout: float) -> str:
        try:
            line = self.stdout._lines.get(timeout=timeout)
        except queue.Empty:
            if self.error:
                self.test.fail(f"MCP: server crashed:\n{self.error}")
            self.test.fail(f"MCP: timed out after {timeout}s waiting for a server line")
        if line is None:
            if self.error:
                self.test.fail(f"MCP: server exited before responding:\n{self.error}")
            self.test.fail("MCP: server exited (stdin EOF?) before answering")
        self.lines.append(line)
        return line

    def wait_response(self, req_id, timeout: float = _RESPONSE_WAIT_S) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.test.fail(
                    f"MCP: no response with id {req_id!r} before timeout; "
                    f"server lines so far: {self.lines[-5:]!r}"
                )
            line = self.recv_line(remaining)
            try:
                obj = json.loads(line)
            except ValueError:
                self.test.fail(f"MCP: server wrote a non-JSON line: {line[:200]!r}")
            if not isinstance(obj, dict):
                self.test.fail(f"MCP: server line is not a JSON object: {line[:200]!r}")
            if "method" in obj and "id" in obj:
                method = obj["method"]
                if method != "roots/list":
                    self.test.fail(
                        f"MCP: harness cannot answer server request method {method!r}"
                    )
                self._send_obj(
                    {
                        "jsonrpc": "2.0",
                        "id": obj["id"],
                        "result": {
                            "roots": [
                                {"uri": uri, "name": f"root{i}"}
                                for i, uri in enumerate(self.roots_uris)
                            ]
                        },
                    }
                )
                continue
            if "method" in obj:  # server->client notification; tolerated
                self.server_notifications.append(obj)
                continue
            if obj.get("id") == req_id:
                return obj
            self.unexpected.append(obj)

    def request(self, method: str, params: dict | None = None):
        req_id = self._next_id
        self._next_id += 1
        frame: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            frame["params"] = params
        self._send_obj(frame)
        return req_id

    def notify(self, method: str, params: dict | None = None) -> None:
        frame: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        self._send_obj(frame)

    def initialize(self) -> dict:
        req_id = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        )
        response = self.wait_response(req_id)
        self.notify("notifications/initialized")
        return response

    def call_tool(self, name: str, arguments: dict, timeout: float = _RESPONSE_WAIT_S) -> dict:
        req_id = self.request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        return self.wait_response(req_id, timeout=timeout)


def tool_envelope(test: unittest.TestCase, response: dict, tag: str) -> tuple[dict, dict]:
    """Extract (mcp result object, parsed JSON envelope) from a tools/call
    response; assert the pinned content shape on the way out."""
    result = response.get("result")
    test.assertIsInstance(result, dict, f"{tag}: tools/call must answer with a result object")
    content = result.get("content")
    test.assertIsInstance(content, list, f"{tag}: tools/call result must carry content")
    test.assertTrue(content, f"{tag}: tools/call content must not be empty")
    first = content[0]
    test.assertIsInstance(first, dict, f"{tag}: content[0] must be an object")
    test.assertEqual(
        first.get("type"), "text", f"{tag}: content[0].type must be text"
    )
    text = first.get("text")
    test.assertIsInstance(text, str, f"{tag}: content[0].text must be a string")
    try:
        env = json.loads(text)
    except ValueError:
        test.fail(f"{tag}: content[0].text must parse as the JSON envelope, got {text[:200]!r}")
    test.assertIsInstance(env, dict, f"{tag}: the envelope must be a JSON object")
    return result, env


def pin_env_for(test: unittest.TestCase, root: Path, db: Path | None = None) -> None:
    """ZAXBYGRAPH_HOME inside the temp root; ZAXBYGRAPH_DB pinned to `db`
    (or removed) so the user's real store/db is never consulted."""
    saved = {k: os.environ.get(k) for k in ("ZAXBYGRAPH_HOME", "ZAXBYGRAPH_DB")}
    os.environ["ZAXBYGRAPH_HOME"] = str(root / "zhome")
    if db is not None:
        os.environ["ZAXBYGRAPH_DB"] = str(db)
    else:
        os.environ.pop("ZAXBYGRAPH_DB", None)

    def restore() -> None:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    test.addCleanup(restore)


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """In-process cli.main with the SystemExit pattern of test_cli_envelope."""
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            if isinstance(exc.code, int):
                code = exc.code
            elif exc.code is None:
                code = 0
            else:
                code = 2
    return code, out.getvalue(), err.getvalue()


def seed_small_db(db_path: Path) -> None:
    """One issue + one merged PR touching one file, offline."""
    conn = connect(db_path)
    init_schema(conn)
    src = FakeGitHubSource()
    src.add_issue(issue(1, title="probe one", body="needle", state="open"))
    src.add_pr(
        issue(3, title="probe pr", body="adds a file", kind="pr", state="closed"),
        pull(3, changed_files=1, merged=True),
        files=[pr_file("src/probe.py")],
    )
    sync_repo(conn, src, REPO)
    conn.close()


class McpProtocolTests(unittest.TestCase):
    """AC1: initialize then tools/list over stdio advertise the 11 typed
    tools, each with a JSON input schema."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        pin_env_for(self, self.root)

    def test_initialize_and_tools_list(self) -> None:
        session = McpSession(self, cwd=str(self.root))
        self.addCleanup(session.stop)
        response = session.initialize()
        result = response.get("result")
        self.assertIsInstance(result, dict, "MCP1: initialize must answer with a result object")
        self.assertIsInstance(
            result.get("protocolVersion"),
            str,
            "MCP1: initialize result must carry a protocolVersion string",
        )
        capabilities = result.get("capabilities")
        self.assertIsInstance(
            capabilities, dict, "MCP1: initialize result must carry capabilities"
        )
        self.assertIn(
            "tools", capabilities, "MCP1: capabilities must advertise tools"
        )
        server_info = result.get("serverInfo")
        self.assertIsInstance(
            server_info, dict, "MCP1: initialize result must carry serverInfo"
        )
        self.assertIn("name", server_info, "MCP1: serverInfo must carry a name")

        req_id = session.request("tools/list", {})
        response = session.wait_response(req_id)
        result = response.get("result")
        self.assertIsInstance(result, dict, "MCP1: tools/list must answer with a result object")
        tools = result.get("tools")
        self.assertIsInstance(tools, list, "MCP1: tools/list result.tools must be a list")
        names = set()
        for tool in tools:
            self.assertIsInstance(tool, dict, "MCP1: each tool must be an object")
            name = tool.get("name")
            self.assertIsInstance(name, str, "MCP1: each tool must carry a name")
            names.add(name)
            self.assertIsInstance(
                tool.get("inputSchema"),
                dict,
                f"MCP1: tool {name} must carry a JSON inputSchema object",
            )
        missing = EXPECTED_TOOLS - names
        extra = names - EXPECTED_TOOLS
        self.assertFalse(
            missing,
            f"MCP1: tools/list is missing required tools: {sorted(missing)}; got {sorted(names)}",
        )
        self.assertFalse(
            extra,
            f"MCP1: tools/list returned unexpected tools: {sorted(extra)}",
        )
        session.stop()


class McpResolutionTests(StoreHarness):
    """AC2: from a linked worktree the server resolves the same slug-keyed
    store DB as the main checkout; client roots win over the server cwd."""

    def seed_other_store(self) -> Path:
        db = self.home / "github.com" / "acme" / "other" / "history.db"
        conn = connect(db)
        init_schema(conn)
        src = FakeGitHubSource()
        src.add_issue(issue(1, title="OTHERCORPUS probe", body="needle"))
        sync_repo(conn, src, OTHER_SLUG)
        conn.close()
        return db

    def test_worktree_and_roots_resolve_same_db(self) -> None:
        mainline = self.make_repo("mainline")
        self.seed_store()
        other_store = self.seed_other_store()
        other_checkout = self.make_repo("otherline")
        git("remote", "set-url", "origin", OTHER_ORIGIN, cwd=other_checkout)

        worktree = self.root / "wt"
        git("worktree", "add", str(worktree), cwd=mainline)

        def detach() -> None:
            os.chdir(self.saved_cwd)
            import subprocess

            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=str(mainline),
                capture_output=True,
            )

        self.addCleanup(detach)
        self.assertEqual(db_files_under(worktree), set())

        # The store path the MAIN checkout resolves for acme/widget.
        widget_store = store_db_path("github.com", "acme/widget")
        self.assertEqual(widget_store, self.store_db())

        # Session A: cwd is the linked worktree, no roots, no repo argument
        # -> must resolve the main checkout's slug-keyed store DB and serve
        # its corpus.
        session_a = McpSession(self, cwd=str(worktree))
        self.addCleanup(session_a.stop)
        session_a.initialize()
        response = session_a.call_tool("get_item", {"number": 1})
        _result, env = tool_envelope(self, response, "MCP2")
        self.assertIs(env.get("ok"), True, f"MCP2: worktree read must succeed: {env}")
        self.assertEqual(env.get("repo"), "acme/widget", "MCP2: repo resolved from the worktree cwd origin")
        self.assertEqual(
            Path(str(env.get("db"))),
            widget_store,
            "MCP2: the worktree must resolve the same slug-keyed store DB as the main checkout",
        )
        data = env.get("data")
        self.assertIsInstance(data, dict, "MCP2: get_item data must be an object")
        self.assertEqual(data.get("number"), 1, "MCP2: get_item must answer item 1")
        self.assertEqual(data.get("title"), SEEDED_TITLE, "MCP2: the store corpus must be served")
        session_a.stop()
        self.assertEqual(db_files_under(worktree), set(), "MCP2: the read must not create a worktree DB")

        # Session B: same worktree cwd, but the client's roots name the
        # OTHER checkout -> roots must WIN over the cwd-derived repo.
        session_b = McpSession(self, cwd=str(worktree))
        self.addCleanup(session_b.stop)
        session_b.roots_uris = [other_checkout.as_uri()]
        session_b.initialize()
        response = session_b.call_tool("graph_status", {})
        _result, env = tool_envelope(self, response, "MCP2")
        self.assertIs(env.get("ok"), True, f"MCP2: roots-resolved read must succeed: {env}")
        self.assertEqual(
            env.get("repo"),
            OTHER_SLUG,
            "MCP2: client-supplied roots must be preferred over the server cwd",
        )
        self.assertEqual(
            Path(str(env.get("db"))),
            other_store,
            "MCP2: roots resolution must land on the root checkout's store DB",
        )
        repos = env.get("data", {}).get("repos")
        self.assertIsInstance(repos, list, "MCP2: graph_status data.repos must be a list")
        self.assertEqual([r.get("repo") for r in repos], [OTHER_SLUG])
        self.assertEqual(int(repos[0]["item_count"]), 1, "MCP2: the OTHER corpus must be served")
        session_b.stop()


class McpErrorTests(unittest.TestCase):
    """AC3: no corpus / invalid SQL answer as structured tool errors with the
    envelope error object, and no DB file is created."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def test_no_corpus_and_bad_sql_are_structured_errors(self) -> None:
        # -- no corpus -----------------------------------------------------
        # Fresh store home, cwd outside any git repo, explicit repo argument
        # naming a slug that has never been synced.
        pin_env_for(self, self.root)
        expected_store = Path(os.environ["ZAXBYGRAPH_HOME"]) / "github.com" / "acme" / "nocorpus" / "history.db"
        session = McpSession(self, cwd=str(self.root))
        self.addCleanup(session.stop)
        session.initialize()
        response = session.call_tool("graph_status", {"repo": "acme/nocorpus"})
        result, env = tool_envelope(self, response, "MCP3")
        self.assertIs(
            result.get("isError"), True, "MCP3: a no-corpus tool call must set isError true"
        )
        self.assertIs(env.get("ok"), False, "MCP3: the no-corpus envelope must have ok false")
        error = env.get("error")
        self.assertIsInstance(error, dict, "MCP3: the envelope must carry an error object")
        self.assertEqual(
            error.get("code"), "no_corpus", f"MCP3: error.code must be no_corpus, got {error.get('code')!r}"
        )
        self.assertIsInstance(error.get("message"), str, "MCP3: error.message must be a string")
        self.assertTrue(
            str(error.get("message", "")).strip(),
            "MCP3: error.message must be non-empty",
        )
        hint = error.get("hint")
        self.assertIsInstance(hint, str, "MCP3: the no-corpus error must carry a hint string")
        self.assertTrue(str(hint).strip(), "MCP3: the no-corpus hint must be non-empty")
        session.stop()
        self.assertFalse(
            expected_store.exists(),
            "MCP3: a failed read must not create the store DB file",
        )
        self.assertFalse(
            expected_store.parent.exists(),
            "MCP3: a failed read must not create the store directory",
        )

        # -- invalid SQL ---------------------------------------------------
        db = self.root / "history.db"
        seed_small_db(db)
        pin_env_for(self, self.root, db=db)
        session = McpSession(self, cwd=str(self.root))
        self.addCleanup(session.stop)
        session.initialize()
        response = session.call_tool("sql", {"repo": REPO, "statement": "DELETE FROM items"})
        result, env = tool_envelope(self, response, "MCP3")
        self.assertIs(
            result.get("isError"), True, "MCP3: an invalid sql tool call must set isError true"
        )
        self.assertIs(env.get("ok"), False, "MCP3: the bad-sql envelope must have ok false")
        error = env.get("error")
        self.assertIsInstance(error, dict, "MCP3: bad sql must carry an error object")
        self.assertIn(
            error.get("code"),
            ("bad_sql", "bad_request"),
            f"MCP3: bad sql error.code must be bad_sql or bad_request, got {error.get('code')!r}",
        )
        self.assertTrue(
            str(error.get("message", "")).strip(),
            "MCP3: bad sql error.message must be non-empty",
        )
        # The session must survive the error: a legal read still answers,
        # and the rejected DELETE deleted nothing.
        response = session.call_tool(
            "sql", {"repo": REPO, "statement": "SELECT COUNT(*) AS c FROM items"}
        )
        result, env = tool_envelope(self, response, "MCP3")
        self.assertIs(result.get("isError", False), False, "MCP3: a legal sql read must not be an error")
        self.assertIs(env.get("ok"), True, f"MCP3: a legal sql read must succeed: {env}")
        rows = env.get("data", {}).get("rows")
        self.assertIsInstance(rows, list, "MCP3: sql data.rows must be a list")
        self.assertEqual(int(rows[0][0]), 2, "MCP3: the rejected DELETE must not have removed rows")
        session.stop()


class _BlockingRunner:
    """Injected background-sync seam: records the call, then blocks until
    the test releases it (models a sync holding the run lock)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, db_path, repo) -> None:
        self.calls.append((str(db_path), str(repo)))
        self.started.set()
        self.release.wait(timeout=15)


class McpFreshnessTests(TempDBTest):
    """AC5: a stale read answers immediately from current data, starts ONE
    locked incremental sync in the background, and reports
    freshness.refreshing true; a second read while it runs starts no other."""

    def test_stale_read_answers_now_and_refreshes_in_background(self) -> None:
        self.src.add_issue(issue(1, title="fresh one", body="needle", state="open"))
        self.src.add_issue(issue(2, title="fresh two", body="see #1", state="closed"))
        self.sync()
        # Force staleness: both sync stamps far in the past.
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

        # Read 1: answered NOW from current data, refreshing reported.
        response = session.call_tool("graph_status", {"repo": REPO})
        result, env = tool_envelope(self, response, "MCP5")
        self.assertIs(result.get("isError", False), False, "MCP5: the stale read must still answer")
        self.assertIs(env.get("ok"), True, f"MCP5: the stale read must succeed: {env}")
        repos = env.get("data", {}).get("repos")
        self.assertIsInstance(repos, list, "MCP5: graph_status data.repos must be a list")
        self.assertEqual(int(repos[0]["item_count"]), 2, "MCP5: the answer must come from the CURRENT data")
        fresh = env.get("freshness")
        self.assertIsInstance(fresh, dict, "MCP5: the envelope must carry freshness")
        self.assertIsInstance(fresh.get("age_s"), int, "MCP5: freshness.age_s must be an int")
        self.assertGreater(fresh["age_s"], 0, "MCP5: the DB was made stale; age_s must show it")
        self.assertIs(
            fresh.get("refreshing"), True, "MCP5: a stale read must report freshness.refreshing true"
        )

        # Exactly one background sync started (wait for its first action).
        if not runner.started.wait(timeout=10):
            self.fail("MCP5: the stale read never started a background sync")
        self.assertEqual(
            runner.calls,
            [(str(self.db_path), REPO)],
            "MCP5: the sync seam must be called once with (db_path, repo)",
        )

        # Read 2 while the refresh is still in flight: answered, refreshing
        # reported, and NO second background sync started.
        response = session.call_tool("graph_status", {"repo": REPO})
        result, env = tool_envelope(self, response, "MCP5")
        self.assertIs(result.get("isError", False), False, "MCP5: the second read must still answer")
        self.assertIs(env.get("ok"), True, f"MCP5: the second read must succeed: {env}")
        self.assertIs(
            env.get("freshness", {}).get("refreshing"),
            True,
            "MCP5: a read while the refresh runs must report refreshing true",
        )
        time.sleep(0.3)  # grace: a buggy second start would have appended by now
        self.assertEqual(
            len(runner.calls),
            1,
            "MCP5: a second read while the refresh is in flight must not start another sync",
        )

        runner.release.set()
        session.stop()


class McpResourceTests(StoreHarness):
    """AC6: resources/read for zaxbygraph://schema returns the same DDL and
    column notes as the `schema` CLI command on the same database.

    The session's cwd is a real git checkout of REPO (resources/read carries
    no repo argument, so the server resolves it from roots/cwd); ZAXBYGRAPH_DB
    pins the resolved database to the seeded temp file for both sides.
    """

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

    def test_schema_resource_matches_cli(self) -> None:
        session = McpSession(self, cwd=str(self.checkout))
        self.addCleanup(session.stop)
        session.initialize()

        req_id = session.request("resources/list", {})
        response = session.wait_response(req_id)
        resources = (response.get("result") or {}).get("resources")
        self.assertIsInstance(
            resources, list, "MCP6: resources/list must answer with a resources list"
        )
        uris = [r.get("uri") for r in resources if isinstance(r, dict)]
        self.assertIn(
            "zaxbygraph://schema",
            uris,
            f"MCP6: resources/list must advertise zaxbygraph://schema, got {uris}",
        )

        req_id = session.request(
            "resources/read", {"uri": "zaxbygraph://schema"}
        )
        response = session.wait_response(req_id)
        contents = (response.get("result") or {}).get("contents")
        self.assertIsInstance(
            contents, list, "MCP6: resources/read must answer with contents"
        )
        self.assertTrue(contents, "MCP6: resources/read contents must not be empty")
        first = contents[0]
        self.assertIsInstance(first, dict, "MCP6: contents[0] must be an object")
        text = first.get("text")
        self.assertIsInstance(text, str, "MCP6: contents[0].text must be a string")
        try:
            resource_doc = json.loads(text)
        except ValueError:
            self.fail("MCP6: the schema resource text must parse as JSON")
        self.assertIsInstance(resource_doc, dict, "MCP6: the schema resource must be a JSON object")
        tables = resource_doc.get("tables")
        self.assertIsInstance(tables, list, "MCP6: the schema resource must carry tables")
        table_names = {t.get("name") for t in tables if isinstance(t, dict)}
        self.assertIn("edges", table_names, "MCP6: the schema resource must cover the edges table")

        # The `schema` CLI command on the same DB: same DDL + column notes.
        code, out, err = run_cli(
            ["schema", "--repo", REPO, "--db", str(self.db), "--format", "json"]
        )
        self.assertEqual(code, 0, f"MCP6: the schema CLI command must succeed: {err}")
        cli_envelope = json.loads(out)
        cli_tables = cli_envelope.get("data", {}).get("tables")
        self.assertIsInstance(cli_tables, list, "MCP6: the schema CLI envelope must carry data.tables")
        self.assertEqual(
            tables,
            cli_tables,
            "MCP6: the schema resource must equal the schema CLI command's tables",
        )
        session.stop()


if __name__ == "__main__":
    unittest.main()
