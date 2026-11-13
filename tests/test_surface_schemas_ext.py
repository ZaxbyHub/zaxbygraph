"""UNFROZEN implementation-review pins (08b round on issues #71/#72).

The frozen C17 check exercises 4 of the 16 tools, only success envelopes,
and its mini-validator treats a list `type` (the registry's
["object","null"] unions) as always-valid — so two real shapes that failed
their own declared outputSchema stayed green. These legs are the
implementation pins the review asked for (finding 1 plus the code answers
to findings 2-4 and questions 1-4):

- ALL 16 tools: a REAL success result AND a representative error result
  validate against the tool's declared outputSchema, with a validator that
  understands type unions (["type","null"]), properties, required, items.
- brief.data.body is the AC6 {untrusted_text} object and sql warnings are
  {column, literal, stored_values} row objects — the two shapes finding 1
  caught, pinned against the REAL envelopes.
- MCP related/path answer not_found for a ghost number (finding 2) and
  file_history answers unknown_path with suggestions (finding 3) — the AC2
  legs C11 never exercised.
- source_unreachable carries git's ACTIONABLE fatal/error lines, not the
  misleading "and the repository exists." footer (finding 4).
- exclude_bots omits bot-authored ITEMS however they matched, counting
  them in bots_omitted (question 1).
- --max-body-chars 0 / max_body_chars 0 is the explicit uncapped override
  and negatives are bad_request on BOTH adapters (question 2).
- search --offset past the end reports truncated:true (question 3).
- A refresh whose pre-run sync_state snapshot could not be READ never
  touches the store (question 4).

Hermetic: FakeGitHubSource only, scratch stores under tempfile dirs, no
network.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, issue, pr_file, pull, review
from zaxbygraph import cli
from test_mcp import McpSession, pin_env_for, run_cli, tool_envelope
from zaxbygraph.db import connect, init_schema
from zaxbygraph import query
from zaxbygraph.query import page_truncated
from zaxbygraph.sync import sync_repo

import zaxbygraph.mcp_server as mcp_server
from zaxbygraph.snapshot import _fetch_failure

_NO_REFRESH_S = 10**9


class _Problems(list):
    """Multi-leg assertion collector: every leg runs, every failure is named
    by one parent-level assertion."""

    @contextlib.contextmanager
    def leg(self, label: str):
        try:
            yield
        except Exception as exc:  # ROOT-041: any failure names the leg and continues
            self.append(f"{label}: {type(exc).__name__}: {exc}")


def _seed_scratch_db(db_path: Path, seeder) -> Path:
    conn = connect(db_path)
    init_schema(conn)
    src = FakeGitHubSource()
    seeder(src)
    sync_repo(conn, src, REPO)
    conn.close()
    return db_path


def _schema_seed(src: FakeGitHubSource) -> None:
    """A mixed corpus for the all-16-tool legs: open issues, a closed issue
    closed by a merged PR, two PRs sharing a file, a human needle item, and
    a BOT-authored item matched only through a human comment."""
    src.add_issue(issue(1, title="first issue", body="the start", state="open"))
    src.add_issue(issue(2, title="second issue", body="see #1", state="closed"))
    src.add_pr(issue(3, title="touch store", body="fixes #2", kind="pr", state="closed"), pull(3, changed_files=2, merged=True), files=[pr_file("src/store.py"), pr_file("src/db.py")])
    src.add_pr(issue(4, title="other pr", body="more files", kind="pr", state="closed"), pull(4, changed_files=1, merged=True), files=[pr_file("src/store.py")])
    src.add_pr(issue(5, title="chore deps", body="routine bump", kind="pr", state="closed", author="dependabot[bot]"), pull(5, changed_files=1, merged=True), files=[pr_file("pkg/x.txt")])
    src.comment_on(5, "human says zyzzyxneedle here", author="alice")
    src.reviews[5] = [review(910_005, "APPROVED", body="human review: zyzzyxneedle looks fine")]
    src.add_issue(issue(6, title="real work", body="mentions zyzzyxneedle", state="open"))


def _flag_bot(db_path: Path) -> None:
    conn = connect(db_path)
    conn.execute("UPDATE actors SET is_bot = 1 WHERE login = 'dependabot[bot]'")
    conn.commit()
    conn.close()


def _env_of(out: str, tag: str) -> dict:
    try:
        return json.loads(out)
    except ValueError:
        raise AssertionError(f"{tag}: stdout must parse as the JSON envelope; got {out[:200]!r}") from None


# ---------------------------------------------------------------------------
# The union-aware validator (the frozen C17 mini-validator, extended for the
# type unions the registry emits — ["object","null"] etc.)

def _type_ok(declared: str, value) -> bool:
    checks = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list), "string": lambda v: isinstance(v, str), "integer": lambda v: isinstance(v, int) and not isinstance(v, bool), "boolean": lambda v: isinstance(v, bool), "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool), "null": lambda v: v is None}
    check = checks.get(declared)
    return check(value) if check else True


def _validate(schema, value, path: str, errors: list) -> None:
    """Minimal stdlib JSON-schema type checker (no external dependency).
    Unlike the frozen C17 checker, a LIST `type` is a union: the value must
    satisfy at least one declared alternative (this is what let the real
    brief/sql envelopes drift from their declarations unnoticed)."""
    if not isinstance(schema, dict):
        return
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_type_ok(name, value) for name in names):
            errors.append(f"{path}: expected {declared}, got {type(value).__name__}")
            return
    if isinstance(value, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, sub in properties.items():
                if key in value:
                    _validate(sub, value[key], f"{path}.{key}", errors)
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, element in enumerate(value):
                _validate(items, element, f"{path}[{index}]", errors)


#: (arguments, expected error code) — a representative error result per
#: tool. Every one must be isError:true with data:null and must validate
#: against the SAME outputSchema the success result does.
_ERROR_CALLS = {
    "graph_status": ({"repo": "NOT A SLUG"}, "bad_request"),
    "search": ({"repo": REPO, "query": "needle", "cursor": "abc"}, "bad_request"),
    "get_item": ({"repo": REPO, "number": 99999}, "not_found"),
    "related": ({"repo": REPO, "number": 99999}, "not_found"),
    "path": ({"repo": REPO, "a": "99999", "b": "1"}, "not_found"),
    "pr_overlap": ({"repo": REPO, "numbers": [99999, 3]}, "not_found"),
    "file_history": ({"repo": REPO, "path": "SRC\\STORE.PY"}, "unknown_path"),
    "what_closed": ({"repo": REPO, "number": 99999}, "not_found"),
    "open_items": ({"repo": REPO, "cursor": "abc"}, "bad_request"),
    "sql": ({"repo": REPO, "statement": "DELETE FROM items"}, "bad_sql"),
    "sync": ({"repo": REPO, "source": "bogus"}, "bad_request"),
    "similar_items": ({"repo": REPO, "number": 99999}, "not_found"),
    "triage": ({"repo": REPO, "cursor": "abc"}, "bad_request"),
    "bundle": ({"repo": REPO, "count": "x"}, "bad_request"),
    "brief": ({"repo": REPO, "number": 99999}, "not_found"),
    "churn": ({"repo": REPO, "limit": "x"}, "bad_request"),
}

_SUCCESS_CALLS = {
    "graph_status": {"repo": REPO},
    "search": {"repo": REPO, "query": "needle"},
    "get_item": {"repo": REPO, "number": 1},
    "related": {"repo": REPO, "number": 3},
    "path": {"repo": REPO, "a": "1", "b": "2"},
    "pr_overlap": {"repo": REPO, "numbers": [3, 4]},
    "file_history": {"repo": REPO, "path": "src/store.py"},
    "what_closed": {"repo": REPO, "number": 2},
    "open_items": {"repo": REPO},
    # Out-of-vocabulary literal: ok:true with EMPTY rows PLUS a warning —
    # the warning-bearing sql shape finding 1 caught being declared wrong.
    "sql": {"repo": REPO, "statement": "SELECT number FROM items WHERE kind = 'zzz_kind'", "limit": 5},
    "sync": {"repo": REPO},
    "similar_items": {"repo": REPO, "number": 6},
    "triage": {"repo": REPO},
    "bundle": {"repo": REPO},
    "brief": {"repo": REPO, "number": 1},
    "churn": {"repo": REPO},
}

_ALL_TOOLS = sorted(_SUCCESS_CALLS)


class _NoopRunner:
    """Injected sync seam: the sync tool's success shape without any real
    fetch (offline, instant)."""

    def __call__(self, db_path, repo) -> None:
        return None


class OutputSchemaExtTests(unittest.TestCase):
    """Finding 1: every tool's REAL success AND error results validate
    against its declared outputSchema, under a union-aware validator."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = _seed_scratch_db(self.root / "history.db", _schema_seed)
        _flag_bot(self.db_path)
        pin_env_for(self, self.root, db=self.db_path)
        self.session = McpSession(self, cwd=str(self.root), repo=REPO, db=str(self.db_path), stale_after_s=_NO_REFRESH_S, sync_runner=_NoopRunner())
        self.addCleanup(self.session.stop)
        self.session.initialize()
        listing = self.session.wait_response(self.session.request("tools/list", {})).get("result") or {}
        self.tools = {t.get("name"): t for t in listing.get("tools") or [] if isinstance(t, dict)}

    def test_all_sixteen_tools_success_and_error_validate(self) -> None:
        problems = _Problems()
        for name in _ALL_TOOLS:
            with problems.leg(f"{name} success"):
                tool = self.tools.get(name)
                self.assertIsInstance(tool, dict, f"tools/list must advertise {name}; got {sorted(self.tools)}")
                schema = (tool or {}).get("outputSchema")
                self.assertIsInstance(schema, dict, f"{name} must declare an outputSchema")
                result, env = tool_envelope(self, self.session.call_tool(name, _SUCCESS_CALLS[name]), f"ext:{name}")
                self.assertIs(result.get("isError"), False, f"{name}: a successful call must not be flagged isError; got {result.get('isError')!r}")
                self.assertIs(env.get("ok"), True, f"{name}: the success call must succeed; got {env.get('error')!r}")
                errors: list = []
                _validate(schema, env, name, errors)
                self.assertFalse(errors, f"{name}: the REAL success result must validate against its declared outputSchema: {errors}")
            with problems.leg(f"{name} error"):
                tool = self.tools.get(name)
                schema = (tool or {}).get("outputSchema")
                arguments, want_code = _ERROR_CALLS[name]
                result, env = tool_envelope(self, self.session.call_tool(name, arguments), f"ext:{name}")
                self.assertIs(result.get("isError"), True, f"{name}: the error call must be flagged isError; got isError={result.get('isError')!r} env={json.dumps(env)[:160]}")
                self.assertIs(env.get("ok"), False, f"{name}: the error call must answer ok:false; got {json.dumps(env)[:160]}")
                self.assertIsNone(env.get("data"), f"{name}: an error envelope carries data:null; got {env.get('data')!r}")
                error = env.get("error") or {}
                self.assertEqual(error.get("code"), want_code, f"{name}: the representative error must answer {want_code}; got {error!r}")
                errors = []
                _validate(schema, env, name, errors)
                self.assertFalse(errors, f"{name}: the REAL error result must validate against the SAME declared outputSchema: {errors}")
        self.assertFalse(problems, "08b finding 1: all 16 tools' real success and error results must validate against their declared outputSchemas:\n- " + "\n- ".join(problems))

    def test_brief_body_and_sql_warnings_real_shapes(self) -> None:
        """The two shapes finding 1 caught: brief.data.body is ALWAYS the
        AC6 untrusted wrapper object, and sql warnings are row objects with
        column/literal/stored_values — and the registry now declares both."""
        problems = _Problems()
        with problems.leg("brief body"):
            _result, env = tool_envelope(self, self.session.call_tool("brief", _SUCCESS_CALLS["brief"]), "ext:brief")
            body = (env.get("data") or {}).get("body")
            self.assertIsInstance(body, dict, f"brief.data.body is the AC6 wrapper object {{untrusted_text}}; got {type(body).__name__} {body!r}")
            self.assertIsInstance(body.get("untrusted_text"), str, f"brief.data.body.untrusted_text is the body string; got {body!r}")
        with problems.leg("sql warnings"):
            _result, env = tool_envelope(self, self.session.call_tool("sql", _SUCCESS_CALLS["sql"]), "ext:sql")
            data = env.get("data") or {}
            warnings = data.get("warnings")
            self.assertTrue(isinstance(warnings, list) and warnings, f"a warning-bearing sql must carry non-empty warnings; got {json.dumps(data)[:200]}")
            entry = warnings[0]
            self.assertIsInstance(entry, dict, f"each warning is an object; got {entry!r}")
            for key in ("column", "literal", "stored_values"):
                self.assertIn(key, entry, f"the warning row must carry {key}; got {sorted(entry)}")
            self.assertEqual(entry.get("literal"), "zzz_kind", f"the warning names the out-of-vocabulary literal; got {entry!r}")
            self.assertIsInstance(entry.get("stored_values"), list, f"stored_values is the stored vocabulary list; got {entry.get('stored_values')!r}")
            self.assertTrue(all(isinstance(v, str) for v in entry.get("stored_values") or []), f"the enum/repo vocabularies are TEXT; got {entry.get('stored_values')!r}")
        self.assertFalse(problems, "08b finding 1: brief.data.body and sql warnings must match their declared shapes:\n- " + "\n- ".join(problems))

    def test_registry_declares_the_real_shapes(self) -> None:
        """The declarations themselves (not just the results): brief.body is
        an object with a required untrusted_text string, sql.warnings items
        are objects with required column/literal/stored_values, and the
        get_item max_body_chars input floor is 0 (question 2)."""
        brief_data = self.tools["brief"]["outputSchema"]["properties"]["data"]
        body = (brief_data.get("properties") or {}).get("body") or {}
        self.assertIn("object", body.get("type") if isinstance(body.get("type"), list) else [body.get("type")], f"brief.body must be declared an object; got {body!r}")
        self.assertIn("untrusted_text", body.get("properties") or {}, "brief.body must declare the untrusted_text member")
        self.assertIn("untrusted_text", body.get("required") or [], "untrusted_text must be required in brief.body")
        sql_data = self.tools["sql"]["outputSchema"]["properties"]["data"]
        items = ((sql_data.get("properties") or {}).get("warnings") or {}).get("items") or {}
        self.assertIn("object", items.get("type") if isinstance(items.get("type"), list) else [items.get("type")], f"sql.warnings items must be declared objects; got {items!r}")
        for key in ("column", "literal", "stored_values"):
            self.assertIn(key, items.get("required") or [], f"sql.warnings items must require {key}; got {items.get('required')!r}")
        get_item_input = self.tools["get_item"]["inputSchema"]["properties"]["max_body_chars"]
        self.assertEqual(get_item_input.get("minimum"), 0, f"max_body_chars must accept the explicit 0 (uncapped) override; declared {get_item_input!r}")


