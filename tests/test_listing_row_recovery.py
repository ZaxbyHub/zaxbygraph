"""Listing-row recovery checks (issue #88, collated trace 88-91).

Every check drives the REAL production seams -- ``sync_repo`` over a fake
listing page, the REAL ``GhApiSource`` REST walker with a stubbed ``_api``,
a bulk page source with ``fetch_children``, ``GraphQLSource
._fetch_children_chunk`` with a stubbed ``_graphql``, the torn-refresh and
torn-pending-retry seams, and the exact-number (``--numbers``) seam -- and
encodes the EXPECTED post-fix contract from the trace's issue summary: a
listing row the sync cannot parse (non-dict element, missing ``number``,
non-integer ``number``, missing/null/non-digit ``id``, on the listing, the
bulk-children, the torn-refresh, the torn-retry and the targeted paths) is
skipped AND recorded, never counted as ingested, never bundled into the
jsonl sidecar, pins the issue-#65 recovery floor with a ``last_error``,
and keeps the run answering incomplete until the row parses; the next
ordinary run re-drains from the pin and clears it once the row parses; a
targeted or forced run cannot launder the pin.

All checks are RED at the trace's base commit and GREEN after the fix.
Hermetic: FakeGitHubSource / stubbed ``_api`` only, no network, no gh
subprocess, no live GitHub, no cwd dependence. pytest collects the
module-level functions; the module-level ``load_tests`` bridge wraps the
same functions as unittest.FunctionTestCase so ``python -m unittest
discover -s tests`` runs them too.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, issue, pull

from zaxbygraph.cli import _freshness
from zaxbygraph.db import connect, init_schema
from zaxbygraph.github import GhApiSource
from zaxbygraph.graphql import GraphQLSource
from zaxbygraph.store import write_pending
from zaxbygraph.sync import sync_repo

#: An older recovery floor than any fixture stamp (AC10's pre-existing pin).
OLDER_FLOOR = "2025-12-31T00:00:00Z"

#: The pin a malformed-row skip persists (mirrors probe88_launder).
PIN_MESSAGE = "malformed listing row: skipped a row that cannot be parsed"

#: A stamp newer than every fixture stamp (torn-refresh / watermark legs).
FUTURE_STAMP = "2027-01-01T00:00:00Z"


class PageSource(FakeGitHubSource):
    """Serves one fixed listing page and honors ``since`` like the real
    listing does (the probe88_rest fixture pattern)."""

    def __init__(self, page: list) -> None:
        super().__init__()
        self.page = page

    def list_issues(self, since):
        page = [
            r for r in self.page
            if not isinstance(r, dict)
            or (since is None or str(r.get("updated_at", "")) >= since)
        ]
        if page:
            yield page


class BulkPageSource(PageSource):
    """A bulk-pages source whose ``fetch_children`` is the GraphQL bulk
    seam (issue #5 AC2): records every batch it is handed and answers no
    pre-provided children, so each row falls through to the per-item REST
    path like a degraded bulk page."""

    def __init__(self, page: list) -> None:
        super().__init__(page)
        self.bulk_batches: list[list] = []

    def fetch_children(self, batch):
        self.bulk_batches.append([dict(r) for r in batch])
        return {}


class ExactNumberSource(FakeGitHubSource):
    """A targeted-refresh source: ``get_issues`` answers the prescribed
    malformed observation ``(None, None)`` -- no row and no status, the
    unclassifiable-fetch shape a real ``gh`` payload cannot produce for a
    well-formed item (GhApiSource.get_issues maps a non-dict payload to
    exactly this)."""

    def get_issues(self, numbers):
        return {n: (None, None) for n in numbers}


def _bad_number_row(number: int = 2, updated_at: str | None = None) -> dict:
    """A listing row whose ``number`` is present but not an integer."""
    row = dict(issue(number, updated_at=updated_at))
    row["number"] = "not-a-number"
    return row


def _open_store():
    """A fresh initialized store; returns (cleanup, conn)."""
    td = tempfile.TemporaryDirectory(prefix="rowrecovery_",
                                     ignore_cleanup_errors=True)
    conn = connect(Path(td.name) / "history.db")
    init_schema(conn)
    return td, conn


def _events_dir(td) -> Path:
    """The run's jsonl events DIRECTORY (sync_repo's ``jsonl_path`` is a
    directory; the records land in ``events.jsonl`` inside it)."""
    return Path(td.name) / "sidecar"


def _sidecar(td) -> Path:
    """The run's jsonl event log file (one JSON object per line)."""
    return _events_dir(td) / "events.jsonl"


