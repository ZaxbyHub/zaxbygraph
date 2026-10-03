"""Acceptance checks for issue-trace 3-json-envelope-output-contract (issue #3).

Black-box spec of the JSON envelope output contract. Authored at arm's length
against the issue's acceptance criteria AC1-AC7; must FAIL at base bf0b548 and
PASS once the criteria are implemented.

Every failure carries a distinctive single-line tag (ENV1..ENV3, SCHEMA,
JSONL, BODY, IDENT) so a line-wise grep can tell the checks apart.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, issue, pr_file, pull
from zaxbygraph.cli import main
from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo

# Item 20 carries bodies past the AC6 threshold of 500 chars.
LONG_BODY = "b" * 900
LONG_COMMENT = "c" * 800

IDENTITY_LINE_RE = re.compile(
    r"^# db=.* repo=\S+ items=\d+ synced=\d+ complete=(yes|no)$"
)


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run main() in-process, catching the SystemExit argparse raises for an
    unknown subcommand or flag (main() calls parse_args internally, so that
    exit happens inside main). Returns (code, stdout, stderr) so callers
    assert with AssertionError instead of erroring."""

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


def pin_env_into(root: Path):
    """Point ZAXBYGRAPH_HOME/ZAXBYGRAPH_DB inside the temp root so the
    user-level store is never touched by an in-process read. Returns a
    restore callable."""
    saved = {k: os.environ.get(k) for k in ("ZAXBYGRAPH_HOME", "ZAXBYGRAPH_DB")}
    os.environ["ZAXBYGRAPH_HOME"] = str(root / "zhome")
    os.environ["ZAXBYGRAPH_DB"] = str(root / "history.db")

    def restore():
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    return restore


def seed_envelope_db(db_path: str) -> None:
    """Two unconnected issues, one merged PR touching a file, and one item
    with over-500-char bodies: gives path/related/churn/open/sql and the
    body-truncation check something to chew on. Offline, FakeGitHubSource."""
    conn = connect(Path(db_path))
    init_schema(conn)
    src = FakeGitHubSource()
    src.add_issue(issue(1, title="first issue", body="needle one", state="open"))
    src.add_issue(issue(2, title="second issue", body="no links here", state="open"))
    src.add_pr(
        issue(10, title="touch store", body="adds store file", kind="pr", state="closed"),
        pull(10, changed_files=1, merged=True),
        files=[pr_file("src/store.py")],
    )
    src.add_issue(issue(20, title="long bodies", body=LONG_BODY, state="open"))
    src.comment_on(20, LONG_COMMENT)
    sync_repo(conn, src, REPO)
    conn.close()


def setUp_harness(test: unittest.TestCase) -> str:
    """Shared setUp body: temp dir, seeded DB, pinned env. Returns db path."""
    test._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    test.addCleanup(test._td.cleanup)
    root = Path(test._td.name)
    test.addCleanup(pin_env_into(root))
    db = str(root / "history.db")
    seed_envelope_db(db)
    return db