class McpGhostInputExtTests(unittest.TestCase):
    """Findings 2 and 3: the MCP related/path/file_history surfaces answer
    the AC2 codes for unknown inputs (the legs C11 never exercised)."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = _seed_scratch_db(self.root / "history.db", _schema_seed)
        _flag_bot(self.db_path)
        pin_env_for(self, self.root, db=self.db_path)
        self.session = McpSession(self, cwd=str(self.root), repo=REPO, db=str(self.db_path), stale_after_s=_NO_REFRESH_S)
        self.addCleanup(self.session.stop)
        self.session.initialize()

    def test_ghost_numbers_answer_not_found(self) -> None:
        for tool, arguments in (
            ("related", {"repo": REPO, "number": 99999}),
            ("path", {"repo": REPO, "a": "99999", "b": "1"}),
            ("path", {"repo": REPO, "a": "1", "b": "99999"}),
        ):
            with self.subTest(tool=tool, arguments=arguments):
                result, env = tool_envelope(self, self.session.call_tool(tool, arguments), "ext:ghost")
                self.assertIs(result.get("isError"), True, f"{tool} on an unstored number must be a tool error, not a runtime internal error; got {json.dumps(env)[:160]}")
                error = env.get("error") or {}
                self.assertEqual(error.get("code"), "not_found", f"{tool} must map a ghost number to not_found exactly like the CLI and what_closed; got {error!r}")
                self.assertNotEqual(error.get("code"), "runtime", "the 08b regression: a caller mistake answered runtime with a server traceback")

    def test_unknown_path_answers_unknown_path_with_suggestions(self) -> None:
        for label, path in (("cased+backslashed", "SRC\\STORE.PY"), ("absent", "src/nothing.py")):
            with self.subTest(label=label):
                result, env = tool_envelope(self, self.session.call_tool("file_history", {"repo": REPO, "path": path}), "ext:unknown_path")
                self.assertIs(result.get("isError"), True, f"an unstored path must be a tool error, never ok:true with entries:[]; got {json.dumps(env)[:160]}")
                error = env.get("error") or {}
                self.assertEqual(error.get("code"), "unknown_path", f"the MCP tool must mirror the CLI's unknown_path coding; got {error!r}")
                if label == "cased+backslashed":
                    self.assertIn("src/store.py", str(error.get("hint", "")), f"the refusal must suggest the stored path after backslash normalisation; got {error.get('hint')!r}")
        # the stored path itself still answers ok:true with entries.
        _result, env = tool_envelope(self, self.session.call_tool("file_history", {"repo": REPO, "path": "src\\store.py"}), "ext:known_path")
        self.assertIs(env.get("ok"), True, f"a stored path (after backslash normalisation) must still answer; got {json.dumps(env)[:160]}")
        self.assertTrue((env.get("data") or {}).get("entries"), "the stored path has history in the fixture")


class SearchHonestyExtTests(unittest.TestCase):
    """Questions 1 and 3: bot-authored ITEMS are omitted however they
    matched (and counted), and an offset past the end reports
    truncated:true on both adapters."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def test_bot_authored_item_omitted_however_it_matched(self) -> None:
        db_path = _seed_scratch_db(self.root / "history.db", _schema_seed)
        _flag_bot(db_path)
        base = ["--repo", REPO, "--db", str(db_path), "--format", "json"]

        code, out, err = run_cli(["search", "zyzzyxneedle", *base])
        self.assertEqual(code, 0, f"plain search must succeed: {err}")
        plain = _env_of(out, "q1")["data"]
        self.assertEqual(sorted(i["number"] for i in plain["items"]), [5, 6], f"fixture: both the bot item (via its human comment) and the human item match; got {plain['items']!r}")

        code, out, err = run_cli(["search", "zyzzyxneedle", "--no-bots", *base])
        self.assertEqual(code, 0, f"--no-bots search must succeed: {err}")
        data = _env_of(out, "q1")["data"]
        numbers = [i["number"] for i in data["items"]]
        self.assertNotIn(5, numbers, "a BOT-authored item must be omitted under --no-bots even when a HUMAN comment matched it (its row never surfaces under the bot's title)")
        self.assertIn(6, numbers, "human-authored matches must stay under --no-bots")
        self.assertGreater(int(data.get("bots_omitted") or 0), 0, "the omitted bot-authored item must be counted in bots_omitted")
        self.assertEqual(data["total_matches"], 1, f"counts are computed over the filtered stream; total_matches must be 1, got {data['total_matches']!r}")

        # The reviews arm joins items with a REAL predicate (08b r2: a
        # tautological reviews.number = reviews.number let the planner drop
        # the join, fanning items out repo-wide so the bot filter saw the
        # wrong rows and the bot PR survived a HUMAN review match).
        code, out, err = run_cli(["search", "looks fine", "--no-bots", *base])
        self.assertEqual(code, 0, f"the review-only search must succeed: {err}")
        data_r = _env_of(out, "q1")["data"]
        self.assertNotIn(5, [i["number"] for i in data_r["items"]], "a BOT-authored item must be omitted under --no-bots even when the match came only from a HUMAN REVIEW (the reviews arm must join items by repo AND number)")
        code, out, err = run_cli(["search", "looks fine", *base])
        self.assertEqual(code, 0, err)
        data_p = _env_of(out, "q1")["data"]
        self.assertIn(5, [i["number"] for i in data_p["items"]], "fixture sanity: without --no-bots the review-only match surfaces")

    def test_offset_past_the_end_reports_truncated(self) -> None:
        def seed(src: FakeGitHubSource) -> None:
            for number in (21, 22, 23):
                src.add_issue(issue(number, title=f"both {number}", body="needle sunset here"))

        db_path = _seed_scratch_db(self.root / "history.db", seed)
        base = ["--repo", REPO, "--db", str(db_path), "--format", "json"]

        # CLI: 0 items returned of 3 matched at offset 99 -> truncated:true.
        code, out, err = run_cli(["search", "needle sunset", "--limit", "20", "--offset", "99", *base])
        self.assertEqual(code, 0, f"an offset past the end must succeed: {err}")
        env = _env_of(out, "q3")
        self.assertEqual(env["data"]["items"], [], "fixture: offset 99 is past all 3 matches")
        self.assertEqual(env["data"]["total_matches"], 3, "fixture: 3 items match")
        self.assertIs(env.get("truncated"), True, f"a page that skipped past matches the caller never saw must not read as 'everything shown'; got truncated={env.get('truncated')!r}")

        # The exact end stays false: an empty TERMINAL page must not loop a
        # cursor follower forever.
        code, out, err = run_cli(["search", "needle sunset", "--limit", "20", "--offset", "3", *base])
        self.assertEqual(code, 0, err)
        self.assertIs(_env_of(out, "q3").get("truncated"), False, "a page that ends exactly at the end has nothing more to offer")

        # MCP: the same honest flag through the cursor surface.
        pin_env_for(self, self.root, db=db_path)
        session = McpSession(self, cwd=str(self.root), repo=REPO, db=str(db_path), stale_after_s=_NO_REFRESH_S)
        self.addCleanup(session.stop)
        session.initialize()
        _result, env = tool_envelope(self, session.call_tool("search", {"repo": REPO, "query": "needle sunset", "limit": 20, "cursor": "99"}), "q3")
        self.assertIs(env.get("ok"), True, f"the MCP paged read must succeed: {env.get('error')!r}")
        self.assertEqual((env.get("data") or {}).get("items"), [], "fixture: cursor 99 is past all matches")
        self.assertIs(env.get("truncated"), True, f"MCP search must report the same honest overshoot flag; got truncated={env.get('truncated')!r}")
        self.assertNotIn("next_cursor", env.get("data") or {}, "an overshoot page must NOT emit next_cursor: str(offset) would repeat the cursor it was handed and trap a while-truncated follower in a self-loop (08b r2)")
        session.stop()

        # Unit truth table for the shared formula.
        table = (
            ((0, 5, 20, 6), True),   # 5 of 6 returned: more remain
            ((0, 6, 20, 6), False),  # everything returned
            ((4, 2, 20, 6), False),  # the last full page: nothing remains
            ((3, 0, 20, 3), False),  # empty terminal page: no cursor loop
            ((99, 0, 20, 3), True),  # overshoot: 0 of 3 shown
            ((5, 0, 5, 3), True),    # overshoot with a short page
            ((0, 0, 20, 0), False),  # nothing matched at all
            ((0, 0, 20, None), False),  # an unknown total stays false
        )
        for args, want in table:
            with self.subTest(args=args):
                self.assertIs(page_truncated(*args), want)

    def test_mcp_related_over_budget_reports_truncated(self) -> None:
        # TF-002 (#71 AC7 MCP leg): the CLI-only frozen check never drove the
        # MCP related adapter's truncated promotion over budget.
        def seed(src: FakeGitHubSource) -> None:
            # One hub mentioning 109 stored neighbors (the frozen C7 fixture's
            # shape): mention edges give a 110-node one-hop neighborhood.
            neighbors = range(1, 110)
            src.add_issue(issue(200, title="hub", state="open", body="star " + " ".join(f"#{n}" for n in neighbors)))
            for n in neighbors:
                src.add_issue(issue(n, title=f"star {n}", state="open"))

        db_path = _seed_scratch_db(self.root / "history.db", seed)
        pin_env_for(self, self.root, db=db_path)
        session = McpSession(self, cwd=str(self.root), repo=REPO, db=str(db_path), stale_after_s=_NO_REFRESH_S)
        self.addCleanup(session.stop)
        session.initialize()
        _result, env = tool_envelope(
            self,
            session.call_tool("related", {"repo": REPO, "number": 200}),
            "tf002",
        )
        self.assertIs(env.get("ok"), True, f"related must succeed: {env.get('error')!r}")
        self.assertGreater(
            len((env.get("data") or {}).get("nodes") or []), 0, "fixture sanity: the hub must return its neighborhood"
        )
        self.assertIs(
            env.get("truncated"), True,
            "a >100-node neighborhood over the default budget must promote truncated:true on the MCP surface",
        )
        session.stop()

    def test_mcp_brief_exclude_bots_filters_ranking(self) -> None:
        # TF-003 (#71 AC6 brief MCP leg): the flag threads into derive's
        # comment ranking and bot-authored items are dropped.
        def seed(src: FakeGitHubSource) -> None:
            src.add_issue(issue(40, title="bot brief target", body="briefme", state="open", comments=2))
            src.comment_on(40, "human note briefme", author="alice")
            src.comment_on(40, "bot noise briefme", author="dependabot[bot]")

        db_path = _seed_scratch_db(self.root / "history.db", seed)
        conn = connect(db_path)
        init_schema(conn)
        conn.execute("UPDATE actors SET is_bot = 1 WHERE login = 'dependabot[bot]'")
        conn.commit()
        conn.close()
        pin_env_for(self, self.root, db=db_path)
        session = McpSession(self, cwd=str(self.root), repo=REPO, db=str(db_path), stale_after_s=_NO_REFRESH_S)
        self.addCleanup(session.stop)
        session.initialize()
        _result, env = tool_envelope(
            self,
            session.call_tool("brief", {"repo": REPO, "number": 40, "exclude_bots": True}),
            "tf003",
        )
        self.assertIs(env.get("ok"), True, f"brief must succeed: {env.get('error')!r}")
        data = env.get("data") or {}
        authors = [c.get("author") for c in data.get("comments") or []]
        self.assertNotIn("dependabot[bot]", authors, f"bot comments must be filtered from the brief ranking; got {authors}")
        self.assertIn("alice", authors, "the human comment must survive")
        session.stop()

    def test_sql_env_knobs_clamp_and_wire(self) -> None:
        # TF-004: the documented env-knob semantics (clamp/fallback) and the
        # CLI/MCP adapter byte-budget wiring were untested. The knobs read
        # os.environ at CALL time, so mock.patch.dict is enough -- NO
        # importlib.reload: a reload re-executes the module and forks the
        # SqlTimeoutError/UnknownFilePathError classes, so the cli and
        # mcp_server catch arms stop matching them (closeout X-2: ordering
        # this test before the frozen C1 leg broke 'runtime' != 'timeout').
        from unittest import mock

        with mock.patch.dict(os.environ, {"ZAXBYGRAPH_SQL_TIMEOUT_S": "9999", "ZAXBYGRAPH_SQL_MAX_BYTES": "-5"}):
            self.assertEqual(query.sql_deadline_from_env(), 600.0,
                             "the timeout clamp (0,600] pins 9999 to 600")
            self.assertEqual(query.sql_max_bytes_from_env(), query._DEFAULT_SQL_MAX_BYTES,
                             "a non-positive byte budget falls back to the default")
        with mock.patch.dict(os.environ, {"ZAXBYGRAPH_SQL_TIMEOUT_S": "junk", "ZAXBYGRAPH_SQL_MAX_BYTES": "4096"}):
            self.assertEqual(query.sql_deadline_from_env(), query._DEFAULT_SQL_DEADLINE_S,
                             "malformed timeout falls back to the default")
            self.assertEqual(query.sql_max_bytes_from_env(), 4096,
                             "a positive byte budget is honoured")

        # Adapter wiring: a tiny byte budget through the CLI answers
        # truncated with total_rows (the budget, not the row limit, tripped).
        def seed(src: FakeGitHubSource) -> None:
            for n in range(1, 6):
                src.add_issue(issue(n, title=f"wide {n}", body="y" * 900, state="open"))

        db_path = _seed_scratch_db(self.root / "history.db", seed)
        base = ["--repo", REPO, "--db", str(db_path)]
        saved_max = os.environ.get("ZAXBYGRAPH_SQL_MAX_BYTES")
        os.environ["ZAXBYGRAPH_SQL_MAX_BYTES"] = "2000"
        try:
            code, out, err = run_cli(["sql", "SELECT number, body FROM items ORDER BY number", "--format", "json", *base])
            self.assertEqual(code, 0, err)
            env = _env_of(out, "tf004")
            data = env["data"]
            self.assertTrue(env.get("truncated") or data.get("truncated"), "a 2000-byte budget must truncate a ~4.5KB result")
            self.assertLess(len(data.get("rows") or []), 5, "fewer than all 5 rows ride under the budget")
        finally:
            if saved_max is None:
                os.environ.pop("ZAXBYGRAPH_SQL_MAX_BYTES", None)
            else:
                os.environ["ZAXBYGRAPH_SQL_MAX_BYTES"] = saved_max

    def test_search_total_matches_equal_across_limits(self) -> None:
        # cubic-t355: the AC5 frozen check never compared total_matches at
        # limit 1 vs 20; pin the equality here (limit-independent counts).
        def seed(src: FakeGitHubSource) -> None:
            for n in range(1, 9):
                src.add_issue(issue(n, title=f"equal counts {n}", body="needle sunset here"))

        db_path = _seed_scratch_db(self.root / "history.db", seed)
        base = ["--repo", REPO, "--db", str(db_path), "--format", "json"]
        totals = []
        for limit in ("1", "20"):
            code, out, err = run_cli(["search", "needle sunset", "--limit", limit, *base])
            self.assertEqual(code, 0, err)
            data = _env_of(out, "t355")["data"]
            totals.append((data["total_matches"], data["strict_matches"], data["broadened_matches"]))
        self.assertEqual(totals[0], totals[1], f"counts must be limit-independent; got {totals}")

    def test_item_bounded_body_is_real_and_marked(self) -> None:
        # cubic-t718: the frozen AC5 leg accepts a silently DROPPED body;
        # pin body non-empty + the truncation marker + the omitted count.
        def seed(src: FakeGitHubSource) -> None:
            src.add_issue(issue(50, title="bounded", body="z" * 30000, state="open"))

        db_path = _seed_scratch_db(self.root / "history.db", seed)
        base = ["--repo", REPO, "--db", str(db_path), "--format", "json"]
        code, out, err = run_cli(["item", "50", *base])
        self.assertEqual(code, 0, err)
        data = _env_of(out, "t718")["data"]
        body = data.get("body") or ""
        self.assertGreater(len(body), 0, "a bounded body must still be delivered, never dropped")
        self.assertIn("truncated", body, f"the body must carry its truncation marker; got tail {body[-80:]!r}")
        self.assertEqual(int(data.get("body_chars_omitted") or 0), 30000 - 20480, "the omitted-char count names exactly what was cut")

    def test_usage_envelope_survives_a_tty_with_explicit_format(self) -> None:
        # Final-critic r1 f1: the real entry points call main() with no argv
        # list; the machine-format pre-scan must fall back to sys.argv and
        # honour --format=json (and the =value spelling) even when stdout
        # claims to be a TTY - the frozen check passes argv explicitly and
        # a piped subprocess stdout is never a TTY, so neither could see it.
        import io as _io

        class _FakeTty(_io.StringIO):
            def isatty(self) -> bool:
                return True

        db_path = _seed_scratch_db(self.root / "history.db", _schema_seed)
        real_out, real_argv = sys.stdout, sys.argv
        fake = _FakeTty()
        sys.stdout = fake
        sys.argv = [
            "zaxbygraph", "item", "--number", "1", "--format", "json",
            "--db", str(db_path), "--repo", REPO,
        ]
        try:
            code = cli.main(None)
        finally:
            sys.stdout, sys.argv = real_out, real_argv
        self.assertEqual(code, 2, "the usage failure keeps exit 2")
        env = json.loads(fake.getvalue())
        self.assertEqual(env.get("error", {}).get("code"), "bad_request", "an explicit --format json on a TTY must still answer the envelope on stdout")
        self.assertIn("--number", str(env["error"].get("message", "")), "argparse's own message rides the envelope")

        # The =value spelling through the same path.
        fake2 = _FakeTty()
        sys.stdout = fake2
        sys.argv = [
            "zaxbygraph", "item", "--number=1", "--format=json",
            "--db", str(db_path), "--repo", REPO,
        ]
        try:
            code = cli.main(None)
        finally:
            sys.stdout, sys.argv = real_out, real_argv
        self.assertEqual(code, 2)
        env2 = json.loads(fake2.getvalue())
        self.assertEqual(env2.get("error", {}).get("code"), "bad_request", "the --format=json spelling is recognised by the pre-scan too")

        # An INVALID --format value must fall through to the not-a-TTY json
        # default so a PIPED caller keeps the envelope (final-critic r2 R2-2):
        # the stdout here is a plain StringIO (isatty False), the pipe shape.
        fake3 = _io.StringIO()
        sys.stdout = fake3
        sys.argv = [
            "zaxbygraph", "status", "--format", "xml",
            "--db", str(db_path), "--repo", REPO,
        ]
        try:
            code = cli.main(None)
        finally:
            sys.stdout, sys.argv = real_out, real_argv
        self.assertEqual(code, 2)
        env3 = json.loads(fake3.getvalue())
        self.assertEqual(env3.get("error", {}).get("code"), "bad_request", "an invalid --format value is argparse's error; the envelope default still applies")

    def test_large_text_pipe_exits_quietly(self) -> None:
        # Final-critic r2 R2-1: text mode on a payload larger than the pipe
        # buffer must ride the same boundary wrapper (print() bypassed it).
        # >64 KiB verified (TI-003): the fixture's bodies ARE the payload via
        # item multi-number — `open` never rendered bodies and the old leg
        # measured 8.8 KiB, a ~660-byte margin over the 8 KiB chunk.
        def seed(src: FakeGitHubSource) -> None:
            for number in range(30):
                src.add_issue(issue(number, title=f"drain {number}", body="x" * 4000, state="open", comments=4))

        db_path = _seed_scratch_db(self.root / "history.db", seed)
        src_dir = str(Path(__file__).resolve().parents[1] / "src")
        env = dict(os.environ)
        env["PYTHONPATH"] = src_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["ZAXBYGRAPH_HOME"] = str(self.root / "zhome")
        env["ZAXBYGRAPH_DB"] = str(db_path)
        for fmt in ("text", "json"):
            with tempfile.TemporaryFile() as err_file:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "zaxbygraph", "item", *[str(n) for n in range(1, 31)],
                     "--format", fmt,
                     "--repo", REPO, "--db", str(db_path)],
                    stdout=subprocess.PIPE, stderr=err_file, stdin=subprocess.DEVNULL, env=env, cwd=str(self.root),
                )
                proc.stdout.close()
                try:
                    returncode = proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=10)
                    self.fail(f"{fmt}: the CLI never exited after its stdout reader went away")
                err_file.seek(0)
                stderr_text = err_file.read().decode("utf-8", "replace")
            self.assertEqual(returncode, 0, f"{fmt}: a broken pipe on a large payload exits 0; stderr: {stderr_text[:400]}")
            self.assertNotIn("Traceback", stderr_text, f"{fmt}: no traceback on the boundary path")

    def test_single_item_fields_projection(self) -> None:
        # Final-critic r2 non-blocking: pin the single-number --fields
        # projection (and the no-flag byte-identity).
        db_path = _seed_scratch_db(self.root / "history.db", _schema_seed)
        base = ["--repo", REPO, "--db", str(db_path)]
        code, out, err = run_cli(["item", "1", "--fields", "number,title", "--format", "json", *base])
        self.assertEqual(code, 0, err)
        data = _env_of(out, "q4")["data"]
        self.assertEqual(sorted(data.keys()), ["number", "title"], f"the single item projects to the requested keys; got {sorted(data.keys())}")
        code_a, out_a, _ = run_cli(["item", "1", "--format", "json", *base])
        code_b, out_b, _ = run_cli(["item", "1", "--format", "json", *base])
        self.assertEqual(code_a, 0, out_a)
        self.assertEqual(code_b, 0, out_b)

        def _strip_freshness(text: str) -> str:
            env = json.loads(text)
            env.pop("freshness", None)  # age_s ticks per second (TI-001)
            env["db"] = ""
            return json.dumps(env, sort_keys=True)

        self.assertEqual(
            _strip_freshness(out_a),
            _strip_freshness(out_b),
            "no-flag output is deterministic (byte-identical across runs"
            " once the ticking freshness clock is stripped)",
        )


