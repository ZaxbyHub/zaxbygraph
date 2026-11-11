"""Phase 4.2 guardrail probes for the listing-completeness design (issue #65
AC1): the properties the approved plan's guardrail set names that the frozen
C1 check does not already pin.

NOT frozen acceptance checks — these are demonstration probes. They drive the
REAL `GhApiSource.list_issues` through a fake `_api` transport, exactly like
C1, and each one names the property it demonstrates:

* a stable run walks the window exactly twice (and no more);
* a FULL page is re-read once for boundary verification, a short page is not;
* a shift the boundary verification sees ends the pass early and forces
  another walk (the `ListingShiftedError` path C1's hazards do not take);
* a >100-item block sharing one `updated_at` (ties spanning page boundaries)
  loses nothing;
* a duplicate full page followed by unseen rows stores every item exactly
  once (idempotent ingest, distinct-count accounting);
* repeated updates of one already-ingested item produce one row, no
  watermark regression, and the newest version;
* continuous churn fails the run incomplete (`listing_not_quiescent`) with
  the recovery floor persisted, and the NEXT run recovers from it.

Offline: no network, no live `gh`.
"""
from __future__ import annotations

import unittest
from copy import deepcopy

from fixtures import REPO, FakeGitHubSource, TempDBTest, comment, issue, ts
from zaxbygraph.github import GhApiSource
from zaxbygraph.sync import SyncError, sync_repo


class _WindowTransport:
    """Fake ``_api`` transport serving `repos/<slug>/issues?...` as
    per_page-sized pages over a live sort=updated&direction=asc order,
    honoring ``since``.

    ``on_request`` (optional) runs before a listing request is served and
    may mutate the corpus; it receives the request index (1-based) and the
    transport. ``page_override`` maps a requested page number to the page
    actually served, which is how a duplicate page is produced. The
    boundary verification is counted two ways: ``boundary_probes`` (how
    often the same page was re-requested) and ``boundary_mismatches`` (how
    often that re-read disagreed with the page just served).
    Children and releases answer empty; anything else is unexpected."""

    PAGE = 100

    def __init__(self, count: int = 250, on_request=None, repo: str = REPO,
                 page_override: dict[int, int] | None = None) -> None:
        self.items = {
            n: issue(n, comments=0, updated_at=ts(n)) for n in range(1, count + 1)
        }
        self.on_request = on_request
        self.repo = repo
        self.page_override = page_override or {}
        self.listing_requests = 0
        self.boundary_probes = 0
        self.boundary_mismatches = 0
        #: Every ``since=`` the walk bound, in request order — the guardrails
        #: assert WHICH window a run drained from, not just that it ran.
        self.since_seen: list[str | None] = []
        self._last_served: tuple[int, frozenset[int], int] | None = None

    def _ordered(self, since: str | None) -> list[dict]:
        rows = sorted(self.items.values(), key=lambda r: (r["updated_at"], r["number"]))
        if since:
            rows = [r for r in rows if r["updated_at"] >= since]
        return rows

    def _window(self, params: dict) -> list[dict]:
        per_page, page, since = (int(params.get("per_page", self.PAGE)),
                                 int(params.get("page", 1)), params.get("since"))
        page = self.page_override.get(page, page)
        ordered = self._ordered(since)
        return [deepcopy(r) for r in
                ordered[(page - 1) * per_page:page * per_page]]

    def __call__(self, path, paginate=False, _enrich_rate=True):
        if path.startswith(f"repos/{self.repo}/issues?"):
            self.listing_requests += 1
            if self.on_request is not None:
                self.on_request(self.listing_requests, self)
            params = dict(kv.split("=", 1) for kv in path.split("?", 1)[1].split("&"))
            self.since_seen.append(params.get("since"))
            page = int(params.get("page", 1))
            served = self._window(params)
            if (
                self._last_served is not None
                and self._last_served[0] == page
                and self._last_served[2] >= self.PAGE
            ):
                # The boundary verification re-reads the page just served,
                # and only a FULL page is followed by one — a short page
                # ends the walk, so a later request for the same page
                # number is a new pass, not a re-read.
                self.boundary_probes += 1
                if {r["number"] for r in served} != self._last_served[1]:
                    self.boundary_mismatches += 1
            self._last_served = (
                page, frozenset(r["number"] for r in served), len(served)
            )
            return served
        if path.startswith(f"repos/{self.repo}/"):
            return []  # child fetches and releases: empty answers
        raise AssertionError(f"unexpected gh api path: {path!r}")


