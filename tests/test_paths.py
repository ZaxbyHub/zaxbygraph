"""Issue #2 acceptance (AC1/AC2) plus round-2 critic strengthenings.

Materialize verbatim as tests/test_paths.py.

AC1  WorktreeResolutionTests.test_worktree_resolves_to_existing_graph: a read
     executed inside a `git worktree` of a checkout resolves its graph from
     the user-level store (<ZAXBYGRAPH_HOME>/github.com/<owner>/<repo>/
     history.db) and never creates a repo-local DB anywhere under the worktree
     tree.
AC2  GlobalStoreTests: two separate clones of the same origin resolve to the
     same store DB; neither clone directory gains a DB file.
R2   WorktreeResolutionTests.test_read_serves_main_worktree_legacy_db: with no
     store, reads serve the legacy DB under the MAIN worktree (git common
     dir) and still create nothing (plan-critic round 1 note 2).
R2   RemoteInfoTests: origin parsing keeps the host for non-github hosts
     (any-host https, scp-form, ssh) - the <host>/<owner>/<repo> key.

Hermetic: temp dirs only, env restored, cwd restored, no network (local git
repos with an https origin URL that is never contacted).
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from fixtures import FakeGitHubSource, issue, pr_file, pull
from test_migrations import V1_SCHEMA_SQL
from zaxbygraph.cli import main
from zaxbygraph.db import connect, init_schema
from zaxbygraph.repo import remote_info
from zaxbygraph.sync import sync_repo

SLUG = "acme/widget"
ORIGIN_URL = "https://github.com/acme/widget.git"
SEEDED_TITLE = "seeded widget one"


def run_cmd(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def git(*argv: str, cwd: Path) -> None:
    proc = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"git {argv} failed:\n{proc.stderr}"


def db_files_under(root: Path) -> set[Path]:
    if not root.exists():
        return set()
    return {p for p in root.rglob("*") if p.is_file() and p.suffix == ".db"}


class StoreHarness(unittest.TestCase):
    """Temp ZAXBYGRAPH_HOME + temp git repos; env and cwd restored."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.home = self.root / "zaxbygraph-home"
        self.home.mkdir()
        self.saved_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.saved_cwd)
        self.set_env("ZAXBYGRAPH_HOME", str(self.home))
        self.unset_env("ZAXBYGRAPH_DB")

    def set_env(self, key: str, value: str) -> None:
        old = os.environ.get(key)

        def restore() -> None:
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

        os.environ[key] = value
        self.addCleanup(restore)

    def unset_env(self, key: str) -> None:
        if key not in os.environ:
            return
        old = os.environ[key]
        del os.environ[key]
        self.addCleanup(lambda: os.environ.__setitem__(key, old))

    def make_repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir()
        git("init", "-b", "main", cwd=repo)
        git("config", "user.email", "t@example.com", cwd=repo)
        git("config", "user.name", "Tester", cwd=repo)
        (repo / "README.md").write_text("probe\n", encoding="utf-8")
        git("add", "-A", cwd=repo)
        git("commit", "-m", "init", cwd=repo)
        git("remote", "add", "origin", ORIGIN_URL, cwd=repo)
        return repo

    def store_db(self) -> Path:
        return self.home / "github.com" / "acme" / "widget" / "history.db"

    def seed_store(self) -> Path:
        """Build the user-level store corpus for acme/widget directly."""
        db = self.store_db()
        conn = connect(db)
        init_schema(conn)
        src = FakeGitHubSource()
        src.add_issue(issue(1, title=SEEDED_TITLE, body="store corpus needle"))
        src.add_pr(
            issue(3, title="widget pr", body="adds a file", kind="pr", state="closed"),
            pull(3, changed_files=1, merged=True),
            files=[pr_file("widget/x.py")],
        )
        sync_repo(conn, src, SLUG)
        conn.close()
        return db


