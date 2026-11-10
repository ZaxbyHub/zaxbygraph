"""Frozen acceptance checks: sync integrity (listing coverage, restarts,
degradation, profile inheritance, torn rows, capability probe limits).

Authored at arm's length from the acceptance criteria and the verified root
causes only; every docstring stays true at both ends (RED at base for the
SEMANTIC reason named, GREEN after the fix). Offline: no network, no live
`gh`; the real REST source drives a fake ``_api`` transport.
"""
from __future__ import annotations

import argparse
import io
import unittest
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch

from fixtures import (  # noqa: E501 -- long frozen assertion messages below
    REPO, FakeGitHubSource, RateLimitedError, TempDBTest,
    issue, pr_file, pull, ts,
)
from zaxbygraph.cli import cmd_sync
from zaxbygraph.github import GhApiSource, GitHubError
from zaxbygraph.graphql import GraphQLSource
from zaxbygraph.query import status
from zaxbygraph.sync import SyncError, sync_repo

@contextmanager
def _fake_sync_clock(sleep_sink: list):
    """Patch the sync's injectable clock; every time.sleep appends to
    sleep_sink (telling post-window retry calls apart from every other)."""
    clock = patch("zaxbygraph.sync.time")
    mocked = clock.start()
    epoch = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    mocked.time.return_value = epoch
    mocked.monotonic.return_value = epoch
    mocked.sleep.side_effect = lambda delay: sleep_sink.append(delay)
    try:
        yield mocked
    finally:
        clock.stop()

class _MovingOrderTransport:
    """Fake ``_api`` transport for the REAL GhApiSource.list_issues: serves
    ``repos/<slug>/issues?...`` as per_page-sized pages over a live
    sort=updated&direction=asc order, honoring ``since``. The hazard fires
    ONCE, applied when the given listing request arrives (before it is
    served): ``update`` bumps a delivered item's updated_at (it moves to the
    tail); ``delete`` removes a delivered item; ``tie-swap`` flips the
    relative order of two items sharing an updated_at across the page
    boundary. All three shift the asc order so the next page's window skips
    a live item -- the update at the opening boundary (fire_after=2), the
    delete and the tie-swap after page 2 (fire_after=3). Children/releases
    answer empty; anything else is unexpected."""

    def __init__(self, count: int = 250, hazard: str = "update",
                 fire_after: int = 2, move_number: int = 50,
                 delete_number: int = 150, swap_pair: tuple[int, int] = (200, 201),
                 repo: str = REPO) -> None:
        self.items = {
            n: issue(n, comments=0, updated_at=ts(n)) for n in range(1, count + 1)
        }
        if hazard == "tie-swap":
            lo, hi = swap_pair
            self.items[hi]["updated_at"] = self.items[lo]["updated_at"]
        self.hazard = hazard
        self.fire_after = fire_after
        self.move_number = move_number
        self.delete_number = delete_number
        self.swap_pair = swap_pair
        self.repo = repo
        self.mutated = False
        self.listing_requests = 0
        self._tiebreak: dict[int, int] = {}

    def _fire(self) -> None:
        if self.hazard == "update":
            self.items[self.move_number]["updated_at"] = ts(10_000)
        elif self.hazard == "delete":
            del self.items[self.delete_number]
        elif self.hazard == "tie-swap":
            lo, hi = self.swap_pair
            self._tiebreak[lo] = hi
            self._tiebreak[hi] = lo
        self.mutated = True

    def _window(self, params: dict) -> list[dict]:
        per_page, page, since = (int(params.get("per_page", 100)),
                                 int(params.get("page", 1)), params.get("since"))
        ordered = sorted(self.items.values(),
                         key=lambda r: (r["updated_at"],
                                        self._tiebreak.get(int(r["number"]), int(r["number"]))))
        if since:
            ordered = [r for r in ordered if r["updated_at"] >= since]
        return [deepcopy(r) for r in
                ordered[(page - 1) * per_page:page * per_page]]

    def __call__(self, path, paginate=False, _enrich_rate=True):
        if path.startswith(f"repos/{self.repo}/issues?"):
            self.listing_requests += 1
            if self.listing_requests == self.fire_after and not self.mutated:
                self._fire()
            params = dict(
                kv.split("=", 1) for kv in path.split("?", 1)[1].split("&")
            )
            return self._window(params)
        if path.startswith(f"repos/{self.repo}/"):
            return []  # child fetches and releases: empty answers
        raise AssertionError(f"unexpected gh api path: {path!r}")

