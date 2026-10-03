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

from fixtures import REPO, FakeGitHubSource, issue, scrubbed_env, sync_repo
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
        # Issue #2: a fresh path is no corpus - exit 3, nothing created, and
        # the message names the path and the exact sync command. (Was: empty
        # JSON with exit 0; behavior intentionally changed, see PR notes.)
        # Issue #3: the failure additionally answers on stdout with the
        # structured error envelope (stderr keeps the one-line echo).
        fresh = Path(self._td.name) / "missing" / "history.db"
        code, out, err = self.run_cmd(["status", "--db", str(fresh), "--format", "json"])
        self.assertEqual(code, 3, err)
        payload = json.loads(out)
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"]["code"], "no_corpus")
        self.assertIn(str(fresh), payload["error"]["message"])
        self.assertIn("zaxbygraph sync --repo", payload["error"]["message"])
        self.assertTrue(err.lstrip().startswith("error:"), err)
        self.assertIn(str(fresh), err)
        self.assertIn("zaxbygraph sync --repo", err)
        self.assertFalse(fresh.exists(), "read created the database file")
        self.assertFalse(fresh.parent.exists(), "read created the database directory")

    def test_search_json_shape(self) -> None:
        # Issue #2: reads resolve the repo and require its corpus. --repo is
        # explicit here (origin resolution would key a different slug in
        # forks/CI), and the corpus row makes the no-corpus guard pass.
        conn = connect(Path(self.db))
        conn.execute("INSERT INTO sync_state(repo) VALUES (?)", (REPO,))
        conn.commit()
        conn.close()
        code, out, err = self.run_cmd(
            ["search", "nothing", "--repo", REPO, "--db", self.db, "--format", "json"]
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
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
        # Issue #2: scoped lookup needs the corpus row (same reason as
        # test_search_json_shape).
        conn = connect(Path(self.db))
        conn.execute("INSERT INTO sync_state(repo) VALUES (?)", (REPO,))
        conn.commit()
        conn.close()
        code, out, err = self.run_cmd(
            ["item", "99", "--repo", REPO, "--db", self.db, "--format", "json"]
        )
        self.assertEqual(code, 1)

    def test_sync_env_guard_exits_2(self) -> None:
        # An environment guard (database newer than this build) can never
        # succeed on retry: README's exit-2 contract, not exit 1.
        conn = connect(Path(self.db))
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(
                ["sync", "--repo", "acme/forgegate", "--db", self.db, "--format", "json"]
            )
        self.assertEqual(code, 2)
        payload = json.loads(out.getvalue())
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"]["code"], "bad_request")
        self.assertIn("newer than this build", err.getvalue())

    def test_argparse_error_output_is_utf8(self) -> None:
        # argparse errors are user-facing output: the UTF-8 pinning must run
        # before parse_args so a non-ASCII token survives a hostile locale.
        env = scrubbed_env()
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        proc = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8=0",
                "-m",
                "zaxbygraph",
                "item",
 "üser",
                "--db",
                self.db,
            ],
            capture_output=True,
            env=env,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 2)
        # The output must be valid UTF-8 with no crash: pre-reorder, a
        # cp1252-locale stderr wrote the token as byte 0xFC and this decode
        # raised (the defect this pins lives on Windows; POSIX argv arrives
        # surrogate-escaped under LC_ALL=C, so the token itself cannot
        # survive there regardless of stream encoding).
        stderr = proc.stderr.decode("utf-8")
        self.assertIn("invalid int value", stderr)
        self.assertNotIn("Traceback", stderr)
        if sys.platform == "win32":
            self.assertIn("üser", stderr)

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
        env = scrubbed_env()
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
        data = json.loads(proc.stdout.decode("utf-8"))["data"]
        self.assertEqual(data["title"], self.EMOJI_TITLE)

    def test_export_graph_emoji_under_cp1252_stdout(self) -> None:
        proc = self._run_cli(["export-graph", "--repo", REPO])
        self.assertEqual(
            proc.returncode, 0, "cli failed\nstdout=%r\nstderr=%r" % (proc.stdout, proc.stderr)
        )
        data = json.loads(proc.stdout.decode("utf-8"))["data"]
        self.assertTrue(data["nodes"])
        labels = " ".join(n.get("label", "") for n in data["nodes"])
        self.assertIn(self.EMOJI_TITLE, labels)


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
        # Content assertions on two representative commands: a fold regression
        # on the lookup path must not merely return valid-but-empty output.
        _, out = self._run_cmd(["search", "needle", "--repo", "ACME/ForgeGate"])
        self.assertIn("findable", json.loads(out)["data"]["items"][0]["title"])
        _, out = self._run_cmd(["status", "--repo", "ACME/ForgeGate"])
        self.assertEqual(json.loads(out)["data"]["repos"][0]["repo"], "acme/forgegate")

    def test_query_layer_folds_repo(self) -> None:
        conn = connect(Path(self.db))
        self.addCleanup(conn.close)
        from zaxbygraph.query import item as query_item
        from zaxbygraph.query import search as query_search
        from zaxbygraph.query import status as query_status

        self.assertIsNotNone(query_item(conn, 1, repo="ACME/ForgeGate"))
        self.assertEqual(query_status(conn, "ACME/ForgeGate")["repos"][0]["repo"], "acme/forgegate")
        self.assertTrue(query_search(conn, "needle", repo="ACME/ForgeGate")["items"])

    def test_empty_repo_flag_means_no_filter(self) -> None:
        # --repo '' keeps today's "no filter" meaning rather than raising
        code, out = self._run_cmd(["status", "--repo", ""])
        self.assertEqual(code, 0, out)
        code, out = self._run_cmd(["search", "needle", "--repo", ""])
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["data"]["items"])


