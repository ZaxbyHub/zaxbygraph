from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile

import os
import subprocess
import sys

from fixtures import REPO, FakeGitHubSource, issue, sync_repo
from zaxbygraph.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]
from zaxbygraph.db import connect, init_schema


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.db = str(Path(self._td.name) / "history.db")
        conn = connect(Path(self.db))
        init_schema(conn)
        conn.close()

    def tearDown(self) -> None:
        self._td.cleanup()

    def run_cmd(self, argv: list[str]) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_help(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            main(["--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_status_empty_json(self) -> None:
        code, out, err = self.run_cmd(["status", "--db", self.db, "--format", "json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIn("repos", data)
        self.assertIn("counts", data)
        self.assertEqual(data["repos"], [])

    def test_search_json_shape(self) -> None:
        code, out, err = self.run_cmd(
            ["search", "nothing", "--db", self.db, "--format", "json"]
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIn("items", data)
        self.assertIn("comments", data)
        self.assertEqual(data["items"], [])

    def test_sql_write_rejected(self) -> None:
        code, out, err = self.run_cmd(
            ["sql", "INSERT INTO items VALUES (1)", "--db", self.db, "--format", "json"]
        )
        self.assertNotEqual(code, 0)
        self.assertTrue(err)

    def test_item_missing(self) -> None:
        code, out, err = self.run_cmd(["item", "99", "--db", self.db, "--format", "json"])
        self.assertEqual(code, 1)

    def test_sync_storage_fault_reported_not_traceback(self) -> None:
        # A storage fault before the sync starts (bad DB file) is a result
        # object on stdout with a non-zero exit, never a traceback.
        bad = Path(self._td.name) / "notadb"
        bad.write_text("garbage", encoding="utf-8")
        code, out, err = self.run_cmd(
            ["sync", "--repo", "acme/forgegate", "--db", str(bad), "--format", "json"]
        )
        self.assertEqual(code, 1)
        self.assertIn('"ok": false', out)
        self.assertNotIn("Traceback", out + err)


def _scrubbed_env() -> dict:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO")
    }
    if os.name == "posix":
        env.update(LC_ALL="C", LANG="C", PYTHONCOERCECLOCALE="0")
    return env


class CliEncodingTests(unittest.TestCase):
    """Issue #1 AC2: emoji/CJK output must survive a non-UTF-8 stdout."""

    EMOJI_TITLE = "Crash \U0001f680 \u8d77\u52d5"

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.db = str(Path(self._td.name) / "history.db")
        conn = connect(Path(self.db))
        init_schema(conn)
        src = FakeGitHubSource()
        src.add_issue(
            issue(1, title=self.EMOJI_TITLE, body="macron \u0101 dash \u2014 rocket \U0001f680")
        )
        sync_repo(conn, src, REPO)
        conn.close()

    def _run_cli(self, argv: list[str]) -> subprocess.CompletedProcess:
        env = _scrubbed_env()
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        return subprocess.run(
            [sys.executable, "-X", "utf8=0", "-m", "zaxbygraph", *argv, "--db", self.db],
            capture_output=True,
            env=env,
            cwd=str(REPO_ROOT),
        )

    def test_item_emoji_title_under_cp1252_stdout(self) -> None:
        proc = self._run_cli(
            ["item", "1", "--repo", REPO, "--format", "json"]
        )
        self.assertEqual(
            proc.returncode, 0, "cli failed\nstdout=%r\nstderr=%r" % (proc.stdout, proc.stderr)
        )
        data = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(data["title"], self.EMOJI_TITLE)

    def test_export_graph_emoji_under_cp1252_stdout(self) -> None:
        proc = self._run_cli(["export-graph", "--repo", REPO])
        self.assertEqual(
            proc.returncode, 0, "cli failed\nstdout=%r\nstderr=%r" % (proc.stdout, proc.stderr)
        )
        data = json.loads(proc.stdout.decode("utf-8"))
        self.assertTrue(data["nodes"])


class LookupCaseFoldTests(unittest.TestCase):
    """Issue #1 AC5: --repo lookups are case-insensitive on every command."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.db = str(Path(self._td.name) / "history.db")
        conn = connect(Path(self.db))
        init_schema(conn)
        src = FakeGitHubSource()
        src.add_issue(issue(1, title="findable", body="needle"))
        sync_repo(conn, src, "ACME/ForgeGate")  # canonical storage is lowercase
        conn.close()

    def _run_cmd(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main([*argv, "--db", self.db, "--format", "json"])
        return code, out.getvalue()

    def test_every_read_command_matches_mixed_case_repo(self) -> None:
        for argv in (
            ["item", "1"],
            ["search", "needle"],
            ["related", "1"],
            ["churn"],
            ["open"],
            ["path", "1", "1"],
            ["export-graph"],
            ["status"],
        ):
            with self.subTest(cmd=argv[0]):
                code, out = self._run_cmd([*argv, "--repo", "ACME/ForgeGate"])
                self.assertEqual(code, 0, out)
                self.assertTrue(out.strip(), "command produced no output")

    def test_query_layer_folds_repo(self) -> None:
        conn = connect(Path(self.db))
        self.addCleanup(conn.close)
        from zaxbygraph.query import item as query_item

        self.assertIsNotNone(query_item(conn, 1, repo="ACME/ForgeGate"))

    def test_empty_repo_flag_means_no_filter(self) -> None:
        # --repo '' keeps today's "no filter" meaning rather than raising
        code, out = self._run_cmd(["status", "--repo", ""])
        self.assertEqual(code, 0, out)