class ErrorContractTests(unittest.TestCase):
    """AC1: a failed sql in JSON mode answers on stdout with a structured
    error object carrying code/message/hint, and still exits 1."""

    def setUp(self) -> None:
        self.db = setUp_harness(self)

    def test_sql_bad_column_json_error_on_stdout_with_hint(self) -> None:
        code, out, err = run_cli(
            [
                "sql",
                "SELECT pr_number FROM pr_files",
                "--repo",
                REPO,
                "--db",
                self.db,
                "--format",
                "json",
            ]
        )
        self.assertTrue(code == 1, "ENV1: a bad-column sql must still exit 1")
        self.assertTrue(
            out.strip(),
            "ENV1: the failed sql error object must be written to stdout",
        )
        try:
            payload = json.loads(out)
        except ValueError:
            self.fail("ENV1: failed sql stdout must parse as one JSON object")
        self.assertIsInstance(payload, dict, "ENV1: failed sql payload must be an object")
        self.assertIs(payload.get("ok"), False, "ENV1: failed sql payload must have ok false")
        error = payload.get("error")
        self.assertIsInstance(
            error, dict, "ENV1: failed sql payload must carry an error object"
        )
        self.assertEqual(
            error.get("code"),
            "no_such_column",
            "ENV1: error.code must be no_such_column",
        )
        message = str(error.get("message", ""))
        self.assertTrue(
            "pr_number" in message,
            "ENV1: error.message must mention the bad column pr_number",
        )
        hint = error.get("hint")
        if isinstance(hint, str):
            hint_text = hint
        elif isinstance(hint, (list, tuple)):
            hint_text = " ".join(str(x) for x in hint)
        elif hint is None:
            hint_text = ""
        else:
            hint_text = json.dumps(hint, default=str)
        self.assertTrue(
            "number" in hint_text,
            "ENV1: error.hint must list real pr_files columns such as number",
        )


class EnvelopeTests(unittest.TestCase):
    """AC2/AC3: every command's JSON success output is one self-describing
    envelope object; sql rows are objects by default with --rows array and
    --limit honored."""

    def setUp(self) -> None:
        self.db = setUp_harness(self)

    def test_every_command_returns_envelope(self) -> None:
        commands = [
            ["status"],
            ["search", "needle"],
            ["item", "1"],
            ["related", "1"],
            ["churn"],
            ["open"],
            ["path", "1", "2"],
            ["sql", "SELECT number FROM items"],
            ["where"],
            ["export-graph"],
        ]
        required = ("ok", "db", "repo", "freshness", "data", "truncated")
        problems: list[str] = []
        for argv in commands:
            code, out, err = run_cli(
                [*argv, "--repo", REPO, "--db", self.db, "--format", "json"]
            )
            if code != 0:
                problems.append(argv[0] + " exited " + str(code))
                continue
            try:
                payload = json.loads(out)
            except ValueError:
                problems.append(argv[0] + " stdout is not JSON")
                continue
            if not isinstance(payload, dict):
                problems.append(argv[0] + " stdout is not a JSON object")
                continue
            missing = [k for k in required if k not in payload]
            if missing:
                problems.append(argv[0] + " missing " + ",".join(missing))
                continue
            if payload["ok"] is not True:
                problems.append(argv[0] + " ok is not True")
            if not isinstance(payload["db"], str) or not payload["db"]:
                problems.append(argv[0] + " db is not a path string")
            if payload["repo"] != REPO:
                problems.append(argv[0] + " repo is not " + REPO)
            fresh = payload["freshness"]
            if not isinstance(fresh, dict) or any(
                k not in fresh for k in ("synced_at", "age_s", "complete")
            ):
                problems.append(
                    argv[0] + " freshness lacks synced_at age_s complete"
                )
            if argv[0] == "path":
                data = payload.get("data")
                if not isinstance(data, dict) or data.get("path") is not None:
                    problems.append(
                        "path data.path must be null for two unconnected numbers"
                    )
        self.assertFalse(
            problems,
            "ENV2: commands not returning the JSON envelope: " + "; ".join(problems),
        )

    def test_sql_rows_are_objects_and_limit_flag(self) -> None:
        sql = "SELECT number, repo FROM items ORDER BY number"
        code, out, err = run_cli(
            ["sql", sql, "--repo", REPO, "--db", self.db, "--format", "json"]
        )
        self.assertTrue(code == 0, "ENV3: baseline sql must exit 0")
        payload = json.loads(out)
        data = payload.get("data") if isinstance(payload, dict) else None
        self.assertIsInstance(
            data, dict, "ENV3: sql envelope must carry a data object"
        )
        self.assertTrue("columns" in data, "ENV3: sql data must list columns")
        rows = data.get("rows")
        self.assertIsInstance(rows, list, "ENV3: sql data must carry rows")
        self.assertTrue(bool(rows), "ENV3: sql must return the seeded rows")
        self.assertTrue(
            all(isinstance(r, dict) for r in rows),
            "ENV3: default sql rows must be objects keyed by column name",
        )
        self.assertTrue(
            all("number" in r and "repo" in r for r in rows),
            "ENV3: each default row must be keyed by the selected column names",
        )

        code2, out2, _err2 = run_cli(
            [
                "sql",
                sql,
                "--repo",
                REPO,
                "--db",
                self.db,
                "--format",
                "json",
                "--rows",
                "array",
            ]
        )
        self.assertTrue(code2 == 0, "ENV3: sql --rows array must be accepted")
        payload2 = json.loads(out2)
        rows2 = (
            payload2.get("data", {}).get("rows")
            if isinstance(payload2, dict)
            else None
        )
        self.assertIsInstance(rows2, list, "ENV3: --rows array rows must be a list")
        self.assertTrue(
            bool(rows2) and all(isinstance(r, list) for r in rows2),
            "ENV3: --rows array must keep positional list rows",
        )

        code3, out3, _err3 = run_cli(
            [
                "sql",
                sql,
                "--repo",
                REPO,
                "--db",
                self.db,
                "--format",
                "json",
                "--limit",
                "1",
            ]
        )
        self.assertTrue(code3 == 0, "ENV3: sql --limit must be accepted")
        payload3 = json.loads(out3)
        rows3 = (
            payload3.get("data", {}).get("rows")
            if isinstance(payload3, dict)
            else None
        )
        self.assertIsInstance(rows3, list, "ENV3: --limit rows must be a list")
        self.assertTrue(
            len(rows3) == 1,
            "ENV3: sql --limit 1 must return exactly one row",
        )