class ListingIntegrityTests(TempDBTest):
    def test_listing_survives_mid_sync_update(self) -> None:
        """RED at base, GREEN after the fix, three hazards that each shift the
        sort=updated&direction=asc order between listing pages so the next
        page's fixed-offset window skips a live item. (1) An already-listed
        item's updated_at is bumped between pages 1 and 2 (it moves to the
        tail): 249 of 250 stored, number 101 never delivered. (2) A
        page-2-delivered item is DELETED between pages 2 and 3: the shift
        skips number 201 -- no re-delivery signal fires, so no movement
        detection can see it. (3) Two items sharing an updated_at swap
        relative order across the page-2/page-3 boundary: number 201 is
        skipped the same way. The contract: a run ingests EVERY item even
        when the listing order moves mid-run at ANY boundary (the fix's
        second full pass re-walks the window and delivers the skipped
        item); each hazard must actually fire (transport-recorded), and no
        live item is left missing."""
        # (1) Opening-boundary update: the moved item leaves page 1's window.
        transport = _MovingOrderTransport()
        self.src = GhApiSource(*REPO.split("/", 1), gh_bin="gh")
        self.src._api = transport  # inject at the _api transport seam
        result = self.sync()
        self.assertTrue(transport.mutated, "the mid-sync update must actually fire (guards a vacuous pass where the hazard never happened)")
        stored = {int(r[0]) for r in self.conn.execute("SELECT number FROM items")}
        missing = sorted(set(range(1, 251)) - stored)
        self.assertEqual(missing, [], "mid-sync update moved a listed item to the tail and the run "
                         f"silently dropped {missing} ({len(stored)} of 250 stored, ingested={result['ingested']})")
        states = dict(self.conn.execute("SELECT number, state FROM items"))
        self.assertTrue(all(s == "open" for s in states.values()), "a live item must never be marked gone by a moving listing")

        # (2) Page-2 deleted mover: the shift skips a live item at the next
        # boundary with NO re-delivery signal (own repo).
        gone_repo = "acme/p2delete"
        transport = _MovingOrderTransport(hazard="delete", fire_after=4, repo=gone_repo)
        self.src = GhApiSource(*gone_repo.split("/", 1), gh_bin="gh")
        self.src._api = transport
        result = sync_repo(self.conn, self.src, gone_repo)
        self.assertTrue(transport.mutated, "the page-2 deletion must actually fire")
        stored = {int(r[0]) for r in self.conn.execute(
            "SELECT number FROM items WHERE repo = ?", (gone_repo,))}
        missing = sorted(set(range(1, 251)) - stored - {150})
        self.assertEqual(missing, [],
                         f"a page-2 deleted mover shifted the order and the run silently dropped {missing} "
                         f"({len(stored)} of 249 live stored, ingested={result['ingested']})")

        # (3) Page-2 same-timestamp reorder across the boundary: the swap
        # moves a live item into the already-served window (own repo).
        swap_repo = "acme/p2swap"
        transport = _MovingOrderTransport(hazard="tie-swap", fire_after=4, repo=swap_repo)
        self.src = GhApiSource(*swap_repo.split("/", 1), gh_bin="gh")
        self.src._api = transport
        result = sync_repo(self.conn, self.src, swap_repo)
        self.assertTrue(transport.mutated, "the page-2 tie-swap must actually fire")
        stored = {int(r[0]) for r in self.conn.execute(
            "SELECT number FROM items WHERE repo = ?", (swap_repo,))}
        missing = sorted(set(range(1, 251)) - stored)
        self.assertEqual(missing, [],
                         f"a page-2 same-timestamp reorder shifted the order and the run silently dropped {missing} "
                         f"({len(stored)} of 250 stored, ingested={result['ingested']})")

        # (4) Complete first pass, destructive SECOND pass: the first walk
        # delivers everything; the hazard fires during the second walk (a
        # delivered item deleted at its page boundary), so the second walk's
        # set is a strict subset of the first -- a subset-termination rule
        # would stop here, while the fix's set-equality rule walks on until
        # a pass reproduces its predecessor exactly. The skipped item was
        # already stored by pass 1, so the store must end complete (own repo).
        late_repo = "acme/p2late"
        transport = _MovingOrderTransport(hazard="delete", fire_after=9,
                                          delete_number=2, repo=late_repo)
        self.src = GhApiSource(*late_repo.split("/", 1), gh_bin="gh")
        self.src._api = transport
        result = sync_repo(self.conn, self.src, late_repo)
        self.assertTrue(transport.mutated, "the second-walk deletion must actually fire")
        self.assertGreaterEqual(transport.listing_requests, 7,
                                "the run must walk the window more than twice when a later pass loses coverage "
                                f"(listing requests: {transport.listing_requests})")
        stored = {int(r[0]) for r in self.conn.execute(
            "SELECT number FROM items WHERE repo = ?", (late_repo,))}
        missing = sorted(set(range(1, 251)) - stored - {2})
        self.assertEqual(missing, [],
                         f"a destructive second walk must not lose the items the first walk stored: missing {missing} "
                         f"({len(stored)} of 249 live stored, ingested={result['ingested']})")

