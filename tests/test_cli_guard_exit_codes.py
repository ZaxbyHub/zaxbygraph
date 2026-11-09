"""The CLI exit-code and envelope matrix for the two new fault-driven refusals.

`README.md`'s exit-code table draws a line the base code crossed: a `2` or `3`
"will never succeed on retry unchanged (fix the request, or sync first), while a
`1` sometimes will". A destination that cannot be stat-ed, or that is busy or
I/O-faulted, is the second kind -- the refusal's own hint says "close readers of
the destination and re-pull". So `dest_uninspectable` and the fault-driven
`out_is_database` answer 1, and every knowable guard rejection keeps 2.

An exit number alone is not discriminating, because 2 is shared with usage
errors, so every case here asserts the process exit AND `ok: false` AND the exact
`error.code` AND a stable message substring AND the hint where one is supplied.
The export rows are the point of the matrix: the SAME `out_is_database` code
answers 1 for a stat fault and 2 for an ordinary hard-link refusal.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fixtures import REPO, FakeGitHubSource, issue  # noqa: E402

from zaxbygraph.cli import main  # noqa: E402
from zaxbygraph.db import connect, init_schema  # noqa: E402
from zaxbygraph.snapshot import publish  # noqa: E402
from zaxbygraph.sync import sync_repo  # noqa: E402

FAULT = PermissionError(13, "Permission denied")


def _norm(p) -> str:
    return os.path.normcase(os.fspath(p))


_real_stat = os.stat


def _make_stat_fault(path):
    target = _norm(path)

    def _faulting_stat(self_path, *a, **kw):
        if _norm(self_path) == target:
            raise FAULT
        return _real_stat(self_path, *a, **kw)

    return _faulting_stat


def _is_file_swallow(self, *a, **kw):
    try:
        import stat as stat_mod
        return stat_mod.S_ISREG(self.stat().st_mode)
    except OSError:
        return False


def _exists_swallow(self, *a, **kw):
    try:
        self.stat()
    except OSError:
        return False
    return True


def _build_store(td: Path) -> Path:
    db = td / "history.db"
    conn = connect(db)
    init_schema(conn)
    conn.row_factory = sqlite3.Row
    src = FakeGitHubSource()
    for n in range(201, 205):
        src.add_issue(issue(n, title=f"Report {n}", body=f"body {n}",
                            created_at="2027-06-01T00:00:00Z"))
    sync_repo(conn, src, REPO)
    conn.close()
    return db


class _CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def run_cmd(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def assert_envelope(self, code, stdout, *, exit_code, error_code,
                        message_part, hint_part=None):
        self.assertEqual(code, exit_code, f"stdout={stdout[:400]}")
        env = json.loads(stdout)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], error_code)
        self.assertIn(message_part, env["error"]["message"])
        if hint_part is not None:
            self.assertIn(hint_part, env["error"].get("hint", ""))


class SnapshotPullExitTests(_CliCase):
    """`snapshot pull`: exit 1 for an inspection fault, 2 for a knowable one."""

    def _published(self):
        db = _build_store(self.td / "src")
        bare = self.td / "src" / "snapshot.git"
        bare.mkdir()
        import subprocess
        env = dict(os.environ)
        env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid",
                    "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
                    "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z"})
        subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                       check=True, capture_output=True, env=env, timeout=180)
        publish(db, str(bare), publisher="t")
        return str(bare)

    def test_stat_fault_answers_exit_one(self):
        bare = self._published()
        dest = _build_store(self.td)
        resolved = Path(dest).resolve()
        patches = [
            mock.patch.object(Path, "stat", _make_stat_fault(resolved)),
            mock.patch.object(Path, "is_file", _is_file_swallow),
            mock.patch.object(Path, "exists", _exists_swallow),
        ]
        for p in patches:
            p.start()
        try:
            code, out, _ = self.run_cmd(
                ["snapshot", "pull", bare, "--db", str(dest),
                 "--repo", REPO, "--format", "json"])
        finally:
            for p in reversed(patches):
                p.stop()
        self.assert_envelope(code, out, exit_code=1,
                             error_code="dest_uninspectable",
                             message_part="could not be inspected",
                             hint_part="re-pull")

    def test_multi_repo_answers_exit_two(self):
        bare = self._published()
        merged = self.td / "merged.db"
        conn = connect(merged)
        init_schema(conn)
        conn.row_factory = sqlite3.Row
        src = FakeGitHubSource()
        for n in range(601, 604):
            src.add_issue(issue(n, title=f"R{n}", body="b",
                                created_at="2027-06-01T00:00:00Z"))
        sync_repo(conn, src, REPO)
        src2 = FakeGitHubSource()
        src2.add_issue(issue(901, title="Other", body="x",
                             created_at="2027-06-01T00:00:00Z"))
        sync_repo(conn, src2, "acme/other")
        conn.close()
        code, out, _ = self.run_cmd(
            ["snapshot", "pull", bare, "--db", str(merged),
             "--repo", REPO, "--format", "json"])
        self.assert_envelope(code, out, exit_code=2,
                             error_code="multi_repo_destination",
                             message_part="also holds acme/other")

    def test_newer_local_answers_exit_two(self):
        bare = self._published()
        dest = _build_store(self.td)
        conn = sqlite3.connect(str(dest))
        conn.execute("UPDATE sync_state SET issues_since='2099-01-01T00:00:00Z'"
                     " WHERE repo=?", (REPO,))
        conn.commit()
        conn.close()
        code, out, _ = self.run_cmd(
            ["snapshot", "pull", bare, "--db", str(dest),
             "--repo", REPO, "--format", "json"])
        self.assert_envelope(code, out, exit_code=2, error_code="local_newer",
                             message_part="local store is newer")


class ExportCorpusExitTests(_CliCase):
    """`export-corpus`: the same `out_is_database` code, two exit classes."""

    def test_stat_fault_answers_exit_one(self):
        db = _build_store(self.td)
        out = self.td / "corpus.jsonl"
        patches = [
            mock.patch("os.stat", _make_stat_fault(out)),
            mock.patch("os.path.exists",
                       lambda p, *a, **k: _swallow(p)),
        ]
        for p in patches:
            p.start()
        try:
            code, stdout, _ = self.run_cmd(
                ["export-corpus", "--repo", REPO, "--db", str(db),
                 "--out", str(out), "--format", "json"])
        finally:
            for p in reversed(patches):
                p.stop()
        self.assert_envelope(code, stdout, exit_code=1,
                             error_code="out_is_database",
                             message_part="could not be inspected",
                             hint_part="re-run the export")

    def test_ordinary_hard_link_answers_exit_two(self):
        db = _build_store(self.td)
        out = self.td / "linked.jsonl"
        os.link(db, out)
        code, stdout, _ = self.run_cmd(
            ["export-corpus", "--repo", REPO, "--db", str(db),
             "--out", str(out), "--format", "json"])
        self.assert_envelope(code, stdout, exit_code=2,
                             error_code="out_is_database",
                             message_part="hard link")

    def test_sidecar_out_answers_exit_two(self):
        # The -journal is NOT pre-created: a stray one is a fake hot journal
        # that makes SQLite refuse the store's own read-only open, which would
        # answer store_unreadable before the guard is ever reached.
        db = _build_store(self.td)
        out = self.td / "history.db-journal"
        code, stdout, _ = self.run_cmd(
            ["export-corpus", "--repo", REPO, "--db", str(db),
             "--out", str(out), "--format", "json"])
        self.assert_envelope(code, stdout, exit_code=2,
                             error_code="out_is_database",
                             message_part="database (or a sidecar)")


def _swallow(path):
    try:
        os.stat(path)
    except (OSError, ValueError):
        return False
    return True


if __name__ == "__main__":
    unittest.main()