class SchemaCommandTests(unittest.TestCase):
    """AC4: `zaxbygraph schema [TABLE]` prints live-DB DDL plus per-column
    notes, including TEXT-typed edges.src_id/dst_id and the NULL-unless-
    --include-patches caveat for pr_files.patch."""

    def setUp(self) -> None:
        self.db = setUp_harness(self)

    def test_schema_command_lists_columns_and_notes(self) -> None:
        code_all, out_all, _err_all = run_cli(
            ["schema", "--repo", REPO, "--db", self.db]
        )
        self.assertTrue(
            code_all == 0,
            "SCHEMA: zaxbygraph schema must be a recognized subcommand exiting 0",
        )
        code_one, out_one, _err_one = run_cli(
            ["schema", "pr_files", "--repo", REPO, "--db", self.db]
        )
        self.assertTrue(
            code_one == 0,
            "SCHEMA: zaxbygraph schema pr_files must exit 0",
        )
        combined = out_all + "\n" + out_one
        self.assertTrue(
            "pr_files" in combined,
            "SCHEMA: schema output must name the pr_files table",
        )
        self.assertTrue(
            "number" in out_one,
            "SCHEMA: the pr_files schema must list the real column number",
        )
        self.assertTrue(
            "src_id" in combined,
            "SCHEMA: schema output must cover edges.src_id",
        )
        self.assertTrue(
            "dst_id" in combined,
            "SCHEMA: schema output must cover edges.dst_id",
        )
        self.assertTrue(
            "TEXT" in combined,
            "SCHEMA: per-column notes must state edges src_id dst_id are TEXT-typed",
        )
        self.assertTrue(
            "include-patches" in combined,
            "SCHEMA: pr_files.patch note must say NULL unless include-patches was used",
        )


