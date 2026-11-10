"""Frozen acceptance checks: sync verdicts (extraction-fault containment,
real deletions, deletion-pass honesty, repository-access failures).

Authored at arm's length from the acceptance criteria and the verified root
causes only; every check here is DISCRIMINATING, and each docstring stays
true at both ends: RED at base means the check fails for the SEMANTIC reason
named there; GREEN after the fix means the same assertions hold.

Offline discipline (repo invariant): no network, no live `gh`. Every source
is FakeGitHubSource-based; extraction faults are injected at the seams
store.ingest_item calls per item (edges_from_item / edges_from_comment),
and the deletion oracle mirrors the real GraphQL existence probe's
production chunking and its refusal to mass-mark an uncorroborated chunk.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fixtures import REPO, FakeGitHubSource, TempDBTest, comment, issue, ts
from zaxbygraph.github import GitHubError
from zaxbygraph.graphql import GRAPHQL_DELETED_PAGE
from zaxbygraph.sync import SyncError, sync_repo


class OversizedReferenceTests(TempDBTest):
    def test_oversized_digit_reference_does_not_wedge(self) -> None:
        """RED at base, GREEN after the fix: an item whose body contains a
        # reference longer than any sane issue number (here 5000 digits) is
        skipped as a token -- producing NO edge at all (not a reference to
        a truncated number) -- while a valid #5-style reference in the SAME
        payload still produces its edge; the item itself is ingested and
        the watermark advances past it. At base the int conversion raises
        ValueError: Exceeds the limit (4300 digits) and the run aborts with
        the watermark stuck before the item, so every later attempt dies on
        it again."""
        body = "see #" + "9" * 5000 + " and fixes #5"
        self.src.add_issue(issue(1, title="one", updated_at=ts(10)))
        self.src.add_issue(issue(2, title="huge", body=body, updated_at=ts(20)))
        self.src.add_issue(issue(3, title="three", updated_at=ts(30)))
        try:
            result = self.sync()
        except ValueError as exc:
            self.fail("an oversized #<digits> token must be skipped, not wedge "
                      f"the run on int conversion: {exc}")
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 3,
                         "the item carrying the oversized token must be ingested")
        stored_body = self.conn.execute(
            "SELECT body FROM items WHERE number = 2").fetchone()[0]
        self.assertIn("9" * 5000, stored_body, "the payload itself is stored verbatim")
        referenced = {
            str(r[0])
            for r in self.conn.execute(
                "SELECT DISTINCT dst_id FROM edges WHERE repo = ? "
                "AND src_type = 'item' AND src_id = '2'", (REPO,))
        }
        self.assertEqual(
            referenced, {"5"},
            "the oversized token must produce no edge (no truncated-number "
            f"reference) while the valid #5 reference still lands: {referenced}")
        self.assertEqual(
            result["issues_since"], ts(30),
            "the watermark must advance past the item carrying the oversized "
            "token (stuck at "
            f"{self.conn.execute('SELECT issues_since FROM sync_state WHERE repo = ?', (REPO,)).fetchone()[0]!r})")


def _real(module: str, name: str):
    import importlib

    return getattr(importlib.import_module(module), name)


class ExtractionFaultTests(TempDBTest):
    def test_extraction_fault_is_logged_not_fatal(self) -> None:
        """RED at base, GREEN after the fix, three cases (the general form
        of the oversized-token check): a deterministic extraction fault for
        ONE item is contained -- the item's rows are ingested without the
        failed extraction, the fault is logged to fetch_log naming it, the
        run continues, and the watermark advances past the item. The same
        containment for a COMMENT-level fault must keep the item's own
        edges and the other comments' edges. A sqlite3 storage fault during
        the item's transaction is NOT contained: it must still abort the
        run. At base every extraction fault aborts the run at the item."""
        for n in (1, 2, 3):
            self.src.add_issue(issue(n, updated_at=ts(n * 10)))

        def exploding_edges(repo, raw):
            if int(raw["number"]) == 2:
                raise ValueError("deterministic extraction fault marker")
            return _real("zaxbygraph.extract", "edges_from_item")(repo, raw)

        # Case 1: first-emitter fault for one item.
        with patch("zaxbygraph.store.edges_from_item", side_effect=exploding_edges):
            try:
                result = self.sync()
            except ValueError as exc:
                self.fail("an extraction fault for one item must be contained "
                          f"and logged, never fatal to the run: {exc}")
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 3,
                         "the faulted item's rows must still be ingested")
        self.assertGreaterEqual(
            self.count("SELECT COUNT(*) FROM fetch_log WHERE resource = 'item' "
                       "AND resource_id = '2' AND lower(note) LIKE "
                       "'%deterministic extraction fault%'"),
            1, "the contained fault must be logged to fetch_log for the item")
        self.assertEqual(result["issues_since"], ts(30),
                         "the watermark must advance past the faulted item")

        # Case 2: COMMENT extraction fault for one comment; everything else lands.
        self.src.add_issue(issue(6, title="six", author="alice", updated_at=ts(60)))
        for cid, body, author in (
            (50_101, "see #3", "carol"),   # the fault target
            (50_102, "see #4", "dave"),    # the healthy sibling
        ):
            self.src.issue_comments[6].append(comment(cid, body, author=author))
        self.src.issues[6]["comments"] = 2

        def exploding_comments(repo, number, raw):
            if int(raw.get("id") or 0) == 50_101:
                raise ValueError("deterministic extraction fault marker")
            return _real("zaxbygraph.extract", "edges_from_comment")(
                repo, number, raw)

        with patch("zaxbygraph.store.edges_from_comment",
                   side_effect=exploding_comments):
            try:
                result = self.sync()
            except ValueError as exc:
                self.fail("a comment-level extraction fault must be contained, "
                          f"never fatal to the run: {exc}")
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM items WHERE number = 6").fetchone(),
            "the item whose comment faulted must still be ingested")
        # Rows do not hash like tuples (the connection's row_factory is
        # sqlite3.Row), so the set must hold real tuples for assertIn.
        edges = {tuple(e) for e in self.conn.execute(
            "SELECT src_type, src_id, rel, dst_id FROM edges WHERE repo = ? "
            "AND (dst_id = '6' OR (src_type = 'item' AND src_id = '6'))",
            (REPO,))}
        self.assertIn(("actor", "alice", "authored", "6"), edges,
                      "the item's OWN edges must still land")
        self.assertIn(("actor", "dave", "commented", "6"), edges,
                      "the healthy comment's edges must still land")
        self.assertIn(("item", "6", "mentions", "4"), edges,
                      "the healthy comment's reference must still land")
        self.assertNotIn(("actor", "carol", "commented", "6"), edges,
                         "the faulted comment's edges are the contained part")
        self.assertGreaterEqual(
            self.count("SELECT COUNT(*) FROM fetch_log WHERE resource = 'item' "
                       "AND resource_id = '6' AND lower(note) LIKE "
                       "'%deterministic extraction fault%'"),
            1, "the contained comment fault must be logged to fetch_log")

        # Case 3: a STORAGE fault is not containable -- the run must abort.
        self.src.add_issue(issue(7, title="seven", updated_at=ts(70)))

        def storage_fault(repo, raw):
            if int(raw["number"]) == 7:
                import sqlite3

                raise sqlite3.OperationalError("disk I/O error")
            return _real("zaxbygraph.extract", "edges_from_item")(repo, raw)

        with patch("zaxbygraph.store.edges_from_item", side_effect=storage_fault):
            with self.assertRaises(SyncError) as ctx:
                self.sync()
        self.assertIn("disk I/O error", str(ctx.exception))
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM items WHERE number = 7").fetchone(),
            "the item whose transaction hit the storage fault must not be stored")
        err = self.conn.execute(
            "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)).fetchone()[0]
        self.assertIn("disk I/O error", err or "",
                      "the storage fault must be recorded, not swallowed")


class _CorroboratingOracleSource(FakeGitHubSource):
    """A deletion oracle mirroring the CORRECTED GraphQL existence probe
    seam (graphql.py check_deleted): `check_deleted(numbers,
    corroborator=None)` owns the production chunking
    (GRAPHQL_DELETED_PAGE numbers per chunk) and is the SINGLE fallback
    owner. The exact truth table: a legacy one-argument call keeps the
    base all-nothing refusal; an offered witness that RESOLVES corroborates
    its chunk (the witness is excluded from verdicts); an offered witness
    that resolves NOTHING (dead -- deleted since the listing) raises, the
    same evidentiary class as the base refusal; and a chunk with no witness
    available (the callable returned None, the all-gone window) resolves
    through ONE probe_repository() reachability read -- readable marks the
    chunk's gone items, unreadable raises. The never-offered call shape
    (corroborator None, the one-argument legacy contract the BASE sync
    speaks) keeps the production refusal -- that refusal is what makes
    this check RED at base. Records, per chunk, the resolution set
    (candidates + witness) and what the witness callable returned."""

    def __init__(self, live: set[int], gone: set[int],
                 corroborator_dies: bool = False) -> None:
        super().__init__()
        self.live = set(live)
        self.gone = set(gone)
        self.corroborator_dies = corroborator_dies
        self.chunks: list[list[int]] = []
        self.resolutions: list[list[int]] = []
        self.corroborators: list[int | None] = []
        self.probe_calls = 0
        self.probe_readable = True

    def check_deleted(self, numbers, corroborator=None) -> dict[int, str]:
        verdicts: dict[int, str] = {}
        nums = sorted({int(n) for n in numbers})
        for start in range(0, len(nums), GRAPHQL_DELETED_PAGE):
            chunk = nums[start:start + GRAPHQL_DELETED_PAGE]
            offered = callable(corroborator)
            corrob = corroborator() if offered else None
            corrob = int(corrob) if corrob is not None else None
            self.chunks.append(chunk)
            self.corroborators.append(corrob)
            self.resolutions.append(sorted(set(chunk) | ({corrob} if corrob is not None else set())))
            if corrob is not None and corrob not in self.live:
                # Dead witness: offered but absent from the repository --
                # the same evidentiary class as the base all-nothing refusal.
                raise GitHubError(
                    "deletion probe witness resolved no nodes; "
                    "refusing to mark items gone without corroboration")
            corroborated = any(n in self.live for n in chunk) or (
                corrob is not None and corrob in self.live)
            if not corroborated:
                if not offered:  # legacy one-argument call: no witness
                    raise GitHubError(  # machinery offered at all
                        "deletion probe resolved no nodes for a whole chunk; "
                        "refusing to mark items gone without corroboration")
                self.probe_repository()  # the oracle's OWN fallback: readable
                # marks the chunk's gone items, unreadable raises
            if corrob is not None and self.corroborator_dies:
                self.live.discard(corrob)  # it was deleted between the chunks
            for n in chunk:
                if n in self.gone:
                    verdicts[n] = "deleted"
        return verdicts

    def probe_repository(self) -> bool:
        self.probe_calls += 1
        if not self.probe_readable:
            raise GitHubError("repository unreadable HTTP 404", status=404)
        return True


class _LegacyOracleSource(FakeGitHubSource):
    """A LEGACY one-argument deletion oracle (the DeletingSource shape from
    tests/test_sync_deletion.py): it takes exactly `numbers`, resolves the
    deletion itself, and must keep working through the corrected sync's
    signature-inspection adapter -- being handed a `corroborator` it never
    accepted would raise TypeError and lose the verdicts."""

    def __init__(self) -> None:
        super().__init__()
        self.deletions: dict[int, str] = {}
        self.calls: list[list[int]] = []

    def check_deleted(self, numbers) -> dict[int, str]:
        self.calls.append(sorted(int(n) for n in numbers))
        return {n: self.deletions[n] for n in numbers if n in self.deletions}


class RealDeletionTests(TempDBTest):
    """One helper per scenario, all against this store under distinct repo
    slugs so each full sync starts from its own corpus."""

    def _states(self, repo: str) -> dict[int, str]:
        return {int(r[0]): r[1]
                for r in self.conn.execute(
                    "SELECT number, state FROM items WHERE repo = ?", (repo,))}

    def _drain_and_mark(self, repo: str, src: _CorroboratingOracleSource,
                        remove: range) -> None:
        for n in remove:
            del src.issues[n]
        try:
            sync_repo(self.conn, src, repo, force=True)
        except SyncError as exc:
            self.fail(f"deletion verdicts must be recorded, not refused: {exc}")

    def _seed_and_sync(self, src: _CorroboratingOracleSource, repo: str,
                       numbers: range) -> None:
        for n in numbers:
            src.add_issue(issue(n, updated_at=ts(n)))
        sync_repo(self.conn, src, repo)

    def test_real_deletion_is_recorded(self) -> None:
        """RED at base, GREEN after the fix, six scenarios; the corrected
        contract: check_deleted(candidates, corroborator=None) owns the
        50-number chunking and is the SINGLE fallback owner -- an offered
        witness that resolves corroborates its chunk, an offered witness
        that resolves NOTHING (dead) raises like the base refusal, and a
        chunk with no witness available resolves through ONE
        probe_repository() reachability read (readable marks, unreadable
        raises); the sync passes its corroborator witness only to oracles
        whose signature accepts one. Single chunk: when a full sync finds
        stored items absent from the drained listing and they are really
        gone, the run marks them deleted, each probe chunk's resolution
        carrying a known-live corroborator excluded from the verdicts. At
        base the pass hands the oracle the candidates with no witness
        machinery at all, the production refusal fires, and the run dies
        with SyncError: deletion probe resolved no nodes for a whole
        chunk. Multi-chunk: with more candidates than the production probe
        chunk size, EVERY chunk's resolution must carry a known-live
        corroborator that is not a candidate, no corroborator is ever
        marked deleted, and all genuinely-deleted items are marked.
        All-gone, readable: no known-live number exists while the
        repository itself is readable -- the items are marked deleted
        backed by exactly ONE probe_repository() reachability read.
        All-gone, unreadable: the reachability read fails, the run raises,
        nothing is marked. Dead witness: the sole live number corroborates
        chunk 1 and is gone by chunk 2, so the later chunk's offered
        witness resolves nothing -- the run RAISES, the in-flight verdict
        set is discarded (nothing marked, nothing fabricated), and every
        candidate stays in the durable pending set for the next run's
        retry. Mixed legacy/new: a one-argument oracle keeps working
        through the sync's signature adapter (no TypeError, its verdicts
        still land)."""
        # Single chunk: candidates {3, 4}, live {1, 2}.
        self.src = _CorroboratingOracleSource(live={1, 2}, gone={3, 4})
        self._seed_and_sync(self.src, REPO, range(1, 5))
        self._drain_and_mark(REPO, self.src, remove=range(3, 5))
        states = self._states(REPO)
        self.assertEqual(states[3], "deleted", "gone item 3 must be marked deleted")
        self.assertEqual(states[4], "deleted", "gone item 4 must be marked deleted")
        self.assertEqual((states[1], states[2]), ("open", "open"))
        self.assertTrue(self.src.chunks, "the deletion oracle must be consulted")
        self.assertTrue(any(any(n in (1, 2) for n in r) for r in self.src.resolutions),
                        f"the probe chunk's resolution must carry a known-live corroborator (resolutions: {self.src.resolutions})")

        # Multi-chunk: 70 gone candidates across >1 production chunks, 60 live.
        many_repo = "acme/many"
        many = _CorroboratingOracleSource(live=set(range(1, 61)), gone=set(range(61, 131)))
        self._seed_and_sync(many, many_repo, range(1, 131))
        self._drain_and_mark(many_repo, many, remove=range(61, 131))
        states = self._states(many_repo)
        self.assertTrue(all(states[n] == "deleted" for n in range(61, 131)),
                        "every genuinely-deleted item must be marked across all probe chunks")
        self.assertTrue(all(states[n] == "open" for n in range(1, 61)),
                        "no corroborator (a known-live number) may ever be marked deleted")
        self.assertGreaterEqual(len(many.chunks), 2,
                                f"the candidate set must exercise multiple probe chunks ({GRAPHQL_DELETED_PAGE}-size chunking); chunks: {many.chunks}")
        candidates = set(range(61, 131))
        for resolution, corrob in zip(many.resolutions, many.corroborators):
            self.assertTrue(any(n not in candidates and n in many.live for n in resolution),
                            f"EVERY chunk's resolution must carry a corroborator live number that is not a candidate (resolution: {resolution})")
            if corrob is not None:
                self.assertEqual(states[corrob], "open", f"the corroborator {corrob} must never be marked deleted")

        # All-gone, readable: no known-live number; exactly one reachability read.
        ghost_repo = "acme/ghosted"
        ghost = _CorroboratingOracleSource(live=set(), gone=set(range(1, 6)))
        self._seed_and_sync(ghost, ghost_repo, range(1, 6))
        self._drain_and_mark(ghost_repo, ghost, remove=range(1, 6))
        states = self._states(ghost_repo)
        self.assertTrue(all(s == "deleted" for s in states.values()),
                        f"an all-gone corpus in a readable repository must be marked deleted via repository-reachability corroboration (states: {states})")
        self.assertEqual(ghost.probe_calls, 1, "exactly one probe_repository() reachability read must back the all-gone marking")

        # All-gone, unreadable: the reachability read fails, nothing is marked.
        blind_repo = "acme/blind"
        blind = _CorroboratingOracleSource(live=set(), gone=set(range(1, 4)))
        blind.probe_readable = False
        self._seed_and_sync(blind, blind_repo, range(1, 4))
        for n in range(1, 4):
            del blind.issues[n]
        with self.assertRaises(SyncError):
            sync_repo(self.conn, blind, blind_repo, force=True)
        self.assertEqual(blind.probe_calls, 1, "the unreadable reachability read must have been attempted")
        self.assertTrue(all(s == "open" for s in self._states(blind_repo).values()),
                        "an unreadable repository must mark nothing gone")

        # Dead witness between listing and probe: the sole live number
        # corroborates chunk 1 and is gone by chunk 2, so the later chunk's
        # offered witness resolves nothing -- the same evidentiary class as
        # the base all-nothing refusal. The run must RAISE, mark nothing
        # this run (the in-flight verdict set is discarded -- nothing is
        # fabricated), and leave every candidate in the durable pending set
        # so the next run retries them (no corroborated verdict is lost).
        dying_repo = "acme/dying"
        dying = _CorroboratingOracleSource(live={1}, gone=set(range(51, 102)), corroborator_dies=True)
        self._seed_and_sync(dying, dying_repo, range(1, 102))
        for n in range(51, 102):
            del dying.issues[n]
        with self.assertRaises(SyncError):
            sync_repo(self.conn, dying, dying_repo, force=True)
        self.assertGreaterEqual(len(dying.chunks), 2, "the fixture must span more than one probe chunk")
        self.assertIsNotNone(dying.corroborators[0], "the first chunk must have been corroborated")
        self.assertEqual(dying.probe_calls, 0,
                         "a dead witness raises -- it does not fall back to the reachability probe")
        states = self._states(dying_repo)
        self.assertTrue(all(s == "open" for s in states.values()),
                        "a raise discards the in-flight verdict set: nothing is marked and nothing is fabricated")
        self.assertEqual(states[1], "open", "the dead corroborator itself must never be marked deleted")
        pending = {int(r[0]) for r in self.conn.execute(
            "SELECT number FROM sync_pending WHERE repo = ? AND reason = 'deletion'",
            (dying_repo,))}
        self.assertTrue(set(range(51, 102)) <= pending,
                        f"every candidate must stay in the durable pending set for retry (pending: {sorted(pending)})")

        # Mixed legacy/new oracle: a one-argument oracle (no corroborator
        # parameter) still works through the sync's signature adapter -- a
        # normal deletion it resolves itself must land, with no TypeError
        # from a corroborator the oracle never accepted.
        legacy_repo = "acme/legacy"
        legacy = _LegacyOracleSource()
        for n in range(1, 4):
            legacy.add_issue(issue(n, updated_at=ts(n)))
        sync_repo(self.conn, legacy, legacy_repo)
        del legacy.issues[2]
        legacy.deletions[2] = "deleted"
        try:
            sync_repo(self.conn, legacy, legacy_repo, force=True)
        except (SyncError, TypeError) as exc:
            self.fail(f"a legacy one-argument oracle must keep working through the signature "
                      f"adapter, not crash on a passed corroborator: {exc}")
        self.assertTrue(legacy.calls, "the legacy oracle must be consulted")
        states = self._states(legacy_repo)
        self.assertEqual(states[2], "deleted", "the legacy oracle's own verdict must land")
        self.assertEqual((states[1], states[3]), ("open", "open"))


class _FlakyOracleSource(FakeGitHubSource):
    """Deletion oracle whose availability is toggled by the test: while
    `fail` is set the pass itself fails; once cleared it reports nothing
    gone."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = False

    def check_deleted(self, numbers) -> dict[int, str]:
        if self.fail:
            raise GitHubError("deletion oracle unavailable HTTP 502", status=502)
        return {}