class _WindowRateSource(FakeGitHubSource):
    """Five items; the listing dies with a machine-readable rate-limit window
    exactly once, after two items have committed. Every list_issues call
    records (since, after_rate_sleep): after_rate_sleep is True only for a
    call made immediately after sleeping a rate window (the retry path)."""

    def __init__(self, sleeps: list) -> None:
        super().__init__()
        self.calls: list[tuple[str | None, bool]] = []
        self._sleeps = sleeps
        self.limited = False
        for n in (1, 2, 3, 4, 5):
            self.add_issue(issue(n, updated_at=ts(n * 100)))

    def list_issues(self, since: str | None):
        after_sleep = bool(self._sleeps)
        self._sleeps.clear()
        self.calls.append((since, after_sleep))
        items = sorted(self.issues.values(), key=lambda r: (r["updated_at"], r["number"]))
        if since is not None:
            items = [r for r in items if r["updated_at"] >= since]
        for index, rec in enumerate(items):
            if index == 2 and not self.limited:
                self.limited = True
                raise RateLimitedError("API rate limit exceeded HTTP 429", remaining=0, reset_at="2026-01-01T12:00:30Z")
            yield dict(rec)

class RateWindowRestartTests(TempDBTest):
    def test_rate_window_resumes_from_committed_watermark(self) -> None:
        """RED at base, GREEN after the fix: a listing restarted after
        sleeping through a rate-limit window must resume from the watermark
        the committed items already advanced (the updated_at of the last
        committed item), not from the run-start binding -- the only restart
        that uses the committed watermark; a later completeness pass may
        re-drain from the run-start floor and is exempt. At base the restart
        is handed since=None though the watermark moved, and ingested
        double-counts the re-delivered items."""
        sleeps: list = []
        self.src = _WindowRateSource(sleeps)
        with _fake_sync_clock(sleeps):
            result = self.sync()
        self.assertTrue(self.src.limited, "the rate window must actually fire")
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 5, "a repeatedly-windowed run must still store every item once the window is slept out")
        restarts = [s for s, after_sleep in self.src.calls[1:] if after_sleep]
        self.assertGreaterEqual(len(restarts), 1, f"a listing restart after a slept window must have happened (captures: {self.src.calls})")
        committed_watermark = ts(200)  # items 1 and 2 committed before the window
        for restart_since in restarts:
            self.assertEqual(restart_since, committed_watermark, "a listing restarted after a slept rate window must resume "
                             f"from the committed watermark; got {restart_since!r} (captures: {self.src.calls})")
        self.assertEqual(result["ingested"], 5, "ingested counts DISTINCT item numbers across restarted passes")
        self.assertEqual(result["issues_since"], ts(500))

class _ForbiddenBulkSource(FakeGitHubSource):
    """A GraphQL-shaped source whose bulk children call (fetch_children) is
    refused with a non-rate-limit 403 while every per-item REST path works."""

    def fetch_children(self, items) -> dict:
        raise GitHubError("gh: Forbidden (HTTP 403)", status=403)