class FormatTests(unittest.TestCase):
    """AC5/AC6/AC7: jsonl prefix parsing and --fields projection, body
    truncation via --max-body-chars, and the single stderr identity line."""

    def setUp(self) -> None:
        self.db = setUp_harness(self)

    def test_jsonl_prefix_parses_and_fields_projects(self) -> None:
        code, out, _err = run_cli(
            ["open", "--repo", REPO, "--db", self.db, "--format", "jsonl"]
        )
        self.assertTrue(
            code == 0,
            "JSONL: open --format jsonl must be accepted and exit 0",
        )
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertTrue(
            len(lines) >= 1,
            "JSONL: jsonl output must have at least one line",
        )
        for line in lines[:3]:
            try:
                obj = json.loads(line)
            except ValueError:
                self.fail(
                    "JSONL: each jsonl line must parse as a standalone JSON object"
                )
            self.assertIsInstance(
                obj,
                dict,
                "JSONL: each jsonl line must be a JSON object",
            )

        code2, out2, _err2 = run_cli(
            [
                "open",
                "--repo",
                REPO,
                "--db",
                self.db,
                "--format",
                "json",
                "--fields",
                "repo,number",
            ]
        )
        self.assertTrue(
            code2 == 0,
            "JSONL: open --fields repo,number must be accepted and exit 0",
        )
        payload = json.loads(out2)
        rows = payload.get("data") if isinstance(payload, dict) else None
        self.assertIsInstance(
            rows,
            list,
            "JSONL: the open envelope data must be the list of rows",
        )
        self.assertTrue(bool(rows), "JSONL: open must return the open items")
        bad = [
            r
            for r in rows
            if not isinstance(r, dict) or set(r.keys()) != {"repo", "number"}
        ]
        self.assertFalse(
            bad,
            "JSONL: --fields repo,number must project every row to exactly keys repo and number",
        )

    def test_item_max_body_chars(self) -> None:
        code, out, _err = run_cli(
            [
                "item",
                "20",
                "--repo",
                REPO,
                "--db",
                self.db,
                "--format",
                "json",
                "--max-body-chars",
                "500",
            ]
        )
        self.assertTrue(
            code == 0,
            "BODY: item --max-body-chars must be accepted and exit 0",
        )
        payload = json.loads(out)
        data = payload.get("data") if isinstance(payload, dict) else None
        self.assertIsInstance(
            data,
            dict,
            "BODY: the item envelope data must hold the item record",
        )
        body = data.get("body")
        self.assertIsInstance(body, str, "BODY: the item record must carry a body")
        self.assertTrue(
            len(body) <= 500,
            "BODY: the item body must be truncated to at most 500 chars",
        )
        self.assertIs(
            data.get("truncated"),
            True,
            "BODY: the truncated item must be marked truncated true",
        )
        self.assertIs(
            payload.get("truncated"),
            True,
            "BODY: the envelope truncated flag must be true when bodies were cut",
        )
        comments = data.get("comments")
        self.assertIsInstance(
            comments,
            list,
            "BODY: the item record must carry its comments",
        )
        self.assertTrue(bool(comments), "BODY: the seeded long comment must be present")
        for c in comments:
            cbody = c.get("body")
            self.assertIsInstance(
                cbody,
                str,
                "BODY: each comment must carry a body",
            )
            self.assertTrue(
                len(cbody) <= 500,
                "BODY: each comment body must be truncated to at most 500 chars",
            )
            self.assertIs(
                c.get("truncated"),
                True,
                "BODY: each truncated comment must be marked truncated true",
            )

    def test_stderr_identity_line(self) -> None:
        code, out, err = run_cli(
            ["status", "--repo", REPO, "--db", self.db, "--format", "json"]
        )
        self.assertTrue(
            code == 0,
            "IDENT: status must succeed for the identity-line check",
        )
        lines = [line for line in err.splitlines() if line.strip()]
        self.assertTrue(
            len(lines) == 1,
            "IDENT: stderr must carry exactly one non-empty identity line, got "
            + str(len(lines)),
        )
        self.assertTrue(
            IDENTITY_LINE_RE.match(lines[0]) is not None,
            "IDENT: the stderr line must match db repo items synced complete shape",
        )


if __name__ == "__main__":
    unittest.main()