def _state(conn) -> dict:
    row = conn.execute(
        "SELECT last_error, listing_floor, listing_floor_active"
        " FROM sync_state WHERE repo = ?", (REPO,)).fetchone()
    return {} if row is None else dict(row)


def _items(conn) -> list:
    return [r[0] for r in conn.execute(
        "SELECT number FROM items WHERE repo = ? ORDER BY number", (REPO,))]


def _skip_records(conn) -> int:
    """fetch_log rows that record a skipped/unparseable listing row. The
    well-formed per-item plumbing writes only NULL notes and
    'children incomplete: ...' notes, so any other note is a skip record."""
    return int(conn.execute(
        "SELECT COUNT(*) FROM fetch_log"
        " WHERE note IS NOT NULL AND note NOT LIKE 'children incomplete%'"
    ).fetchone()[0])


def _skip_note(conn) -> str | None:
    """The most recent skip record's note text (bounded-repr evidence)."""
    row = conn.execute(
        "SELECT note FROM fetch_log WHERE note IS NOT NULL"
        " AND note NOT LIKE 'children incomplete%' ORDER BY id DESC"
    ).fetchone()
    return None if row is None else row[0]


def _bundle_numbers(path: Path) -> list:
    """``payload["number"]`` of every item_bundle record in the sidecar."""
    out: list = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("resource") == "item_bundle":
            payload = rec.get("payload")
            if isinstance(payload, dict):
                out.append(payload.get("number"))
    return out


def test_non_int_number_pins_floor():
    """AC1 (#88): a REST row with a present-but-non-integer ``number`` is
    skipped, recorded (with a bounded note), pinned, never counted or
    bundled, and answers the run incomplete. RED at base (nothing pins the
    floor, last_error stays None); GREEN after the fix."""
    td, conn = _open_store()
    try:
        result = sync_repo(conn, PageSource([issue(1), _bad_number_row(), issue(3)]),
                           REPO, jsonl_path=_events_dir(td))
        assert _items(conn) == [1, 3], "good rows stored, malformed row not"
        assert _skip_records(conn) >= 1, "the skip is recorded"
        state = _state(conn)
        assert state["listing_floor_active"] == 1, "recovery floor pinned"
        assert result["last_error"] is not None, "run answers incomplete"
        # Accounting: the malformed row is never counted as ingested...
        assert result["ingested"] == 2, "malformed row never counted"
        # ...and never bundled into the replay sidecar.
        bundles = _bundle_numbers(_sidecar(td))
        assert set(bundles) == {1, 3}, "malformed row never bundled"
        assert len(bundles) == result["ingested"], \
            "one bundle per ingested row, no more"
        note = _skip_note(conn)
        assert note is not None and len(note) < 200, \
            "the skip note bounds the embedded repr"
    finally:
        conn.close()
        td.cleanup()


def test_missing_number_is_recorded():
    """AC2 (#88): a REST row with no ``number`` key is recorded (not
    silently dropped), pinned, never counted or bundled, and answers
    incomplete. RED at base (no record, no pin); GREEN after the fix."""
    td, conn = _open_store()
    try:
        no_number = {"id": 77_001, "title": "row with no number key",
                     "updated_at": "2026-01-01T12:00:05Z"}
        result = sync_repo(conn, PageSource([issue(1), no_number, issue(3)]),
                           REPO, jsonl_path=_events_dir(td))
        assert _items(conn) == [1, 3], "good rows stored, malformed row not"
        assert _skip_records(conn) >= 1, "the skip is recorded"
        state = _state(conn)
        assert state["listing_floor_active"] == 1, "recovery floor pinned"
        assert result["last_error"] is not None, "run answers incomplete"
        assert result["ingested"] == 2, "malformed row never counted"
        bundles = _bundle_numbers(_sidecar(td))
        assert set(bundles) == {1, 3}, "malformed row never bundled"
        assert len(bundles) == result["ingested"], \
            "one bundle per ingested row, no more"
    finally:
        conn.close()
        td.cleanup()


