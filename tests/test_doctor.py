"""Issue #2 acceptance (AC7): `doctor --consolidate` adopts the freshest
COMPLETE legacy corpus into the user-level store, leaving every original
untouched, and reports per-DB item and garbled-row counts.

Materialize verbatim as tests/test_doctor.py.

Pinned report shape (--format json):
    {"ok": true,
     "adopted": "<path of adopted DB or null>",
     "scanned": [{"path": str, "items": int, "garbled": int,
                  "complete": bool, "adopted": bool}, ...]}
Pinned semantics:
  * complete  = sync_state shows a finished full sync
                (full_sync_pending = 0 AND last_full_sync_at IS NOT NULL).
  * freshest  = greatest sync_state.issues_since among complete candidates.
  * --scan DIR is repeatable and contributes every file named history.db
    found under DIR (recursively), in addition to the repo-local candidates
    <repo-root>/.zaxbygraph/history.db and <repo-root>/.swarm/zaxbygraph/history.db.
  * garbled rows = rows whose text columns contain mojibake signatures
    (e.g. 'Ã' or 'â€').
  * v1 candidates (no full_sync_pending column, user_version 0, pre-PR-1
    mixed-case repos) are adopted under the case-folded slug: the candidate
    file is copied and migrated on the copy; the original is never modified
    (plan-critic round 1 blocker 4).
  * re-consolidation replaces the slug's rows in the store (round 2
    blocker 2): no stale rows or watermark survive from a prior adoption.

Hermetic: temp dirs only, env restored, cwd restored, no network.
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

from fixtures import FakeGitHubSource, issue
from test_migrations import V1_SCHEMA_SQL
from zaxbygraph.cli import main
from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo

SLUG = "acme/widget"
ORIGIN_URL = "https://github.com/acme/widget.git"
MOJIBAKE_TITLE = "Broken title: rÃ©sumÃ© garbled"


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


def read_only_rows(path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [tuple(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.home = self.root / "zaxbygraph-home"
        self.home.mkdir()
        self.scan_a = self.root / "scan-a"
        self.scan_b = self.root / "scan-b"
        (self.scan_a / "fresh").mkdir(parents=True)
        (self.scan_b / "stale").mkdir(parents=True)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        git("init", "-b", "main", cwd=self.checkout)
        git("config", "user.email", "t@example.com", cwd=self.checkout)
        git("config", "user.name", "Tester", cwd=self.checkout)
        (self.checkout / "README.md").write_text("probe\n", encoding="utf-8")
        git("add", "-A", cwd=self.checkout)
        git("commit", "-m", "init", cwd=self.checkout)
        git("remote", "add", "origin", ORIGIN_URL, cwd=self.checkout)
        self._saved_cwd = os.getcwd()
        self.addCleanup(os.chdir, self._saved_cwd)
        self._set_env("ZAXBYGRAPH_HOME", str(self.home))
        self._unset_env("ZAXBYGRAPH_DB")

    def _set_env(self, key: str, value: str) -> None:
        old = os.environ.get(key)

        def restore() -> None:
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

        os.environ[key] = value
        self.addCleanup(restore)

    def _unset_env(self, key: str) -> None:
        if key not in os.environ:
            return
        old = os.environ[key]
        del os.environ[key]
        self.addCleanup(lambda: os.environ.__setitem__(key, old))

    def _seed_complete(self, path: Path, items: list[tuple[int, str, str]]) -> None:
        """A legacy DB whose sync_state shows a finished full sync."""
        conn = connect(path)
        init_schema(conn)
        src = FakeGitHubSource()
        for number, title, updated in items:
            src.add_issue(issue(number, title=title, body="corpus", updated_at=updated))
        sync_repo(conn, src, SLUG)
        conn.close()

    def _seed_incomplete(self, path: Path) -> None:
        """A legacy DB that never finished a full sync (must never be adopted),
        carrying the newest watermark of all candidates on purpose."""
        conn = connect(path)
        init_schema(conn)
        conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, state, raw_json) "
            "VALUES (9105, ?, 5, 'issue', ?, 'open', '{}')",
            (SLUG, "incomplete corpus item"),
        )
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, item_count, full_sync_pending) "
            "VALUES (?, '2026-08-01T00:00:00Z', 1, 1)",
            (SLUG,),
        )
        conn.commit()
        conn.close()

    def test_consolidate_adopts_freshest_complete_and_counts_garbled_rows(self) -> None:
        older = self.checkout / ".zaxbygraph" / "history.db"  # repo-local candidate
        fresh = self.scan_a / "fresh" / "history.db"  # found via --scan
        incomplete = self.scan_b / "stale" / "history.db"  # found via --scan
        self._seed_complete(older, [(1, "older widget item", "2026-02-01T00:00:00Z")])
        self._seed_complete(
            fresh,
            [
                (1, "first widget item", "2026-06-02T00:00:00Z"),
                (2, MOJIBAKE_TITLE, "2026-06-01T00:00:00Z"),
            ],
        )
        self._seed_incomplete(incomplete)

        os.chdir(self.checkout)
        code, out, err = run_cmd(
            [
                "doctor",
                "--consolidate",
                "--repo",
                SLUG,
                "--scan",
                str(self.scan_a),
                "--scan",
                str(self.scan_b),
                "--format",
                "json",
            ]
        )
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertIs(report["ok"], True)
        # Resolved-form keys: see the WhereTests note on Windows 8.3 short
        # temp paths vs cwd-derived long paths.
        scanned = {str(Path(e["path"]).resolve()): e for e in report["scanned"]}
        self.assertEqual(len(scanned), 3, scanned)
        for path in (older, fresh, incomplete):
            self.assertIn(str(path.resolve()), scanned)
        # Older complete DB: reported, not adopted, no garbled rows.
        self.assertEqual(scanned[str(older)]["items"], 1)
        self.assertIs(scanned[str(older)]["complete"], True)
        self.assertIs(scanned[str(older)]["adopted"], False)
        self.assertEqual(scanned[str(older)]["garbled"], 0)
        # Fresher complete DB: adopted, and its mojibake row is counted.
        self.assertEqual(scanned[str(fresh)]["items"], 2)
        self.assertIs(scanned[str(fresh)]["complete"], True)
        self.assertIs(scanned[str(fresh)]["adopted"], True)
        self.assertGreaterEqual(scanned[str(fresh)]["garbled"], 1)
        # Incomplete DB: reported but never adopted, newest watermark or not.
        self.assertIs(scanned[str(incomplete)]["complete"], False)
        self.assertIs(scanned[str(incomplete)]["adopted"], False)
        self.assertEqual(str(Path(report["adopted"]).resolve()), str(fresh.resolve()))

        # The user-level store now holds the freshest corpus, mojibake intact.
        store = self.home / "github.com" / "acme" / "widget" / "history.db"
        self.assertTrue(store.exists(), f"store DB not created at {store}")
        titles = [r[0] for r in read_only_rows(store, "SELECT title FROM items ORDER BY number")]
        self.assertEqual(titles, ["first widget item", MOJIBAKE_TITLE])

        # Every original is untouched: copy, never move or delete.
        self.assertTrue(older.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(incomplete.exists())
        self.assertEqual(len(read_only_rows(older, "SELECT id FROM items")), 1)
        self.assertEqual(len(read_only_rows(fresh, "SELECT id FROM items")), 2)
        self.assertEqual(len(read_only_rows(incomplete, "SELECT id FROM items")), 1)

    def _seed_v1_mixed_case(self, path: Path) -> None:
        """A pre-PR-1 legacy DB built from the GENUINE v1 DDL (the same
        V1_SCHEMA_SQL tests/test_migrations.py froze from base e2ef892 -
        plan-critic round-3 blocker 2: a phantom reduced schema would break
        connect+init_schema's own index/fold SQL). user_version 0, no
        full_sync_pending column, mixed-case repo value: exactly what
        adoption must canonicalize (plan-critic round 1 blocker 4)."""
        conn = sqlite3.connect(str(path))
        try:
            conn.executescript(V1_SCHEMA_SQL)
            conn.execute(
                "INSERT INTO sync_state(repo, issues_since, last_full_sync_at, item_count) "
                "VALUES ('Acme/Widget', '2026-07-05T00:00:00Z', '2026-07-05T00:00:00Z', 1)"
            )
            conn.execute(
                "INSERT INTO items(id, repo, number, kind, title, body, labels_text, "
                "state, author, created_at, updated_at, raw_json) "
                "VALUES (300, 'Acme/Widget', 1, 'issue', 'mixed case v1 item', "
                "'corpus', '', 'open', 'alice', '2026-07-01T00:00:00Z', "
                "'2026-07-05T00:00:00Z', '{}')"
            )
            conn.execute("PRAGMA user_version = 0")
            conn.commit()
        finally:
            conn.close()

    def test_consolidate_adopts_v1_mixed_case_corpus(self) -> None:
        """A v1, mixed-case candidate adopts under the folded slug and the
        folded rows serve reads; the original file is untouched."""
        legacy = self.scan_a / "v1" / "history.db"
        legacy.parent.mkdir(parents=True)
        self._seed_v1_mixed_case(legacy)

        os.chdir(self.checkout)
        code, out, err = run_cmd(
            ["doctor", "--consolidate", "--repo", SLUG, "--scan", str(self.scan_a),
             "--format", "json"]
        )
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertIs(report["ok"], True)
        self.assertEqual(str(Path(report["adopted"])), str(legacy))

        store = self.home / "github.com" / "acme" / "widget" / "history.db"
        self.assertTrue(store.exists())
        rows = read_only_rows(store, "SELECT repo, title FROM items")
        self.assertEqual(rows, [("acme/widget", "mixed case v1 item")])

        # The folded corpus serves a default read from the checkout.
        code, out, err = run_cmd(["item", "1", "--format", "json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["title"], "mixed case v1 item")

        # Original untouched: still v1, still mixed case, still one item.
        self.assertEqual(int(read_only_rows(legacy, "PRAGMA user_version")[0][0]), 0)
        self.assertEqual(
            read_only_rows(legacy, "SELECT repo, number FROM items"),
            [("Acme/Widget", 1)],
        )

    def test_reconsolidation_replaces_slug_rows(self) -> None:
        """Round-2 critic blocker 2: re-consolidation must replace the
        slug's rows - no stale items or watermark survive from the first
        adoption."""
        first = self.scan_a / "first" / "history.db"
        second = self.scan_a / "second" / "history.db"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        self._seed_complete(
            first,
            [
                (1, "first widget item", "2026-06-02T00:00:00Z"),
                (2, "second widget item", "2026-06-01T00:00:00Z"),
            ],
        )

        os.chdir(self.checkout)
        argv = ["doctor", "--consolidate", "--repo", SLUG, "--scan", str(self.scan_a),
                "--format", "json"]
        code, out, err = run_cmd(argv)
        self.assertEqual(code, 0, err)
        store = self.home / "github.com" / "acme" / "widget" / "history.db"
        self.assertEqual(
            len(read_only_rows(store, "SELECT id FROM items")), 2, "first adoption"
        )

        # A NEWER corpus arrives afterwards: re-consolidation adopts it and
        # the store must hold exactly its corpus (stale rows gone, watermark
        # new). Round-3 blocker 1: the second candidate must be seeded
        # BETWEEN the two identical invocations, or the test would demand
        # different results from identical inputs.
        self._seed_complete(second, [(1, "newer widget item", "2026-08-09T00:00:00Z")])
        code, out, err = run_cmd(argv)
        self.assertEqual(code, 0, err)
        # Select on (number, title), not items.id: the fixtures assign
        # GitHub id 10_000 + number and adoption keeps GitHub ids
        # (plan-critic round-4 blocker B1').
        rows = read_only_rows(store, "SELECT number, title FROM items ORDER BY number")
        self.assertEqual(rows, [(1, "newer widget item")])
        wm = read_only_rows(store, "SELECT issues_since FROM sync_state WHERE repo = 'acme/widget'")
        self.assertEqual(wm, [("2026-08-09T00:00:00Z",)])


if __name__ == "__main__":
    unittest.main()
