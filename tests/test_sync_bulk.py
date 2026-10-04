"""Sync-level regression tests from the PR-13 review (swarm-pr-feedback):
production bulk-children wiring through sync_repo, the rate-window cap,
the --source CLI switch, and malformed-listing-entry handling."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fixtures import (
    REPO,
    TempDBTest,
    add_page_prs,
    issue,
    ts,
)
from zaxbygraph.sync import SyncError, sync_repo
from fixtures import (
    RateLimitedError,
)


class _AlwaysRateLimitedSource:
    """A listing that 429s on every attempt with a machine-readable budget:
    drives the _MAX_RATE_WINDOWS exhaustion branch end to end."""

    REMAINING = 0
    RESET_AT = "2026-10-03T12:20:00Z"

    def list_issues(self, since):
        raise RateLimitedError(
            "API rate limit exceeded HTTP 429",
            remaining=self.REMAINING,
            reset_at=self.RESET_AT,
        )

    def list_releases(self):
        return []


class BulkWiringTests(TempDBTest):
    def test_page_shaped_bulk_source_wires_fetch_children(self) -> None:
        """The real GraphQLSource shape: PAGES of plain items plus a
        fetch_children bulk call. The whole provided-map branch of
        _resolve_children must run with zero per-item REST fallbacks."""
        self.src = None  # replaced below; TempDBTest's default is unused
        from fixtures import PageShapedBulkSource

        src = PageShapedBulkSource()
        add_page_prs(src, 50)
        result = sync_repo(self.conn, src, REPO)
        self.assertEqual(result["ingested"], 50)
        self.assertEqual(src.fallback_calls, 0)
        # One listing page (50 items) -> one fetch_children call for the page.
        self.assertEqual(len(src.fetch_children_calls), 1)
        self.assertEqual(src.fetch_children_calls[0], 50)
        self.assertEqual(self.count("SELECT COUNT(*) FROM items WHERE kind = 'pr'"), 50)
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM comments WHERE kind = 'issue_comment'"), 100
        )
        self.assertEqual(self.count("SELECT COUNT(*) FROM reviews"), 50)
        self.assertEqual(self.count("SELECT COUNT(*) FROM pr_files"), 100)


class RateWindowCapTests(TempDBTest):
    def test_cap_exhaustion_sleeps_five_windows_then_raises(self) -> None:
        src = _AlwaysRateLimitedSource()
        clock = MagicMock()
        base = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
        clock.time.return_value = base.timestamp()
        clock.monotonic.return_value = base.timestamp()
        with patch("zaxbygraph.sync.time", clock):
            with self.assertRaises(SyncError):
                sync_repo(self.conn, src, REPO)
        slept = [c.args[0] for c in clock.sleep.call_args_list if c.args]
        self.assertEqual(len(slept), 5, f"windows slept: {slept}")
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 0)

    def test_floor_sleeps_proactively_and_counts_toward_cap(self) -> None:
        from fixtures import FakeGitHubSource

        class FloorSource(FakeGitHubSource):
            """Reports an exhausted budget before the first listing, then
            serves normally (as after a real window reset)."""

            def __init__(self) -> None:
                super().__init__()
                self.rate_limit_remaining: int | None = 0
                self.rate_limit_reset_at: str | None = (
                    (datetime.now(timezone.utc) + timedelta(seconds=30))
                    .strftime("%Y-%m-%dT%H:%M:%SZ")
                )

            def list_issues(self, since):
                # The floor sleep happens before the first attempt; by the
                # time the listing runs the budget has "reset".
                self.rate_limit_remaining = 4999
                return super().list_issues(since)

        src = FloorSource()
        src.add_issue(issue(1, updated_at=ts(10)))
        clock = MagicMock()
        base = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
        clock.time.return_value = base.timestamp()
        clock.monotonic.return_value = base.timestamp()
        with patch("zaxbygraph.sync.time", clock):
            result = sync_repo(self.conn, src, REPO)
        self.assertEqual(result["ingested"], 1)
        waited = [c.args[0] for c in clock.sleep.call_args_list if c.args]
        self.assertEqual(len(waited), 1, "one proactive floor sleep")
        self.assertGreaterEqual(waited[0], 0)

    def test_floor_at_cap_proceeds_without_sleeping(self) -> None:
        from fixtures import FakeGitHubSource
        from zaxbygraph.github import GitHubError

        class StuckFloorSource(FakeGitHubSource):
            """Budget pinned at the floor forever and a listing that keeps
            raising attr-bearing 429s: the floor sleeps and the error sleeps
            must together cap at _MAX_RATE_WINDOWS, then raise."""

            rate_limit_remaining: int | None = 0
            rate_limit_reset_at: str = "2026-10-03T12:20:00Z"

            def list_issues(self, since):
                raise RateLimitedError(
                    "API rate limit exceeded HTTP 429",
                    remaining=0,
                    reset_at=self.rate_limit_reset_at,
                )

        src = StuckFloorSource()
        clock = MagicMock()
        base = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
        clock.time.return_value = base.timestamp()
        clock.monotonic.return_value = base.timestamp()
        with patch("zaxbygraph.sync.time", clock):
            with self.assertRaises(SyncError):
                sync_repo(self.conn, src, REPO)
        slept = [c.args[0] for c in clock.sleep.call_args_list if c.args]
        self.assertEqual(len(slept), 5, "floor sleeps capped at _MAX_RATE_WINDOWS")


class MalformedListingTests(TempDBTest):
    def test_malformed_number_is_skipped_with_a_trail(self) -> None:
        self.src.add_issue(issue(1, updated_at=ts(10)))
        broken = issue(2, updated_at=ts(20))
        broken["number"] = None  # malformed: not coercible to int
        self.src.issues[2] = broken
        self.src.add_issue(issue(3, updated_at=ts(30)))
        result = self.sync()
        self.assertEqual(result["ingested"], 2)
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 2)
        trail = self.count(
            "SELECT COUNT(*) FROM fetch_log WHERE repo = ? AND note LIKE 'skipped malformed%'",
            (REPO,),
        )
        self.assertEqual(trail, 1)


class SourceSwitchTests(TempDBTest):
    def _args(self, source):
        return argparse.Namespace(
            repo=REPO,
            db=str(self.db_path),
            force=False,
            include_patches=False,
            jsonl=None,
            jsonl_flag=False,
            format="json",
            source=source,
        )

    def test_source_rest_selects_the_rest_source(self) -> None:
        from zaxbygraph.cli import cmd_sync
        from unittest.mock import patch as mock_patch

        from zaxbygraph.github import GhApiSource
        from zaxbygraph.graphql import GraphQLSource

        constructed = {}

        class RestProbe(GhApiSource):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                constructed["rest"] = True

            def list_issues(self, since):
                raise RuntimeError("probe stop")

        class GraphProbe(GraphQLSource):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                constructed["graphql"] = True

        with mock_patch("zaxbygraph.cli.GhRestSource", RestProbe), mock_patch(
            "zaxbygraph.cli.GhApiSource", GraphProbe
        ):
            code = cmd_sync(self._args("rest"))
        self.assertEqual(code, 1)  # the probe aborts the sync; the point is construction
        self.assertIn("rest", constructed)
        self.assertNotIn("graphql", constructed)

    def test_default_source_is_graphql(self) -> None:
        from zaxbygraph.cli import cmd_sync
        from unittest.mock import patch as mock_patch

        from zaxbygraph.github import GhApiSource
        from zaxbygraph.graphql import GraphQLSource

        constructed = {}

        class RestProbe(GhApiSource):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                constructed["rest"] = True

        class GraphProbe(GraphQLSource):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                constructed["graphql"] = True

            def list_issues(self, since):
                raise RuntimeError("probe stop")

        with mock_patch("zaxbygraph.cli.GhRestSource", RestProbe), mock_patch(
            "zaxbygraph.cli.GhApiSource", GraphProbe
        ):
            code = cmd_sync(self._args("graphql"))
        self.assertEqual(code, 1)
        self.assertIn("graphql", constructed)
        self.assertNotIn("rest", constructed)


if __name__ == "__main__":
    import unittest

    unittest.main()
