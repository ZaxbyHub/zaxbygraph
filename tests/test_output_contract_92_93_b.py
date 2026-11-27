"""Acceptance checks for issue-trace 92-93-output-contract-honesty (issues
#92+#93, decisions D5/D6/D7/D9), amended through plan-critic rounds 1-2
and review wave 5. Companions: _92_93.py (D1-D3), _d.py (D4), _c.py (D8),
contract data in _e.py, runtime helpers in _f.py. Pinned decisions:

- D5 (AC6) EXACT snippets, independent of production strings: claude is
  the unchanged command line; zcode/opencode are JSON strings parsing to
  the mcp.servers.zaxbygraph shapes. Healthy init, JOINED init and doctor
  carry data['mcp_clients'] == the snippets; init's `mcp` key stays the
  Claude line (PRESERVING; round 5: the endswith('init') pin actually
  runs now). README fidelity (round 5: the section slice is BOUNDED at
  the next level-2 heading): the registration section's fenced JSON
  blocks EQUAL the pinned configs, the stale mcp.<name> shape is ABSENT,
  the claude command appears verbatim, all three clients are named.
- D6 (AC12): every envelope carries "version": "0.3" == cli.ENVELOPE_VERSION
  (CLI json success/failure/usage-error, compact, MCP both ways; jsonl
  rows keep parsing).
- D7 (AC13): _ENVELOPE_KEYS includes "version"; every tool's outputSchema
  (registry AND advertised tools/list) requires it as a string; existing
  keys retained (PRESERVING).
- D9 (AC14): zero-row out-of-vocabulary reads answer the annotate-only
  "hints" key; valid_values EQUALS the CANONICAL closed set in production
  order (round 5: items.state is [open, closed, deleted, transferred] —
  sync._mark_gone writes the extra two). The hint fires on zero rows + a
  literal outside the canonical set, INCLUDING the empty literal ('',
  round 5) and empty-table corpora, on CLI sql AND MCP sql; no hint when
  rows are non-empty or the literal is valid (round 5: deleted/
  transferred are valid); `warnings` unchanged (PRESERVING). Filtered
  reads refuse (`open --kind` at parse; query.open_items raises; MCP
  open_items has no kind filter — no MCP leg by design). Both surfaces
  DECLARE the optional typed hints array (see _f).
- D9 (AC15): in-vocabulary behavior stays byte-identical to base
  (tests.test_query + tests.test_search green) plus exact-row pins.

Hermetic: FakeGitHubSource, tempfile stores, patched seams; no network,
no gh. Failures carry OC-AC5/6/12/13/14/15 tags.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import zaxbygraph

_WORKTREE = Path(__file__).resolve().parents[1]
if not Path(zaxbygraph.__file__).resolve().is_relative_to(_WORKTREE / "src"):
    raise AssertionError(
        f"OC-ENV: zaxbygraph resolved to {zaxbygraph.__file__}, not this"
        f" worktree's src ({_WORKTREE / 'src'}); set"
        f" PYTHONPATH=<worktree>/src{os.pathsep}<worktree>/tests"
    )

from fixtures import REPO, FakeGitHubSource, issue  # noqa: E402
from test_mcp import McpSession, pin_env_for, run_cli, tool_envelope  # noqa: E402
from test_output_contract_92_93_e import (  # noqa: E402
    CLAUDE_SNIPPET, OPENCODE_CONFIG, ZCODE_CONFIG, hint_problems,
    parse_envelope, readme_json_fences, seed_corpus,
)
from test_output_contract_92_93_f import hints_declaration_problems  # noqa: E402
from test_release import MCP_LINE  # noqa: E402
from zaxbygraph import cli as cli_mod  # noqa: E402

_NO_REFRESH_S = 10**9


def registration_readme_problems(readme: str) -> list:
    """The README registration-section fidelity problems (round 5, cubic
    #14: the slice is BOUNDED at the next level-2 heading, so fences that
    moved to a later section are missing here and stale shapes in later
    sections never flag)."""

    match = re.search(r"^## MCP server\s*$", readme, re.M)
    section = re.split(r"^## ", readme[match.end():], maxsplit=1, flags=re.M)[0] if match else ""
    problems: list = []
    low = re.sub(r"\s+", " ", section).lower()
    for client in ("claude", "zcode", "opencode"):
        if client not in low:
            problems.append(f"the README MCP registration guidance must name the {client!r} client")
    if CLAUDE_SNIPPET not in section:
        problems.append(f"the README must carry the claude command verbatim: {CLAUDE_SNIPPET!r}")
    fences = readme_json_fences(section)
    for client, expected in (("zcode", ZCODE_CONFIG), ("opencode", OPENCODE_CONFIG)):
        if not any(fence == expected for fence in fences):
            problems.append(f"the README registration section must carry a fenced JSON block exactly equal to the {client} config {json.dumps(expected)}; parsed blocks: {fences}")
    for fence in fences:
        mcp_block = fence.get("mcp")
        if isinstance(mcp_block, dict) and "servers" not in mcp_block:
            problems.append(f"the stale opencode shape (mcp.<name> without the servers level) must be ABSENT from the README; found {json.dumps(fence)}")
    return problems


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db = self.root / "history.db"
        seed_corpus(self.db)
        pin_env_for(self, self.root, db=self.db)
        self.base = ["--repo", REPO, "--db", str(self.db), "--format", "json"]

    def _mcp(self):
        from test_output_contract_92_93_f import NoopRunner

        session = McpSession(self, cwd=str(self.root), repo=REPO, db=str(self.db), stale_after_s=_NO_REFRESH_S, sync_runner=NoopRunner())
        self.addCleanup(session.stop)
        session.initialize()
        return session


class TestAc6RegistrationSnippets(_StoreCase):
    """D5 (AC6): exact per-client MCP registration guidance, carried by
    healthy init, JOINED init, and doctor through one shared dict."""

    def test_ac6_registration_snippets_cover_clients(self) -> None:
        snippets = getattr(cli_mod, "MCP_REGISTRATION_SNIPPETS", None)
        self.assertIsInstance(
            snippets, dict,
            f"OC-AC6: cli must expose MCP_REGISTRATION_SNIPPETS as a dict of client -> command/config string; got {snippets!r}",
        )
        problems: list[str] = []
        low_keys = {str(key).lower() for key in (snippets or {})}
        for client in ("claude", "zcode", "opencode"):
            if not any(client in key for key in low_keys):
                problems.append(f"snippet keys must cover {client!r}; got {sorted(snippets or {})}")
        for key, value in (snippets or {}).items():
            blob = str(value).lower()
            if "zaxbygraph" not in blob or "mcp" not in blob:
                problems.append(f"the {key} snippet must name zaxbygraph and mcp; got {value!r}")
        # EXACT snippets, independent of production strings.
        if isinstance(snippets, dict):
            if snippets.get("claude") != CLAUDE_SNIPPET:
                problems.append(f"the claude snippet must be the unchanged command line {CLAUDE_SNIPPET!r}; got {snippets.get('claude')!r}")
            for client, expected in (("zcode", ZCODE_CONFIG), ("opencode", OPENCODE_CONFIG)):
                value = snippets.get(client)
                try:
                    parsed = json.loads(value) if isinstance(value, str) else None
                except ValueError:
                    parsed = None
                if parsed != expected:
                    problems.append(f"the {client} snippet must parse to {json.dumps(expected)}; got {value!r} (parsed {parsed!r})")
        # Healthy init (offline), JOINED init (joins a running sync), and
        # doctor: all three payloads carry mcp_clients == the shared dict;
        # the init `mcp` key stays the Claude line (PRESERVING).
        import zaxbygraph.claims as claims_mod

        class _AuthedGh:
            def __call__(self, argv):
                if argv[:2] == ["gh", "--version"]:
                    return (0, "gh version 2.92.0\n", "")
                return (0, " Logged in to github.com\n", "")

        def _fake_sync_core(conn, slug, **kwargs):
            src = FakeGitHubSource()
            src.add_issue(issue(1, title="init probe", body="hello"))
            from zaxbygraph.sync import sync_repo

            sync_repo(conn, src, slug)
            return {"ingested": 1, "items": 1}

        for label, argv, patch_lock in (
            ("healthy init", ["init", "--repo", REPO, "--db", str(self.root / "init.db"), "--format", "json"], False),
            ("joined init", ["init", "--repo", REPO, "--db", str(self.db), "--format", "json"], True),
            ("doctor", ["doctor", *self.base], False),
        ):
            saved = (claims_mod.run_gh, getattr(cli_mod, "_sync_core", None), cli_mod.acquire_sync_lock)
            claims_mod.run_gh = _AuthedGh()
            cli_mod._sync_core = _fake_sync_core
            if patch_lock:
                cli_mod.acquire_sync_lock = lambda db_path, wait=False: None
            try:
                code, out, err = run_cli(argv)
            finally:
                claims_mod.run_gh, cli_mod.acquire_sync_lock = saved[0], saved[2]
                if saved[1] is not None:
                    cli_mod._sync_core = saved[1]
            env = parse_envelope(self, out, "OC-AC6")
            data = env.get("data") or {}
            if code != 0:
                problems.append(f"{label}: must succeed; got exit {code}, err {err[:160]!r}")
            if label == "joined init" and data.get("joined") is not True:
                problems.append(f"joined init: data.joined must be true; got {data.get('joined')!r}")
            if data.get("mcp_clients") != snippets:
                problems.append(f"{label}: data['mcp_clients'] must equal MCP_REGISTRATION_SNIPPETS; got {data.get('mcp_clients')!r}")
            if label.endswith("init") and data.get("mcp") != MCP_LINE:
                problems.append(f"{label}: the init `mcp` key stays the unchanged Claude line (PRESERVING); got {data.get('mcp')!r}")

        # README fidelity: the registration section (BOUNDED, round 5)
        # carries the pinned zcode/opencode fences, no stale mcp.<name>
        # shape, and the verbatim claude command (see the helper + its
        # self-test).
        problems += registration_readme_problems((_WORKTREE / "README.md").read_text(encoding="utf-8"))
        self.assertFalse(
            problems,
            "OC-AC6: exact per-client registration snippets and their surfaces:\n- "
            + "\n- ".join(problems),
        )

    def test_ac6_registration_section_bounded_selftest(self) -> None:
        # ROUND 5 (cubic #14): the fidelity scan covers ONLY the '## MCP
        # server' section (bounded at the next level-2 heading). Fences
        # moved under a later heading must be flagged MISSING, and a stale
        # mcp.<name> fence in a later section must NOT flag.
        moved = (
            "## MCP server\n\nRegister it (Claude Code):\n\n```bash\n"
            + CLAUDE_SNIPPET + "\n```\n\n## License\n\n```json\n"
            + json.dumps(ZCODE_CONFIG) + "\n```\n\n```json\n"
            + json.dumps(OPENCODE_CONFIG) + "\n```\n"
        )
        problems = registration_readme_problems(moved)
        self.assertTrue(
            any("zcode" in p for p in problems) and any("opencode" in p for p in problems),
            f"OC-AC6: SELF-TEST: fences under a later heading must be flagged missing; got {problems!r}",
        )
        stale_later = (
            "## MCP server\n\nRegister it (Claude Code; ZCode and opencode below):\n\n```bash\n"
            + CLAUDE_SNIPPET + "\n```\n\n```json\n" + json.dumps(ZCODE_CONFIG)
            + "\n```\n\n```json\n" + json.dumps(OPENCODE_CONFIG) + "\n```\n\n"
            "## License\n\n```json\n" + json.dumps({"mcp": {"zaxbygraph": {"type": "local", "command": ["zaxbygraph", "mcp"]}}}) + "\n```\n"
        )
        problems = registration_readme_problems(stale_later)
        self.assertEqual(
            problems, [],
            f"OC-AC6: SELF-TEST: a stale mcp.<name> fence in a LATER section must not flag; got {problems!r}",
        )


class TestAc12EnvelopeVersion(_StoreCase):
    """D6 (AC12): every envelope names its contract version '0.3'."""

    def test_ac12_envelope_carries_version(self) -> None:
        self.assertEqual(
            getattr(cli_mod, "ENVELOPE_VERSION", None), "0.3",
            f"OC-AC12: cli.ENVELOPE_VERSION must be '0.3'; got {getattr(cli_mod, 'ENVELOPE_VERSION', None)!r}",
        )
        problems: list[str] = []

        def check(label, argv, want_code, want_ok):
            code, out, err = run_cli(argv)
            env = parse_envelope(self, out, "OC-AC12")
            version = env.get("version")
            if code != want_code or env.get("ok") is not want_ok:
                problems.append(f"{label}: exit {code}, ok {env.get('ok')!r}")
            if version != "0.3" or version != getattr(cli_mod, "ENVELOPE_VERSION", None):
                problems.append(
                    f"{label}: version must be the string '0.3' equal to cli.ENVELOPE_VERSION; got {version!r}"
                )

        check("CLI json success", ["status", *self.base], 0, True)
        check("CLI json failure", ["item", "99999", *self.base], 1, False)
        check("CLI usage error", ["status", "--format", "json", "--definitely-not-a-flag", "--db", str(self.db)], 2, False)
        check("CLI compact", ["status", *self.base, "--format", "compact"], 0, True)
        # jsonl list payloads print rows only: jsonl must simply not break.
        code, out, err = run_cli(["open", *self.base, "--format", "jsonl"])
        lines = [line for line in out.splitlines() if line.strip()]
        if code != 0 or not lines:
            problems.append(f"CLI jsonl: exit {code}, {len(lines)} lines")
        for line in lines[:3]:
            try:
                row = json.loads(line)
            except ValueError:
                problems.append(f"CLI jsonl: a row line does not parse: {line[:80]!r}")
                break
            if not isinstance(row, dict):
                problems.append(f"CLI jsonl: a row line is not an object: {line[:80]!r}")
                break

        session = self._mcp()
        for label, tool, arguments, want_ok in (
            ("MCP success", "graph_status", {"repo": REPO}, True),
            ("MCP failure", "get_item", {"repo": REPO, "number": 99999}, False),
        ):
            result, env = tool_envelope(self, session.call_tool(tool, arguments), "OC-AC12")
            version = env.get("version")
            if env.get("ok") is not want_ok or version != "0.3" or version != getattr(cli_mod, "ENVELOPE_VERSION", None):
                problems.append(f"{label}: ok {env.get('ok')!r}, version {version!r}")
        session.stop()
        self.assertFalse(
            problems,
            "OC-AC12: every envelope must carry version '0.3' (json AND"
            " compact; jsonl rows keep parsing):\n- " + "\n- ".join(problems),
        )


class TestAc13McpSchemaVersion(_StoreCase):
    """D7 (AC13): the envelope version is declared in the MCP schemas --
    the module registry AND every ADVERTISED tool's outputSchema."""

    def test_ac13_mcp_schema_declares_version(self) -> None:
        import zaxbygraph.mcp_server as mcp_server

        keys = getattr(mcp_server, "_ENVELOPE_KEYS", None)
        self.assertIsNotNone(
            keys,
            "OC-AC13: mcp_server._ENVELOPE_KEYS must exist (the envelope key"
            " registry the outputSchemas' required lists derive from)",
        )
        problems: list[str] = []
        for existing in ("ok", "db", "repo", "freshness", "data", "truncated"):
            if existing not in keys:
                problems.append(f"_ENVELOPE_KEYS must retain the existing key {existing!r} (PRESERVING)")
        if "version" not in keys:
            problems.append(f"_ENVELOPE_KEYS must include 'version'; got {keys!r}")

        def check_schema(name, schema):
            if not isinstance(schema, dict):
                problems.append(f"{name}: no outputSchema object")
                return
            required = schema.get("required") or []
            if "version" not in required:
                problems.append(f"{name}: outputSchema.required lacks 'version' (required: {required})")
            prop = (schema.get("properties") or {}).get("version")
            declared = prop.get("type") if isinstance(prop, dict) else None
            names = declared if isinstance(declared, list) else ([declared] if declared else [])
            if "string" not in names:
                problems.append(f"{name}: version must be declared a string property; got {prop!r}")
            for existing in ("ok", "db", "repo", "freshness", "data", "truncated"):
                if existing not in required:
                    problems.append(f"{name}: outputSchema.required lost the existing key {existing!r} (PRESERVING)")

        for tool in getattr(mcp_server, "_TOOLS", []) or []:
            check_schema(tool.get("name"), tool.get("outputSchema"))
        # Every ADVERTISED tool (the live tools/list this server answers).
        session = self._mcp()
        listing = session.wait_response(session.request("tools/list", {})).get("result") or {}
        advertised = listing.get("tools") or []
        if not advertised:
            problems.append("tools/list must advertise at least one tool")
        for tool in advertised:
            check_schema(tool.get("name"), tool.get("outputSchema"))
        session.stop()
        self.assertFalse(
            problems,
            "OC-AC13: every tool's outputSchema (registry AND advertised)"
            " must declare the envelope version (required + string) while"
            " retaining the existing keys:\n- " + "\n- ".join(problems),
        )


