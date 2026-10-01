from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from unittest.mock import patch

from fixtures import REPO, FakeGitHubSource, TempDBTest, issue, pr_file, pull
from zaxbygraph.cli import cmd_sync
from zaxbygraph.store import ingest_item as real_ingest_item
from zaxbygraph.query import item, status
from zaxbygraph.sync import SyncError, sync_repo


class SyncTests(TempDBTest):
    def test_full_then_incremental_since(self) -> None:
        self.src.add_issue(issue(1, title="one", updated_at="2026-01-01T00:00:00Z"))
        self.src.add_issue(issue(2, title="two", updated_at="2026-01-02T00:00:00Z"))
        first = self.sync()
        self.assertEqual(first["ingested"], 2)
        self.assertEqual(first["issues_since"], "2026-01-02T00:00:00Z")
        second = self.sync()
        # inclusive since refetches #2
        self.assertEqual(second["ingested"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 2)

    def test_comment_only_update_advances_watermark(self) -> None:
        self.src.add_issue(issue(1, title="one", updated_at="2026-01-01T00:00:00Z", comments=0))
        self.src.add_issue(issue(2, title="two", updated_at="2026-01-02T00:00:00Z", comments=0))
        self.sync()
        before = self.conn.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.src.comment_on(1, "ping")
        result = self.sync()
        after = self.conn.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertGreater(after, before)
        self.assertGreaterEqual(result["ingested"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM comments"), 1)

    def test_429_on_extra_get_leaves_earlier_items(self) -> None:
        self.src.add_issue(issue(1, title="one", updated_at="2026-01-01T00:00:00Z", comments=0))
        self.src.add_issue(issue(2, title="two", updated_at="2026-01-02T00:00:00Z", comments=0))
        self.src.add_pr(
            issue(3, title="three", updated_at="2026-01-03T00:00:00Z", comments=0, kind="pr"),
            pull(3, changed_files=1),
            files=[pr_file("src/a.py")],
        )
        self.src.fail_after(1)  # first extra fetch is get_pull(#3)
        with self.assertRaises(SyncError):
            self.sync()
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 2)
        since = self.conn.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertEqual(since, "2026-01-02T00:00:00Z")
        err = self.conn.execute(
            "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertIn("429", err)

    def test_force_clears_watermark(self) -> None:
        self.src.add_issue(issue(1, title="one", updated_at="2026-01-01T00:00:00Z"))
        self.sync()
        result = self.sync(force=True)
        self.assertTrue(result["full"])
        self.assertEqual(result["ingested"], 1)

    def test_skip_comments_get_when_zero(self) -> None:
        self.src.add_issue(issue(1, title="one", comments=0))
        self.sync()
        self.assertEqual(self.src.extra_fetches, 0)

    def test_skip_files_get_when_changed_zero(self) -> None:
        self.src.add_pr(
            issue(3, title="pr", comments=0, kind="pr"),
            pull(3, changed_files=0),
            files=[pr_file("src/ghost.py")],
        )
        before = self.src.extra_fetches
        self.sync()
        # pull + reviews + review comments; not files
        self.assertEqual(self.src.extra_fetches - before, 3)
        self.assertEqual(self.count("SELECT COUNT(*) FROM pr_files"), 0)

    def test_releases_replace_transactional(self) -> None:
        self.src.add_issue(issue(1, title="one"))
        self.src.releases = [
            {
                "id": 1,
                "tag_name": "v0.1.0",
                "name": "v0.1.0",
                "body": "first",
                "draft": False,
                "prerelease": False,
                "author": {"login": "alice"},
                "created_at": "2026-01-01T00:00:00Z",
                "published_at": "2026-01-01T00:00:00Z",
                "html_url": "https://github.com/acme/forgegate/releases/tag/v0.1.0",
            }
        ]
        self.sync()
        self.src.releases = [
            {
                "id": 2,
                "tag_name": "v0.2.0",
                "name": "v0.2.0",
                "body": "second",
                "draft": False,
                "prerelease": False,
                "author": {"login": "alice"},
                "created_at": "2026-02-01T00:00:00Z",
                "published_at": "2026-02-01T00:00:00Z",
                "html_url": "https://github.com/acme/forgegate/releases/tag/v0.2.0",
            }
        ]
        self.sync(force=True)
        tags = [r[0] for r in self.conn.execute("SELECT tag_name FROM releases").fetchall()]
        self.assertEqual(tags, ["v0.2.0"])

    def test_jsonl_writes_events_in_dir(self) -> None:
        self.src.add_issue(issue(1, title="one", comments=0))
        dest = Path(self._td.name) / "jsonl"
        self.sync(jsonl_path=dest)
        text = (dest / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"resource": "item"', text)
        self.assertTrue(dest.is_dir())

    def test_duplicate_comment_and_pr_file_from_pagination_do_not_raise(self) -> None:
        self.src.add_pr(
            issue(3, title="three", comments=1, kind="pr"),
            pull(3, changed_files=1),
            files=[pr_file("src/a.py")],
        )
        self.src.comment_on(3, "hello")
        self.src.duplicate_comments = True
        self.src.duplicate_pr_files = True
        result = self.sync()
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM comments"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM pr_files"), 1)

    def test_sqlite_error_mid_sync_records_last_error_and_is_resumable(self) -> None:
        self.src.add_issue(issue(1, title="one", updated_at="2026-01-01T00:00:00Z"))
        self.src.add_issue(issue(2, title="two", updated_at="2026-01-02T00:00:00Z"))
        calls = {"n": 0}

        def fake_ingest(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("disk I/O error")
            return real_ingest_item(*a, **kw)

        with patch("zaxbygraph.sync.ingest_item", side_effect=fake_ingest):
            with self.assertRaises(SyncError):
                self.sync()
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 1)
        since = self.conn.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertEqual(since, "2026-01-01T00:00:00Z")
        err = self.conn.execute(
            "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertIn("disk I/O error", err)

    def test_cli_reports_mid_sync_sqlite_error_as_clean_exit(self) -> None:
        self.src.add_issue(issue(1, title="one", updated_at="2026-01-01T00:00:00Z"))
        self.src.add_issue(issue(2, title="two", updated_at="2026-01-02T00:00:00Z"))
        calls = {"n": 0}

        def fake_ingest(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("disk I/O error")
            return real_ingest_item(*a, **kw)

        args = argparse.Namespace(
            repo=REPO,
            db=str(self.db_path),
            force=False,
            include_patches=False,
            jsonl=None,
            jsonl_flag=False,
            format="json",
        )
        with patch("zaxbygraph.sync.ingest_item", side_effect=fake_ingest), patch(
            "zaxbygraph.cli.GhApiSource", return_value=self.src
        ):
            code = cmd_sync(args)
        self.assertEqual(code, 1)

    def test_successful_sync_clears_last_error(self) -> None:
        self.conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_error) VALUES (?, ?, ?)",
            (REPO, "2099-01-01T00:00:00Z", "old 429"),
        )
        self.conn.commit()
        self.sync()
        err = self.conn.execute(
            "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertIsNone(err)


class SyncStateTests(TempDBTest):
    """State-truthfulness tests from issue #1 (last_error, complete, slugs)."""

    def test_unexpected_exception_records_last_error(self) -> None:
        self.src.add_issue(issue(1))

        class BoomSource(FakeGitHubSource):
            def list_issues(self, since):
                raise RuntimeError("reader thread died")

        with self.assertRaises(RuntimeError):
            sync_repo(self.conn, BoomSource(), REPO)
        err = self.conn.execute(
            "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertIsNotNone(err)
        self.assertIn("reader thread died", err)

    def test_keyboard_interrupt_mid_item_records_last_error(self) -> None:
        self.src.add_issue(issue(1, updated_at="2026-01-01T00:00:10Z"))

        # Interrupt INSIDE the per-item transaction (after BEGIN IMMEDIATE,
        # before commit): KeyboardInterrupt bypasses the `except Exception`
        # rollback guard, so the outer handler must roll the open transaction
        # back before recording, or the recorder itself would fail.
        with patch("zaxbygraph.sync.ingest_item", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.sync()
        err = self.conn.execute(
            "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]
        self.assertIsNotNone(err)
        self.assertIn("KeyboardInterrupt", err)
        # the connection is usable after the open transaction was rolled back
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM sync_state WHERE repo = ?", (REPO,)), 1
        )
        # and a later clean run completes
        result = self.sync()
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 1)

    def test_resumed_full_sync_reports_complete(self) -> None:
        self.src.add_issue(issue(1, updated_at="2026-01-01T00:00:10Z"))  # no extra GETs
        self.src.add_pr(
            issue(2, updated_at="2026-01-02T00:00:00Z", kind="pr"), pull(2)
        )
        self.src.fail_after(1)  # first extra GET is item 2's get_pull
        with self.assertRaises(SyncError):
            self.sync()
        state = status(self.conn, REPO)["repos"][0]
        self.assertFalse(state["complete"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 1)

        self.src.fail_after(None)  # clean resume
        self.sync()
        state = status(self.conn, REPO)["repos"][0]
        self.assertTrue(state["complete"])
        row = self.conn.execute(
            "SELECT last_full_sync_at, full_sync_pending FROM sync_state WHERE repo = ?",
            (REPO,),
        ).fetchone()
        self.assertIsNotNone(row["last_full_sync_at"])
        self.assertEqual(row["full_sync_pending"], 0)

    def test_force_interruption_marks_pending(self) -> None:
        self.src.add_issue(issue(1, updated_at="2026-01-01T00:00:10Z"))
        self.sync()  # complete corpus
        self.assertTrue(status(self.conn, REPO)["repos"][0]["complete"])

        self.src.add_pr(
            issue(2, updated_at="2026-01-02T00:00:00Z", kind="pr"), pull(2)
        )
        self.src.fail_after(1)
        with self.assertRaises(SyncError):
            self.sync(force=True)  # force nulls the watermark, then interrupts
        state = status(self.conn, REPO)["repos"][0]
        self.assertFalse(state["complete"])

        self.src.fail_after(None)
        self.sync()  # resume without --force completes the forced full sync
        state = status(self.conn, REPO)["repos"][0]
        self.assertTrue(state["complete"])

    def test_slug_case_folds(self) -> None:
        self.src.add_issue(issue(1))
        sync_repo(self.conn, self.src, "ZaxbyHub/ForgeGate")
        sync_repo(self.conn, self.src, "zaxbyhub/forgegate")
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM sync_state WHERE repo LIKE 'zaxbyhub/%'"),
            1,
        )
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM sync_state WHERE repo != 'zaxbyhub/forgegate'"),
            0,
        )
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM items WHERE repo != 'zaxbyhub/forgegate'"),
            0,
        )
        # lookups fold too
        self.assertIsNotNone(item(self.conn, 1, repo="ZaxbyHub/ForgeGate"))
