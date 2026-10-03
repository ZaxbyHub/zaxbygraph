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
        does — not zeros from a closed connection."""
        code, out, err = self._run(["where", "--format", "json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
        lines = [line for line in err.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, err)
        match = re.match(
            r"^# db=.* repo=(\S+) items=(\d+) synced=(\d+) complete=(yes|no)$",
            lines[0],
        )
        self.assertIsNotNone(match, lines[0])
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