def _source(transport: _WindowTransport, repo: str = REPO) -> GhApiSource:
    src = GhApiSource(*repo.split("/", 1), gh_bin="gh")
    src._api = transport  # inject at the _api transport seam
    return src


def _stored(conn, repo: str) -> set[int]:
    return {int(r[0]) for r in conn.execute(
        "SELECT number FROM items WHERE repo = ?", (repo,))}


class ListingGuardrailTests(TempDBTest):
    """One store, one repo per probe (the guardrails are per-window)."""

    def _run(self, transport: _WindowTransport, repo: str):
        return sync_repo(self.conn, _source(transport, repo), repo)

    # -- stable-walk accounting ------------------------------------------

    def test_stable_run_walks_exactly_twice(self) -> None:
        """A listing that never moves is walked exactly twice: the second
        walk proves quiescence, the set-equality rule stops there, and a
        third walk would mean the termination condition is not set
        equality. A FULL page costs two requests (the page plus its
        boundary re-read), a short page one."""
        repo = "acme/gw-stable"
        # 250 items: 100 (full), 100 (full), 50 (short) per walk.
        transport = _WindowTransport(count=250, repo=repo)
        result = self._run(transport, repo)
        self.assertIsNone(result["last_error"])
        self.assertEqual(len(_stored(self.conn, repo)), 250)
        self.assertEqual(
            transport.listing_requests, 10,
            "two walks of 2 full pages (2 requests each) + 1 short page: "
            f"{transport.listing_requests} requests",
        )
        self.assertEqual(transport.boundary_probes, 4,
                         "the two full pages per walk are each re-read once")
        self.assertEqual(transport.boundary_mismatches, 0,
                         "a stable listing never disagrees with its own re-read")

    def test_short_page_is_not_reread(self) -> None:
        """The boundary verification only applies to a FULL page: the page
        just served is the boundary the next page's offset is measured
        from, and a short page ends the walk (nothing follows it)."""
        repo = "acme/gw-short"
        transport = _WindowTransport(count=50, repo=repo)
        self._run(transport, repo)
        self.assertEqual(len(_stored(self.conn, repo)), 50)
        self.assertEqual(transport.listing_requests, 2,
                         "one short page per walk, two walks, no re-read")
        self.assertEqual(transport.boundary_probes, 0)

    # -- the boundary-verification path ----------------------------------

    def test_boundary_shift_forces_another_walk(self) -> None:
        """A shift the boundary verification SEES (the re-read of the page
        just served disagrees with it) ends the pass early through
        `ListingShiftedError` and the next pass re-walks the window. The
        store still ends complete — this is the path C1's hazards do not
        take, because those shift only the NEXT page's window."""
        repo = "acme/gw-boundary"

        def new_item_lands_on_page_one(index: int, transport: _WindowTransport) -> None:
            # Request 2 is page 1's boundary re-read: a brand-new item with
            # the oldest timestamp lands at the head of the order, so the
            # re-read's page-1 set is {0, 1..99} against the served {1..100}.
            if index == 2:
                transport.items[0] = issue(0, comments=0, updated_at=ts(0))

        transport = _WindowTransport(count=250, on_request=new_item_lands_on_page_one,
                                     repo=repo)
        result = self._run(transport, repo)
        self.assertIsNone(result["last_error"])
        self.assertGreaterEqual(transport.boundary_mismatches, 1,
                                "the boundary re-read must observe the shift")
        self.assertGreaterEqual(
            transport.listing_requests, 7,
            "the early-ended pass must be followed by a full re-walk "
            f"(listing requests: {transport.listing_requests})")
        self.assertEqual(len(_stored(self.conn, repo)), 251,
                         "the new item and every original one are stored")
        states = {int(r[0]): r[1] for r in self.conn.execute(
            "SELECT number, state FROM items WHERE repo = ?", (repo,))}
        self.assertTrue(all(s == "open" for s in states.values()),
                        "a live item must never be marked gone by a shifting listing")

    # -- tie blocks -------------------------------------------------------

    def test_many_equal_timestamps_lose_nothing(self) -> None:
        """More than a page's worth of items sharing ONE updated_at: the
        tie block spans page boundaries, so a rule that relies on the
        timestamp alone to decide "already covered" would skip half of
        them. Every item is ingested and the run still walks exactly
        twice."""
        repo = "acme/gw-ties"
        transport = _WindowTransport(count=250, repo=repo)
        for rec in transport.items.values():
            rec["updated_at"] = ts(500)
        result = self._run(transport, repo)
        self.assertIsNone(result["last_error"])
        self.assertEqual(len(_stored(self.conn, repo)), 250)
        self.assertEqual(transport.listing_requests, 10)

    # -- duplicate pages --------------------------------------------------

    def test_duplicate_full_page_then_unseen_rows(self) -> None:
        """Pagination legitimately hands the same page twice (a cursor that
        did not advance, or a re-requested offset): here the walk's page 2
        is served page 1's window again, so the same 100 items arrive as
        two consecutive FULL pages, and the real page 2 arrives one request
        later. Ingest must be idempotent — one row per number, no duplicate
        children — the walk must still reach the rows the duplicate page
        displaced, and the result's `ingested` counts DISTINCT numbers
        rather than deliveries."""
        repo = "acme/gw-dup"
        transport = _WindowTransport(count=150, repo=repo,
                                     page_override={2: 1, 3: 2})
        result = self._run(transport, repo)
        self.assertIsNone(result["last_error"])
        self.assertEqual(len(_stored(self.conn, repo)), 150)
        self.assertEqual(result["ingested"], 150,
                         "ingested counts distinct numbers, not deliveries")
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM items WHERE repo = ? AND number = 1",
                       (repo,)),
            1, "a re-delivered row upserts, it does not duplicate")
        self.assertEqual(
            transport.listing_requests, 10,
            "two walks of page1 + re-read + duplicate page + re-read + the"
            f" real page 2: {transport.listing_requests} requests")

    def test_repeated_updates_of_one_item_keep_one_row(self) -> None:
        """An item updated again and again during the walk is re-delivered
        by later pages and passes. Exactly one row survives, it holds the
        NEWEST version, and the watermark never regresses."""
        repo = "acme/gw-updates"

        def bump_on_every_request(index: int, transport: _WindowTransport) -> None:
            transport.items[7]["updated_at"] = ts(10_000 + index)

        transport = _WindowTransport(count=60, on_request=bump_on_every_request,
                                     repo=repo)
        result = self._run(transport, repo)
        self.assertIsNone(result["last_error"])
        rows = list(self.conn.execute(
            "SELECT number, title, updated_at FROM items WHERE repo = ?"
            " AND number = 7", (repo,)))
        self.assertEqual(len(rows), 1, "one item row per number, always")
        self.assertEqual(rows[0]["updated_at"], ts(10_000 + transport.listing_requests),
                         "the stored row is the newest version seen")
        self.assertEqual(result["issues_since"], ts(10_000 + transport.listing_requests),
                         "the watermark is the newest updated_at, never a regression")
        self.assertEqual(len(_stored(self.conn, repo)), 60)

    # -- the honest-failure cap and its recovery --------------------------

    def test_continuous_churn_fails_incomplete_with_a_floor(self) -> None:
        """A listing that moves on EVERY request never quiesces: the pass
        cap fails the run incomplete (`listing_not_quiescent`), keeps the
        full-sync pending marker, suppresses the clean finish, and persists
        the recovery floor ACTIVE. A full run's floor is NULL — the
        documented meaning is "re-drain from the beginning", because the
        window it could not quiesce is the whole corpus."""
        repo = "acme/gw-churn"

        def churn(index: int, transport: _WindowTransport) -> None:
            transport.items[index % 250 + 1]["updated_at"] = ts(20_000 + index)

        transport = _WindowTransport(count=250, on_request=churn, repo=repo)
        with self.assertRaises(SyncError) as ctx:
            self._run(transport, repo)
        self.assertIn("listing_not_quiescent", str(ctx.exception))
        row = self.conn.execute(
            "SELECT listing_floor, listing_floor_active, full_sync_pending,"
            " last_error FROM sync_state WHERE repo = ?", (repo,)
        ).fetchone()
        self.assertTrue(row["listing_floor_active"],
                        "the recovery floor must stay active for the next run")
        self.assertIsNone(row["listing_floor"],
                          "a full run's floor is NULL: re-drain from the beginning")
        self.assertTrue(row["full_sync_pending"],
                        "a failed full sync keeps its pending marker")
        self.assertIn("listing_not_quiescent", row["last_error"])
        self.assertIsNone(self.conn.execute(
            "SELECT last_full_sync_at FROM sync_state WHERE repo = ?", (repo,)
        ).fetchone()[0], "an incomplete run must not stamp a clean finish")

    def test_incremental_churn_floor_names_the_window(self) -> None:
        """The same cap trip on an INCREMENTAL run persists a floor that
        NAMES the window: the next run re-drains from the earlier of the
        floor and the watermark, so an item the churn skipped can never be
        sealed by the advancing per-item watermark bumps."""
        repo = "acme/gw-incfloor"
        stable = _WindowTransport(count=60, repo=repo)
        self._run(stable, repo)
        before = self.conn.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (repo,)
        ).fetchone()[0]

        def churn(index: int, transport: _WindowTransport) -> None:
            transport.items[index % 60 + 1]["updated_at"] = ts(30_000 + index)

        churning = _WindowTransport(count=60, on_request=churn, repo=repo)
        with self.assertRaises(SyncError):
            self._run(churning, repo)
        row = self.conn.execute(
            "SELECT listing_floor, listing_floor_active FROM sync_state"
            " WHERE repo = ?", (repo,)
        ).fetchone()
        self.assertTrue(row["listing_floor_active"])
        self.assertIsNotNone(row["listing_floor"],
                             "an incremental run's floor names its window")
        self.assertLessEqual(row["listing_floor"], before,
                             "the floor is the window this run drained from")

    def test_next_run_recovers_from_the_persisted_floor(self) -> None:
        """After the cap trip, the NEXT run re-drains from the persisted
        floor and, once the listing is stable, completes cleanly — and the
        floor is resolved and cleared so it cannot pin later runs to an old
        window forever."""
        repo = "acme/gw-recover"

        def churn(index: int, transport: _WindowTransport) -> None:
            transport.items[index % 250 + 1]["updated_at"] = ts(20_000 + index)

        churning = _WindowTransport(count=250, on_request=churn, repo=repo)
        with self.assertRaises(SyncError):
            self._run(churning, repo)
        floor = self.conn.execute(
            "SELECT listing_floor, listing_floor_active FROM sync_state"
            " WHERE repo = ?", (repo,)
        ).fetchone()
        self.assertTrue(floor["listing_floor_active"])

        # The following run sees a stable listing (a fresh transport, same
        # corpus shape) and must finish: every item stored, the floor
        # cleared, a clean finish stamped.
        stable = _WindowTransport(count=250, repo=repo)
        result = self._run(stable, repo)
        self.assertIsNone(result["last_error"])
        self.assertEqual(len(_stored(self.conn, repo)), 250)
        after = self.conn.execute(
            "SELECT listing_floor, listing_floor_active, full_sync_pending,"
            " last_full_sync_at FROM sync_state WHERE repo = ?", (repo,)
        ).fetchone()
        self.assertFalse(after["listing_floor_active"],
                         "a clean run resolves the recovery floor")
        self.assertIsNone(after["listing_floor"])
        self.assertFalse(after["full_sync_pending"])
        self.assertIsNotNone(after["last_full_sync_at"])

    def test_next_run_drains_from_a_named_floor(self) -> None:
        """The half of cap recovery the NULL-floor case cannot show: an
        INCREMENTAL cap trip persists a floor that NAMES a window, and the
        next run's first listing request must be bound to exactly that
        window — the earlier of the floor and the watermark. A mutant that
        ignores a non-NULL floor drains from the watermark instead and
        seals the window the churn skipped."""
        repo = "acme/gw-namedfloor"
        seed = _WindowTransport(count=60, repo=repo)
        self._run(seed, repo)

        def churn(index: int, transport: _WindowTransport) -> None:
            transport.items[index % 60 + 1]["updated_at"] = ts(30_000 + index)

        churning = _WindowTransport(count=60, on_request=churn, repo=repo)
        with self.assertRaises(SyncError):
            self._run(churning, repo)
        row = self.conn.execute(
            "SELECT listing_floor, listing_floor_active FROM sync_state"
            " WHERE repo = ?", (repo,)
        ).fetchone()
        self.assertTrue(row["listing_floor_active"])
        floor = row["listing_floor"]
        self.assertIsNotNone(floor, "the incremental floor must name a window")

        stable = _WindowTransport(count=60, repo=repo)
        result = self._run(stable, repo)
        self.assertIsNone(result["last_error"])
        self.assertEqual(
            stable.since_seen[0], floor,
            f"the next run must re-drain from the floor {floor!r}, not from"
            f" the watermark ({stable.since_seen[0]!r})",
        )
        after = self.conn.execute(
            "SELECT listing_floor, listing_floor_active FROM sync_state"
            " WHERE repo = ?", (repo,)
        ).fetchone()
        self.assertFalse(after["listing_floor_active"],
                         "the floor resolves once the re-drain completes")

    def test_force_drains_from_the_beginning_over_an_active_floor(self) -> None:
        """`--force` means a from-the-beginning re-read, which SUBSUMES an
        active floor's window: the floor may only ever RAISE a non-NULL
        watermark, never narrow a NULL one. Under the old read the floor
        won over the forced run's NULL watermark, so the forced sync read
        only the old floor window, reported `full: false`, and never ran
        the deletion pass. The floor itself survives a failed forced run —
        only a clean finish clears it."""
        repo = "acme/gw-forcefloor"
        seed = _WindowTransport(count=60, repo=repo)
        self._run(seed, repo)

        def churn(index: int, transport: _WindowTransport) -> None:
            transport.items[index % 60 + 1]["updated_at"] = ts(30_000 + index)

        with self.assertRaises(SyncError):
            self._run(_WindowTransport(count=60, on_request=churn, repo=repo), repo)
        self.assertTrue(self.conn.execute(
            "SELECT listing_floor_active FROM sync_state WHERE repo = ?",
            (repo,),
        ).fetchone()[0])

        forced = _WindowTransport(count=60, repo=repo)
        result = sync_repo(self.conn, _source(forced, repo), repo, force=True)
        self.assertIsNone(result["last_error"])
        self.assertIsNone(
            forced.since_seen[0],
            "a forced run must bind no since= at all: from the beginning,"
            f" not the floor ({forced.since_seen[0]!r})",
        )
        self.assertTrue(result["full"],
                        "a forced run starts a full sync, so it runs the"
                        " deletion pass and can stamp completion")
        after = self.conn.execute(
            "SELECT listing_floor, listing_floor_active FROM sync_state"
            " WHERE repo = ?", (repo,)
        ).fetchone()
        self.assertFalse(after["listing_floor_active"],
                         "the clean finish resolves the floor it subsumed")

    def test_transport_error_degrades_children_to_rest(self) -> None:
        """A typed transport failure on the bulk children query (the gh
        subprocess could not run or did not answer) is a degradable
        endpoint failure: the run finishes over the per-item REST path and
        records exactly one degradation row, instead of aborting. The typed
        class is what makes the distinction — a statusless GraphQL
        validation error must stay fatal (C3's other half)."""
        from zaxbygraph.github import GitHubTransportError

        class _TransportDegradeSource(FakeGitHubSource):
            """Bulk children die once with a typed transport failure; the
            per-item REST methods answer normally."""

            def __init__(self) -> None:
                super().__init__()
                self.bulk_calls = 0

            def fetch_children(self, items):
                self.bulk_calls += 1
                raise GitHubTransportError(
                    "gh api graphql timed out after 300s"
                )

        repo = "acme/gw-transport"
        src = _TransportDegradeSource()
        for n in (1, 2, 3):
            src.add_issue(issue(n, comments=1, updated_at=ts(n * 10)))
            src.issue_comments[n].append(
                comment(50_000 + n, f"note {n}", created_at=ts(n * 10 + 1))
            )
        result = sync_repo(self.conn, src, repo)
        self.assertIsNone(result["last_error"],
                          "a transport failure must degrade, not abort")
        self.assertEqual(
            result["source"], "rest (graphql unavailable)",
            "the envelope names the degraded value exactly as README"
            " documents it",
        )
        self.assertEqual(len(_stored(self.conn, repo)), 3)
        self.assertEqual(src.bulk_calls, 1,
                         "the bulk call is tried once, then REST takes over")
        notes = [
            r[0] for r in self.conn.execute(
                "SELECT note FROM fetch_log WHERE repo = ?"
                " AND resource = 'graphql'", (repo,)
            )
        ]
        self.assertEqual(len(notes), 1, "exactly one degradation row")
        self.assertIn("degraded to REST", notes[0])
        self.assertEqual(
            self.count(
                "SELECT COUNT(*) FROM comments WHERE repo = ? AND kind ="
                " 'issue_comment'", (repo,)
            ),
            3, "every item's children must arrive over the REST fallback",
        )