def test_non_dict_element_recorded():
    """AC3 (#88): a raw non-dict listing element in the REAL REST page is
    recorded (not silently dropped by the source filter or ``_batch``),
    pinned, never counted or bundled, and answers incomplete. RED at base
    (the real ``GhApiSource.list_issues`` filters non-dicts before the
    sync ever sees them, so nothing is recorded); GREEN after the fix."""
    td, conn = _open_store()
    try:
        source = GhApiSource("acme", "forgegate")
        page = [issue(1), "not-a-dict", issue(3)]  # short page: len < 100

        def fake_api(path, paginate=False, _enrich_rate=True):
            if "/issues?" in path:      # the listing walker's page read
                return page
            return [] if paginate else {}   # per-item children / releases

        source._api = fake_api
        result = sync_repo(conn, source, REPO, jsonl_path=_events_dir(td))
        assert _items(conn) == [1, 3], "good rows stored, malformed row not"
        assert _skip_records(conn) >= 1, "the non-dict element is recorded"
        state = _state(conn)
        assert state["listing_floor_active"] == 1, "recovery floor pinned"
        assert result["last_error"] is not None, "run answers incomplete"
        assert result["ingested"] == 2, "non-dict element never counted"
        bundles = _bundle_numbers(_sidecar(td))
        assert set(bundles) == {1, 3}, "non-dict element never bundled"
        assert len(bundles) == result["ingested"], \
            "one bundle per ingested row, no more"
    finally:
        conn.close()
        td.cleanup()


def test_bulk_chunk_skips_malformed_number():
    """AC4 (#88): the bulk GraphQL child builder skips-and-logs a
    non-integer ``number`` instead of aborting the chunk, the malformed
    row contributes no alias and no children, and the end-to-end bulk page
    carrying the same shape is skipped+recorded+pinned with the good rows
    still ingested. RED at base (ValueError escapes
    ``_fetch_children_chunk`` on the direct leg); GREEN after the fix."""
    source = GraphQLSource("acme", "forgegate")
    captured: dict = {}

    def fake_graphql(query, aliases=None, *, stripped=False):
        captured["query"] = query
        captured["aliases"] = dict(aliases or {})
        # The real _graphql resets this before returning; the stub owes the
        # caller the same instance state.
        source.last_dropped_sections = {}
        return {"repository": {}}

    source._graphql = fake_graphql
    chunk = [
        {"number": "not-a-number", "updated_at": "2026-01-01T12:00:00Z"},
        {"number": 2, "updated_at": "2026-01-01T12:00:20Z"},
    ]
    out = source._fetch_children_chunk(chunk)
    assert "i2: issue(number: 2)" in captured["query"], \
        "the well-formed row still gets its alias"
    assert "not-a-number" not in captured["query"], \
        "the malformed number is never rendered into the query"
    assert set(out) <= {2}, \
        "the malformed row contributes no children entry"

    # End-to-end: the same malformed shape inside a bulk listing page.
    td, conn = _open_store()
    try:
        bulk = BulkPageSource([
            issue(1),
            {"id": 9002, "number": "not-a-number",
             "updated_at": "2026-01-01T12:00:20Z"},
            issue(3),
        ])
        result = sync_repo(conn, bulk, REPO, jsonl_path=_events_dir(td))
        assert _items(conn) == [1, 3], "good rows stored, malformed row not"
        assert _skip_records(conn) >= 1, "the skip is recorded"
        state = _state(conn)
        assert state["listing_floor_active"] == 1, "recovery floor pinned"
        assert result["last_error"] is not None, "run answers incomplete"
        assert result["ingested"] == 2, "malformed row never counted"
        assert set(_bundle_numbers(_sidecar(td))) == {1, 3}, \
            "malformed row never bundled"
    finally:
        conn.close()
        td.cleanup()