class TestAc14ZeroRowVocabularyHint(_StoreCase):
    """D9 (AC14): zero-row out-of-vocabulary reads answer the annotate-only
    hints block with the CANONICAL valid_values; filtered reads refuse."""

    def test_ac14_zero_row_vocabulary_hint(self) -> None:
        problems: list[str] = []
        session = self._mcp()

        def run_sql(surface: str, statement: str):
            """(ok-or-exit, data) for one sql read on the CLI or the MCP tool."""
            if surface == "CLI":
                code, out, _err = run_cli(["sql", statement, *self.base])
                env = parse_envelope(self, out, "OC-AC14")
                return code, env.get("ok"), (env.get("data") or {})
            result, env = tool_envelope(self, session.call_tool("sql", {"repo": REPO, "statement": statement}), "OC-AC14")
            return None, env.get("ok"), (env.get("data") or {})

        # HINT legs: zero rows + a literal outside the CANONICAL set,
        # INCLUDING the empty comments table and (round 5, cubic #12) the
        # EMPTY literal '' — outside every vocabulary; state='merged'
        # hints with the 4-value set in production order.
        for surface, statement, column, literal in (
            ("CLI", "SELECT number FROM items WHERE kind = 'pull_request'", "items.kind", "pull_request"),
            ("MCP", "SELECT number FROM items WHERE kind = 'pull_request'", "items.kind", "pull_request"),
            ("CLI", "SELECT number FROM items WHERE state = 'merged'", "items.state", "merged"),
            ("CLI", "SELECT pk FROM comments WHERE kind = 'zzz'", "comments.kind", "zzz"),
            ("MCP", "SELECT pk FROM comments WHERE kind = 'zzz'", "comments.kind", "zzz"),
            ("CLI", "SELECT number FROM items WHERE kind = ''", "items.kind", ""),
            ("CLI", "SELECT number FROM items WHERE state = ''", "items.state", ""),
        ):
            code, ok, data = run_sql(surface, statement)
            if code == 1 or ok is not True or data.get("rows") != []:
                problems.append(f"{surface} {literal!r}: exit {code}, ok {ok!r}, rows {data.get('rows')!r}")
            problems += hint_problems(data.get("hints"), f"OC-AC14: {surface} {column}={literal!r}", column, literal)
        # PRESERVING: the existing warnings key stays on the warning-bearing
        # CLI read.
        _code, _ok, data = run_sql("CLI", "SELECT number FROM items WHERE kind = 'pull_request'")
        if not (isinstance(data.get("warnings"), list) and data.get("warnings")):
            problems.append("OC-AC14: the existing warnings key is retained unchanged (PRESERVING)")
        # NO-HINT legs: rows non-empty, valid literals with zero rows
        # (round 5: deleted/transferred are VALID state literals), and a
        # valid literal on the empty comments table.
        for surface, statement, why in (
            ("CLI", "SELECT number FROM items WHERE kind = 'issue'", "rows non-empty"),
            ("MCP", "SELECT number FROM items WHERE kind = 'issue'", "rows non-empty"),
            ("CLI", "SELECT number FROM items WHERE kind = 'pr' AND number = 999", "valid literal, zero rows"),
            ("CLI", "SELECT number FROM items WHERE state = 'deleted'", "valid state literal, zero rows"),
            ("CLI", "SELECT number FROM items WHERE state = 'transferred'", "valid state literal, zero rows"),
            ("CLI", "SELECT pk FROM comments WHERE kind = 'issue_comment'", "valid literal (empty table)"),
            ("MCP", "SELECT pk FROM comments WHERE kind = 'issue_comment'", "valid literal (empty table)"),
            ("CLI", "SELECT number FROM items WHERE kind = 'pull_request' OR kind = 'issue'", "mixed OR query"),
            ("MCP", "SELECT number FROM items WHERE kind = 'pull_request' OR kind = 'issue'", "mixed OR query"),
        ):
            code, ok, data = run_sql(surface, statement)
            if why == "rows non-empty" or why == "mixed OR query":
                if ok is not True or not data.get("rows"):
                    problems.append(f"{surface} OR/issue query ({why}): ok {ok!r}, rows {data.get('rows')!r}")
            elif data.get("rows") != []:
                problems.append(f"{surface} ({why}): fixture sanity, rows must be []: {data.get('rows')!r}")
            if "hints" in data:
                problems.append(f"{surface}: hints must be ABSENT when {why}; got {data.get('hints')!r}")

        # Filtered reads refuse rather than answer zero rows silently:
        # the CLI refuses an invalid --kind at parse, and the QUERY LAYER
        # refuses it directly (query.open_items). MCP open_items has NO
        # kind filter — no MCP leg by design.
        code, out, err = run_cli(["open", "--kind", "pull_request", *self.base])
        env = parse_envelope(self, out, "OC-AC14") if out.strip().startswith("{") else {}
        message = str((env.get("error") or {}).get("message", ""))
        if code != 2 or (env.get("error") or {}).get("code") != "bad_request":
            problems.append(f"CLI open --kind pull_request: exit {code}, error {(env.get('error') or {}).get('code')!r}")
        if not (re.search("(?<![a-z_])issue(?![a-z_])", message) and re.search("(?<![a-z_])pr(?![a-z_])", message)):
            problems.append(f"CLI open --kind refusal must name issue/pr; got {message[:160]!r}")
        from zaxbygraph.db import connect_readonly_query

        conn = connect_readonly_query(self.db)
        try:
            from zaxbygraph import query as query_mod

            query_mod.open_items(conn, kind="pull_request")
            problems.append("query.open_items(conn, kind='pull_request') must raise ValueError (the query layer refuses invalid kind literals)")
        except ValueError:
            pass
        finally:
            conn.close()

        # Both surfaces must DECLARE the optional typed hints array (see
        # _f.hints_declaration_problems, rounds 2-5).
        problems += hints_declaration_problems(self, "OC-AC14")
        session.stop()
        self.assertFalse(
            problems,
            "OC-AC14: zero-row out-of-vocabulary reads must answer the"
            " annotate-only hints block with canonical valid_values, filtered"
            " reads must refuse, and both schemas must declare hints:\n- "
            + "\n- ".join(problems),
        )


