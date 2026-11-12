"""Acceptance checks for issue #72 (surface honesty: reads that tell the
truth about what they did not return). RED at base by assertion; GREEN once
the criteria ship.

AC -> -k method map: AC1 test_failed_first_sync_is_still_no_corpus, AC2
test_unknown_inputs_are_named, AC3 test_dropped_pool_members_and_bad_cursors,
AC4 test_fields_and_jsonl_apply_everywhere, AC5 test_search_counts_are_stable,
AC6 test_search_prefers_item_text, AC7 test_snapshot_unreachable_is_not_absent,
AC8 test_output_schemas_describe_real_shapes, AC10
test_out_of_vocabulary_literal_warns, AC11 test_unknown_table_and_pragma_hints,
AC12 test_usage_errors_are_envelopes, AC13 test_item_reads_several_numbers,
AC14 test_cli_shapes_are_described. PRESERVING: AC9 =
tests/test_mcp_typed.py -k test_tools_declare_schema_and_annotations.

Hermetic: FakeGitHubSource only, scratch stores under tempfile dirs, local
git repos with https origins never contacted, no network.
"""

from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, comment, issue, pr_file, pull
from test_mcp import McpSession, pin_env_for, run_cli, tool_envelope
from zaxbygraph.db import connect, init_schema
from zaxbygraph.github import GitHubError
from zaxbygraph.query import assert_read_sql
from zaxbygraph.sync import sync_repo

_NO_REFRESH_S = 10**9

#: AC8/AC14: the per-tool data keys the MCP outputSchema (and the CLI
#: `schema --output` shape table) must declare, by MCP tool name.
EXPECTED_TOOL_DATA_KEYS = {
    "graph_status": {"repos", "counts"},
    "search": {"items", "total_matches"},
    "sql": {"columns", "rows"},
    "get_item": {"number", "comments"},
}

#: AC14: CLI command -> the MCP tool whose data shape it must match.
_CLI_TO_MCP_TOOL = {"status": "graph_status", "search": "search", "sql": "sql", "item": "get_item"}

#: AC14: every read command that must appear in `schema --output` and the SKILL.md shape table.
REQUIRED_COMMANDS = ("status", "search", "item", "related", "sql", "open", "churn", "similar", "triage", "bundle", "brief")


class _Problems(list):
    """Multi-leg assertion collector: every leg runs, every failure is named by one parent-level assertion (an unambiguous RED for the -k filter)."""

    @contextlib.contextmanager
    def leg(self, label: str):
        try:
            yield
        except AssertionError as exc:
            self.append(f"{label}: {exc}")


def _seed_scratch_db(db_path: Path, seeder) -> Path:
    conn = connect(db_path)
    init_schema(conn)
    src = FakeGitHubSource()
    seeder(src)
    sync_repo(conn, src, REPO)
    conn.close()
    return db_path


def _base_seed(src: FakeGitHubSource) -> None:
    """A small mixed corpus: two issues, one merged PR touching two files, three open labeled issues carrying the search needle."""
    src.add_issue(issue(1, title="first issue", body="the start", state="open"))
    src.add_issue(issue(2, title="second issue", body="see #1", state="closed"))
    src.add_pr(issue(3, title="touch store", body="fixes #2", kind="pr", state="closed"), pull(3, changed_files=2, merged=True), files=[pr_file("src/store.py"), pr_file("src/db.py")])
    labels = [{"name": "bug", "color": "c"}]
    for number, body in ((11, "open bug needle sunset"), (12, "open bug needle"), (13, "open bug needle")):
        src.add_issue(issue(number, title=f"open {number}", body=body, state="open", labels=labels))


def _env_of(out: str, tag: str) -> dict:
    try:
        return json.loads(out)
    except ValueError:
        raise AssertionError(f"{tag}: stdout must parse as the JSON envelope; got {out[:200]!r}") from None