def test_missing_id_skips_and_records():
    """AC5 (#88): a listing row with a valid ``number`` but a bad ``id``
    (missing, JSON-null, non-digit) is skipped-and-recorded rather than
    raising out of ``sync_repo`` -- on the main listing path, the
    torn-refresh path (a re-fetched row that must not mix with a newer
    pull payload), and the pending torn-retry path (whose pending row must
    SURVIVE). RED at base (KeyError 'id' escapes the main path first);
    GREEN after the fix."""
    def _assert_skipped(conn, result, good, sidecar):
        assert _items(conn) == good, "good rows stored, id-less row not"
        assert _skip_records(conn) >= 1, "the skip is recorded"
        state = _state(conn)
        assert state["listing_floor_active"] == 1, "recovery floor pinned"
        assert result["last_error"] is not None, "run answers incomplete"
        assert result["ingested"] == len(good), "id-less row never counted"
        assert 2 not in _bundle_numbers(sidecar), \
            "id-less row never bundled"

    # Main path: the ``id`` key is absent. This leg is FIRST so the base
    # signature stays the KeyError the issue claims.
    td, conn = _open_store()
    try:
        no_id = dict(issue(2))
        del no_id["id"]
        result = sync_repo(conn, PageSource([issue(1), no_id, issue(3)]),
                           REPO, jsonl_path=_events_dir(td))
        _assert_skipped(conn, result, [1, 3], _sidecar(td))
    finally:
        conn.close()
        td.cleanup()

    # JSON-null ``id``.
    td, conn = _open_store()
    try:
        null_id = dict(issue(2))
        null_id["id"] = None
        result = sync_repo(conn, PageSource([issue(1), null_id, issue(3)]),
                           REPO, jsonl_path=_events_dir(td))
        _assert_skipped(conn, result, [1, 3], _sidecar(td))
    finally:
        conn.close()
        td.cleanup()

    # Non-digit ``id``.
    td, conn = _open_store()
    try:
        bad_id = dict(issue(2))
        bad_id["id"] = "xyz"
        result = sync_repo(conn, PageSource([issue(1), bad_id, issue(3)]),
                           REPO, jsonl_path=_events_dir(td))
        _assert_skipped(conn, result, [1, 3], _sidecar(td))
    finally:
        conn.close()
        td.cleanup()

    # Torn-refresh path: the PR's pull payload is NEWER than its listing
    # row, so the sync re-fetches the row through the exact-number seam;
    # the refreshed row has no ``id`` and must be skipped, not ingested.
    class RefreshSource(FakeGitHubSource):
        def __init__(self, refreshed: dict) -> None:
            super().__init__()
            self.refreshed = refreshed

        def get_issues(self, numbers):
            return {n: (self.refreshed, 200) for n in numbers}

    td, conn = _open_store()
    try:
        refreshed = dict(issue(2, kind="pr", updated_at="2026-01-01T12:01:00Z"))
        del refreshed["id"]
        src = RefreshSource(refreshed)
        pr_row = issue(2, kind="pr", updated_at="2026-01-01T12:00:10Z")
        pull_raw = pull(2)
        pull_raw["updated_at"] = "2026-01-01T12:00:50Z"   # newer than listing
        src.add_pr(pr_row, pull_raw, files=[])
        src.add_issue(issue(1))
        result = sync_repo(conn, src, REPO, jsonl_path=_events_dir(td))
        assert _items(conn) == [1], "good item still ingested, PR skipped"
        assert _skip_records(conn) >= 1, "the skip is recorded"
        state = _state(conn)
        assert state["listing_floor_active"] == 1, "recovery floor pinned"
        assert result["last_error"] is not None, "run answers incomplete"
        assert result["ingested"] == 1, "id-less refreshed row never counted"
        assert 2 not in _bundle_numbers(_sidecar(td)), \
            "id-less refreshed row never bundled"
    finally:
        conn.close()
        td.cleanup()

    # Pending torn-retry path: a durable torn obligation whose exact-number
    # refresh answers a missing-id row. The skip must not lose the debt.
    class PendingRetrySource(FakeGitHubSource):
        def get_issues(self, numbers):
            row = dict(issue(2))
            del row["id"]
            return {n: (row, 200) for n in numbers}

    td, conn = _open_store()
    try:
        write_pending(conn, REPO, [2], "torn")
        src = PendingRetrySource()
        src.add_issue(issue(1))
        result = sync_repo(conn, src, REPO, jsonl_path=_events_dir(td))
        assert _items(conn) == [1], "good item still ingested, owed row not"
        assert _skip_records(conn) >= 1, "the skip is recorded"
        state = _state(conn)
        assert state["listing_floor_active"] == 1, "recovery floor pinned"
        assert result["last_error"] is not None, "run answers incomplete"
        torn_left = int(conn.execute(
            "SELECT COUNT(*) FROM sync_pending"
            " WHERE repo = ? AND number = 2 AND reason = 'torn'",
            (REPO,)).fetchone()[0])
        assert torn_left == 1, "the pending torn row survives the skip"
        assert 2 not in _bundle_numbers(_sidecar(td)), \
            "id-less owed row never bundled"
    finally:
        conn.close()
        td.cleanup()