class TestAc15InVocabularyAnswerUnchanged(unittest.TestCase):
    """D9 (AC15): in-vocabulary behavior stays byte-identical to base --
    the existing vocabulary/surface suites still pass, and exact rows are
    pinned for valid literals."""

    def test_ac15_in_vocabulary_answer_unchanged(self) -> None:
        # Exact rows: a valid literal matching rows, and one matching zero
        # rows (no hints key, rows == []).
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        db = root / "history.db"
        seed_corpus(db)
        pin_env_for(self, root, db=db)
        base = ["--repo", REPO, "--db", str(db), "--format", "json"]
        code, out, err = run_cli(["sql", "SELECT number FROM items WHERE kind = 'pr' ORDER BY number", *base])
        env = parse_envelope(self, out, "OC-AC15")
        self.assertEqual(code, 0, f"OC-AC15: the valid-literal read must succeed: {err[:160]}")
        self.assertEqual(
            env.get("data", {}).get("rows"),
            [{"number": 3}, {"number": 4}],
            f"OC-AC15: the valid literal must answer the exact expected row list; got {env.get('data', {}).get('rows')!r}",
        )
        code, out, err = run_cli(["sql", "SELECT number FROM items WHERE kind = 'pr' AND number = 999", *base])
        env = parse_envelope(self, out, "OC-AC15")
        self.assertEqual(code, 0, f"OC-AC15: the zero-row valid read must succeed: {err[:160]}")
        self.assertEqual(
            env.get("data", {}).get("rows"), [],
            "OC-AC15: the zero-row valid read answers rows == []",
        )
        self.assertNotIn(
            "hints", env.get("data") or {},
            "OC-AC15: a valid literal must never carry the hints key",
        )
        # Byte-identity to base: the existing vocabulary/surface suites.
        env_vars = dict(os.environ)
        pythonpath = os.pathsep.join([str(_WORKTREE / "src"), str(_WORKTREE / "tests")])
        env_vars["PYTHONPATH"] = (
            pythonpath + os.pathsep + env_vars["PYTHONPATH"] if env_vars.get("PYTHONPATH") else pythonpath
        )
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_query", "tests.test_search", "-b"],
            capture_output=True, text=True, cwd=str(_WORKTREE), env=env_vars, timeout=600, check=False,
        )
        tail = (proc.stderr or "")[-800:]
        self.assertEqual(
            proc.returncode,
            0,
            "OC-AC15: in-vocabulary answers must stay byte-identical to base"
            f" (tests.test_query + tests.test_search must stay green); tail:\n{tail}",
        )


if __name__ == "__main__":
    unittest.main()