# ==== issue-trace 2-worktree-global-store: acceptance append (AC3/AC6) ====
# Appended by .agents/issue-traces/2-worktree-global-store/repro/patches/
# patch_test_cli.py -- append-only; every class above is untouched and every
# import needed below is restated here (no header edits).
import io
import json
import os
import socket
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from fixtures import FakeGitHubSource, issue
from zaxbygraph.cli import main
from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo

WIDGET_SLUG = "acme/widget"
WIDGET_ORIGIN = "https://github.com/acme/widget.git"


def _issue2_git(*argv, cwd):
    proc = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"git {argv} failed:\n{proc.stderr}"


class _Issue2Harness(unittest.TestCase):
    """Temp ZAXBYGRAPH_HOME + git checkout of acme/widget; env and cwd restored."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.home = self.root / "zaxbygraph-home"
        self.home.mkdir()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        _issue2_git("init", "-b", "main", cwd=self.checkout)
        _issue2_git("config", "user.email", "t@example.com", cwd=self.checkout)
        _issue2_git("config", "user.name", "Tester", cwd=self.checkout)
        (self.checkout / "README.md").write_text("probe\n", encoding="utf-8")
        _issue2_git("add", "-A", cwd=self.checkout)
        _issue2_git("commit", "-m", "init", cwd=self.checkout)
        _issue2_git("remote", "add", "origin", WIDGET_ORIGIN, cwd=self.checkout)
        self._issue2_set_env("ZAXBYGRAPH_HOME", str(self.home))
        self._issue2_unset_env("ZAXBYGRAPH_DB")
        self._saved_cwd = os.getcwd()
        self.addCleanup(os.chdir, self._saved_cwd)
        os.chdir(self.checkout)

    def _issue2_set_env(self, key, value):
        old = os.environ.get(key)

        def restore():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

        os.environ[key] = value
        self.addCleanup(restore)

    def _issue2_unset_env(self, key):
        if key not in os.environ:
            return
        old = os.environ[key]
        del os.environ[key]
        self.addCleanup(lambda: os.environ.__setitem__(key, old))

    def _issue2_run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def _issue2_entries_under(self, root):
        if not root.exists():
            return []
        return sorted(str(p) for p in root.rglob("*"))


class NoCorpusTests(_Issue2Harness):
    """Issue #2 AC3: with no corpus for the resolved repo, every read command
    (a) creates no file or directory anywhere, (b) exits 3, and (c) prints an
    `error: ` message naming the resolved DB path and the exact sync command.
    A resolved DB that exists but holds only OTHER repos is exit 2 instead."""

    READS = [
        ["status"],
        ["search", "needle"],
        ["item", "1"],
        ["related", "1"],
        ["churn"],
        ["open"],
        ["path", "1", "2"],
        ["sql", "SELECT repo, number FROM items"],
        ["export-graph"],
    ]

    def test_reads_never_create_and_exit_3(self):
        store_db = self.home / "github.com" / "acme" / "widget" / "history.db"
        for argv in self.READS:
            with self.subTest(cmd=argv[0]):
                checkout_before = self._issue2_entries_under(self.checkout)
                home_before = self._issue2_entries_under(self.home)
                code, out, err = self._issue2_run([*argv, "--format", "json"])
                self.assertEqual(
                    code, 3, f"{argv}: exit {code}, stderr={err!r}"
                )
                self.assertTrue(err.lstrip().startswith("error:"), err)
                self.assertIn(str(store_db), err, err)
                self.assertIn("zaxbygraph sync --repo acme/widget", err)
                # Nothing is created: not under the checkout ...
                self.assertEqual(self._issue2_entries_under(self.checkout), checkout_before)
                self.assertFalse((self.checkout / ".zaxbygraph").exists())
                self.assertFalse((self.checkout / ".swarm").exists())
                # ... and not under the user-level store (no file, no dir).
                self.assertEqual(self._issue2_entries_under(self.home), home_before)

        # A resolved DB that exists but holds only OTHER repos exits 2 and
        # names the repos it does hold.
        other_db = self.root / "other.db"
        conn = connect(other_db)
        init_schema(conn)
        src = FakeGitHubSource()
        src.add_issue(issue(101, title="other repo item"))
        sync_repo(conn, src, "other/repo")
        conn.close()
        code, out, err = self._issue2_run(
            ["status", "--db", str(other_db), "--format", "json"]
        )
        self.assertEqual(code, 2, err)
        self.assertTrue(err.lstrip().startswith("error:"), err)
        self.assertIn("other/repo", err)


class WhereTests(_Issue2Harness):
    """Issue #2 AC6: `where` reports the full resolution chain and always
    exits 0.

    Pinned JSON keys: cwd, git_common_dir, slug, db, exists, items,
    watermark, complete, legacy (list of path strings), serving (the DB
    reads actually use). db/exists/items/watermark/complete describe the
    USER-LEVEL STORE for the slug (plan-critic round 1 blocker 3); with no
    store DB for the slug: exists=false, items=0, watermark=null,
    complete=false, and serving points at the legacy DB that reads fall
    back to.
    """

    def test_where_prints_resolution_chain(self):
        legacy = self.checkout / ".zaxbygraph" / "history.db"
        conn = connect(legacy)
        init_schema(conn)
        src = FakeGitHubSource()
        src.add_issue(issue(1, title="legacy widget item"))
        sync_repo(conn, src, WIDGET_SLUG)
        conn.close()

        code, out, err = self._issue2_run(["where", "--format", "json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
        for key in (
            "cwd",
            "git_common_dir",
            "slug",
            "db",
            "exists",
            "items",
            "watermark",
            "complete",
            "legacy",
            "serving",
        ):
            self.assertIn(key, data)
        self.assertEqual(Path(data["cwd"]).resolve(), self.checkout.resolve())
        # The resolution chain's git element: the MAIN worktree root (which
        # for this harness IS the checkout). Asserting the value (not mere
        # truthiness) keeps the chain step discriminating - implementation
        # review round 1: str(None) is truthy, so the old assertTrue passed
        # even with the chain broken.
        self.assertEqual(
            Path(data["git_common_dir"]).resolve(), self.checkout.resolve()
        )
        self.assertEqual(data["slug"], WIDGET_SLUG)
        self.assertEqual(
            Path(data["db"]),
            self.home / "github.com" / "acme" / "widget" / "history.db",
        )
        self.assertIs(data["exists"], False)
        self.assertEqual(data["items"], 0)
        self.assertIsNone(data["watermark"])
        self.assertIs(data["complete"], False)
        self.assertIsInstance(data["legacy"], list)
        # Compare RESOLVED forms: on Windows CI the tempfile root arrives as
        # an 8.3 short path (RUNNER~1) while the CLI's cwd-derived strings
        # carry the long form; str equality across that boundary is the
        # test's job to normalize, not the tool's.
        self.assertIn(
            str(legacy.resolve()),
            [str(Path(p).resolve()) for p in data["legacy"]],
        )
        # With the store absent and the legacy DB present, reads serve the
        # legacy DB while db/exists still describe the (absent) store.
        self.assertEqual(Path(data["serving"]), legacy.resolve())

    def test_where_no_repo_resolves_cleanly_or_errors(self):
        # Implementation review round 1: `where --repo ''` must never
        # traceback - without --db there is no slug to resolve a store from
        # (documented exit 2), and with --db it reports the named file.
        code, out, err = self._issue2_run(["where", "--repo", "", "--format", "json"])
        self.assertEqual(code, 2, err)
        self.assertTrue(err.lstrip().startswith("error:"), err)
        self.assertNotIn("Traceback", err)
        code, out, err = self._issue2_run(
            ["where", "--repo", "", "--db", str(self.root / "plain.db"), "--format", "json"]
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
        self.assertIsNone(data["slug"])
        self.assertEqual(Path(data["serving"]), Path(self.root / "plain.db"))

        # Review round 2: a POPULATED file must not read as empty. Slug-less
        # items is the unfiltered count; watermark/complete stay per-slug
        # null/false instead of claiming a row.
        populated = self.root / "populated.db"
        pconn = connect(populated)
        init_schema(pconn)
        psrc = FakeGitHubSource()
        psrc.add_issue(issue(501, title="one"))
        psrc.add_issue(issue(502, title="two"))
        sync_repo(pconn, psrc, "acme/widget")
        pconn.close()
        code, out, err = self._issue2_run(
            ["where", "--repo", "", "--db", str(populated), "--format", "json"]
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
        self.assertIs(data["exists"], True)
        self.assertEqual(data["items"], 2)
        self.assertIsNone(data["watermark"])
        self.assertIs(data["complete"], False)