def test_corrected_row_recovers_without_force():
    """AC6 (#88): once the previously unparseable row parses -- with its
    ``updated_at`` UNCHANGED -- the next ordinary sync ingests it (and
    bundles it) and clears the floor (the run re-drains from the pin; the
    re-drained window also re-serves the boundary good row, so run 2's
    ingested count is at least the corrected row). RED at base (the
    corrected row is never re-served, items stay [1, 3]); GREEN after the
    fix."""
    td, conn = _open_store()
    try:
        sidecar = _sidecar(td)
        run1 = sync_repo(conn, PageSource(
            [issue(1), _bad_number_row(), issue(3)]), REPO, jsonl_path=_events_dir(td))
        assert run1["ingested"] == 2, "malformed row never counted in run 1"
        assert 2 not in _bundle_numbers(sidecar), \
            "malformed row never bundled in run 1"
        # Captured (not yet asserted) so the anchored run-2 assertion below
        # stays the first base failure: run 2 clears the floor, so the pin
        # can only be observed between the runs.
        pinned_after_run1 = _state(conn)["listing_floor_active"] == 1
        # Same row corrected, updated_at unchanged (the sealing probe).
        run2 = sync_repo(conn, PageSource(
            [issue(1), issue(2), issue(3)]), REPO, jsonl_path=_events_dir(td))
        assert _items(conn) == [1, 2, 3], "corrected row recovered"
        assert _state(conn)["listing_floor_active"] == 0, "floor cleared"
        assert pinned_after_run1, "run 1 pinned the floor"
        assert run2["last_error"] is None, "run answers clean"
        assert run2["ingested"] >= 1, "the corrected row is counted"
        assert 2 in _bundle_numbers(sidecar), "corrected row is bundled"
    finally:
        conn.close()
        td.cleanup()


def test_floor_stays_pinned_until_fixed():
    """AC7 (#88): while the row stays unparseable, every ordinary run keeps
    the floor pinned and answers incomplete, and never counts or bundles
    the malformed row. RED at base (both runs finish clean); GREEN after
    the fix."""
    td, conn = _open_store()
    try:
        page = [issue(1), _bad_number_row(), issue(3)]
        run1 = sync_repo(conn, PageSource(page), REPO, jsonl_path=_events_dir(td))
        assert run1["ingested"] == 2, "malformed row never counted in run 1"
        run2 = sync_repo(conn, PageSource(page), REPO, jsonl_path=_events_dir(td))
        state = _state(conn)
        assert state["listing_floor_active"] == 1, "floor stays pinned"
        assert run2["last_error"] is not None, "run answers incomplete"
        # The re-drained window re-serves only boundary good rows; the
        # malformed row itself is never counted in either run.
        assert run2["ingested"] >= 1, "boundary good rows may re-count"
        assert _items(conn) == [1, 3], "malformed row never stored"
        assert 2 not in _bundle_numbers(_sidecar(td)), \
            "malformed row never bundled"
    finally:
        conn.close()
        td.cleanup()