class GraphqlDegradationTests(TempDBTest):
    def test_graphql_unavailable_degrades_to_rest(self) -> None:
        """RED at base, GREEN after the fix: when the bulk children query
        fails with a non-rate-limit 403, the run must finish over the
        per-item REST path, record exactly one fetch_log row for the
        degradation, and report source 'rest (graphql unavailable)'. At base
        the GitHubError propagates as SyncError and zero REST calls happen."""
        self.src = _ForbiddenBulkSource()
        self.src.add_issue(issue(1, title="one", comments=1, updated_at=ts(10)))
        self.src.comment_on(1, "hello")
        try:
            result = self.sync()
        except SyncError as exc:
            self.fail(f"a non-rate-limit GraphQL failure must degrade to REST, not abort the run: {exc}")
        self.assertEqual(result["ingested"], 1, "the run must complete")
        self.assertEqual(self.count("SELECT COUNT(*) FROM comments"), 1, "children must be fetched over REST after the degradation")
        self.assertIsNone(result["last_error"])
        reported = str(result.get("source") or "")
        self.assertIn("rest", reported, f"the result must report the REST degradation; got {reported!r}")
        self.assertIn("graphql", reported, f"the result must name the graphql unavailability; got {reported!r}")
        self.assertEqual(self.count("SELECT COUNT(*) FROM fetch_log WHERE lower(note) LIKE '%graphql unavailable%'"),
                         1, "exactly one fetch_log row must record the degradation")

def _sync_args(test: TempDBTest, **overrides) -> argparse.Namespace:
    """The cmd_sync Namespace the plumbing suite drives."""
    base = dict(
        repo=REPO, db=str(test.db_path), force=False, include_patches=False,
        jsonl=None, jsonl_flag=False, format="json", source=None, wait=False,
        numbers=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)

class BareSyncProfileTests(TempDBTest):
    def test_bare_sync_inherits_source_and_patches(self) -> None:
        """RED at base, GREEN after the fix: a sync run WITHOUT --source or
        --include-patches uses the store's recorded fetch_source and
        include_patches profile. At base the bare run re-stamps
        include_patches to 0, erases the stored patch on re-ingest, and
        constructs GraphQL for a store stamped fetch_source='rest'."""
        src = FakeGitHubSource()
        src.add_pr(issue(3, title="pr", kind="pr", comments=0, updated_at=ts(30)),
                   pull(3, changed_files=1), files=[pr_file("src/a.py")])
        with patch("zaxbygraph.cli.GhRestSource", return_value=src), patch(
            "zaxbygraph.cli.GhApiSource", return_value=src
        ):
            with redirect_stdout(io.StringIO()):
                code = cmd_sync(_sync_args(self, include_patches=True, source="rest"))
        self.assertEqual(code, 0)
        row = self.conn.execute(
            "SELECT include_patches, fetch_source FROM sync_state WHERE repo = ?",
            (REPO,),
        ).fetchone()
        self.assertEqual(tuple(row), (1, "rest"), "stamping run must record the profile")
        self.assertIsNotNone(
            self.conn.execute("SELECT patch FROM pr_files WHERE number = 3").fetchone()[0],
            "the stamping run must store the patch")

        built: list[str] = []

        def factory(name):
            def make(a, b):
                built.append(name)
                return src
            return make

        with patch("zaxbygraph.cli.GhRestSource",
                   side_effect=factory("rest")), patch(
            "zaxbygraph.cli.GhApiSource", side_effect=factory("graphql")
        ):
            with redirect_stdout(io.StringIO()):
                code = cmd_sync(_sync_args(self))
        self.assertEqual(code, 0)
        row = self.conn.execute(
            "SELECT include_patches, fetch_source FROM sync_state WHERE repo = ?",
            (REPO,),
        ).fetchone()
        self.assertEqual(tuple(row), (1, "rest"),
                         "a bare sync must keep the store's recorded "
                         "include_patches and fetch_source")
        self.assertIsNotNone(
            self.conn.execute("SELECT patch FROM pr_files WHERE number = 3").fetchone()[0],
            "a bare sync must re-ingest with the recorded include_patches so "
            "the stored patch survives")
        self.assertEqual(built, ["rest"],
                         "a bare sync of a rest-stamped store must construct "
                         f"the REST source, not the GraphQL default ({built})")

