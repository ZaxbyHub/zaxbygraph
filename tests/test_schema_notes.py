"""Issue #3 companions: schema-notes/doc drift guard, text-mode smoke, and
the envelope guardrail over every registered subcommand.

These tests accompany the frozen acceptance checks in test_cli_envelope.py
(which stay byte-identical); they pin the surfaces the frozen checks leave
free so the contract cannot silently regress:
- COLUMN_NOTES agrees with the live schema and with docs/schema.md;
- text mode renders bare data (never envelope keys);
- every subcommand build_parser() registers emits the envelope on success.
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
from zaxbygraph.cli import build_parser, main
from zaxbygraph.db import connect, init_schema
from zaxbygraph.schema_notes import COLUMN_NOTES
from zaxbygraph.sync import sync_repo

REPO_ROOT = Path(__file__).resolve().parents[1]


def _seed(db_path: Path) -> None:
    conn = connect(db_path)
    init_schema(conn)
    src = FakeGitHubSource()
    src.add_issue(issue(1, title="guardrail one", body="needle", state="open"))
    src.add_issue(issue(2, title="guardrail two", body="other", state="open"))
    src.add_pr(
        issue(10, title="guardrail pr", kind="pr", state="closed"),
        pull(10, changed_files=1, merged=True),
        files=[pr_file("src/store.py")],
    )
    sync_repo(conn, src, REPO)
    conn.close()


def _guardrail_commands() -> list[list[str]]:
    """One success-path invocation per envelope-covered subcommand. `sync`
    is excluded (network write; its envelope is pinned by test_sync.py)."""
    return [
        ["status"],
        ["search", "needle"],
        ["item", "1"],
        ["related", "1"],
        ["churn"],
        ["open"],
        ["path", "1", "2"],
        ["sql", "SELECT number FROM items"],
        ["schema"],
        ["schema", "pr_files"],
        ["where"],
        ["export-graph"],
        ["doctor"],
    ]


class EnvelopeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        self.db = str(root / "history.db")
        _seed(Path(self.db))
        saved = {k: os.environ.get(k) for k in ("ZAXBYGRAPH_HOME", "ZAXBYGRAPH_DB")}
        os.environ["ZAXBYGRAPH_HOME"] = str(root / "zhome")
        os.environ["ZAXBYGRAPH_DB"] = self.db

        def restore() -> None:
            for key, val in saved.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val

        self.addCleanup(restore)

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*argv, "--repo", REPO, "--db", self.db])
        return code, out.getvalue(), err.getvalue()

    def test_column_notes_reference_live_columns(self) -> None:
        """Every note must name a real table.column in the live schema, and
        every table that carries notes must exist."""
        conn = connect(Path(self.db))
        self.addCleanup(conn.close)
        live = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for table, notes in COLUMN_NOTES.items():
            self.assertIn(table, live, f"COLUMN_NOTES names unknown table {table}")
            cols = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            for column in notes:
                self.assertIn(
                    column, cols, f"COLUMN_NOTES[{table}] names unknown column {column}"
                )

    def test_documented_caveats_cannot_drift(self) -> None:
        """The two caveats the issue calls out must exist BOTH in COLUMN_NOTES
        and in docs/schema.md - the command and the doc cannot drift apart."""
        edges = COLUMN_NOTES["edges"]
        self.assertIn("TEXT", edges["src_id"])
        self.assertIn("TEXT", edges["dst_id"])
        self.assertIn("--include-patches", COLUMN_NOTES["pr_files"]["patch"])
        doc = (REPO_ROOT / "docs" / "schema.md").read_text(encoding="utf-8")
        self.assertIn("TEXT", doc)
        self.assertIn("include-patches", doc)

    def test_schema_command_text_mode_smoke(self) -> None:
        """Text mode renders bare data and never envelope keys (plan item 4):
        no ok:/freshness:/truncated: lines leak into the text rendering."""
        code, out, err = self._run(["schema", "pr_files", "--format", "text"])
        self.assertEqual(code, 0, err)
        self.assertIn("pr_files", out)
        self.assertIn("patch", out)
        for banned in ("ok:", "freshness:", "truncated:"):
            self.assertNotIn(banned, out)

    def test_where_identity_line_matches_payload(self) -> None:
        """4.5 review finding 1 (HIGH): `where`'s stderr identity line must
        report the same items/synced/complete facts its envelope payload
        does — not zeros from a closed connection.

        The fixture seeds the USER-LEVEL STORE (store_db_path under the
        pinned ZAXBYGRAPH_HOME), because that is the file cmd_where's
        store-exists branch reads; seeding only --db would leave that branch
        untaken and the test would pass on the buggy pre-fix code (4.5
        round-2 tautology finding R2-1)."""
        from zaxbygraph.paths import store_db_path
        from zaxbygraph.repo import DEFAULT_HOST

        store = store_db_path(DEFAULT_HOST, REPO)
        store.parent.mkdir(parents=True, exist_ok=True)
        _seed(store)
        code, out, err = self._run(["where", "--format", "json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
        self.assertTrue(data["exists"], "store-exists branch must be taken")
        self.assertGreater(data["items"], 0, "store corpus must be nonzero")
        lines = [line for line in err.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, err)
        match = re.match(
            r"^# db=.* repo=(\S+) items=(\d+) synced=(\d+) complete=(yes|no)$",
            lines[0],
        )
        self.assertIsNotNone(match, lines[0])
        self.assertEqual(match.group(1), REPO)
        self.assertEqual(int(match.group(2)), data["items"])
        self.assertEqual(match.group(4), "yes" if data["complete"] else "no")

    def test_text_mode_failure_prints_nothing_to_stdout(self) -> None:
        """4.5 review finding 2 (MEDIUM): a text-mode failure answers only
        through the stderr echo — never a literal None on stdout (text is
        the TTY default, so this is the interactive path)."""
        code, out, err = self._run(["item", "99999", "--format", "text"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "", repr(out))
        self.assertTrue(err.lstrip().startswith("error:"), err)

    def test_pre_open_error_envelope_names_the_request(self) -> None:
        """4.5 review finding 4 (LOW): a failure before the DB opens still
        carries the requested repo/db in its envelope, not null-blind keys."""
        missing = str(Path(self._td.name) / "missing" / "history.db")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["status", "--repo", REPO, "--db", missing, "--format", "json"])
        self.assertEqual(code, 3, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["repo"], REPO)
        self.assertIsNotNone(payload["db"])

    def test_sql_duplicate_columns_survive_both_row_modes(self) -> None:
        """PRR-001 (HIGH): duplicate SELECT column names must not silently
        drop values. Objects mode suffixes repeats (n2, n2_2, ...); array
        mode keeps every positional value (the old sqlite3.Row first-match
        lookup substituted the first duplicate's value into later slots)."""
        sql = "SELECT number AS n, repo AS n, number AS n FROM items ORDER BY number LIMIT 1"
        code, out, err = self._run(["sql", sql, "--format", "json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
        self.assertEqual(data["columns"], ["n", "n", "n"])
        row = data["rows"][0]
        self.assertEqual(list(row.keys()), ["n", "n_2", "n_3"])
        # Positions: 0=number, 1=repo, 2=number. Every position keeps its OWN
        # value — the old code collapsed positions 1-2 into position 0's.
        self.assertEqual(row["n"], row["n_3"])
        self.assertEqual(row["n_2"], REPO)
        code2, out2, err2 = self._run(["sql", sql, "--format", "json", "--rows", "array"])
        self.assertEqual(code2, 0, err2)
        row2 = json.loads(out2)["data"]["rows"][0]
        self.assertEqual(len(row2), 3)
        self.assertEqual(row2[0], row2[2])
        self.assertEqual(row2[1], REPO)

    def test_sql_suffix_never_collides_with_real_columns(self) -> None:
        """4.5-review re-gate finding: a generated suffix must never claim a
        REAL column's name. Both input orders exercise the collision."""
        # Order 1: generated n_2 would collide with the real n_2 at pos 3.
        sql1 = "SELECT number AS n, repo AS n, number AS n_2 FROM items ORDER BY number LIMIT 1"
        code1, out1, err1 = self._run(["sql", sql1, "--format", "json"])
        self.assertEqual(code1, 0, err1)
        data1 = json.loads(out1)["data"]
        row1 = data1["rows"][0]
        self.assertEqual(len(row1), 3, (data1["columns"], row1))
        # Real n_2 keeps its own key and its own value (a number).
        self.assertIsInstance(row1["n_2"], int)
        self.assertEqual(row1["n"], 1)
        self.assertEqual(row1["n_3"], REPO)
        # Order 2: the real n_2 comes FIRST; the duplicate gets n_3.
        sql2 = "SELECT number AS n_2, number AS n, repo AS n FROM items ORDER BY number LIMIT 1"
        code2, out2, err2 = self._run(["sql", sql2, "--format", "json"])
        self.assertEqual(code2, 0, err2)
        data2 = json.loads(out2)["data"]
        row2 = data2["rows"][0]
        self.assertEqual(len(row2), 3, (data2["columns"], row2))
        self.assertIsInstance(row2["n_2"], int)
        self.assertIsInstance(row2["n"], int)
        # Positions: 0=number(n_2), 1=number(n), 2=repo(n-dup -> n_3).
        self.assertEqual(row2["n"], row2["n_2"])
        self.assertEqual(row2["n_3"], REPO)

    def test_sql_rows_array_fields_projection_aligns(self) -> None:
        """PRR-002 (MEDIUM): --fields with --rows array must project the
        rows by position so columns and rows stay aligned."""
        sql = "SELECT number, repo FROM items ORDER BY number LIMIT 1"
        code, out, err = self._run(
            ["sql", sql, "--format", "json", "--rows", "array", "--fields", "repo"]
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
        self.assertEqual(data["columns"], ["repo"])
        self.assertEqual(data["rows"], [[REPO]])

    def test_compact_format_single_line_envelope(self) -> None:
        """PRR-025c: --format compact emits the whole envelope on one line."""
        code, out, err = self._run(["status", "--format", "compact"])
        self.assertEqual(code, 0, err)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        for key in ("ok", "db", "repo", "freshness", "data", "truncated"):
            self.assertIn(key, payload)

    def test_schema_unknown_table_not_found(self) -> None:
        """PRR-025d: `schema <unknown>` exits 1 with a not_found envelope."""
        code, out, err = self._run(["schema", "no_such_table", "--format", "json"])
        self.assertEqual(code, 1, err)
        payload = json.loads(out)
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_truncate_bodies_marks_reviews(self) -> None:
        """PRR-025e: the reviews branch of _truncate_bodies cuts and marks
        exactly like comments do."""
        from zaxbygraph.cli import _truncate_bodies

        data = {
            "body": "b" * 600,
            "comments": [{"body": "c" * 600}],
            "reviews": [{"body": "r" * 600}, {"body": "short"}],
        }
        cut = _truncate_bodies(data, 500)
        self.assertTrue(cut)
        self.assertEqual(len(data["body"]), 500)
        self.assertIs(data["truncated"], True)
        self.assertIs(data["comments"][0]["truncated"], True)
        self.assertEqual(len(data["reviews"][0]["body"]), 500)
        self.assertIs(data["reviews"][0]["truncated"], True)
        self.assertNotIn("truncated", data["reviews"][1])

    def test_bad_sql_error_envelope_names_the_request(self) -> None:
        """PRR-004: a pre-open bad_sql failure still carries the requested
        repo/db in its envelope."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(
                ["sql", "DELETE FROM items", "--repo", REPO, "--db", self.db,
                 "--format", "json"]
            )
        self.assertEqual(code, 2, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["error"]["code"], "bad_sql")
        self.assertEqual(payload["repo"], REPO)
        self.assertIsNotNone(payload["db"])

    def test_hint_ignores_from_inside_string_literals(self) -> None:
        """PRR-006: the column hint scans code spans only — a FROM inside a
        string literal must not mis-target the probe."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(
                ["sql", "SELECT 'FROM items' AS x, no_such_col FROM pr_files",
                 "--repo", REPO, "--db", self.db, "--format", "json"]
            )
        self.assertEqual(code, 1, err.getvalue())
        error = json.loads(out.getvalue())["error"]
        self.assertEqual(error["code"], "no_such_column")
        self.assertIn("columns of pr_files:", error.get("hint", ""))

    def test_every_registered_subcommand_emits_envelope(self) -> None:
        """Guardrail: every subcommand build_parser() registers (minus the
        network sync; its envelope is pinned by test_sync.py) must answer
        with the self-describing envelope on success. Adding a new command
        without the envelope turns this red (issue #3 defect class)."""
        parser = build_parser()
        sub_action = next(
            a
            for a in parser._actions
            if getattr(a, "dest", None) == "cmd" and getattr(a, "choices", None)
        )
        registered = set(sub_action.choices)
        required = ("ok", "db", "repo", "freshness", "data", "truncated")
        problems: list[str] = []
        seen: set[str] = set()
        for argv in _guardrail_commands():
            name = argv[0]
            seen.add(name)
            self.assertIn(name, registered, f"guardrail command {name} not registered")
            code, out, err = self._run([*argv, "--format", "json"])
            if code != 0:
                problems.append(f"{name} exited {code}: {err.strip()[:80]}")
                continue
            try:
                payload = json.loads(out)
            except ValueError:
                problems.append(f"{name} stdout is not JSON")
                continue
            missing = [k for k in required if k not in payload]
            if missing:
                problems.append(f"{name} missing {','.join(missing)}")
        uncovered = registered - seen - {"sync"}
        self.assertFalse(
            uncovered, f"guardrail gap: subcommands with no envelope check: {sorted(uncovered)}"
        )
        self.assertFalse(
            problems,
            "guardrail: commands missing the JSON envelope: " + "; ".join(problems),
        )


if __name__ == "__main__":
    unittest.main()