def test_targeted_run_cannot_launder_floor():
    """AC8 (#88): a targeted (``numbers=[...]``) run finishing over an
    active malformed-row floor must not clear ``last_error`` or let the
    freshness envelope report complete -- neither alone, nor over an open
    torn obligation, and a targeted exact-number malformed observation
    (``get_issues`` answering ``(None, None)``) records and pins instead
    of passing silently. RED at base (the plain targeted finish clears
    last_error, complete reads True); GREEN after the fix."""
    td, conn = _open_store()
    try:
        seed = FakeGitHubSource()
        seed.add_issue(issue(1))
        sync_repo(conn, seed, REPO)
        # The pin a malformed-row skip would persist.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE sync_state SET listing_floor = ?, listing_floor_active = 1,"
            " last_error = ? WHERE repo = ?",
            ("2026-01-01T00:00:00Z", PIN_MESSAGE, REPO))
        conn.commit()
        refresh = FakeGitHubSource()
        refresh.add_issue(issue(1))
        result = sync_repo(conn, refresh, REPO, numbers=[1])
        state = _state(conn)
        assert state["last_error"] is not None, \
            "targeted run must not clear the pin's last_error"
        assert result["last_error"] is not None
        assert _freshness(conn, REPO)["complete"] is False, \
            "freshness must stay incomplete over an unresolved floor"
    finally:
        conn.close()
        td.cleanup()

    # The same targeted finish over the pin PLUS an open torn obligation:
    # the durable debt alone must not become the only thing keeping the
    # error -- but it certainly must not license clearing it either.
    td, conn = _open_store()
    try:
        seed = FakeGitHubSource()
        seed.add_issue(issue(1))
        sync_repo(conn, seed, REPO)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE sync_state SET listing_floor = ?, listing_floor_active = 1,"
            " last_error = ? WHERE repo = ?",
            ("2026-01-01T00:00:00Z", PIN_MESSAGE, REPO))
        conn.commit()
        write_pending(conn, REPO, [1], "torn")
        refresh = FakeGitHubSource()
        refresh.add_issue(issue(1))
        sync_repo(conn, refresh, REPO, numbers=[1])
        assert _state(conn)["last_error"] is not None, \
            "an open obligation must not license clearing the error either"
    finally:
        conn.close()
        td.cleanup()

    # A targeted exact-number malformed observation: the source answers
    # (None, None) for the requested number -- no row, no status.
    td, conn = _open_store()
    try:
        result = sync_repo(conn, ExactNumberSource(), REPO, numbers=[1])
        assert result["ingested"] == 0, "an unclassifiable row is not ingested"
        state = _state(conn)
        assert state["listing_floor_active"] == 1, "recovery floor pinned"
        assert result["last_error"] is not None, "run answers incomplete"
        assert _skip_records(conn) >= 1, "the observation is recorded"
    finally:
        conn.close()
        td.cleanup()


def test_forced_run_over_malformed_listing_pins_floor():
    """AC9 (#88): a forced run completing over a listing that still carries
    an unparseable row pins the floor and answers incomplete, never
    counting or bundling the malformed row. RED at base (the forced run
    finishes clean); GREEN after the fix."""
    td, conn = _open_store()
    try:
        result = sync_repo(
            conn, PageSource([issue(1), _bad_number_row(), issue(3)]),
            REPO, force=True, jsonl_path=_events_dir(td))
        state = _state(conn)
        assert state["listing_floor_active"] == 1, "recovery floor pinned"
        assert result["last_error"] is not None, "run answers incomplete"
        assert _freshness(conn, REPO)["complete"] is False
        assert result["ingested"] == 2, "malformed row never counted"
        assert set(_bundle_numbers(_sidecar(td))) == {1, 3}, \
            "malformed row never bundled"
    finally:
        conn.close()
        td.cleanup()


def test_older_floor_still_honored():
    """AC10 (#88): with an older pre-existing recovery floor active and a
    malformed row also present, the pin keeps the OLDER floor (min/earlier,
    never replaced) and stays active after the run. RED at base (the
    finalizer clears the floor outright); GREEN after the fix."""
    td, conn = _open_store()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO sync_state(repo, listing_floor, listing_floor_active,"
            " last_error) VALUES (?, ?, 1, ?)",
            (REPO, OLDER_FLOOR, PIN_MESSAGE))
        conn.commit()
        # The malformed row's updated_at (ts(20) = 2026-01-01T12:00:20Z) is
        # NEWER than the seeded floor; the pin must keep the older value.
        result = sync_repo(
            conn, PageSource([issue(1), _bad_number_row(), issue(3)]), REPO)
        state = _state(conn)
        assert state["listing_floor_active"] == 1, "older floor stays honored"
        assert state["listing_floor"] == OLDER_FLOOR, \
            "the pin takes the earlier floor, never replaces it"
        assert result["last_error"] is not None, "run answers incomplete"
    finally:
        conn.close()
        td.cleanup()


_CHECKS = [
    test_non_int_number_pins_floor,
    test_missing_number_is_recorded,
    test_non_dict_element_recorded,
    test_bulk_chunk_skips_malformed_number,
    test_missing_id_skips_and_records,
    test_corrected_row_recovers_without_force,
    test_floor_stays_pinned_until_fixed,
    test_targeted_run_cannot_launder_floor,
    test_forced_run_over_malformed_listing_pins_floor,
    test_older_floor_still_honored,
]


def load_tests(loader, standard_tests, pattern):
    """Bridge so ``python -m unittest discover -s tests`` runs the same
    module-level checks pytest collects (published argv parity)."""
    suite = unittest.TestSuite(standard_tests)
    for fn in _CHECKS:
        suite.addTest(unittest.FunctionTestCase(fn))
    return suite