class McpRefreshSourceTests(TempDBTest):
    def test_mcp_refresh_honours_fetch_source(self) -> None:
        """RED at base, GREEN after the fix: the MCP server's background
        refresh runner (the seam the staleness kick and the sync tool funnel
        into) must construct the source recorded in sync_state.fetch_source:
        a rest-stamped store never gets the GraphQL source, a graphql-stamped
        store still does, and the sync tool's schema must accept a source
        argument. At base the runner unconditionally constructs GraphQLSource
        and the schema has no source property."""
        import zaxbygraph.mcp_server as mcp_server

        src = FakeGitHubSource()
        src.add_issue(issue(1, updated_at=ts(10)))
        seen: dict[str, object] = {}

        def record_sync(conn, source, repo, **kw):
            seen[str(repo)] = source
            return {"ingested": 0}

        def runner_for_stamp(stamp: str):
            sync_repo(self.conn, src, REPO, fetch_source=stamp)
            self.conn.commit()
            seen.clear()
            with patch("zaxbygraph.mcp_server.sync_repo", side_effect=record_sync):
                mcp_server._default_sync_runner(self.db_path, REPO)
            return seen.get(REPO)

        rest_source = runner_for_stamp("rest")
        self.assertIsNotNone(rest_source, "the runner must reach sync_repo")
        self.assertNotIsInstance(
            rest_source, GraphQLSource,
            "a store stamped fetch_source='rest' must be refreshed over REST, "
            f"not GraphQLSource (got {type(rest_source).__name__})")
        self.assertIsInstance(runner_for_stamp("graphql"), GraphQLSource,
                              "a graphql-stamped store must keep the GraphQL refresh")

        sync_tool = next(t for t in mcp_server._TOOLS if t.get("name") == "sync")
        props = sync_tool.get("inputSchema", {}).get("properties", {})
        self.assertIn("source", props, "the sync tool's advertised schema must "
                      f"accept a source argument (advertised: {sorted(props)})")

def _dated_pull(number: int, updated_at: str, merged_at: str | None,
                sha: str = "abc", merged_by: str | None = None) -> dict:
    """REST pull payload with the explicit updated_at the tear detection
    compares against the listing row's, plus the pull-owned trio."""
    raw = pull(number, changed_files=0)
    raw.update(updated_at=updated_at, merged_at=merged_at,
               merge_commit_sha=sha if merged_at else None)
    if merged_by is not None:
        raw["merged_by"] = {"login": merged_by}
    return raw

class _TornPullSource(FakeGitHubSource):
    """Merged PRs whose pull payloads postdate their listing rows (the
    torn-row hazard). get_issues is the listing-row-by-number refresh seam:
    it serves each number's refresh queue progressively, counts fetches,
    and rate_once raises one rate window on the first refresh."""

    def __init__(self) -> None:
        super().__init__()
        self.refresh: dict[int, list[dict]] = {}
        self.rate_once: set[int] = set()
        self.fetch_counts: dict[int, int] = {}

    def get_issues(self, numbers):
        out = {}
        for n in numbers:
            self.fetch_counts[n] = self.fetch_counts.get(n, 0) + 1
            if n in self.rate_once:
                self.rate_once.discard(n)
                raise RateLimitedError("API rate limit exceeded HTTP 429", remaining=0, reset_at="2026-01-01T12:01:00Z")
            queue = self.refresh.get(n) or []
            row = dict(queue.pop(0)) if queue else None
            out[n] = (row, 200) if row is not None else (None, 404)
        return out