class NoCorpusTests(unittest.TestCase):
    """AC1: zero items AND no full sync ever completed -> reads answer exit 3 / no_corpus, including after a FAILED first sync."""

    def test_failed_first_sync_is_still_no_corpus(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        db_path = root / "history.db"

        class _FailingSource(FakeGitHubSource):
            def list_issues(self, since):
                raise GitHubError("HTTP 502 bad gateway", status=502)
                yield  # pragma: no cover - generator shape only

        # The store a FAILED first sync leaves: DB file + sync_state row with
        # last_error set, zero items (built through the real sync path).
        conn = connect(db_path)
        init_schema(conn)
        src = _FailingSource()
        src.add_issue(issue(1, title="never lands", body="needle"))
        try:
            sync_repo(conn, src, REPO)
        except Exception:  # noqa: BLE001 - the failure itself is expected
            pass
        row = conn.execute("SELECT last_error, full_sync_pending FROM sync_state WHERE repo = ?", (REPO,)).fetchone()
        self.assertIsNotNone(row, "fixture: a failed first sync leaves a row")
        self.assertIsNotNone(row["last_error"], "fixture: a failed first sync records last_error")
        items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn.close()
        self.assertEqual(items, 0, "fixture: the failed first sync stored no items")

        # Amend fixture (fix-plan v3 CF-E): a LAGGING-CACHED-COUNT store —
        # sync_state.item_count is a finish-time snapshot, so a store whose
        # run committed items but never finished can sit at item_count=0
        # while real rows exist. Built by simulating the lag directly on a
        # healthy store (the snapshot is a plain column).
        lagging_db = root / "lagging.db"
        conn = connect(lagging_db)
        init_schema(conn)
        src = FakeGitHubSource()
        src.add_issue(issue(2, title="lagging snapshot item", body="needle"))
        sync_repo(conn, src, REPO)
        conn.execute("UPDATE sync_state SET item_count = 0 WHERE repo = ?", (REPO,))
        conn.commit()
        conn.close()

        problems = _Problems()
        for argv in (["status"], ["search", "needle"]):
            with problems.leg(f"`{argv[0]}`"):
                code, out, err = run_cli([*argv, "--repo", REPO, "--db", str(db_path), "--format", "json"])
                env = _env_of(out, "AC1")
                self.assertEqual(code, 3, f"with zero items and no full sync ever completed the answer must be the no-corpus exit 3; got exit {code} with ok={env.get('ok')}")
                self.assertIs(env.get("ok"), False, f"a never-completed corpus must not answer ok:true-empty (data={json.dumps(env.get('data'))[:120]})")
                self.assertEqual((env.get("error") or {}).get("code"), "no_corpus", f"error.code must be no_corpus after a FAILED first sync; got {env.get('error')!r}")

        # Amend leg (fix-plan v3 CF-E): the lagging cached count. The
        # no-corpus decision must be governed by the LIVE item COUNT, never
        # the cached sync_state.item_count snapshot. COUNTER-EXAMPLE
        # CONTRAST LEG: at base there is no corpus_state gate at all, so the
        # base already answers these reads — this leg pins the FIX against a
        # naive cached-count implementation (the same role the C4
        # positive-contrast leg plays for roots).
        with problems.leg("lagging cached item_count (live COUNT governs)"):
            for argv in (["status"], ["search", "needle"]):
                code, out, err = run_cli([*argv, "--repo", REPO, "--db", str(lagging_db), "--format", "json"])
                env = _env_of(out, "AC1")
                self.assertEqual(code, 0, f"a store with COMMITTED items must NOT answer no-corpus just because the cached sync_state.item_count snapshot lags at 0 (the live COUNT governs); `{argv[0]}` exited {code} with {json.dumps(env)[:160]}")
                self.assertIs(env.get("ok"), True, f"the live COUNT (>0 committed items) must clear the corpus gate even at cached item_count=0; `{argv[0]}` answered {json.dumps(env)[:160]}")

        # Amend leg (fix-plan v3 CF-E): the MCP side answers the same
        # no-corpus class for the zero-item never-synced store (ONE shared
        # corpus decision across both read adapters — neither can drift).
        # RED at base: the store HAS a sync_state row (the failed sync left
        # it), so the base MCP answers ok:true instead of no_corpus.
        with problems.leg("MCP no-corpus (zero-item store)"):
            pin_env_for(self, root, db=db_path)
            session = McpSession(self, cwd=str(root), repo=REPO, db=str(db_path), stale_after_s=_NO_REFRESH_S)
            self.addCleanup(session.stop)
            session.initialize()
            result, env = tool_envelope(self, session.call_tool("graph_status", {"repo": REPO}), "AC1")
            self.assertIs(result.get("isError"), True, f"the MCP tool must answer the no-corpus class for a zero-item never-synced store, not a success result; got isError={result.get('isError')!r} env={json.dumps(env)[:160]}")
            self.assertIs(env.get("ok"), False, f"MCP graph_status on the failed-first-sync store must not answer ok:true-empty; got {json.dumps(env)[:160]}")
            self.assertEqual((env.get("error") or {}).get("code"), "no_corpus", f"MCP error.code must be no_corpus for the zero-item store (the same corpus decision the CLI makes); got {env.get('error')!r}")
            session.stop()

        self.assertFalse(problems, "AC1: a failed first sync is still no corpus:\n- " + "\n- ".join(problems))


class UnknownInputTests(unittest.TestCase):
    """AC2: unstored numbers answer not_found; an unknown file-history path answers unknown_path with case-insensitive + basename suggestions after backslash normalisation."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = _seed_scratch_db(self.root / "history.db", _base_seed)

    def _run(self, argv):
        return run_cli([*argv, "--repo", REPO, "--db", str(self.db_path), "--format", "json"])

    def test_unknown_inputs_are_named(self) -> None:
        problems = _Problems()

        for argv in (["related", "999"], ["path", "999", "1"], ["explain", "999", "1"]):
            with problems.leg(f"`{argv[0]} 999`"):
                code, out, err = self._run(argv)
                env = _env_of(out, "AC2")
                self.assertIs(env.get("ok"), False, f"an unstored number must fail, not answer ok:true with an empty body (data={json.dumps(env.get('data'))[:120]})")
                self.assertEqual((env.get("error") or {}).get("code"), "not_found", f"an unstored number must name not_found; got {env.get('error')!r}")

        # stored path is src/store.py; the first probe is cased AND backslashed.
        for label, path in (("cased+backslashed", "SRC\\STORE.PY"), ("absent", "src/nothing.py")):
            with problems.leg(f"file-history {label}"):
                code, out, err = self._run(["file-history", path])
                env = _env_of(out, "AC2")
                self.assertIs(env.get("ok"), False, f"a path matching nothing stored must fail, not answer ok:true-empty (data={json.dumps(env.get('data'))[:120]})")
                self.assertIn("unknown_path", json.dumps(env), "the refusal must carry reason unknown_path")
                if label == "cased+backslashed":
                    self.assertIn("src/store.py", json.dumps(env), "the unknown_path refusal must suggest the stored path (case-insensitive match after backslash normalisation)")

        self.assertFalse(problems, "AC2: unknown inputs must be named, never answered as empty success:\n- " + "\n- ".join(problems))


class DroppedAndCursorTests(unittest.TestCase):
    """AC3: bundle --pool drops members as dropped:[{number, reason}]; MCP search/list cursors that do not parse answer bad_request."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = _seed_scratch_db(self.root / "history.db", _base_seed)

    def test_dropped_pool_members_and_bad_cursors(self) -> None:
        problems = _Problems()

        with problems.leg("bundle --pool"):
            code, out, err = run_cli(["bundle", "--pool", "11,77,78", "--repo", REPO, "--db", str(self.db_path), "--format", "json"])
            self.assertEqual(code, 0, f"bundle --pool must succeed: {err}")
            data = _env_of(out, "AC3")["data"]
            dropped = data.get("dropped")
            self.assertIsInstance(dropped, list, f"bundle --pool must report dropped:[{{number, reason}}] for pool members it could not use; data keys are {sorted(data)}")
            dropped_numbers = {e.get("number") for e in dropped if isinstance(e, dict)}
            self.assertTrue({77, 78}.issubset(dropped_numbers), f"the unstored pool members 77 and 78 must be reported as dropped; got {dropped!r}")
            self.assertTrue(all(isinstance(e, dict) and e.get("reason") for e in dropped), f"every dropped entry must carry a reason; got {dropped!r}")

        with problems.leg("MCP cursors"):
            pin_env_for(self, self.root, db=self.db_path)
            session = McpSession(self, cwd=str(self.root), repo=REPO, db=str(self.db_path), stale_after_s=_NO_REFRESH_S)
            self.addCleanup(session.stop)
            session.initialize()
            for tool, arguments in (("search", {"repo": REPO, "query": "needle", "cursor": "abc"}), ("open_items", {"repo": REPO, "cursor": "abc"})):
                with problems.leg(f"MCP {tool} cursor"):
                    reply = session.call_tool(tool, arguments)
                    _result, env = tool_envelope(self, reply, "AC3")
                    self.assertIs(env.get("ok"), False, "cursor 'abc' must fail, not silently reset to the first page (ok:true)")
                    self.assertEqual((env.get("error") or {}).get("code"), "bad_request", f"an unparseable cursor must answer bad_request; got {env.get('error')!r}")
            session.stop()

        self.assertFalse(problems, "AC3: dropped pool members and bad cursors must be named:\n- " + "\n- ".join(problems))


class ProjectionFormatTests(unittest.TestCase):
    """AC4: --fields projects and --format jsonl streams the list payload on search, item, related, similar, triage, bundle."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = _seed_scratch_db(self.root / "history.db", _base_seed)

    def _run(self, argv):
        return run_cli([*argv, "--repo", REPO, "--db", str(self.db_path)])

    def test_fields_and_jsonl_apply_everywhere(self) -> None:
        # command -> (argv, list payload key, a --fields field in its rows)
        table = (
            ("search", ["search", "needle"], "items", "number"),
            ("item", ["item", "11", "12"], None, "number"),
            ("related", ["related", "3"], "nodes", "number"),
            ("similar", ["similar", "11"], "candidates", "number"),
            ("triage", ["triage"], "rows", "number"),
            ("bundle", ["bundle"], "bundles", "members"),
        )
        problems = _Problems()
        for name, argv, list_key, field in table:
            with problems.leg(name):
                code, out, err = self._run([*argv, "--format", "json"])
                self.assertEqual(code, 0, f"`{name}` must succeed on this store (exit {code}: {err.strip().splitlines()[:1]})")
                data = _env_of(out, "AC4")["data"]
                if list_key is None:  # item: one row per number (AC13)
                    self.assertIsInstance(data, list, f"multi-number item must return a list; got {type(data).__name__}")
                    rows = data
                else:
                    rows = data.get(list_key)
                    self.assertIsInstance(rows, list, f"data must carry the {list_key!r} list payload; data keys are {sorted(data) if isinstance(data, dict) else type(data)}")
                expected_lines = len(rows)

                # --fields changes the projected payload.
                code, out_fields, err = self._run([*argv, "--fields", field, "--format", "json"])
                self.assertEqual(code, 0, f"{name} --fields must succeed: {err}")
                self.assertNotEqual(out_fields, out, f"`--fields {field}` must change {name}'s projected output; it changed nothing")
                if name == "search":
                    projected = _env_of(out_fields, "AC4")["data"]["items"]
                    self.assertTrue(projected, "projected rows must survive")
                    self.assertTrue(all(set(row) == {field} for row in projected), f"search --fields {field} must project rows to exactly that field; got keys {sorted(set().union(*(set(r) for r in projected)))}")

                # --format jsonl streams one line per row.
                code, out_jsonl, err = self._run([*argv, "--format", "jsonl"])
                self.assertEqual(code, 0, f"{name} --format jsonl must succeed: {err}")
                lines = [line for line in out_jsonl.splitlines() if line.strip()]
                self.assertEqual(len(lines), expected_lines, f"{name} --format jsonl must stream one JSON line per row ({expected_lines}); got {len(lines)} lines")
                for line in lines:
                    json.loads(line)  # every line is standalone JSON

        self.assertFalse(problems, "AC4: --fields and --format jsonl must apply to every list command:\n- " + "\n- ".join(problems))


class SearchCountTests(unittest.TestCase):
    """AC5: fewer items returned than matched -> truncated:true; --offset pages; strict/broadened counts are independent of --limit."""

    def test_search_counts_are_stable(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)

        def seed(src: FakeGitHubSource) -> None:
            for number in (21, 22, 23):
                src.add_issue(issue(number, title=f"both {number}", body="needle sunset here"))
            for number in (24, 25, 26):
                src.add_issue(issue(number, title=f"one {number}", body="needle only here"))

        db_path = _seed_scratch_db(root / "history.db", seed)

        def run(limit, offset=None):
            argv = ["search", "needle sunset", "--limit", str(limit), "--repo", REPO, "--db", str(db_path), "--format", "json"]
            if offset is not None:
                argv += ["--offset", str(offset)]
            return run_cli(argv)

        code, out, err = run(5)
        self.assertEqual(code, 0, f"AC5: search must succeed: {err}")
        env = _env_of(out, "AC5")
        data = env["data"]
        self.assertEqual(len(data["items"]), 5, "AC5: fixture sanity - 5 of 6 matches fit the page")
        self.assertEqual(data["total_matches"], 6, "AC5: fixture sanity - 6 items match broadened")
        self.assertIs(env.get("truncated"), True, f"AC5: returning fewer items than matched must set truncated:true; got envelope truncated={env.get('truncated')!r}")
        for key, expected in (("strict_matches", 3), ("broadened_matches", 6)):
            self.assertIn(key, data, f"AC5: search must report {key} (independent of --limit); data keys are {sorted(data)}")
            self.assertEqual(data[key], expected, f"AC5: {key} must be {expected}; got {data[key]!r}")

        code, out, err = run(2, offset=0)
        self.assertEqual(code, 0, err)
        page1 = [i["number"] for i in _env_of(out, "AC5")["data"]["items"]]
        code, out, err = run(2, offset=2)
        self.assertEqual(code, 0, f"AC5: search --offset must be accepted (exit {code}: {err.strip().splitlines()[:1]})")
        page2 = [i["number"] for i in _env_of(out, "AC5")["data"]["items"]]
        self.assertFalse(set(page1) & set(page2), f"AC5: --offset must page past the first page; page1={page1} page2={page2}")

        code, out, err = run(1)
        self.assertEqual(code, 0, err)
        narrow = _env_of(out, "AC5")["data"]
        code, out, err = run(20)
        self.assertEqual(code, 0, err)
        wide = _env_of(out, "AC5")["data"]
        self.assertEqual(narrow.get("strict_matches"), wide.get("strict_matches"), "AC5: strict_matches must not depend on --limit")
        self.assertEqual(narrow.get("broadened_matches"), wide.get("broadened_matches"), "AC5: broadened_matches must not depend on --limit")


class SearchRankingTests(unittest.TestCase):
    """AC6: item-text matches outrank comment-only matches and scattered tokens; a comment-only match never answers an empty snippet."""

    def test_search_prefers_item_text(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)

        def seed(src: FakeGitHubSource) -> None:
            # Item 1: title/body do NOT contain the phrase; its comment does.
            src.add_issue(issue(1, title="plain title", body="unrelated prose padding text " * 40))
            src.comment_on(1, "quadratic sync")
            # Item 2: body carries the exact adjacent phrase.
            src.add_issue(issue(2, title="other words", body="fix the quadratic sync failure " + "padding words " * 60))
            # Item 3: both tokens, far apart, short body.
            src.add_issue(issue(3, title="scattered", body="quadratic here and sync there"))

        db_path = _seed_scratch_db(root / "history.db", seed)
        code, out, err = run_cli(["search", "quadratic sync", "--repo", REPO, "--db", str(db_path), "--format", "json"])
        self.assertEqual(code, 0, f"AC6: search must succeed: {err}")
        items = _env_of(out, "AC6")["data"]["items"]
        numbers = [i["number"] for i in items]
        self.assertEqual(numbers[0], 2, f"AC6: the item whose TITLE/BODY carries the phrase (item 2) must outrank the scattered-token item 3 and the comment-only item 1; got order {numbers}")
        self.assertLess(numbers.index(2), numbers.index(1), f"AC6: an item-text match must outrank a comment-only match; order {numbers}")
        by_number = {i["number"]: i for i in items}
        self.assertTrue((by_number.get(1, {}).get("snippet") or "").strip(), "AC6: a comment-only match must never answer an empty snippet when a comment snippet exists (snippet must fall back to it)")


class SnapshotHonestyTests(unittest.TestCase):
    """AC7: an unreachable snapshot source is source_unreachable (exit 1, carrying git's message), distinct from snapshot_absent; snapshot envelopes carry the store's real freshness."""

    def test_snapshot_unreachable_is_not_absent(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        db_path = _seed_scratch_db(root / "history.db", _base_seed)

        bogus = root / "definitely-not-a-repo"
        code, out, err = run_cli(["snapshot", "pull", str(bogus), "--repo", REPO, "--db", str(db_path), "--format", "json"])
        env = _env_of(out, "AC7")
        error = env.get("error") or {}
        self.assertEqual(error.get("code"), "source_unreachable", f"AC7: an unreachable snapshot source must answer source_unreachable, distinct from snapshot_absent; got {error.get('code')!r}")
        self.assertEqual(code, 1, f"AC7: source_unreachable is the exit-1 class (per-attempt transport failure); got exit {code}")
        self.assertTrue("git" in str(error.get("message", "")).lower(), f"AC7: the source_unreachable message must carry git's own error text; got {error.get('message')!r}")

        # Contrast: a REAL git repository without the snapshot ref ->
        # snapshot_absent (base behavior preserved), with real freshness.
        import subprocess

        repo_dir = root / "empty-git"
        repo_dir.mkdir()
        for argv in (["init", "-b", "main"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"], ["add", "-A"], ["commit", "-m", "init"]):
            if argv[0] == "add":
                (repo_dir / "f.txt").write_text("x", encoding="utf-8")
            proc = subprocess.run(["git", "-c", "commit.gpgsign=false", *argv], cwd=str(repo_dir), capture_output=True)
            assert proc.returncode == 0, proc.stderr
        code, out, err = run_cli(["snapshot", "pull", str(repo_dir), "--repo", REPO, "--db", str(db_path), "--format", "json"])
        env = _env_of(out, "AC7")
        self.assertEqual((env.get("error") or {}).get("code"), "snapshot_absent", f"AC7: a real repo without the snapshot ref must stay snapshot_absent; got {env.get('error')!r}")
        freshness = env.get("freshness") or {}
        self.assertIsNotNone(freshness.get("synced_at"), f"AC7: snapshot envelopes must carry the store's real freshness; got {freshness!r}")


def _type_ok(declared: str, value) -> bool:
    checks = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list), "string": lambda v: isinstance(v, str), "integer": lambda v: isinstance(v, int) and not isinstance(v, bool), "boolean": lambda v: isinstance(v, bool), "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool), "null": lambda v: v is None}
    check = checks.get(declared)
    return check(value) if check else True


def _validate(schema, value, path: str, errors: list) -> None:
    """Minimal stdlib JSON-schema type checker (no external dependency)."""
    if not isinstance(schema, dict):
        return
    declared = schema.get("type")
    if isinstance(declared, str) and not _type_ok(declared, value):
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


class OutputSchemaTests(unittest.TestCase):
    """AC8: every tool's outputSchema names the envelope keys and that tool's data keys, and real results validate against it."""

    def test_output_schemas_describe_real_shapes(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = _seed_scratch_db(self.root / "history.db", _base_seed)
        pin_env_for(self, self.root, db=self.db_path)
        session = McpSession(self, cwd=str(self.root), repo=REPO, db=str(self.db_path), stale_after_s=_NO_REFRESH_S)
        self.addCleanup(session.stop)
        session.initialize()
        result = session.wait_response(session.request("tools/list", {})).get("result") or {}
        tools = {t.get("name"): t for t in result.get("tools") or [] if isinstance(t, dict)}
        calls = {
            "graph_status": {"repo": REPO},
            "search": {"repo": REPO, "query": "needle"},
            "sql": {"repo": REPO, "statement": "SELECT number FROM items", "limit": 5},
            "get_item": {"repo": REPO, "number": 1},
        }
        problems = _Problems()
        for name, arguments in calls.items():
            with problems.leg(name):
                tool = tools.get(name)
                self.assertIsInstance(tool, dict, f"tools/list must advertise {name}; got {sorted(tools)}")
                schema = (tool or {}).get("outputSchema")
                self.assertIsInstance(schema, dict, f"{name} must declare an outputSchema")
                properties = (schema or {}).get("properties")
                self.assertIsInstance(properties, dict, f"the outputSchema must NAME the envelope keys (ok/data/truncated ...); properties is {properties!r}")
                self.assertTrue({"ok", "data", "truncated"}.issubset(properties), f"must declare the envelope keys; missing {sorted({'ok', 'data', 'truncated'} - set(properties))}; declared {sorted(properties)}")
                data_properties = ((properties.get("data") or {}).get("properties"))
                self.assertIsInstance(data_properties, dict, f"the outputSchema must describe that tool's data keys; data.properties is {data_properties!r}")
                self.assertTrue(EXPECTED_TOOL_DATA_KEYS[name].issubset(data_properties), f"the data schema must name its payload keys; missing {sorted(EXPECTED_TOOL_DATA_KEYS[name] - set(data_properties))}; declared {sorted(data_properties)}")
                # A REAL result validates against the declared schema.
                _result, env = tool_envelope(self, session.call_tool(name, arguments), "AC8")
                self.assertIs(env.get("ok"), True, f"the real call must succeed: {env.get('error')!r}")
                errors: list = []
                _validate(schema, env, name, errors)
                self.assertFalse(errors, f"the real result must validate against its declared outputSchema: {errors}")
        session.stop()
        self.assertFalse(problems, "AC8: every pinned tool's outputSchema must name the envelope and data keys, and real results must validate:\n- " + "\n- ".join(problems))


class VocabularyWarningTests(unittest.TestCase):
    """AC10: sql comparing vocabulary columns to an unknown literal returns rows unchanged PLUS a warning naming column, literal and stored values (repo: the stored slug differing only by case). Guard layers untouched."""

    def test_out_of_vocabulary_literal_warns(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)

        def _seeded(src: FakeGitHubSource) -> None:
            # The base corpus plus stored comment rows: the C19 amend legs
            # probe the COMMENTS table's vocabulary (repo column and the
            # kind='issue' incident), which needs comments to exist.
            _base_seed(src)
            src.comment_on(1, "stored issue comment")

        self.db_path = _seed_scratch_db(root / "history.db", _seeded)

        def _run(statement: str) -> dict:
            code, out, err = run_cli(["sql", statement, "--repo", REPO, "--db", str(self.db_path), "--format", "json"])
            return _env_of(out, "AC10")

        def _warnings(env: dict):
            warnings = env.get("warnings")
            if warnings is None:
                warnings = (env.get("data") or {}).get("warnings")
            return warnings

        probe = connect(self.db_path)
        stored: dict = {}
        for table, column in (("items", "kind"), ("items", "state"), ("comments", "kind"), ("edges", "rel"), ("edges", "source")):
            values = tuple(str(r[0]) for r in probe.execute(f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL ORDER BY 1"))
            if values:
                stored[f"{table}.{column}"] = values
        probe.close()
        self.assertTrue(stored, "fixture: the seeded store has vocabulary")

        problems = _Problems()
        for qualified, values in stored.items():
            with problems.leg(qualified):
                table, column = qualified.split(".")
                literal = "zz_" + values[0]
                env = _run(f"SELECT 1 FROM {table} WHERE {column} = '{literal}' LIMIT 1")
                self.assertIs(env.get("ok"), True, f"an out-of-vocabulary literal must return rows unchanged (ok:true), only warning; got {env.get('error')!r}")
                self.assertEqual(env["data"]["rows"], [], "the literal matches nothing; rows must be unchanged")
                warnings = _warnings(env)
                self.assertTrue(isinstance(warnings, list) and warnings, f"the envelope must carry a non-empty warnings entry naming column, literal and stored values; got {json.dumps(env)[:160]}")
                text = json.dumps(warnings)
                self.assertIn(column, text, f"the warning must name the column; got {text}")
                self.assertIn(literal, text, f"the warning must name the literal; got {text}")
                self.assertTrue(any(value in text for value in values), f"the warning must name the stored values {values}; got {text}")

        with problems.leg("repo case"):
            env = _run("SELECT number FROM items WHERE repo = 'ACME/ForgeGate'")
            self.assertIs(env.get("ok"), True, "the repo probe must stay ok:true")
            self.assertEqual(env["data"]["rows"], [], "the case-mismatched literal matches nothing")
            warnings = _warnings(env)
            self.assertTrue(warnings, "comparing a repo column to a slug differing only by case must produce a warning entry")
            self.assertIn(REPO, json.dumps(warnings), f"the repo warning must name the stored slug {REPO}; got {warnings!r}")

        # Amend leg (fix-plan v3 CF-A, r2-f8): repo columns OUTSIDE items
        # must warn too — the vocabulary scan discovers every column named
        # `repo` from the LIVE store schema (PRAGMA table_info across user
        # tables), never a hardcoded items-only list.
        with problems.leg("comments.repo case (non-items repo column)"):
            env = _run("SELECT 1 FROM comments WHERE repo = 'ACME/ForgeGate' LIMIT 1")
            self.assertIs(env.get("ok"), True, f"the comments.repo probe must stay ok:true; got {env.get('error')!r}")
            self.assertEqual(env["data"]["rows"], [], "the case-mismatched literal matches nothing in comments either")
            warnings = _warnings(env)
            self.assertTrue(warnings, "comparing the COMMENTS table's repo column to a slug differing only by case must produce a warning entry — the repo-column scan is schema-discovered, not items-only")
            self.assertIn(REPO, json.dumps(warnings), f"the comments.repo warning must name the stored slug {REPO}; got {warnings!r}")

        # Amend leg: the recorded incident — `comments.kind = 'issue'` looks
        # plausible but the stored vocabulary is issue_comment/review_comment;
        # the warning must fire (a silent empty answer here once sent an
        # agent hunting a phantom 'issue' kind).
        comment_kinds = tuple(stored.get("comments.kind", ()) or ())
        with problems.leg("comments.kind='issue' incident"):
            env = _run("SELECT 1 FROM comments WHERE kind = 'issue' LIMIT 1")
            self.assertIs(env.get("ok"), True, f"the comments.kind probe must stay ok:true; got {env.get('error')!r}")
            self.assertEqual(env["data"]["rows"], [], "'issue' matches no stored comments.kind (stored values are issue_comment/review_comment)")
            warnings = _warnings(env)
            self.assertTrue(warnings, f"comments.kind='issue' MUST warn: the stored vocabulary is {comment_kinds or ('issue_comment', 'review_comment')}, never 'issue'")
            text = json.dumps(warnings)
            self.assertTrue(any(value in text for value in (comment_kinds or ("issue_comment",))), f"the warning must name the stored comments.kind values {comment_kinds}; got {text}")

        # The read-only guard layers are untouched by the warning pass.
        with self.assertRaises(ValueError):
            assert_read_sql("DELETE FROM items")

        self.assertFalse(problems, "AC10: out-of-vocabulary literals must warn while rows stay unchanged:\n- " + "\n- ".join(problems))


class SqlHintTests(unittest.TestCase):
    """AC11: no_such_table carries a hint listing store tables; the PRAGMA refusal (bad_sql) hints at `zaxbygraph schema [TABLE]`."""

    def test_unknown_table_and_pragma_hints(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        db_path = _seed_scratch_db(root / "history.db", _base_seed)
        base = ["--repo", REPO, "--db", str(db_path), "--format", "json"]

        code, out, err = run_cli(["sql", "SELECT * FROM nope_table", *base])
        error = _env_of(out, "AC11").get("error") or {}
        self.assertEqual(error.get("code"), "no_such_table", f"AC11: base code contract; got {error.get('code')!r}")
        hint = error.get("hint")
        self.assertIsInstance(hint, str, f"AC11: the no_such_table error must carry a hint; got {error!r}")
        for table in ("items", "edges"):
            self.assertIn(table, hint, f"AC11: the no_such_table hint must list the store's tables; expected {table!r} in {hint!r}")

        code, out, err = run_cli(["sql", "PRAGMA journal_mode=DELETE", *base])
        error = _env_of(out, "AC11").get("error") or {}
        self.assertEqual(error.get("code"), "bad_sql", f"AC11: base code contract; got {error.get('code')!r}")
        hint = error.get("hint")
        self.assertIsInstance(hint, str, f"AC11: the PRAGMA refusal must carry a hint; got {error!r}")
        self.assertIn("zaxbygraph schema", hint, f"AC11: the PRAGMA hint must name `zaxbygraph schema [TABLE]`; got {hint!r}")


class UsageErrorEnvelopeTests(unittest.TestCase):
    """AC12: argparse failures under machine formats write a bad_request envelope to STDOUT (argparse's message + nearest-valid-flag hint, exit 2); --db/--repo/--format are accepted BEFORE the subcommand too."""

    def test_usage_errors_are_envelopes(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        self.db_path = _seed_scratch_db(root / "history.db", _base_seed)

        problems = _Problems()
        cases = (
            # (tag, argv, message token, expected hint token)
            ("unknown flag", ["search", "needle", "--limt", "5"], "limt", "--limit"),
            ("bad value", ["item", "--max-body-chars", "x", "1"], "max-body-chars", "--max-body-chars"),
            ("missing positional", ["search"], "query", "search"),
        )
        for tag, argv, message_token, hint_token in cases:
            with problems.leg(tag):
                code, out, err = run_cli([*argv, "--repo", REPO, "--db", str(self.db_path), "--format", "json"])
                self.assertEqual(code, 2, f"argparse failures keep exit 2; got {code}")
                error = _env_of(out, "AC12").get("error") or {}
                self.assertEqual(error.get("code"), "bad_request", f"a usage error under a machine format must answer the bad_request envelope on STDOUT; got stdout {out[:120]!r}")
                self.assertIn(message_token, str(error.get("message", "")), f"the envelope message must carry argparse's own text ({message_token!r}); got {error.get('message')!r}")
                self.assertIn(hint_token, str(error.get("hint", "")), f"the envelope hint must name the nearest valid flag ({hint_token!r}); got {error.get('hint')!r}")

        with problems.leg("jsonl mode"):
            code, out, err = run_cli(["item", "--max-body-chars", "x", "1", "--repo", REPO, "--db", str(self.db_path), "--format", "jsonl"])
            self.assertEqual(code, 2, "jsonl usage errors keep exit 2")
            error = _env_of(out, "AC12").get("error") or {}
            self.assertEqual(error.get("code"), "bad_request", "jsonl-mode usage errors must stay enveloped")

        for argv in (["--db", str(self.db_path), "--repo", REPO, "--format", "json", "status"], ["--format", "json", "--db", str(self.db_path), "--repo", REPO, "search", "needle"]):
            with problems.leg(f"global flags first ({argv[-1]})"):
                code, out, err = run_cli(argv)
                self.assertEqual(code, 0, f"--db/--repo/--format must be accepted before the subcommand (exit {code}: {err.strip().splitlines()[:1]})")
                self.assertIs(_env_of(out, "AC12").get("ok"), True, "the global-flags-first read must succeed")

        self.assertFalse(problems, "AC12: usage errors under machine formats must be bad_request envelopes on stdout, and global flags must work before the subcommand:\n- " + "\n- ".join(problems))


class MultiItemTests(unittest.TestCase):
    """AC13: `item N1 N2 ...` returns one row per number in order, per-row flags apply per row, and unstored numbers are named not_found per row instead of failing the call."""

    def test_item_reads_several_numbers(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)

        def seed(src: FakeGitHubSource) -> None:
            src.add_issue(issue(1, title="first", body="b" * 5000, state="open", comments=3))
            src.issue_comments[1] = [comment(510_000 + i, f"note {i}") for i in range(3)]
            src.issues[1]["comments"] = 3
            src.add_issue(issue(3, title="third", body="short", state="open"))

        db_path = _seed_scratch_db(root / "history.db", seed)
        code, out, err = run_cli(["item", "1", "99", "3", "--max-body-chars", "10", "--max-comments", "1", "--repo", REPO, "--db", str(db_path), "--format", "json"])
        self.assertEqual(code, 0, f"AC13: `item N1 N2 ...` must be accepted (exit {code}: {err.strip().splitlines()[:1]}); one row per number")
        data = _env_of(out, "AC13")["data"]
        self.assertIsInstance(data, list, f"AC13: multi-number item must return one row per number; got {type(data).__name__}")
        self.assertEqual([row.get("number") for row in data], [1, 99, 3], f"AC13: rows must come back one per requested number, in order; got {[row.get('number') for row in data]}")
        first, missing, third = data
        self.assertLessEqual(len(first.get("body") or ""), 10, "AC13: --max-body-chars must apply per row")
        self.assertLessEqual(len(first.get("comments") or []), 1, "AC13: --max-comments must apply per row")
        self.assertEqual(first.get("comments_omitted"), 2, f"AC13: the per-row comment cap must report the omission; got {first.get('comments_omitted')!r}")
        self.assertLessEqual(len(third.get("body") or ""), 10, "AC13: --max-body-chars applies to every row")
        missing_code = ((missing.get("error") or {}).get("code") if isinstance(missing, dict) else None)
        self.assertTrue(missing_code == "not_found" or missing.get("not_found") is True, f"AC13: an unstored number must be named not_found per row instead of failing the whole call; got row {missing!r}")


def _parse_shape_table(text: str) -> dict:
    """Parse the SKILL.md shape table: a markdown table whose header row is `| command | data keys |`; body rows map command -> the comma-separated data-keys cell."""
    rows: dict = {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0].lower() == "command" and "data keys" in cells[1].lower():
            for row_line in lines[index + 1:]:
                if "|" not in row_line:
                    break
                row_cells = [cell.strip() for cell in row_line.strip().strip("|").split("|")]
                if not row_cells or not row_cells[0]:
                    break
                if set(row_cells[0]) <= set("-: "):  # separator row
                    continue
                rows[row_cells[0]] = row_cells[1] if len(row_cells) > 1 else ""
            break
    return rows


class CliShapeTableTests(unittest.TestCase):
    """AC14: `schema --output COMMAND` prints per-command the same shape the MCP schemas declare; SKILL.md carries a matching per-command shape table."""

    def test_cli_shapes_are_described(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        db_path = _seed_scratch_db(root / "history.db", _base_seed)

        problems = _Problems()
        declared: dict = {}
        for command in REQUIRED_COMMANDS:
            with problems.leg(f"schema --output {command}"):
                code, out, err = run_cli(["schema", "--output", command, "--repo", REPO, "--db", str(db_path), "--format", "json"])
                self.assertEqual(code, 0, f"`schema --output {command}` must be accepted (exit {code}: {err.strip().splitlines()[:1]})")
                data = _env_of(out, "AC14")["data"]
                self.assertEqual(data.get("command"), command, f"the schema payload must name its command; got {sorted(data) if isinstance(data, dict) else data!r}")
                keys = data.get("data_keys")
                self.assertTrue(isinstance(keys, list) and keys and all(isinstance(k, str) for k in keys), f"must declare the command's data keys as data_keys; got {keys!r}")
                declared[command] = keys
                mcp_tool = _CLI_TO_MCP_TOOL.get(command)
                if mcp_tool is not None:
                    self.assertTrue(EXPECTED_TOOL_DATA_KEYS[mcp_tool].issubset(keys), f"must declare the same shape the MCP {mcp_tool} schema declares; missing {sorted(EXPECTED_TOOL_DATA_KEYS[mcp_tool] - set(keys))}; declared {keys}")

        with problems.leg("SKILL.md shape table"):
            skill_path = (Path(__file__).resolve().parents[1] / "skills" / "zaxbygraph" / "SKILL.md")
            self.assertTrue(skill_path.is_file(), f"SKILL.md must exist at {skill_path}")
            table = _parse_shape_table(skill_path.read_text(encoding="utf-8"))
            self.assertTrue(table, "SKILL.md must carry a per-command shape table with header `| command | data keys |` (one row per read command)")
            for command in REQUIRED_COMMANDS:
                self.assertIn(command, table, f"the SKILL.md shape table must carry a row for `{command}`; rows present: {sorted(table)}")
                if command in declared:
                    skill_keys = {piece.strip() for piece in table[command].split(",") if piece.strip()}
                    self.assertEqual(skill_keys, set(declared[command]), f"the SKILL.md shape row for `{command}` must match `schema --output {command}`; skill={sorted(skill_keys)} schema={sorted(declared[command])}")

        self.assertFalse(problems, "AC14: `schema --output COMMAND` and the SKILL.md shape table must describe every read command's payload:\n- " + "\n- ".join(problems))


if __name__ == "__main__":
    unittest.main()
