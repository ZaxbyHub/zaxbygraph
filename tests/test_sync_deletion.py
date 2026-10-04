"""Sync-level tests for the issue #5 deletion semantics and rate-limit
restart behavior that the frozen acceptance checks do not cover: marking on
404/410 child fetches, deletion-pass survival across a rate-limit listing
restart, and the no-oracle / incremental no-mark paths."""
from __future__ import annotations

import unittest

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fixtures import (
    REPO,
    GitHubError,
    FakeGitHubSource,
    TempDBTest,
    issue,
    pull,
    pr_file,
    ts,
)
from zaxbygraph.sync import SyncError, sync_repo


class _RateLimitedError(GitHubError):
    """A 429 with machine-readable budget attrs (mirrors fixtures.py's
    RateLimitedError; defined here to keep this file independent)."""

    def __init__(self, message: str, *, remaining: int, reset_at: str) -> None:
        super().__init__(message, status=429)
        self.rate_limit_remaining = remaining
        self.rate_limit_reset = reset_at


class _RestartingDeletionSource(FakeGitHubSource):
    """First listing attempt dies with a rate-limit window AFTER delivering
    page 1; the restarted listing drains and omits gone_number. check_deleted
    is a deletion oracle, so a correct implementation marks the missing item
    even though the run restarted."""

    def __init__(self, gone_number: int, reset_at: str) -> None:
        super().__init__()
        self.gone_number = gone_number
        self.reset_at = reset_at
        self.attempts = 0
        self.check_deleted_calls = 0
        self.fail_first_listing = True
        self.item_gone = False

    def list_issues(self, since: str | None):
        self.attempts += 1
        if self.fail_first_listing and self.attempts == 1:
            items = sorted(self.issues.values(), key=lambda r: (r["updated_at"], r["number"]))
            first = next(r for r in items if int(r["number"]) != self.gone_number)
            yield dict(first)
            raise _RateLimitedError(
                "API rate limit exceeded HTTP 429", remaining=0, reset_at=self.reset_at
            )
        items = sorted(self.issues.values(), key=lambda r: (r["updated_at"], r["number"]))
        for rec in items:
            if self.item_gone and int(rec["number"]) == self.gone_number:
                continue
            if since is not None and rec["updated_at"] < since:
                continue
            yield dict(rec)

    def check_deleted(self, numbers) -> dict[int, str]:
        self.check_deleted_calls += 1
        return {int(n): "deleted" for n in numbers}