class DeletionPassHonestyTests(TempDBTest):
    def test_failed_deletion_pass_is_not_laundered(self) -> None:
        """RED at base, GREEN after the fix: when the deletion pass of a
        full sync fails, full_sync_pending stays set (and last_error stays
        recorded) until a later run actually completes the pass -- a plain
        incremental sync must not clear the marker or launder the error,
        because it never re-runs the pass. At base the following plain sync
        stamps a full sync and clears both."""
        self.src = _FlakyOracleSource()
        self.src.add_issue(issue(1, updated_at=ts(10)))
        self.src.add_issue(issue(2, updated_at=ts(20)))
        self.sync()
        del self.src.issues[2]  # 2 leaves the listing: a deletion candidate
        self.src.fail = True
        with self.assertRaises(SyncError):
            self.sync(force=True)
        row = self.conn.execute(
            "SELECT full_sync_pending, last_error FROM sync_state WHERE repo = ?",
            (REPO,),
        ).fetchone()
        self.assertEqual(row["full_sync_pending"], 1, "the failed pass must stay pending")
        self.assertIsNotNone(row["last_error"])

        # A plain incremental sync must not launder the failed pass.
        self.src.fail = False
        self.src.comment_on(1, "bump")
        try:
            self.sync()
        except SyncError:
            pass  # failing loudly is acceptable; clearing the marker is not
        row = self.conn.execute(
            "SELECT full_sync_pending, last_error FROM sync_state WHERE repo = ?",
            (REPO,),
        ).fetchone()
        self.assertEqual(row["full_sync_pending"], 1,
                         "a plain sync that did not complete the deletion pass "
                         "must keep full_sync_pending set (a full sync was "
                         "laundered)")
        self.assertIsNotNone(row["last_error"],
                             "the recorded deletion-pass failure must survive "
                             "the plain sync")