class _ScriptedWindowsTransport:
    """Serves a FIXED sequence of item sets — one per ``list_issues`` call,
    each a single short page (so no boundary re-read) — then repeats the
    last one. This is the only way to pin the pass-TERMINATION rule rather
    than the recovery rule: a live corpus cannot be made to deliver a strict
    subset of its predecessor on demand, so the windows are scripted."""

    def __init__(self, windows: list[list[int]], repo: str) -> None:
        self.windows = windows
        self.repo = repo
        self.calls = 0

    def __call__(self, path, paginate=False, _enrich_rate=True):
        if path.startswith(f"repos/{self.repo}/issues?"):
            window = self.windows[min(self.calls, len(self.windows) - 1)]
            self.calls += 1
            return [deepcopy(issue(n, comments=0, updated_at=ts(n)))
                    for n in window]
        if path.startswith(f"repos/{self.repo}/"):
            return []  # children and releases
        raise AssertionError(f"unexpected gh api path: {path!r}")


class PassTerminationTests(TempDBTest):
    def test_subset_termination_would_lose_a_live_item(self) -> None:
        """The walk ends on EXACT set equality, not on subset membership.

        Scripted windows: pass 1 delivers {1..8}; pass 2 delivers {1..7} —
        a strict subset, because live item 8 is skipped at a page
        boundary; pass 3 re-walks cleanly and delivers {1..8} again.

        Under subset termination the run would stop after pass 2
        ({1..7} is a subset of {1..8}) with live item 8 never stored and a
        clean success reported. Exact equality sees the sets differ, walks
        on, and stores it; pass 4 reproduces pass 3 and ends the run."""
        repo = "acme/gw-subset"
        transport = _ScriptedWindowsTransport(
            [list(range(1, 9)), list(range(1, 8)),
             list(range(1, 9)), list(range(1, 9))],
            repo,
        )
        result = sync_repo(self.conn, _source(transport, repo), repo)
        self.assertIsNone(result["last_error"])
        self.assertEqual(
            _stored(self.conn, repo), set(range(1, 9)),
            "the live item the second pass skipped must be stored — subset"
            " termination would have stopped before it was delivered",
        )
        self.assertEqual(
            transport.calls, 4,
            "pass1, pass2 (a strict subset), pass3, pass4 (equals pass3):"
            f" {transport.calls} walks",
        )


if __name__ == "__main__":
    unittest.main()