class BodyBoundSemanticsExtTests(unittest.TestCase):
    """Question 2: --max-body-chars 0 / max_body_chars 0 is the explicit
    UNCAPPED override on both adapters; negatives are bad_request."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

        def seed(src: FakeGitHubSource) -> None:
            src.add_issue(issue(31, title="big body", body="z" * 40_000, state="open"))

        self.db_path = _seed_scratch_db(self.root / "history.db", seed)
        pin_env_for(self, self.root, db=self.db_path)

    def test_cli_zero_is_uncapped_and_negative_is_bad_request(self) -> None:
        base = ["--repo", REPO, "--db", str(self.db_path), "--format", "json"]

        code, out, err = run_cli(["item", "31", *base])
        self.assertEqual(code, 0, err)
        default_body = _env_of(out, "q2")["data"]["body"]
        self.assertLess(len(default_body), 40_000, "the default 20 KiB bound must apply without the flag")

        code, out, err = run_cli(["item", "31", "--max-body-chars", "0", *base])
        self.assertEqual(code, 0, err)
        env = _env_of(out, "q2")
        self.assertEqual(len(env["data"]["body"]), 40_000, f"--max-body-chars 0 is the explicit uncapped override; got {len(env['data']['body'])} chars")
        self.assertIs(env.get("truncated"), False, "an uncapped body was not cut")

        code, out, err = run_cli(["item", "31", "--max-body-chars", "-1", *base])
        self.assertEqual(code, 2, f"a negative --max-body-chars is a usage error (exit 2); got {code}")
        error = _env_of(out, "q2").get("error") or {}
        self.assertEqual(error.get("code"), "bad_request", f"the negative value must answer the bad_request envelope; got {error!r}")
        self.assertIn("max-body-chars", str(error.get("message", "")), "the envelope message must name the flag")

    def test_mcp_zero_is_uncapped_and_negative_is_bad_request(self) -> None:
        session = McpSession(self, cwd=str(self.root), repo=REPO, db=str(self.db_path), stale_after_s=_NO_REFRESH_S)
        self.addCleanup(session.stop)
        session.initialize()
        _result, env = tool_envelope(self, session.call_tool("get_item", {"repo": REPO, "number": 31, "max_body_chars": 0}), "q2")
        self.assertIs(env.get("ok"), True, f"max_body_chars=0 must be accepted as the uncapped override; got {env.get('error')!r}")
        self.assertEqual(len((env.get("data") or {}).get("body") or ""), 40_000, "max_body_chars=0 returns the full body")
        _result, env = tool_envelope(self, session.call_tool("get_item", {"repo": REPO, "number": 31, "max_body_chars": -1}), "q2")
        self.assertIs(env.get("ok"), False, "a negative max_body_chars must fail")
        self.assertEqual((env.get("error") or {}).get("code"), "bad_request", f"the negative value is a structured bad_request; got {env.get('error')!r}")
        session.stop()


class SnapshotMessageExtTests(unittest.TestCase):
    """Finding 4: source_unreachable carries git's ACTIONABLE fatal/error
    lines — the diagnostic that says why — never only the misleading
    'and the repository exists.' footer."""

    _GIT_STDERR = (
        b"fatal: '/nowhere/definitely-not-a-repo' does not appear to be a git repository\n"
        b"fatal: Could not read from remote repository.\n"
        b"\n"
        b"Please make sure you have the correct access rights\n"
        b"and the repository exists.\n"
    )

    def test_message_carries_the_actionable_fatal_lines(self) -> None:
        err = _fetch_failure(self._GIT_STDERR, "refs/zaxbygraph/snapshot", "/nowhere/definitely-not-a-repo")
        self.assertIsNotNone(err, "a non-absent-ref failure must classify as source_unreachable")
        self.assertEqual(err.code, "source_unreachable")
        self.assertEqual(err.exit_code, 1, "source_unreachable stays the exit-1 per-attempt class")
        message = err.message
        self.assertIn("git could not fetch", message, "the frozen C16 oracle ('git' in the message) keeps passing UNCHANGED")
        self.assertIn("does not appear to be a git repository", message, f"the FIRST fatal line is the diagnostic; got {message!r}")
        self.assertIn("Could not read from remote repository", message, f"the actionable follow-up line rides along; got {message!r}")
        self.assertNotIn("and the repository exists", message, f"the misleading footer must not be the carried line; got {message!r}")

    def test_fallbacks_when_git_printed_no_fatal_line(self) -> None:
        err = _fetch_failure(b"some unlabelled transport noise\n", "refs/zaxbygraph/snapshot", "src")
        self.assertEqual(err.code, "source_unreachable")
        self.assertIn("some unlabelled transport noise", err.message, "without fatal/error lines the tail is still carried")
        empty = _fetch_failure(b"", "refs/zaxbygraph/snapshot", "src")
        self.assertIn("(no output)", empty.message, "an empty stderr keeps the explicit no-output marker")
        absent = _fetch_failure(b"fatal: couldn't find remote ref refs/zaxbygraph/snapshot\n", "refs/zaxbygraph/snapshot", "src")
        self.assertIsNone(absent, "the absent-ref signature stays snapshot_absent's business (returns None)")

    def test_actionable_lines_are_bounded(self) -> None:
        stderr = b"".join(b"fatal: line %d\n" % i for i in range(10))
        err = _fetch_failure(stderr, "refs/zaxbygraph/snapshot", "src")
        carried = err.message.count("fatal: line")
        self.assertEqual(carried, 3, f"at most 3 actionable lines are carried; got {carried}")


class RefreshSnapshotContainmentExtTests(unittest.TestCase):
    """Question 4: containment distinguishes a genuinely ABSENT pre-run row
    (delete-on-restore is correct) from a snapshot READ failure (the store
    must be left alone — a blind DELETE against an unobserved row is the
    poison the isolation exists to prevent)."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = _seed_scratch_db(self.root / "history.db", lambda src: src.add_issue(issue(1)))

    def test_absent_row_snapshots_to_none_and_restores_to_absent(self) -> None:
        # A slug with no sync_state row: the snapshot is None (not the
        # sentinel), so a restore DELETEs what the failed run created.
        snapshot = mcp_server._snapshot_sync_state(self.db_path, "acme/other")
        self.assertIsNone(snapshot, "a genuinely absent row snapshots to None, never the read-failed sentinel")
        conn = connect(self.db_path)
        conn.execute("INSERT INTO sync_state (repo) VALUES ('acme/other')")
        conn.commit()
        conn.close()
        mcp_server._restore_sync_state(self.db_path, "acme/other", None)
        conn = connect(self.db_path)
        row = conn.execute("SELECT COUNT(*) FROM sync_state WHERE repo = 'acme/other'").fetchone()[0]
        conn.close()
        self.assertEqual(row, 0, "a None snapshot restores the no-row state")

    def test_read_failure_answers_the_sentinel(self) -> None:
        real = mcp_server.connect_readonly_query

        def _unreadable(path):
            raise RuntimeError("simulated unreadable store")

        mcp_server.connect_readonly_query = _unreadable
        try:
            snapshot = mcp_server._snapshot_sync_state(self.db_path, REPO)
        finally:
            mcp_server.connect_readonly_query = real
        self.assertIs(snapshot, mcp_server._SNAPSHOT_READ_FAILED, "a failed read must answer the sentinel, never None (None would license a destructive DELETE)")

    def test_containment_refuses_to_touch_the_store_on_sentinel(self) -> None:
        # The store the failed refresh left behind (a poisoned last_error).
        conn = connect(self.db_path)
        conn.execute("UPDATE sync_state SET last_error = 'HTTP 502' WHERE repo = ?", (REPO,))
        conn.commit()
        conn.close()
        real_connect = mcp_server.connect

        def _must_not_write(path):
            raise AssertionError("containment must not touch a store whose pre-run snapshot could not be read")

        mcp_server.connect = _must_not_write
        try:
            mcp_server._contain_refresh_failure(self.db_path, REPO, mcp_server._SNAPSHOT_READ_FAILED)
        finally:
            mcp_server.connect = real_connect
        with mcp_server._BACKOFF_LOCK:
            armed = (str(self.db_path), REPO) in mcp_server._REFRESH_BACKOFF
            mcp_server._REFRESH_BACKOFF.pop((str(self.db_path), REPO), None)
        self.assertTrue(armed, "the backoff is armed even when the restore is skipped (the failure stays suppressed/reported)")
        conn = connect(self.db_path)
        row = conn.execute("SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)).fetchone()
        conn.close()
        self.assertEqual(row["last_error"], "HTTP 502", "the store is left exactly as the failed run left it")


if __name__ == "__main__":
    unittest.main()