class _Repo404Source(FakeGitHubSource):
    """Repository unreadable at the source: every per-number listing fetch
    comes back 404 (the shape a repo-level 404 produces through the real
    client's per-number catch)."""

    def get_issues(self, numbers):
        return {n: (None, 404) for n in numbers}


class RepoAccessFailureTests(TempDBTest):
    def test_numbers_repo_404_marks_nothing(self) -> None:
        """RED at base, GREEN after the fix: when sync --numbers runs
        against a repository the source cannot read (every requested number
        answers 404 because the repo itself is inaccessible), the run
        changes no item and exits non-zero naming the repository-access
        failure. At base the three live items are marked deleted and the
        run reports success."""
        for n in (1, 2, 3):
            self.src.add_issue(issue(n, updated_at=ts(n * 10)))
        self.sync()
        before = {
            int(r[0]): r[1]
            for r in self.conn.execute("SELECT number, state FROM items")
        }
        dead = _Repo404Source()
        with self.assertRaises(SyncError) as ctx:
            sync_repo(self.conn, dead, REPO, numbers=[1, 2, 3])
        message = str(ctx.exception).lower()
        self.assertTrue(
            "repositor" in message or "404" in message or "not found" in message,
            f"the failure must name the repository-access failure: {ctx.exception!r}")
        after = {
            int(r[0]): r[1]
            for r in self.conn.execute("SELECT number, state FROM items")
        }
        self.assertEqual(after, before,
                         "a repo-level read failure must change no item (live "
                         f"items were marked deleted: {after})")


if __name__ == "__main__":
    unittest.main()