class WorktreeResolutionTests(StoreHarness):
    def test_worktree_resolves_to_existing_graph(self) -> None:
        repo = self.make_repo("mainline")
        self.seed_store()
        worktree = self.root / "wt"
        git("worktree", "add", str(worktree), cwd=repo)

        def detach() -> None:
            # Chdir out first: Windows refuses to delete the process cwd.
            os.chdir(self.saved_cwd)
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=str(repo),
                capture_output=True,
            )

        self.addCleanup(detach)
        # A worktree shares the main repo's commits but has its own checkout
        # directory; before the read, it holds no DB at all.
        self.assertEqual(db_files_under(worktree), set())

        os.chdir(worktree)
        code, out, err = run_cmd(["item", "1", "--format", "json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
        self.assertEqual(data["repo"], SLUG)
        self.assertEqual(data["number"], 1)
        self.assertEqual(data["title"], SEEDED_TITLE)

        # The read must resolve via the user-level store, not by creating a
        # repo-local DB anywhere in the worktree tree.
        self.assertEqual(db_files_under(worktree), set())
        self.assertFalse((worktree / ".zaxbygraph").exists())
        self.assertFalse((worktree / ".swarm").exists())

    def test_read_serves_main_worktree_legacy_db(self) -> None:
        """Plan-critic round 1 note 2: the maintainer's real upgrade path.

        No store exists; the corpus lives in the MAIN checkout's legacy
        .zaxbygraph/history.db. A read from the linked worktree must serve
        that legacy DB (resolution order: store-if-exists, else legacy under
        the git common dir) and must create no store.
        """
        repo = self.make_repo("mainline")
        legacy = repo / ".zaxbygraph" / "history.db"
        legacy.parent.mkdir()
        conn = connect(legacy)
        init_schema(conn)
        src = FakeGitHubSource()
        src.add_issue(issue(7, title="legacy corpus row", body="legacy needle"))
        sync_repo(conn, src, SLUG)
        conn.close()

        worktree = self.root / "wt-legacy"
        git("worktree", "add", str(worktree), cwd=repo)

        def detach() -> None:
            os.chdir(self.saved_cwd)
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=str(repo),
                capture_output=True,
            )

        self.addCleanup(detach)
        self.assertFalse(self.store_db().exists(), "store must not pre-exist")

        os.chdir(worktree)
        code, out, err = run_cmd(["item", "7", "--format", "json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
        self.assertEqual(data["repo"], SLUG)
        self.assertEqual(data["title"], "legacy corpus row")

        # Serving the legacy DB must not fabricate a store or a worktree DB.
        self.assertFalse(self.store_db().exists(), "read must not create the store")
        self.assertEqual(db_files_under(worktree), set())
        self.assertFalse((worktree / ".zaxbygraph").exists())

    def test_mixed_case_legacy_corpus_is_never_called_another_repo(self) -> None:
        """Implementation review round 1: a pre-#9 legacy DB stores the slug
        with user-typed casing. The read guard must fold before claiming the
        DB holds "other repos" - the row IS the resolved repo. The honest
        answer is exit 2 naming the casing and the doctor adoption command.
        (At base this read mutated the legacy DB in place and served it;
        reads no longer mutate, so the actionable message replaces it.)"""
        repo = self.make_repo("mainline")
        legacy = repo / ".zaxbygraph" / "history.db"
        legacy.parent.mkdir()
        # Build the legacy DB at v1 with a mixed-case slug (the pre-#9
        # shape; seeding via connect+init_schema would fold it and defeat
        # the fix under test).
        conn = sqlite3.connect(str(legacy))
        try:
            conn.executescript(V1_SCHEMA_SQL)
            conn.execute(
                "INSERT INTO sync_state(repo, issues_since, last_full_sync_at, item_count)"
                " VALUES ('Acme/Widget', '2026-05-01T00:00:00Z',"
                " '2026-05-01T00:00:00Z', 1)"
            )
            conn.execute(
                "INSERT INTO items(id, repo, number, kind, title, body, labels_text,"
                " state, author, created_at, updated_at, raw_json)"
                " VALUES (400, 'Acme/Widget', 1, 'issue', 'mixed case legacy item',"
                " 'corpus', '', 'open', 'alice', '2026-05-01T00:00:00Z',"
                " '2026-05-01T00:00:00Z', '{}')"
            )
            conn.execute("PRAGMA user_version = 0")
            conn.commit()
        finally:
            conn.close()

        os.chdir(repo)
        code, out, err = run_cmd(["status", "--format", "json"])
        self.assertEqual(code, 2, err)
        self.assertTrue(err.lstrip().startswith("error:"), err)
        self.assertIn("Acme/Widget", err)
        self.assertIn("same repo", err)
        self.assertIn("zaxbygraph doctor --consolidate --repo acme/widget", err)
        self.assertNotIn("this database holds:", err)
        # The read never mutated the legacy file (v1 stays v1).
        probe = sqlite3.connect(str(legacy))
        try:
            version = int(probe.execute("PRAGMA user_version").fetchone()[0])
        finally:
            probe.close()
        self.assertEqual(version, 0)


class GlobalStoreTests(StoreHarness):
    def test_two_clones_share_one_db(self) -> None:
        origin = self.make_repo("origin")
        self.seed_store()
        clones = []
        for name in ("clone-a", "clone-b"):
            dst = self.root / name
            proc = subprocess.run(
                ["git", "clone", str(origin), str(dst)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            git("remote", "set-url", "origin", ORIGIN_URL, cwd=dst)
            clones.append(dst)

        titles = set()
        for clone in clones:
            os.chdir(clone)
            code, out, err = run_cmd(["status", "--format", "json"])
            self.assertEqual(code, 0, err)
            data = json.loads(out)["data"]
            self.assertEqual([r["repo"] for r in data["repos"]], [SLUG])
            self.assertEqual(int(data["repos"][0]["item_count"]), 2)
            code, out, err = run_cmd(["item", "1", "--format", "json"])
            self.assertEqual(code, 0, err)
            titles.add(json.loads(out)["data"]["title"])
            # Neither clone directory may grow a local DB.
            self.assertEqual(db_files_under(clone), set())
            self.assertFalse((clone / ".zaxbygraph").exists())
        self.assertEqual(titles, {SEEDED_TITLE})


class RemoteInfoTests(StoreHarness):
    """Plan-critic round 1 blocker 6: <host> comes from the origin URL.

    Any-host https, scp-form, and ssh URLs must keep their host; the store
    key for a non-github host is <home>/<host>/<owner>/<repo>/history.db.
    """

    def remote_host_and_slug(self, url: str) -> tuple[str, str]:
        repo = self.make_repo("probe")
        git("remote", "set-url", "origin", url, cwd=repo)
        host, slug = remote_info(repo)
        self.assertEqual(slug, SLUG)
        return host, slug

    def test_any_host_https_keeps_host(self) -> None:
        host, _ = self.remote_host_and_slug("https://ghe.example.com/acme/widget.git")
        self.assertEqual(host, "ghe.example.com")

    def test_scp_form_keeps_host(self) -> None:
        host, _ = self.remote_host_and_slug("git@ghe.example.com:acme/widget.git")
        self.assertEqual(host, "ghe.example.com")

    def test_ssh_form_keeps_host(self) -> None:
        host, _ = self.remote_host_and_slug("ssh://git@ghe.example.com/acme/widget.git")
        self.assertEqual(host, "ghe.example.com")

    def test_store_path_uses_origin_host(self) -> None:
        repo = self.make_repo("probe")
        git("remote", "set-url", "origin", "https://ghe.example.com/acme/widget.git", cwd=repo)
        os.chdir(repo)
        code, out, err = run_cmd(["where", "--format", "json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
        self.assertEqual(data["slug"], SLUG)
        expected = self.home / "ghe.example.com" / "acme" / "widget" / "history.db"
        self.assertEqual(Path(data["db"]), expected)


if __name__ == "__main__":
    unittest.main()
