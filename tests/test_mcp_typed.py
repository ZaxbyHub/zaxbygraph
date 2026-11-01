"""Acceptance checks for issue-trace 32-33-mcp-typed-stateless (issue #32).

Authored at arm's length against the issue contract AC1-AC4: the checks
are RED at the base tree and turn GREEN once the typed-tool surface is
implemented. Every failure carries a distinctive single-line marker
(`any_outputSchema False`, `any_annotations False`, `readOnlyHint wrong`,
`registry drift`, `no structuredContent`, `schema validation failed`,
`answers 2025-06-18`, `2025-06-18 echo broken`,
`unsupported negotiation broken`, `missing tools`, `cli envelope
mismatch`) so a line-wise grep can attribute each failure to exactly one
contract point.

CONTRACT PINNED (issue #32):

AC1 tools/list: every tool dict declares an `outputSchema` dict
(permissive: additionalProperties may be true) and an `annotations`
dict whose `readOnlyHint` is a boolean - True for every read tool, and
not True for "sync" (the one writing tool; absent-on-sync reads as
False). The registry is exactly the 16 tools of the extended surface.

AC2 tools/call: every result carries `structuredContent` equal to
json.loads(content[0]["text"]) - the CLI JSON envelope
{ok, db, repo, freshness, data, truncated} - for a successful call AND
a structured failure alike, and structuredContent validates against the
tool's outputSchema at the top level (at least the envelope keys "ok"
and "data").

AC3 initialize negotiation: "2025-11-25" is served and echoed; the
older "2025-06-18" stays served; an UNSERVED version ("1999-01-01") is
answered with the server's latest served version, never echoed.

AC4 the CLI-derived tools similar_items, triage, bundle, brief, churn
join the registry, and the churn / similar_items envelopes match the
CLI's shape on the same seeded store: churn data is a list of per-path
rows each carrying a count-like integer field, its first row's keys
equal to the CLI `churn` command's first-row keys on the same database;
similar_items data carries the CLI's derived payload keys (candidates,
capabilities, derived, ...), compared dynamically against the same CLI
call's data payload.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

# fixtures FIRST (suite convention), then the McpSession harness and the
# existing zaxbygraph surface, then the module under test LAST so a
# missing base module is a loud import failure, not a collection error.
from fixtures import REPO, FakeGitHubSource, issue, pr_file, pull
from test_mcp import McpSession, pin_env_for, run_cli
from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo

import zaxbygraph.mcp_server as mcp_server  # noqa: F401  (base-leg guard)

#: AC1: the exact extended typed-tool surface tools/list must advertise.
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
    "similar_items",
    "triage",
    "bundle",
    "brief",
    "churn",
}

#: AC4: the five CLI-derived tools joining the registry.
NEW_TOOLS = {"similar_items", "triage", "bundle", "brief", "churn"}

#: Huge staleness threshold: these checks must never kick a background
#: sync (they exercise the answer surface, not freshness).
_STALE_AFTER_S = 10**9

#: AC4 count-like churn field names accepted when matching a row's
#: PR-touch count. The live CLI row keys are compared dynamically; this
#: list only recognizes the count field structurally.
_COUNT_FIELD_NAMES = (
    "prs",
    "pr_count",
    "count",
    "prs_count",
    "touch_count",
    "touches",
)


def _seed_store(db_path: Path) -> None:
    """Offline corpus: two issues and two merged PRs touching two paths,
    so get_item/search answer and churn has per-path rows to rank."""
    src = FakeGitHubSource()
    src.add_issue(issue(1, title="seeded widget one", body="needle for search; see #3", state="open"))
    src.add_issue(issue(2, title="second issue", body="also mentions #1", state="closed"))
    src.add_pr(
        issue(3, title="touch a and b", body="fixes #2", kind="pr", state="closed"),
        pull(3, changed_files=2, merged=True),
        files=[pr_file("src/a.py"), pr_file("src/b.py")],
    )
    src.add_pr(
        issue(4, title="touch a again", body="", kind="pr", state="closed"),
        pull(4, changed_files=1, merged=True),
        files=[pr_file("src/a.py")],
    )
    conn = connect(db_path)
    init_schema(conn)
    sync_repo(conn, src, REPO)
    conn.close()


class TestMcpTypedToolSurface(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = self.root / "history.db"
        _seed_store(self.db_path)
        # Keep the user's real store/db out of resolution; the session
        # below pins repo/db explicitly on top of this.
        pin_env_for(self, self.root)

    def _session(self, **overrides) -> McpSession:
        kwargs: dict = {
            "cwd": str(self.root),
            "repo": REPO,
            "db": str(self.db_path),
            "stale_after_s": _STALE_AFTER_S,
        }
        kwargs.update(overrides)
        session = McpSession(self, **kwargs)
        self.addCleanup(session.stop)
        return session

    @staticmethod
    def _tool_entries(session: McpSession) -> list[dict]:
        req_id = session.request("tools/list", {})
        result = session.wait_response(req_id).get("result")
        if not isinstance(result, dict):
            raise AssertionError("AC1: tools/list must answer with a result object")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise AssertionError("AC1: tools/list result.tools must be a list")
        for tool in tools:
            if not isinstance(tool, dict):
                raise AssertionError("AC1: each tool entry must be an object")
        return tools

    def test_tools_declare_schema_and_annotations(self) -> None:
        """AC1: every tool declares an outputSchema dict and annotations
        with a correct readOnlyHint; the registry is exactly 16 tools.

        RED at base (no outputSchema, no annotations, 11 tools); GREEN
        after the fix pins the typed surface."""
        session = self._session()
        session.initialize()
        tools = self._tool_entries(session)
        names = {t.get("name") for t in tools}

        with self.subTest(part="registry"):
            # Cardinality BEFORE the set comparison: a duplicate registry
            # entry would pass a set-equality check while advertising a
            # broken list (pr49 review PRR-026).
            self.assertEqual(
                len(tools),
                16,
                "AC1 registry cardinality: tools/list must advertise exactly 16 "
                f"tool entries (a duplicate entry would inflate the list); got "
                f"{len(tools)}: {names}",
            )
            self.assertEqual(
                names,
                EXPECTED_TOOLS,
                "AC1 registry drift: tools/list must advertise exactly the 16-tool "
                f"registry; missing={sorted(EXPECTED_TOOLS - names)} "
                f"extra={sorted(names - EXPECTED_TOOLS)}",
            )

        with self.subTest(part="outputSchema"):
            without = [t.get("name") for t in tools if not isinstance(t.get("outputSchema"), dict)]
            self.assertFalse(
                without,
                "AC1 any_outputSchema False: every tool must declare an outputSchema "
                "dict (permissive: additionalProperties may be true); "
                f"missing on {without}",
            )

        with self.subTest(part="outputSchema content"):
            # Presence alone is not a schema: each declared outputSchema
            # must actually say type object and be permissive
            # (pr49 review PRR-028).
            for tool in tools:
                name = tool.get("name")
                schema = tool.get("outputSchema")
                self.assertIsInstance(
                    schema,
                    dict,
                    f"AC1 outputSchema content: tool {name!r} must declare an "
                    "outputSchema object whose content can be checked",
                )
                self.assertEqual(
                    schema.get("type"),
                    "object",
                    f"AC1 outputSchema content: tool {name!r} outputSchema must "
                    f"declare type 'object'; got {schema.get('type')!r}",
                )
                self.assertIs(
                    schema.get("additionalProperties"),
                    True,
                    f"AC1 outputSchema content: tool {name!r} outputSchema must be "
                    f"permissive (additionalProperties true); got "
                    f"{schema.get('additionalProperties')!r}",
                )

        with self.subTest(part="annotations"):
            without = [t.get("name") for t in tools if not isinstance(t.get("annotations"), dict)]
            self.assertFalse(
                without,
                "AC1 any_annotations False: every tool must carry an annotations "
                f"dict; missing on {without}",
            )

        with self.subTest(part="readOnlyHint"):
            for tool in tools:
                name = tool.get("name")
                annotations = tool.get("annotations")
                hint = annotations.get("readOnlyHint") if isinstance(annotations, dict) else None
                if name == "sync":
                    self.assertIsNot(
                        hint,
                        True,
                        "AC1 readOnlyHint wrong: sync is the one writing tool; its "
                        "readOnlyHint must be False (or absent)",
                    )
                    self.assertTrue(
                        hint is None or isinstance(hint, bool),
                        f"AC1 readOnlyHint wrong: sync readOnlyHint must be a boolean "
                        f"or absent, got {hint!r}",
                    )
                else:
                    self.assertIs(
                        hint,
                        True,
                        f"AC1 readOnlyHint wrong: read-only tool {name!r} must declare "
                        "readOnlyHint true",
                    )
        session.stop()

    def test_structured_content_matches_text(self) -> None:
        """AC2: tools/call results carry structuredContent equal to the
        parsed text envelope, for successes and structured failures.

        RED at base (results carry only content/isError); GREEN after the
        fix adds structured output."""
        session = self._session()
        session.initialize()
        cases = (
            ("success", {"number": 1}, True),
            ("failure", {"number": 999999}, False),
        )
        for label, arguments, want_ok in cases:
            with self.subTest(call=label):
                reply = session.call_tool("get_item", arguments)
                result = reply.get("result")
                self.assertIsInstance(
                    result, dict, f"AC2: the {label} call must answer with a result object"
                )
                self.assertIn(
                    "structuredContent",
                    result,
                    f"AC2 no structuredContent: the {label} tools/call result must "
                    "carry structuredContent (issue #32 AC2)",
                )
                structured = result.get("structuredContent")
                self.assertIsInstance(
                    structured, dict, "AC2 no structuredContent: structuredContent must be an object"
                )
                content = result.get("content")
                self.assertIsInstance(content, list, f"AC2: the {label} result must carry content")
                self.assertTrue(content, f"AC2: the {label} content must not be empty")
                first = content[0]
                self.assertIsInstance(first, dict, f"AC2: content[0] must be an object")
                text = first.get("text")
                self.assertIsInstance(text, str, f"AC2: content[0].text must be a string")
                envelope = json.loads(text)
                self.assertIs(
                    envelope.get("ok"),
                    want_ok,
                    f"AC2: the {label} envelope must answer ok={want_ok}: {envelope}",
                )
                self.assertEqual(
                    structured,
                    envelope,
                    f"AC2 structuredContent mismatch: structuredContent must equal "
                    f"json.loads(content[0].text) for the {label} call",
                )
                for key in ("ok", "data"):
                    self.assertIn(
                        key,
                        structured,
                        f"AC2 schema validation failed: structuredContent must carry the "
                        f"envelope key {key!r} (top-level outputSchema shape)",
                    )

        # The generic-exception serialization site: an unexpected handler
        # fault must ALSO answer as a tool result carrying structuredContent
        # (issue #32 AC2 covers "succeeds or fails" — every failure path).
        saved = mcp_server._TOOL_HANDLERS.get("get_item")
        try:
            def _boom(_arguments, _ctx):
                raise RuntimeError("injected fault for the generic-exception leg")
            mcp_server._TOOL_HANDLERS["get_item"] = _boom
            reply = session.call_tool("get_item", {"number": 1})
        finally:
            if saved is not None:
                mcp_server._TOOL_HANDLERS["get_item"] = saved
        result = reply.get("result")
        self.assertIsInstance(
            result, dict, "AC2: the faulting call must answer with a result object"
        )
        self.assertTrue(
            result.get("isError"),
            "AC2: the faulting call must answer isError=true",
        )
        self.assertIn(
            "structuredContent",
            result,
            "AC2 no structuredContent: the generic-exception tools/call result must "
            "also carry structuredContent (every failure path, issue #32 AC2)",
        )
        self.assertIsInstance(
            result.get("structuredContent"),
            dict,
            "AC2 no structuredContent: the generic-exception structuredContent must "
            "be an object",
        )
        # Presence alone is not parity on the generic path either: the
        # generic-exception structuredContent must equal the parsed text
        # block and carry the coded runtime error, like every other
        # failure path (pr49 review PRR-027).
        fault_content = result.get("content")
        self.assertIsInstance(
            fault_content, list, "AC2: the generic-exception result must carry content"
        )
        self.assertTrue(fault_content, "AC2: the generic-exception content must not be empty")
        fault_first = fault_content[0]
        self.assertIsInstance(
            fault_first, dict, "AC2: the generic-exception content[0] must be an object"
        )
        fault_envelope = json.loads(fault_first.get("text"))
        self.assertEqual(
            result.get("structuredContent"),
            fault_envelope,
            "AC2 no structuredContent: the generic-exception structuredContent must "
            "equal json.loads(content[0].text) (the runtime envelope)",
        )
        self.assertEqual(
            ((result.get("structuredContent") or {}).get("error") or {}).get("code"),
            "runtime",
            "AC2 no structuredContent: the generic-exception envelope must carry "
            "error.code 'runtime'",
        )
        session.stop()

    def test_negotiates_2025_11_25(self) -> None:
        """AC3: initialize serves and echoes 2025-11-25, keeps echoing the
        older 2025-06-18, and answers an unserved version with the latest
        served one.

        RED at base (2025-11-25 is answered 2025-06-18); GREEN after the
        fix raises the served set."""
        session = self._session()

        def ask(version: str) -> str:
            req_id = session.request(
                "initialize",
                {
                    "protocolVersion": version,
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "0"},
                },
            )
            result = session.wait_response(req_id).get("result")
            self.assertIsInstance(
                result, dict, f"AC3: initialize({version!r}) must answer with a result object"
            )
            answered = result.get("protocolVersion")
            self.assertIsInstance(
                answered, str, f"AC3: initialize({version!r}) must answer a protocolVersion string"
            )
            return answered

        with self.subTest(version="2025-11-25"):
            answered = ask("2025-11-25")
            self.assertEqual(
                answered,
                "2025-11-25",
                "AC3 answers 2025-06-18: initialize(2025-11-25) was answered "
                f"{answered!r}; the server must serve 2025-11-25",
            )

        with self.subTest(version="2025-06-18"):
            answered = ask("2025-06-18")
            self.assertEqual(
                answered,
                "2025-06-18",
                f"AC3 2025-06-18 echo broken: initialize(2025-06-18) was answered {answered!r}",
            )

        with self.subTest(version="1999-01-01 (unserved)"):
            answered = ask("1999-01-01")
            self.assertNotEqual(
                answered,
                "1999-01-01",
                "AC3 unsupported negotiation broken: an unserved protocolVersion "
                "must never be echoed back",
            )
            # The fallback must be the server's LATEST served version, pinned
            # EXACTLY: a lexicographic >= would let any future latest (or a
            # mis-ordered served tuple) slip through unasserted
            # (pr49 review PRR-025).
            self.assertEqual(
                answered,
                "2025-11-25",
                "AC3 answers 2025-06-18: an unserved protocolVersion must be "
                "answered with the server's latest served version 2025-11-25, got "
                f"{answered!r}",
            )
        session.stop()

    def test_new_tools_match_cli(self) -> None:
        """AC4: similar_items, triage, bundle, brief, churn join the
        registry, and churn / similar_items answer the CLI envelope shape
        on the same seeded store.

        RED at base (none of the five tools exist); GREEN after the fix
        exposes the CLI-derived surface over MCP."""
        session = self._session()
        session.initialize()

        names = {t.get("name") for t in self._tool_entries(session)}
        missing = sorted(NEW_TOOLS - names)
        with self.subTest(part="registry"):
            self.assertEqual(
                missing,
                [],
                f"AC4 missing tools: tools/list lacks {missing}; got {sorted(names)}",
            )

        # CLI parity by construction is not assumed: each tool's data
        # payload must be JSON-equal to the same CLI command's data payload
        # on the SAME seeded store (issue #32 AC4 "same envelopes as the
        # CLI commands"). Freshness/db identity fields live outside data.
        parity = (
            ("churn", {}, ["churn"]),
            ("churn", {"merged_only": True}, ["churn", "--merged-only"]),
            ("similar_items", {"number": 1}, ["similar", "1"]),
            ("triage", {}, ["triage"]),
            ("bundle", {}, ["bundle"]),
            ("brief", {"number": 1}, ["brief", "1"]),
        )
        for tool_name, tool_args, cli_argv in parity:
            with self.subTest(part=tool_name):
                reply = session.call_tool(tool_name, tool_args)
                result = reply.get("result")
                if not isinstance(result, dict):
                    self.fail(
                        f"AC4 cli envelope mismatch: {tool_name} answered a JSON-RPC "
                        f"error {reply.get('error')!r} instead of a tools/call result"
                    )
                structured = result.get("structuredContent")
                if not isinstance(structured, dict):
                    self.fail(
                        f"AC4 cli envelope mismatch: the {tool_name} result must carry "
                        "a structuredContent envelope"
                    )
                code, out, err = run_cli(
                    cli_argv + ["--repo", REPO, "--db", str(self.db_path), "--format", "json"]
                )
                self.assertEqual(
                    code,
                    0,
                    f"AC4 cli envelope mismatch: the CLI {cli_argv[0]} command itself "
                    f"must run on the same seeded store (rc={code}, err={err[:200]})",
                )
                cli_data = json.loads(out).get("data")
                self.assertEqual(
                    structured.get("data"),
                    cli_data,
                    f"AC4 cli envelope mismatch: the {tool_name} tool data payload must "
                    f"JSON-equal the CLI {cli_argv[0]} data payload on the same store "
                    f"(tool={json.dumps(structured.get('data'))[:200]} "
                    f"cli={json.dumps(cli_data)[:200]})",
                )

        # _COUNT_FIELD_NAMES is not dead weight: every churn row must carry
        # an integer count field from it — the structural recognition the
        # dynamic CLI-parity comparison cannot express alone
        # (pr49 review PRR-029).
        with self.subTest(part="churn count fields"):
            reply = session.call_tool("churn", {})
            structured = (reply.get("result") or {}).get("structuredContent")
            self.assertIsInstance(
                structured,
                dict,
                "AC4 cli envelope mismatch: the churn call must carry a "
                "structuredContent envelope",
            )
            rows = structured.get("data")
            self.assertIsInstance(
                rows,
                list,
                "AC4 cli envelope mismatch: churn data must be a list of per-path "
                f"rows; got {type(rows).__name__}",
            )
            self.assertTrue(
                rows,
                "AC4 cli envelope mismatch: the seeded store must yield churn rows",
            )
            for row in rows:
                self.assertIsInstance(
                    row,
                    dict,
                    "AC4 cli envelope mismatch: each churn row must be an object; "
                    f"got {row!r}",
                )
                count_fields = [
                    key
                    for key in _COUNT_FIELD_NAMES
                    if isinstance(row.get(key), int) and not isinstance(row.get(key), bool)
                ]
                self.assertTrue(
                    count_fields,
                    f"AC4 cli envelope mismatch: each churn row must carry an "
                    f"integer count field from {_COUNT_FIELD_NAMES}; row keys "
                    f"{sorted(row)}",
                )

        # Failure parity: the derived tools translate the CLI's coded error
        # vocabulary (issue #32 AC4 "same envelopes as the CLI commands"
        # covers failures; not_found and bad_request are reachable offline).
        with self.subTest(part="failure parity not_found"):
            reply = session.call_tool("similar_items", {"number": 999999})
            structured = (reply.get("result") or {}).get("structuredContent")
            self.assertIsInstance(structured, dict, "AC4 cli envelope mismatch: the not_found leg must carry structuredContent")
            self.assertIs(structured.get("ok"), False, f"AC4 cli envelope mismatch: the not_found leg must answer ok=false: {structured}")
            self.assertEqual(
                (structured.get("error") or {}).get("code"),
                "not_found",
                "AC4 cli envelope mismatch: an unknown item must answer the coded "
                f"not_found error; got {(structured.get('error') or {}).get('code')!r}",
            )
        with self.subTest(part="failure parity bad_request"):
            reply = session.call_tool("bundle", {"size": "banana"})
            structured = (reply.get("result") or {}).get("structuredContent")
            self.assertIsInstance(structured, dict, "AC4 cli envelope mismatch: the bad_request leg must carry structuredContent")
            self.assertEqual(
                (structured.get("error") or {}).get("code"),
                "bad_request",
                "AC4 cli envelope mismatch: an invalid bundle size must answer the coded "
                f"bad_request error; got {(structured.get('error') or {}).get('code')!r}",
            )
        with self.subTest(part="bundle invalid element"):
            reply = session.call_tool("bundle", {"pool": [1, "x"]})
            structured = (reply.get("result") or {}).get("structuredContent")
            self.assertIsInstance(structured, dict, "AC4 cli envelope mismatch: the invalid-element leg must carry structuredContent")
            self.assertEqual(
                (structured.get("error") or {}).get("code"),
                "bad_request",
                "AC4 cli envelope mismatch: a non-integer pool element must answer the "
                f"coded bad_request error; got {(structured.get('error') or {}).get('code')!r}",
            )
        # Nondefault arrays: pool/exclude ride as strict int arrays and must
        # reach the query layer as canonical CSV, matching the CLI's
        # --pool/--exclude raw-string form on the same store.
        with self.subTest(part="bundle arrays parity"):
            reply = session.call_tool("bundle", {"pool": [1, 2], "exclude": [4]})
            result = reply.get("result")
            structured = (result or {}).get("structuredContent")
            self.assertIsInstance(structured, dict, "AC4 cli envelope mismatch: the arrays leg must carry structuredContent")
            code, out, err = run_cli(
                ["bundle", "--pool", "1,2", "--exclude", "4", "--repo", REPO, "--db", str(self.db_path), "--format", "json"]
            )
            if code == 0:
                cli_data = json.loads(out).get("data")
                self.assertEqual(
                    structured.get("data"),
                    cli_data,
                    "AC4 cli envelope mismatch: bundle(pool=[1,2], exclude=[4]) data must "
                    f"JSON-equal the CLI --pool 1,2 --exclude 4 data "
                    f"(tool={json.dumps(structured.get('data'))[:160]} cli={json.dumps(cli_data)[:160]})",
                )
            else:
                cli_code = ((json.loads(out).get("error") or {}).get("code"))
                self.assertEqual(
                    (structured.get("error") or {}).get("code"),
                    cli_code,
                    "AC4 cli envelope mismatch: bundle arrays leg must parity the CLI's "
                    f"coded error when the CLI itself fails (tool="
                    f"{(structured.get('error') or {}).get('code')!r} cli={cli_code!r})",
                )
        session.stop()

    def test_churn_store_fault_is_coded(self) -> None:
        """AC13 (implementation-review finding): a churn store fault answers
        the CLI's coded store_unreadable envelope with identity, never the
        generic runtime path - churn skips _translated_derive (it is a
        query.py read), so its translation lives in the handler.

        RED at base (no churn tool: the call answers a JSON-RPC error);
        GREEN after the fix translates churn's store faults."""
        session = self._session()
        session.initialize()
        # Break the store the way a corrupt/clobbered file does: drop a
        # table every schema version has carried (a post-open fault on the
        # next handler's own connection).
        fault_conn = sqlite3.connect(str(self.db_path))
        fault_conn.execute("DROP TABLE pr_files")
        fault_conn.commit()
        fault_conn.close()
        reply = session.call_tool("churn", {})
        result = reply.get("result")
        structured = (result or {}).get("structuredContent")
        self.assertIsInstance(
            structured,
            dict,
            "AC13 churn store fault: the churn call must answer a "
            "structuredContent envelope",
        )
        self.assertIs(
            structured.get("ok"),
            False,
            f"AC13 churn store fault: must answer ok=false: {structured}",
        )
        self.assertEqual(
            (structured.get("error") or {}).get("code"),
            "store_unreadable",
            "AC13 churn store fault: must carry the coded store_unreadable error; got "
            f"{(structured.get('error') or {}).get('code')!r}",
        )
        self.assertEqual(
            structured.get("repo"),
            REPO,
            "AC13 churn store fault: identity lost (repo)",
        )
        self.assertEqual(
            structured.get("db"),
            str(self.db_path),
            "AC13 churn store fault: identity lost (db)",
        )
        session.stop()


if __name__ == "__main__":
    unittest.main()