class TornRowTests(TempDBTest):
    def _row(self, number: int, repo: str = REPO):
        return self.conn.execute("SELECT state, merged_at, updated_at FROM items WHERE repo = ? AND number = ?", (repo, number)).fetchone()

    def _since(self, repo: str = REPO):
        return self.conn.execute("SELECT issues_since FROM sync_state WHERE repo = ?", (repo,)).fetchone()[0]

    def test_no_torn_rows(self) -> None:
        """RED at base, GREEN after the fix, six cases; the tear trigger is
        the pull payload's updated_at strictly newer than the listing row's.
        Stale listing: the refreshed row is the row ingested (no mixture).
        Always-stale FRESH row: a bounded refresh (2 attempts) still older
        than the pull facts SKIPS the item -- no row write, one
        torn_row_unresolved fetch_log row; a listing-only ingest would
        re-create the mixture, because pull_raw=None ABSENT semantics
        PRESERVE the stored pull-owned trio. Always-stale EXISTING row: a
        row already carrying the trio stays BYTE-UNCHANGED (nothing cleared
        or lost), skipped, logged. Current: a listing row at least as new
        as the pull facts needs NO re-fetch. A rate error during the
        refresh follows the per-item error path (slept out, retried, the
        item is not lost). Healthy item AFTER a skipped one: a later
        healthy item commits and advances the watermark past the skipped
        item, so the durable sync_pending set (reason 'torn') -- not the
        watermark -- guarantees the retry: the next run retries the number
        through the exact-number seam, ingests it once its listing caught
        up, and clears the set. At base every stale case stores
        state='open' with merged_at set (the torn mixture) and zero
        re-fetches."""
        self.src = _TornPullSource()
        # Stale listing, one current refresh resolves it (#3).
        self.src.add_pr(issue(3, title="p3", kind="pr", state="open", updated_at=ts(30), comments=0), _dated_pull(3, ts(35), ts(35)), files=[])
        self.src.refresh[3] = [issue(3, title="p3", kind="pr", state="closed", updated_at=ts(60), comments=0)]
        # Always-stale FRESH row (#4, listed LAST so no later item can bump the watermark past its skip).
        self.src.add_pr(issue(4, title="p4", kind="pr", state="open", updated_at=ts(70), comments=0), _dated_pull(4, ts(90), ts(45)), files=[])
        self.src.refresh[4] = [issue(4, title="p4", kind="pr", state="open", updated_at=ts(43), comments=0), issue(4, title="p4", kind="pr", state="open", updated_at=ts(61), comments=0)]
        # Listing row already current: pull facts do not postdate it (#5).
        self.src.add_pr(issue(5, title="p5", kind="pr", state="closed", updated_at=ts(62), comments=0), _dated_pull(5, ts(55), ts(55)), files=[])
        self.sync()

        # Stale listing: one refresh suffices; refreshed row wins, fact kept.
        row3 = self._row(3)
        self.assertIsNotNone(row3["merged_at"], "the pull's merge fact must be stored")
        self.assertEqual(row3["state"], "closed", "a stale listing snapshot must not be stored alongside a newer pull payload "
                         f"(state={row3['state']!r}, merged_at={row3['merged_at']!r})")
        self.assertEqual(row3["updated_at"], ts(60), "the refreshed listing row must be the row ingested")

        # Always-stale FRESH row: SKIPPED, never listing-only ingested -- no row, no watermark bump, one log row.
        self.assertIsNone(self._row(4), "an unresolved tear must SKIP the item: a listing-only ingest keeps the stored trio "
                          "via pull_raw=None ABSENT semantics and re-creates the forbidden mixture")
        self.assertLessEqual(self.src.fetch_counts.get(4, 0), 2, f"the refresh budget is bounded (2 attempts; fetches: {self.src.fetch_counts})")
        self.assertGreaterEqual(self.src.fetch_counts.get(4, 0), 1, "the stale listing row must be refreshed at all")
        self.assertEqual(self.count("SELECT COUNT(*) FROM fetch_log WHERE resource = 'item' AND resource_id = '4' AND lower(note) LIKE '%torn_row_unresolved%'"),
                         1, "exactly one torn_row_unresolved fetch_log row must name the item")
        self.assertLess(self._since(), ts(70), "the skipped item's watermark must not advance past it (the next run re-lists and retries it)")

        # Already-current listing row: no re-fetch at all.
        self.assertEqual(self.src.fetch_counts.get(5, 0), 0, "no listing re-fetch when the listing row is already current")
        row5 = self._row(5)
        self.assertEqual((row5["state"], row5["merged_at"] is not None), ("closed", True), "the current row ingests with its merge fact")

        # Refresh rate window (own repo; its retry cannot disturb the counts above): slept out, retried, not lost.
        faulty = _TornPullSource()
        faulty.add_pr(issue(6, title="p6", kind="pr", state="open", updated_at=ts(36), comments=0), _dated_pull(6, ts(85), ts(50)), files=[])
        faulty.rate_once.add(6)
        faulty.refresh[6] = [issue(6, title="p6", kind="pr", state="closed", updated_at=ts(96), comments=0)]
        with _fake_sync_clock([]):
            try:
                fault_result = sync_repo(self.conn, faulty, "acme/tornfault")
            except SyncError as exc:
                self.fail(f"a rate window during the refresh must be slept out and retried, not abort the run: {exc}")
        row6 = self._row(6, "acme/tornfault")
        self.assertEqual((fault_result["ingested"], row6["state"], row6["merged_at"] is not None), (1, "closed", True),
                         "an item whose refresh hit a rate window must not be lost and the retried refresh must resolve the tear")

        # EXISTING row (own repo): an earlier CONSISTENT ingest stored the
        # pull-owned trio; a later always-stale re-ingest must SKIP the item
        # -- stored row byte-unchanged, watermark stays put, tear logged once.
        keep = "acme/tornkeep"
        keeper = _TornPullSource()
        keeper.add_pr(issue(7, title="p7", kind="pr", state="closed", updated_at=ts(200), comments=0), _dated_pull(7, ts(200), ts(200), sha="aaa700", merged_by="merger"), files=[])
        sync_repo(self.conn, keeper, keep)
        sql7 = "SELECT state, updated_at, merged_at, merge_commit, merged_by FROM items WHERE repo = ? AND number = 7"
        before = tuple(self.conn.execute(sql7, (keep,)).fetchone())
        self.assertEqual(before[2:], (ts(200), "aaa700", "merger"), "the earlier consistent ingest must have stored the pull-owned trio (guards a vacuous pass)")
        keeper.issues[7]["updated_at"] = ts(220)  # a newer listing snapshot
        keeper.pulls[7] = _dated_pull(7, ts(400), ts(410), sha="fff407", merged_by="latecomer")
        keeper.refresh[7] = [issue(7, title="p7", kind="pr", state="closed", updated_at=ts(210), comments=0), issue(7, title="p7", kind="pr", state="closed", updated_at=ts(211), comments=0)]
        with _fake_sync_clock([]):  # both refreshes stay older than the pull
            try:
                sync_repo(self.conn, keeper, keep)
            except SyncError as exc:
                self.fail(f"an unresolved tear must skip the item, not abort the run: {exc}")
        self.assertEqual(tuple(self.conn.execute(sql7, (keep,)).fetchone()), before,
                         "the skipped item's stored row must stay byte-unchanged: listing-owned AND pull-owned fields alike "
                         "(a listing-only ingest would keep the stale trio via pull_raw=None ABSENT semantics and re-create the mixture)")
        self.assertLess(self._since(keep), ts(220), "the skipped existing item's watermark must not advance past it (the next run re-lists and retries)")
        self.assertEqual(self.count("SELECT COUNT(*) FROM fetch_log WHERE repo = ? AND resource = 'item' AND resource_id = '7'"
                                    " AND lower(note) LIKE '%torn_row_unresolved%'", (keep,)),
                         1, "exactly one torn_row_unresolved fetch_log row must name the skipped item")

        # HEALTHY item after a skipped one (own repo): the skip alone cannot
        # promise the retry, because a later healthy item with a newer
        # updated_at commits and advances the watermark PAST the skipped
        # item -- the durable pending set is what guarantees the retry.
        later = "acme/tornlater"
        src = _TornPullSource()
        # #8: torn (pull ts(120) postdates the listing ts(80)); both refreshes stay older.
        src.add_pr(issue(8, title="p8", kind="pr", state="open", updated_at=ts(80), comments=0), _dated_pull(8, ts(120), ts(45)), files=[])
        src.refresh[8] = [issue(8, title="p8", kind="pr", state="open", updated_at=ts(75), comments=0),
                          issue(8, title="p8", kind="pr", state="open", updated_at=ts(76), comments=0)]
        # #9: healthy (pull ts(125) does not postdate the listing ts(130)); listed after #8 so it commits and moves the watermark.
        src.add_pr(issue(9, title="p9", kind="pr", state="closed", updated_at=ts(130), comments=0), _dated_pull(9, ts(125), ts(125)), files=[])
        with _fake_sync_clock([]):
            try:
                sync_repo(self.conn, src, later)
            except SyncError as exc:
                self.fail(f"an unresolved tear must skip the item, not abort the run: {exc}")
        self.assertIsNone(self._row(8, later), "the torn item must be skipped, not stored as a mixture")
        self.assertIsNotNone(self._row(9, later), "the later healthy item must ingest normally")
        self.assertGreater(self._since(later), ts(80),
                           "the later healthy item must advance the watermark PAST the skipped item's original "
                           "listing timestamp -- the guard that makes this case non-vacuous (the skip alone would "
                           "otherwise let the watermark re-list it next run)")
        pending8 = self.count("SELECT COUNT(*) FROM sync_pending WHERE repo = ? AND number = 8 AND reason = 'torn'", (later,))
        self.assertEqual(pending8, 1, "the skipped item must land in the durable pending set (reason 'torn') -- "
                          "the set, not the watermark, is what guarantees the retry after a later healthy item moved it")
        # Run 2: the pending refresh retries #8 (its listing caught up but stays
        # BELOW the advanced watermark, so only the pending set can re-list it).
        src.refresh[8] = [issue(8, title="p8", kind="pr", state="closed", updated_at=ts(125), comments=0)]
        with _fake_sync_clock([]):
            try:
                sync_repo(self.conn, src, later)
            except SyncError as exc:
                self.fail(f"the pending-set retry must not abort the run: {exc}")
        row8 = self._row(8, later)
        self.assertIsNotNone(row8, "the next run must retry the pending item and ingest it once its listing caught up")
        self.assertEqual((row8["state"], row8["merged_at"] is not None), ("closed", True),
                         "the retried item must ingest from the caught-up listing row with its pull fact")
        self.assertEqual(self.count("SELECT COUNT(*) FROM sync_pending WHERE repo = ?", (later,)), 0,
                         "the resolved item must leave the pending set")