class MarkOnFetchFailureTests(TempDBTest):
    def test_404_on_child_fetch_marks_item_deleted(self) -> None:
        self.src.add_pr(
            issue(1, title="one", kind="pr"),
            pull(1, changed_files=1),
            files=[pr_file("src/a.py")],
        )
        self.sync()
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 1)

        # The item comes back in the listing but GitHub now 404s its data.
        self.src.fail_after(None)
        original = self.src.list_pr_files

        def gone(number: int):
            raise GitHubError("HTTP 404 Not Found", status=404)

        self.src.list_pr_files = gone  # type: ignore[method-assign]
        self.sync(force=True)
        state = self.conn.execute(
            "SELECT state FROM items WHERE repo = ? AND number = 1", (REPO,)
        ).fetchone()[0]
        self.assertEqual(state, "deleted")
        # Already-stored children and their edges MUST survive the marking
        # (ingest_item with empty children would erase the last local copy
        # of a deleted item's discussion - PR-13 review Critical).
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM pr_files WHERE number = 1"), 1
        )
        self.assertGreaterEqual(
            self.count(
                "SELECT COUNT(*) FROM edges WHERE repo = ? AND dst_type = 'item'"
                " AND dst_id = '1'",
                (REPO,),
            ),
            1,
        )
        trail = self.count(
            "SELECT COUNT(*) FROM fetch_log WHERE repo = ? AND resource = 'item' "
            "AND resource_id = '1' AND lower(note) LIKE '%deleted%'",
            (REPO,),
        )
        self.assertGreaterEqual(trail, 1)

    def test_410_marks_deleted_too(self) -> None:
        self.src.add_pr(
            issue(2, title="two", kind="pr"),
            pull(2, changed_files=1),
            files=[pr_file("src/b.py")],
        )
        self.sync()

        def gone(number: int):
            raise GitHubError("HTTP 410 Gone", status=410)

        self.src.list_pr_files = gone  # type: ignore[method-assign]
        self.sync(force=True)
        state = self.conn.execute(
            "SELECT state FROM items WHERE repo = ? AND number = 2", (REPO,)
        ).fetchone()[0]
        self.assertEqual(state, "deleted")

    def test_first_sighting_404_stores_then_marks(self) -> None:
        """A 404 on an item that was never stored: the listing payload is
        stored first (truthful row + watermark), the row is marked deleted,
        and the run counts it."""
        from fixtures import pull, pr_file

        self.src.add_pr(
            issue(9, title="nine", kind="pr", updated_at=ts(90)),
            pull(9, changed_files=1),
            files=[pr_file("src/i.py")],
        )

        def gone(number: int):
            raise GitHubError("HTTP 404 Not Found", status=404)

        self.src.list_pr_files = gone  # type: ignore[method-assign]
        result = self.sync(force=True)
        row = self.conn.execute(
            "SELECT state FROM items WHERE repo = ? AND number = 9", (REPO,)
        ).fetchone()
        self.assertIsNotNone(row, "listing payload must be stored before marking")
        self.assertEqual(row[0], "deleted")
        self.assertEqual(result["ingested"], 1)
        trail = self.count(
            "SELECT COUNT(*) FROM fetch_log WHERE repo = ? AND resource_id = '9'"
            " AND lower(note) LIKE '%deleted%'",
            (REPO,),
        )
        self.assertGreaterEqual(trail, 1)

    def test_other_statuses_still_abort(self) -> None:
        self.src.add_pr(
            issue(3, title="three", kind="pr"),
            pull(3, changed_files=1),
            files=[pr_file("src/c.py")],
        )
        self.sync()

        def broken(number: int):
            raise GitHubError("HTTP 500 oops", status=None)

        self.src.list_pr_files = broken  # type: ignore[method-assign]
        self.assertRaises(SyncError, self.sync, force=True)
        state = self.conn.execute(
            "SELECT state FROM items WHERE repo = ? AND number = 3", (REPO,)
        ).fetchone()[0]
        self.assertEqual(state, "open")


class DeletionPassSemanticsTests(TempDBTest):
    def test_marking_survives_rate_limit_listing_restart(self) -> None:
        # fixed instants: deterministic regardless of host clock or load
        base = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
        reset_at = (base + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        src = _RestartingDeletionSource(gone_number=3, reset_at=reset_at)
        src.add_issue(issue(1, updated_at=ts(10)))
        src.add_issue(issue(2, updated_at=ts(20)))
        src.add_issue(issue(3, updated_at=ts(30)))
        # Store the full corpus first (no failure on this listing).
        src.fail_first_listing = False
        sync_repo(self.conn, src, REPO)
        # The item disappears; the next FULL listing rate-limits once and
        # restarts before draining without it.
        src.fail_first_listing = True
        src.item_gone = True
        src.attempts = 0
        clock = MagicMock()
        clock.time.return_value = base.timestamp()
        clock.monotonic.return_value = base.timestamp()
        with patch("zaxbygraph.sync.time", clock):
            result = sync_repo(self.conn, src, REPO, force=True)
        self.assertEqual(result["last_error"], None)
        states = {
            r[0]: r[1]
            for r in self.conn.execute(
                "SELECT number, state FROM items WHERE repo = ?", (REPO,)
            )
        }
        self.assertEqual(states, {1: "open", 2: "open", 3: "deleted"})
        self.assertEqual(src.check_deleted_calls, 1)

    def test_incremental_run_never_consults_the_oracle(self) -> None:
        self.src.add_issue(issue(1, updated_at=ts(10)))
        self.src.add_issue(issue(2, updated_at=ts(20)))
        self.sync()
        oracle_calls: list = []
        self.src.check_deleted = lambda numbers: oracle_calls.append(list(numbers)) or {}
        self.src.comment_on(1, "bump")
        self.sync()
        self.assertEqual(oracle_calls, [])

    def test_no_oracle_means_no_marking(self) -> None:
        self.src.add_issue(issue(1, updated_at=ts(10)))
        self.src.add_issue(issue(2, updated_at=ts(20)))
        self.sync()
        del self.src.issues[2]
        self.sync(force=True)
        states = {
            r[0]: r[1]
            for r in self.conn.execute(
                "SELECT number, state FROM items WHERE repo = ?", (REPO,)
            )
        }
        self.assertEqual(states, {1: "open", 2: "open"})


if __name__ == "__main__":
    unittest.main()