class _ProbeCaptureSource(GraphQLSource):
    """Captures every probe query rendered at the transport seam (_graphql)
    and answers offline with a full introspection payload for every alias."""

    def __init__(self) -> None:
        super().__init__("acme", "forgegate", gh_bin="gh")
        self.queries: list[str] = []

    def _graphql(self, query, aliases=None):
        self.queries.append(query)
        return {
            "i": {"fields": [{"name": "databaseId"}]},
            "p": {"fields": [{"name": "databaseId"}]},
            "ic": {"fields": [{"name": "databaseId"}]},
            "rc": {"fields": [{"name": "databaseId"}]},
            "t": {"enumValues": [{"name": "CLOSED_EVENT"}]},
        }

class _FailingProbeSource(FakeGitHubSource):
    """Capability probe fails (non-rate-limit 403); listing and REST children
    work; the sync duck-types on the probe pair."""

    def probe_capabilities(self):
        raise GitHubError("introspection denied", status=403)

    def attach_capabilities(self, caps) -> None:
        pass

class ProbeLimitTests(TempDBTest):
    def test_probe_respects_introspection_limit(self) -> None:
        """RED at base, GREEN after the fix, two legs.
        Render leg: every rendered probe query uses each introspection
        field (fields, enumValues, __type) at most TWICE -- hosts reject
        queries stacking many introspection selections, so the probe splits
        across queries and the merged result is cached (a second
        probe_capabilities costs no further queries, the merged map still
        covers Issue and PullRequest). At base one query stacks four
        fields( and five __type( selections. Reporting leg: a probe that
        still fails is reported in the sync result and in status.coverage,
        not only in fetch_log."""
        src = _ProbeCaptureSource()
        caps = src.probe_capabilities()
        self.assertTrue(src.queries, "the probe must render at least one query")
        self.assertIn("Issue", caps, "the merged capability map must cover Issue")
        self.assertIn("PullRequest", caps, "the merged map must cover PullRequest")
        for query in src.queries:
            for field, occurrences in (
                ("fields(", query.count("fields(")),
                ("enumValues", query.count("enumValues")),
                ("__type(", query.count("__type(")),
            ):
                self.assertLessEqual(
                    occurrences, 2,
                    f"a rendered probe query uses {field} {occurrences} times; "
                    "each introspection field is allowed at most twice per query")
        before = len(src.queries)
        src.probe_capabilities()
        self.assertEqual(len(src.queries), before,
                         "the merged probe result must be cached: a second "
                         "probe renders no further queries")

        self.src = _FailingProbeSource()
        self.src.add_issue(issue(1, title="one", updated_at=ts(10)))
        result = self.sync()
        self.assertEqual(result["ingested"], 1, "a failed probe must not fail the run")
        self.assertIsNone(result["last_error"])
        self.assertTrue(result.get("graphql_probe_failed"),
                        "the sync result must report the probe failure, not "
                        "only fetch_log")
        coverage = status(self.conn, REPO)["coverage"]
        self.assertGreaterEqual(int(coverage.get("probe_failures") or 0), 1,
                                f"status.coverage must count probe failures "
                                f"(got {coverage!r})")

if __name__ == "__main__":
    unittest.main()
