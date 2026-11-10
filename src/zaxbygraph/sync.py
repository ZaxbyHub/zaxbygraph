from __future__ import annotations

import inspect
import json
import os
import socket
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from zaxbygraph.extract import item_kind
from zaxbygraph.github import (
    GitHubError,
    GitHubSource,
    GitHubTransportError,
    ListingShiftedError,
)
from zaxbygraph.graphql import GH_TIMEOUT_S
from zaxbygraph.repo import validate_slug
from zaxbygraph.db import get_meta, set_meta
from zaxbygraph.store import (
    ISO_Z,
    bump_watermark,
    clear_pending,
    ingest_item,
    log_fetch,
    mark_sync_finished,
    pending_numbers,
    recount,
    replace_releases,
    set_last_error,
    utcnow,
    write_pending,
)


class SyncError(RuntimeError):
    pass


def _degradable_error(exc: BaseException) -> bool:
    """True when a GraphQL failure means the ENDPOINT is unavailable rather
    than the request being wrong (issue #65 AC3): a typed transport failure
    (the gh subprocess could not run or answer), or a non-rate-limit
    401/403/404. A statusless GraphQL validation or schema-drift error keeps
    the plain GitHubError shape and is deliberately NOT degradable — it must
    stay fatal so a schema drift can never be mistaken for an unavailable
    endpoint."""
    if isinstance(exc, GitHubTransportError):
        return True
    status = getattr(exc, "status", None)
    return status in (401, 403, 404)


def _log_graphql_unavailable(
    conn: sqlite3.Connection, repo: str, exc: BaseException
) -> None:
    """The ONE fetch_log row recording a GraphQL-to-REST degradation."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        log_fetch(
            conn, repo, "graphql", None,
            note=f"graphql unavailable; degraded to REST: {exc}",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _fail_not_quiescent(
    conn: sqlite3.Connection, repo: str, since: str | None, passes: int
) -> SyncError:
    """Persist the recovery floor and build the honest-failure error for a
    listing that never quiesced within the pass cap (issue #65 AC1): the run
    is incomplete, the next run re-drains this window from the floor, and a
    clean finish is suppressed so the sync never claims coverage the union of
    these passes did not achieve.

    The floor columns arrive with migration v12; the read and clear paths
    guard on them, so this write does too — on a pre-v12 connection the run
    still fails honestly, it just cannot promise a persisted floor (and the
    error says so rather than claiming one)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        floor_cols = "listing_floor_active" in {
            r[1] for r in conn.execute("PRAGMA table_info(sync_state)")
        }
        if floor_cols:
            conn.execute(
                "UPDATE sync_state SET listing_floor = ?, listing_floor_active = 1"
                " WHERE repo = ?",
                (since, repo),
            )
        set_last_error(
            conn,
            repo,
            "listing_not_quiescent: the listing order kept shifting across"
            f" {passes} passes; the run is incomplete and will retry from the"
            + (" persisted recovery floor"
               if floor_cols else " window it re-drains (no floor column on"
                                  " this pre-v12 store)"),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return SyncError(
        f"listing_not_quiescent after {passes} passes; the run is incomplete"
        + (" (recovery floor persisted)" if floor_cols else
           " (recovery floor not persisted: pre-v12 store)")
    )


def _batch_fully_covered(
    batch: list,
    ingested_versions: dict[int, str],
    ingested_numbers: set[int],
    prev_pass_counts: dict[int, int],
) -> bool:
    """True when EVERY row of this listing page is one this run already
    covered, so the page's bulk children call can be skipped entirely.

    Conservative in the safe direction: a row that is malformed, or whose
    `updated_at` is absent, or that this run skipped makes the whole page
    uncovered — the fetch then happens and the per-item loop decides each
    row on its own (a row it finds covered is skipped there). The per-number
    count within the page plays the same role it plays per row: a source
    that serves several versions of one number in a single walk is never
    treated as a re-delivery. When a page DOES carry the same number twice,
    the binding stamp is the NEWEST of them — the batch is covered only
    when the version this run stored is at least as new as every duplicate,
    and a non-comparable stamp keeps the batch uncovered rather than being
    folded away."""
    counts: dict[int, int] = {}
    stamps: dict[int, object] = {}
    for raw in batch:
        if not isinstance(raw, dict) or "number" not in raw:
            return False
        try:
            number = int(raw["number"])
        except (TypeError, ValueError):
            return False
        counts[number] = counts.get(number, 0) + 1
        stamp = raw.get("updated_at")
        if number not in stamps:
            stamps[number] = stamp
            continue
        prior = stamps[number]
        if not isinstance(prior, str) or not isinstance(stamp, str):
            stamps[number] = prior  # a non-comparable stamp stays uncovered
        elif stamp > prior:
            stamps[number] = stamp
    if not counts:
        return False
    return all(
        _already_covered(
            number, ingested_versions.get(number), stamps.get(number),
            ingested_numbers, count, prev_pass_counts,
        )
        for number, count in counts.items()
    )


def _committed_watermark(
    conn: sqlite3.Connection, repo: str
) -> str | None:
    """The watermark the items committed so far have already advanced to.

    Read AFTER a rate window is slept out, so an interrupted walk resumes
    from it rather than from the run-start binding (issue #65 AC2): the
    run-start binding would re-list everything the pass already delivered,
    which is exactly the re-work the per-item watermark bumps exist to
    avoid. None (no state row, or a NULL watermark) means the walk keeps
    its current floor."""
    row = conn.execute(
        "SELECT issues_since FROM sync_state WHERE repo = ?", (repo,)
    ).fetchone()
    return None if row is None else row["issues_since"]


def _already_covered(
    number: int,
    prior_ts: object,
    row_ts: object,
    ingested_numbers: set[int],
    seen_this_pass: int,
    prev_pass_counts: dict[int, int],
) -> bool:
    """True when this run has ALREADY covered the row the listing just
    delivered, so a verification pass can move on without re-fetching its
    children or recording a second observation (issue #65 AC1).

    Two conditions must both hold:

    * the number was ingested this run and the version stored for it is at
      least as new as the row just served (a newer row is a real
      observation and is always ingested), and
    * the previous pass served at least as many rows for that number.

    The second condition is what keeps a source that legitimately delivers
    several versions of ONE number in a single walk correct: within one
    pass every delivery is its own observation — a recorded replay serves
    the same item bytes with different children on successive pages, and
    each of those must ingest and leave its own tombstones. Across passes,
    the k-th re-delivery of a number the previous pass already served k or
    more times is the verification walk proving coverage, not a new
    observation.

    A row whose `updated_at` cannot be compared (absent, or not a string)
    is never treated as covered: the run cannot prove it is the same
    observation, so it re-ingests. A number this run SKIPPED (a torn row,
    say) is never in `ingested_numbers`, so it is retried rather than
    assumed covered."""
    if number not in ingested_numbers:
        return False
    if not isinstance(prior_ts, str) or not isinstance(row_ts, str):
        return False
    if prior_ts < row_ts:
        return False
    return seen_this_pass <= prev_pass_counts.get(number, 0)


#: Rate-limit windows slept through per fetch-layer call before giving up
#: (issue #5). Five is the ceiling; each sleep is additionally clamped so a
#: hostile or far-future reset can never park the sync (and its whole-run
#: lock) indefinitely.
_MAX_RATE_WINDOWS = 5

#: Listing-completeness passes (issue #65 AC1): the window is walked at least
#: twice and again whenever a pass delivers a set different from its
#: predecessor's. The cap bounds a churning repository: reaching it with
#: unequal sets FAILS the run incomplete (listing_not_quiescent) and persists
#: the recovery floor, so a clean sync never claims coverage it did not get.
_MAX_LISTING_PASSES = 4
#: Listing-row refresh attempts for one torn row (issue #65 AC6): the pull
#: payload postdates the listing snapshot, so the row is re-fetched until
#: the listing catches up — bounded, because a persistently skewed listing
#: must skip the item rather than store the mixture.
_MAX_ITEM_REFRESHES = 2

#: Upper bound on a single sleep-until-reset, in seconds: one hour, the
#: longest real GitHub window. A hostile or far-future reset can therefore
#: park the sync (and its whole-run lock) for at most one hour per window
#: instead of indefinitely; a legitimate window always sleeps in full.
_MAX_WINDOW_SLEEP_S = 60 * 60

#: Nested child sections a bulk item payload may carry inline, mapped to the
#: per-item REST fallback that completes them (issue #5 AC3).
_CHILD_FALLBACK = {
    "issue_comments": "list_issue_comments",
    "review_comments": "list_review_comments",
    "reviews": "list_reviews",
    "files": "list_pr_files",
}


def _batch(payload: object) -> list[dict]:
    """Normalize one yielded listing payload: an item dict is a one-item
    batch, a page list is itself, anything else is skipped."""
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [raw for raw in payload if isinstance(raw, dict)]
    return []


def _rate_attrs(exc: BaseException) -> tuple[int, str] | None:
    remaining = getattr(exc, "rate_limit_remaining", None)
    reset = getattr(exc, "rate_limit_reset", None)
    if remaining is None or reset is None:
        return None
    return (remaining, reset)


def _rate_reset_epoch(reset) -> float:
    """Parse an ISO-Z (or ISO) reset instant; an offset-less value is UTC.
    Anything unparseable raises so the caller can fall back to the original
    error."""
    if not isinstance(reset, str):
        raise ValueError(f"rate_limit_reset is not a string: {reset!r}")
    try:
        return datetime.strptime(reset, ISO_Z).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        parsed = datetime.fromisoformat(reset.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()


def _rate_sleep(exc: BaseException) -> None:
    """Sleep until the reported reset on the sync module's `time` (the
    injectable clock tests patch), clamped to `_MAX_WINDOW_SLEEP_S`. The
    reset string is consumed verbatim; an unparseable one re-raises the
    original rate-limit error rather than a parse failure."""
    reset = getattr(exc, "rate_limit_reset")
    try:
        reset_epoch = _rate_reset_epoch(reset)
    except (ValueError, TypeError, OSError, OverflowError):
        raise exc
    delay = min(_MAX_WINDOW_SLEEP_S, max(0.0, reset_epoch - time.time()))
    time.sleep(delay)


def _call_with_rate_retry(fn, *args):
    """Run one fetch-layer call under the bounded sleep-until-reset loop
    (issue #5 AC4). Raises the last error once `_MAX_RATE_WINDOWS` windows
    have been slept through."""
    for attempt in range(_MAX_RATE_WINDOWS + 1):
        try:
            return fn(*args)
        except GitHubError as exc:
            if _rate_attrs(exc) is None or attempt >= _MAX_RATE_WINDOWS:
                raise
            _rate_sleep(exc)
    raise AssertionError("unreachable")


class _BudgetWindow:
    """Carrier for a source-observed budget floor so `_rate_sleep` can
    consume it exactly like an enriched error."""

    def __init__(self, remaining: int, reset: str) -> None:
        self.rate_limit_remaining = remaining
        self.rate_limit_reset = reset


def _source_budget_floor(source) -> tuple[int, str] | None:
    """(remaining, reset_at) when the source tracks a live budget
    (GraphQLSource, from each page query's rateLimit selection) and that
    budget has reached the floor; None otherwise."""
    remaining = getattr(source, "rate_limit_remaining", None)
    reset_at = getattr(source, "rate_limit_reset_at", None)
    if isinstance(remaining, int) and isinstance(reset_at, str) and remaining <= 0:
        return (remaining, reset_at)
    return None


def _connection_complete(
    links: dict | None, connection: str, links_incomplete: object
) -> bool:
    """True iff the links section was carried AND the connection key is
    present in it AND that connection did not overflow (issue #27: a
    capability-dropped or absent field is never complete, so its stored link
    edges can never be retracted)."""
    if not isinstance(links, dict) or connection not in links:
        return False
    if isinstance(links_incomplete, (list, tuple, set, frozenset)):
        return connection not in links_incomplete
    if links_incomplete:
        return False
    return True


def _resolve_children(
    source: GitHubSource,
    list_raw: dict,
    kind: str,
    include_patches: bool,
    provided: dict | None = None,
) -> tuple[
    dict | None,
    list[dict],
    list[dict],
    list[dict],
    list[dict],
    list[dict] | None,
    dict,
    dict[str, bool],
    dict | None,
    object,
    list[dict] | None,
]:
    """Children for one item: inline or bulk-provided payload where present
    and complete, per-item REST fallback where flagged incomplete or absent
    (issue #5). `provided` is what the source's fetch_children bulk call
    returned for this item; inline payload keys take precedence. Returns an
    11-tuple: the five legacy sections, the GraphQL-only timeline event list
    (None = section never carried), a flags dict, the per-section
    `children_complete` map (issue #23, extended per connection in v10), and
    the v10 `links` dict / `links_incomplete` marker / `pr_commits` list —
    all three None when the section was never carried (issue #27).

    The absent-everywhere path keeps the legacy rules (comments only when the
    count is nonzero, pull details only for PRs, files via changed_files),
    which is what plain REST sources hit. `include_patches` forces the REST
    files fetch even when a payload carried files: GraphQL cannot serve
    patches.
    """
    number = int(list_raw["number"])
    pull_raw = list_raw.get("pull")
    if pull_raw is None and provided is not None:
        pull_raw = provided.get("pull")
    if pull_raw is None and kind == "pr" and "pull" not in list_raw and not (
        provided is not None and "pull" in provided
    ):
        pull_raw = source.get_pull(number)

    sections: dict[str, list[dict]] = {}
    complete: dict[str, bool] = {}
    # Issue #25: a recorded `children_complete` map rides replay bundles (the
    # reader injects all-False for legacy recordings). Its presence — not its
    # values — disables the REST fallbacks below: a recording reproduces what
    # the original ingest saw, so a section it marks False is served as the
    # recorded (possibly partial/empty) list, never re-fetched.
    recorded_complete = (
        provided.get("children_complete") if provided is not None else None
    )
    has_recording = isinstance(recorded_complete, dict)
    for name, fallback_name in _CHILD_FALLBACK.items():
        provided_section = provided.get(name) if provided is not None else None
        provided_flagged = bool(provided.get(f"{name}_incomplete")) if provided is not None else False
        if name in list_raw:
            if include_patches and name == "files":
                sections[name] = list(source.list_pr_files(number))
                complete[name] = False
            elif list_raw.get(f"{name}_incomplete"):
                sections[name] = list(getattr(source, fallback_name)(number))
                complete[name] = False
            else:
                sections[name] = list(list_raw[name] or [])
                complete[name] = True
        elif provided_section is not None or provided_flagged:
            if include_patches and name == "files":
                sections[name] = list(source.list_pr_files(number))
                complete[name] = False
            elif provided_flagged:
                sections[name] = list(getattr(source, fallback_name)(number))
                complete[name] = False
            else:
                sections[name] = list(provided_section or [])
                complete[name] = True
        elif has_recording:
            # Recorded bundle: serve exactly what was recorded (absent
            # section = empty list), flag it per the recording, never
            # re-fetch.
            sections[name] = list(provided_section or [])
            complete[name] = bool(recorded_complete.get(name, False))
        elif kind == "pr" or name == "issue_comments":
            if name == "issue_comments":
                if list_raw.get("comments"):
                    sections[name] = list(source.list_issue_comments(number))
                else:
                    sections[name] = []
            elif name == "files":
                changed = (pull_raw or {}).get("changed_files")
                if include_patches and name == "files":
                    sections[name] = list(source.list_pr_files(number))
                elif changed or changed is None:
                    sections[name] = list(source.list_pr_files(number))
                else:
                    sections[name] = []
            else:
                sections[name] = list(getattr(source, fallback_name)(number))
            complete[name] = False
        else:
            sections[name] = []
            # Not applicable for this item kind (e.g. an issue's PR sections):
            # nothing was requested and nothing is missing, so the section is
            # complete — marking it False logged a permanent per-item note for
            # structurally-N/A sections and drowned the real degradation
            # signals (PR #37 review).
            complete[name] = True
    # Timeline events ride only the GraphQL bulk payload (issue #6): there is
    # deliberately NO per-item REST fallback, so a plain REST source yields
    # none and previously stored timeline edges survive via the delete-owned
    # exemption in store.delete_owned_edges. The same is true of closing
    # references, which ride the pull section of the GraphQL payload only.
    # GraphQL-only sections (issue #27): presence is carried SEPARATELY from
    # completeness. None = the section was never carried (plain REST source,
    # the 404 path, a capability-dropped field) — stored link edges,
    # item_events and pr_commits rows must survive untouched. A carried
    # section replaces stored state only when its completeness key is True.
    # Plain REST is NOT a degradation for these sections: None simply means
    # "nothing was answered".
    timeline: list[dict] | None = None
    links: dict | None = None
    links_incomplete: object = None
    pr_commits: list[dict] | None = None
    flags: dict[str, bool] = {}
    # A source with no bulk context (plain REST) answers none of the
    # GraphQL-only sections by design — that is not a degradation, so the
    # completeness map reads complete (no per-item fetch_log noise, the PR
    # #37 review rule) while the None PRESENCE signals keep every stored
    # link edge / event / commit row untouched.
    rest_source = provided is None
    timeline_complete = rest_source
    if provided is not None:
        provided_timeline = provided.get("timeline")
        if isinstance(provided_timeline, list):
            timeline = list(provided_timeline)
            timeline_complete = not provided.get("timeline_incomplete")
        # A carried NULL timeline (no production mapper emits one) stays
        # ABSENT: presence None keeps stored item_events untouched, the
        # conservative direction (final-critic note).
        provided_links = provided.get("links")
        if isinstance(provided_links, dict):
            links = provided_links
            # The overflow marker arrives as a LIST of connection names from
            # the production mapper; the legacy BOOLEAN form (recorded
            # bundles, fixtures) means every connection overflowed.
            links_incomplete = provided.get("links_incomplete")
        provided_commits = provided.get("pr_commits")
        if isinstance(provided_commits, list):
            pr_commits = list(provided_commits)
        if provided.get("timeline_incomplete"):
            flags["timeline_incomplete"] = True
        if provided.get("closing_refs_incomplete"):
            flags["closing_refs_incomplete"] = True
        if provided.get("pr_commits_incomplete"):
            flags["pr_commits_incomplete"] = True
        overflowed = provided.get("links_incomplete")
        if overflowed:
            names = (
                overflowed
                if isinstance(overflowed, list)
                else sorted(
                    {
                        name
                        for name in (
                            "blocked_by",
                            "parent",
                            "duplicate_of",
                            "open_prs",
                        )
                        if name in (links or {})
                    }
                )
                or ["links"]
            )
            for name in names:
                flags[f"links_overflow_{name}"] = True
    complete["timeline"] = timeline_complete
    if rest_source:
        complete["links_blocked_by"] = True
        complete["links_parent"] = True
        complete["links_duplicate_of"] = True
        complete["links_open_prs"] = True
        complete["pr_commits"] = True
    else:
        complete["links_blocked_by"] = _connection_complete(links, "blocked_by", links_incomplete)
        complete["links_parent"] = _connection_complete(links, "parent", links_incomplete)
        complete["links_duplicate_of"] = _connection_complete(links, "duplicate_of", links_incomplete)
        complete["links_open_prs"] = _connection_complete(links, "open_prs", links_incomplete)
        complete["pr_commits"] = pr_commits is not None and not provided.get(
            "pr_commits_incomplete"
        )
    complete["pull"] = (pull_raw is not None) or kind != "pr"
    if has_recording:
        # The recording's completeness, verbatim, for every key it names
        # (issue #25) — a recorded False overrides the computed True for a
        # provided section, exactly as a recorded True overrides a computed
        # False. Content always comes from the recording; only the map is
        # overridden here.
        for name in (
            *_CHILD_FALLBACK,
            "pull",
            "timeline",
            "links_blocked_by",
            "links_parent",
            "links_duplicate_of",
            "links_open_prs",
            "pr_commits",
        ):
            if name in recorded_complete:
                complete[name] = bool(recorded_complete[name])
    return (
        pull_raw,
        sections["issue_comments"],
        sections["review_comments"],
        sections["reviews"],
        sections["files"],
        timeline,
        flags,
        complete,
        links,
        links_incomplete,
        pr_commits,
    )


def _mark_gone(
    conn: sqlite3.Connection, repo: str, number: int, verdict: str, note: str
) -> None:
    """Record a deletion verdict in place (issue #5 AC5): items.state changes,
    the row stays (edges pointing at it survive), and a fetch_log trail names
    the verdict with a null http_status (no fetch happened on this row)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE items SET state = ?, state_reason = ? "
            "WHERE repo = ? AND number = ?",
            (verdict, f"marked {verdict}: {note}", repo, number),
        )
        log_fetch(conn, repo, "item", str(number), note=f"marked {verdict}: {note}", status=None)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


#: children_complete keys whose False state means a per-item fetch fallback
#: happened (the per-item "children incomplete" fetch_log note). The v10
#: connection gates are deliberately absent: an absent connection is not a
#: per-item fallback.
_LOGGABLE_SECTIONS = frozenset({*_CHILD_FALLBACK, "pull", "timeline"})


def _gone_children_complete() -> dict[str, bool]:
    """The all-False completeness map for a gone item (issue #23): no section
    was answered — there is nothing behind a 404/410."""
    return {
        **{name: False for name in _CHILD_FALLBACK},
        "pull": False,
        "timeline": False,
        "links_blocked_by": False,
        "links_parent": False,
        "links_duplicate_of": False,
        "links_open_prs": False,
        "pr_commits": False,
    }


def _stored_list_raw(conn: sqlite3.Connection, repo: str, number: int) -> dict | None:
    """The stored item's original issues-list payload, for recording a
    deletion of an item whose fetch payload is not at hand (deletion oracle,
    targeted refresh of a vanished number — PR #37 review)."""
    row = conn.execute(
        "SELECT raw_json FROM items WHERE repo = ? AND number = ?", (repo, number)
    ).fetchone()
    if row is None or not row["raw_json"]:
        return None
    try:
        payload = json.loads(row["raw_json"])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _write_gone_bundle(
    jsonl_handle: TextIO | None,
    number: int,
    list_raw: dict | None,
    gone_note: str,
    verdict: str = "deleted",
) -> None:
    """Record a deletion in the sidecar so replay reproduces the final state
    (PR #37 review: every real deletion shape must reach the replay). A
    deletion with no recoverable payload (item never stored) has nothing to
    resurrect and gets no record."""
    if list_raw is None:
        return
    _write_jsonl(
        jsonl_handle,
        "item_bundle",
        {
            "number": number,
            "item": list_raw,
            "pull": None,
            "issue_comments": [],
            "review_comments": [],
            "reviews": [],
            "files": [],
            "timeline": [],
            # Nothing was answered behind the 404/410 (issue #23/#25): the
            # recorded map makes that explicit so replay never tombstones a
            # gone item's children.
            "children_complete": _gone_children_complete(),
            "gone": True,
            "gone_status": None,
            "gone_note": gone_note,
            "gone_verdict": verdict,
        },
    )


def _ingest_and_mark_gone(
    conn: sqlite3.Connection,
    repo: str,
    list_raw: dict,
    number: int,
    exc_status: int,
    *,
    include_patches: bool,
    jsonl_handle: TextIO | None,
    advance_watermark: bool,
    note: str | None = None,
    verdict: str = "deleted",
) -> int:
    """The first-sighting 404/410 path, shared by live per-item errors and
    replayed gone bundles (issue #23) so both produce byte-identical
    `state_reason` values.

    A first-sighting 404 stores the listing payload so the marking is
    truthful and the watermark advances past it. An item that was ALREADY
    stored keeps its children and edges: ingest_item replaces child rows and
    rebuilt edges from scratch, so re-ingesting with empty children would
    destroy the last local copy of a deleted item's discussion. This matches
    the oracle path, which marks without touching children. Returns 1 when a
    first-sighting ingest happened, 0 otherwise."""
    exists = conn.execute(
        "SELECT 1 FROM items WHERE repo = ? AND number = ?",
        (repo, number),
    ).fetchone() is not None
    gone_note = note or f"GitHub returned HTTP {exc_status} while fetching item data"
    if not exists:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ingest_item(
                conn, repo, list_raw,
                pull_raw=None,
                issue_comments=[], review_comments=[],
                reviews=[], files=[],
                include_patches=include_patches,
                children_complete=_gone_children_complete(),
                advance_watermark=advance_watermark,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        _write_gone_bundle(jsonl_handle, number, list_raw, gone_note)
        ingested_here = 1
    else:
        ingested_here = 0
        if advance_watermark:
            # Advance the watermark past the marked item without re-ingesting:
            # otherwise every later run re-lists it until its updated_at ages
            # past the watermark. Targeted runs never advance it.
            conn.execute("BEGIN IMMEDIATE")
            try:
                bump_watermark(conn, repo, list_raw.get("updated_at"))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    if ingested_here == 0:
        # An already-stored deletion must reach the sidecar too, or replay
        # resurrects the deleted item (PR #37 review): the listing payload in
        # hand IS the item's last recorded list payload.
        _write_gone_bundle(jsonl_handle, number, list_raw, gone_note)
    _mark_gone(conn, repo, number, verdict, gone_note)
    return ingested_here


def _assert_repository_readable(source: GitHubSource, repo: str) -> None:
    """Refuse per-item 404 verdicts when the REPOSITORY cannot be read.

    Every requested number answering 404/410 is ambiguous: the repository
    itself is unreachable (private, renamed, deleted) or every requested
    item is genuinely gone. Per-item verdicts built from an access failure
    would fabricate deletions, so ONE repository-level read decides it when
    the source can answer (issue #66 AC5). A source with no such read is
    treated as unreachable — fail-safe: change nothing and name the
    repository-access failure. A probe error that is not a plain 404/410 is
    neither verdict and is reported as its own failure."""
    probe = getattr(source, "probe_repository", None)
    readable: bool | None = None
    if callable(probe):
        try:
            readable = bool(_call_with_rate_retry(probe))
        except GitHubError as exc:
            raise SyncError(
                f"repository {repo} could not be read: {exc}"
            ) from exc
    if readable is not True:
        raise SyncError(
            f"repository {repo} is not readable: every requested item"
            " answered HTTP 404 (the repository may be private, renamed,"
            " or unreachable)"
        )


def _number_of(raw: object) -> int | None:
    if isinstance(raw, dict) and "number" in raw:
        try:
            return int(raw["number"])
        except (TypeError, ValueError):
            return None
    return None


def _numbered_payloads(
    conn: sqlite3.Connection,
    source: GitHubSource,
    repo: str,
    wanted: set[int],
    include_patches: bool,
    jsonl_handle: TextIO | None,
):
    """Listing payloads for a targeted refresh (issue #23).

    A source with `get_issues` fetches exactly those items (same endpoint
    family as the listing, so payload identity is preserved); a number that
    404s goes through the same ingest-and-mark path as a live per-item 404 -
    or, when never stored, only leaves a fetch_log note. A source WITHOUT
    `get_issues` (test fakes, replay) falls back to an UNFILTERED listing -
    the stored watermark is ignored, so explicitly requested items older than
    it are still found - filtered per page to the requested numbers."""
    getter = getattr(source, "get_issues", None)
    if callable(getter):
        results = _call_with_rate_retry(getter, sorted(wanted)) or {}
        # Repository reachability (issue #66 AC5): when EVERY requested
        # number answers 404/410, either the repository itself is
        # unreadable (private, renamed, deleted) or every requested item is
        # genuinely gone — the two are indistinguishable from the item
        # paths alone, and per-item verdicts built from an access failure
        # would fabricate deletions. ONE repository-level read decides it
        # when the source can answer; a source that cannot is treated as
        # unreachable, which changes nothing and fails loudly.
        if results and all(
            isinstance(entry, tuple)
            and len(entry) > 1
            and entry[1] in (404, 410)
            for entry in results.values()
        ):
            _assert_repository_readable(source, repo)
        shells: list[dict] = []
        for n in sorted(wanted):
            entry = results.get(n)
            payload = entry[0] if isinstance(entry, tuple) and len(entry) > 1 else (
                entry if isinstance(entry, dict) else None
            )
            if isinstance(payload, dict):
                shells.append(payload)
        if shells:
            yield shells
        for n in sorted(wanted):
            entry = results.get(n)
            payload = entry[0] if isinstance(entry, tuple) and len(entry) > 1 else (
                entry if isinstance(entry, dict) else None
            )
            status = entry[1] if isinstance(entry, tuple) and len(entry) > 1 else None
            if isinstance(payload, dict):
                continue
            exists = conn.execute(
                "SELECT 1 FROM items WHERE repo = ? AND number = ?", (repo, n)
            ).fetchone() is not None
            if exists and status in (404, 410):
                # The recorded reason carries the REAL status the source
                # reported — never a hardcoded one (issue #23 review).
                gone_note = f"GitHub returned HTTP {status} while fetching item data"
                _write_gone_bundle(
                    jsonl_handle, n, _stored_list_raw(conn, repo, n), gone_note
                )
                _mark_gone(conn, repo, n, "deleted", gone_note)
            elif exists:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    log_fetch(conn, repo, "item", str(n),
                              note="skipped missing number: stored, but the source "
                                   "returned no usable payload this run")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            else:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    log_fetch(conn, repo, "item", str(n),
                              note="skipped missing number: not found and never stored")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        return
    found: set[int] = set()
    malformed_seen = False
    for page in source.list_issues(None):
        batch = [raw for raw in _batch(page) if _number_of(raw) in wanted]
        if any(_number_of(raw) is None and isinstance(raw, dict) for raw in _batch(page)):
            # A listing shell we cannot match could be a requested number:
            # absence evidence is then incomplete, so no deletion verdict is
            # drawn this run (PR #37 review — false-deletion guard).
            malformed_seen = True
        found.update(n for n in (_number_of(raw) for raw in batch) if n is not None)
        if batch:
            yield batch
    for n in sorted(wanted - found):
        exists = conn.execute(
            "SELECT 1 FROM items WHERE repo = ? AND number = ?", (repo, n)
        ).fetchone() is not None
        if exists and not malformed_seen:
            gone_note = "absent from the full listing during a requested refresh"
            _write_gone_bundle(
                jsonl_handle, n, _stored_list_raw(conn, repo, n), gone_note
            )
            _mark_gone(conn, repo, n, "deleted", gone_note)
        elif exists:
            conn.execute("BEGIN IMMEDIATE")
            try:
                log_fetch(conn, repo, "item", str(n),
                          note="skipped missing number: listing contained entries "
                               "this refresh could not match")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        else:
            conn.execute("BEGIN IMMEDIATE")
            try:
                log_fetch(conn, repo, "item", str(n),
                          note="skipped missing number: not found and never stored")
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def _resolve_torn_row(source: GitHubSource, list_raw: dict, pull_ts: str) -> dict | None:
    """Re-fetch the listing row until it is at least as new as the pull
    payload's facts (issue #65 AC6): a stored row must never mix an older
    listing snapshot with a newer pull payload. Bounded at
    _MAX_ITEM_REFRESHES attempts; None means the listing stayed stale and
    the caller skips the item (recording it in the durable pending set)
    rather than storing the forbidden mixture."""
    number = int(list_raw["number"])
    getter = getattr(source, "get_issues", None)
    if not callable(getter):
        return list_raw
    current: dict | None = None
    for _ in range(_MAX_ITEM_REFRESHES):
        results = _call_with_rate_retry(getter, [number]) or {}
        entry = results.get(number)
        row = entry[0] if isinstance(entry, tuple) and len(entry) > 1 else (
            entry if isinstance(entry, dict) else None
        )
        if not isinstance(row, dict):
            return current
        row_ts = row.get("updated_at")
        if isinstance(row_ts, str) and row_ts >= pull_ts:
            return row
        current = row
    return None


def _retry_torn_pending(
    conn: sqlite3.Connection,
    source: GitHubSource,
    repo: str,
    numbers: list[int],
    include_patches: bool,
    seen_numbers: set[int],
    jsonl_handle: TextIO | None,
) -> int:
    """Refresh the torn-pending numbers through the exact-number seam
    (issue #66 AC5): a listing row that has caught up ingests normally and
    leaves the pending set; one still skewed against its pull payload
    stays pending for the next run. A later healthy item's watermark
    advance can therefore never seal a skipped item.

    A number the source now answers 404/410 is NOT still torn — the item
    is gone, and the plan's rule is that a 404 during the refresh follows
    the existing per-item error path (the item is not lost): the
    repository-read gate first (an unreadable repository must never
    fabricate a deletion), then the same ingest-and-mark + sidecar bundle
    every other deletion shape writes, then the pending row clears so the
    number stops costing an exact-number fetch on every future run."""
    getter = getattr(source, "get_issues", None)
    if not callable(getter):
        return 0
    ingested = 0
    for n in numbers:
        results = _call_with_rate_retry(getter, [n]) or {}
        entry = results.get(n)
        row = entry[0] if isinstance(entry, tuple) and len(entry) > 1 else (
            entry if isinstance(entry, dict) else None
        )
        status = entry[1] if isinstance(entry, tuple) and len(entry) > 1 else None
        if not isinstance(row, dict):
            if status in (404, 410):
                _assert_repository_readable(source, repo)
                gone_note = (
                    f"GitHub returned HTTP {status} while fetching item data"
                )
                stored = _stored_list_raw(conn, repo, n)
                if stored is not None:
                    _write_gone_bundle(
                        jsonl_handle, n, stored, gone_note,
                        "deleted" if status == 404 else "transferred",
                    )
                    _mark_gone(conn, repo, n,
                               "deleted" if status == 404 else "transferred",
                               gone_note)
                else:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        log_fetch(
                            conn, repo, "item", str(n),
                            note=(
                                f"skipped pending number: HTTP {status} and"
                                " the item was never stored"
                            ),
                            status=status,
                        )
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                clear_pending(conn, repo, [n], "torn")
            continue  # unresolved (or gone, now resolved): nothing else to do
        try:
            (pull_raw, issue_comments, review_comments, reviews,
             files, timeline, flags, children_complete,
             links, links_incomplete, pr_commits) = _resolve_children(
                source, row, item_kind(row), include_patches,
            )
        except GitHubError:
            continue
        pull_ts = (
            pull_raw.get("updated_at") if isinstance(pull_raw, dict) else None
        )
        list_ts = row.get("updated_at")
        if (
            isinstance(pull_ts, str) and isinstance(list_ts, str)
            and pull_ts > list_ts
        ):
            continue  # still torn: stays pending
        conn.execute("BEGIN IMMEDIATE")
        try:
            ingest_item(
                conn, repo, row,
                pull_raw=pull_raw,
                issue_comments=issue_comments,
                review_comments=review_comments,
                reviews=reviews,
                files=files,
                include_patches=include_patches,
                timeline=timeline,
                children_complete=children_complete,
                links=links,
                links_incomplete=links_incomplete,
                pr_commits=pr_commits,
                advance_watermark=True,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        clear_pending(conn, repo, [n], "torn")
        seen_numbers.add(n)
        ingested += 1
    return ingested


def _deletion_corroborator(seen_numbers: set[int], candidates: list[int]):
    """The known-live witness factory for the deletion probe (issue #66
    AC3/AC12): a number this run's listing delivered that is not itself a
    candidate, or None when every listed item is a candidate (the all-gone
    window — the probe then falls back to repository reachability)."""

    def corroborator() -> int | None:
        live = sorted(set(seen_numbers) - set(candidates))
        return live[0] if live else None

    return corroborator


def _call_deletion_checker(checker, candidates, corroborator):
    """Invoke the source's deletion oracle, passing the witness ONLY to
    oracles whose signature accepts one (issue #66 AC3): a legacy
    one-argument oracle keeps its exact contract — being handed a second
    argument it never declared would raise TypeError and lose the verdicts.
    Runs under the bounded rate-limit retry so a window cannot fail an
    otherwise-complete run."""
    try:
        takes_witness = (
            len(inspect.signature(checker).parameters) >= 2
        )
    except (TypeError, ValueError):
        takes_witness = False
    if takes_witness:
        return _call_with_rate_retry(checker, candidates, corroborator)
    return _call_with_rate_retry(checker, candidates)


def _deletion_pass(
    conn: sqlite3.Connection,
    source: GitHubSource,
    repo: str,
    seen_numbers: set[int],
    jsonl_handle: TextIO | None = None,
) -> None:
    """Consult the source's deletion oracle after a drained full listing.

    Only a run that started at since=None may mark: absence from an
    incremental listing means "not updated", never "gone". The candidate set
    is stored, unmarked items minus everything seen across the whole run
    (including rate-limit restarts), unioned with any numbers a previous
    run left pending (issue #66 AC4: a pass that did not complete retries
    from the durable set, never from a laundered clean state).

    Atomicity (issue #66 AC3): the pending set is written BEFORE the probe,
    the verdicts are buffered, and items are marked ONLY after every chunk
    succeeded. A raise from the probe marks NOTHING — no item is fabricated
    deleted and no corroborated verdict is half-applied — and the pending
    set guarantees every candidate is retried on the next run."""
    checker = getattr(source, "check_deleted", None)
    if not callable(checker):
        return
    rows = conn.execute(
        "SELECT number FROM items WHERE repo = ? AND state NOT IN ('deleted','transferred')",
        (repo,),
    ).fetchall()
    pending = set(pending_numbers(conn, repo, "deletion")["deletion"])
    candidates = sorted(
        ({int(r["number"]) for r in rows} - set(seen_numbers)) | pending
    )
    if not candidates:
        clear_pending(conn, repo, reason="deletion")
        return
    # The durable retry set comes first: any raise below leaves every
    # candidate pending for the next run.
    write_pending(conn, repo, candidates, "deletion")
    verdicts = _call_deletion_checker(
        checker, candidates, _deletion_corroborator(seen_numbers, candidates)
    )
    if not isinstance(verdicts, dict):
        return
    allowed = set(candidates)
    clean: dict[int, str] = {}
    for n, v in verdicts.items():
        # Source-supplied keys/values are untrusted: malformed rows are
        # skipped (never crash a completed run), unknown verdicts are
        # dropped silently by design.
        try:
            number = int(n)
            text = str(v)
        except (TypeError, ValueError):
            continue
        if number in allowed and text in ("deleted", "transferred"):
            clean[number] = text
    # Every chunk succeeded: apply the buffered verdicts and clear the set.
    for number, verdict in sorted(clean.items()):
        gone_note = "reported by source deletion check after a completed full listing"
        # Oracle deletions must reach the sidecar too, or replay resurrects
        # the deleted item (PR #37 review): the stored payload is the item's
        # last recorded list payload. The verdict travels with the bundle so
        # replay reproduces 'transferred' as well as 'deleted'.
        _write_gone_bundle(
            jsonl_handle, number, _stored_list_raw(conn, repo, number), gone_note,
            verdict=verdict,
        )
        _mark_gone(conn, repo, number, verdict, gone_note)
    clear_pending(conn, repo, reason="deletion")


def _error_text(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text[:2000]


def _write_jsonl(handle: TextIO | None, resource: str, payload: object) -> None:
    if handle is None:
        return
    handle.write(json.dumps({"resource": resource, "payload": payload}, ensure_ascii=False))
    handle.write("\n")
    handle.flush()


def _ensure_state_row(
    conn: sqlite3.Connection,
    repo: str,
    include_patches: bool,
    fetch_source: str | None = None,
) -> None:
    """Create/refresh the slug's sync_state profile row.

    Stamps the fetch source EXPLICITLY requested for this listing run
    (issue #48): the store's fetch profile is the operator's deliberate
    choice, so only an operator-stated source rewrites the stamp — a run
    that did not choose (the default-source path, or a caller with no
    source choice at all such as the MCP surface) leaves the recorded
    profile untouched (cubic re-review + review PRR-001-residual: an
    incidental sync must never silently flip a deliberately-rest store).
    The stamp is written at listing start in this dedicated prologue
    transaction (not inside the per-item watermark transaction). A
    mid-run GraphQL-to-REST degradation is a transient fact of one run —
    disclosed in the run result, never written back over the requested
    stamp.
    """
    if fetch_source is None:
        conn.execute(
            """
            INSERT INTO sync_state(repo, include_patches)
            VALUES (?, ?)
            ON CONFLICT(repo) DO UPDATE SET
                include_patches = excluded.include_patches
            """,
            (repo, 1 if include_patches else 0),
        )
    else:
        conn.execute(
            """
            INSERT INTO sync_state(repo, include_patches, fetch_source)
            VALUES (?, ?, ?)
            ON CONFLICT(repo) DO UPDATE SET
                include_patches = excluded.include_patches,
                fetch_source = excluded.fetch_source
            """,
            (repo, 1 if include_patches else 0, fetch_source),
        )
    conn.commit()


def _gh_version(gh_bin: str) -> str:
    """First line of `gh --version`, cached per executable (issue #23): two
    configured binaries in one process each report their own version."""
    cached = _GH_VERSION_CACHE.get(gh_bin)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            [gh_bin, "--version"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=GH_TIMEOUT_S,
        )
        version = (proc.stdout or "").strip().splitlines()[0] if (proc.stdout or "").strip() else "unknown"
    except (OSError, subprocess.SubprocessError):
        version = "unknown"
    _GH_VERSION_CACHE[gh_bin] = version
    return version


_GH_VERSION_CACHE: dict[str, str] = {}


def _gh_host() -> str:
    """Host for the capability-cache key: GH_HOST when set, else github.com
    (gh's GraphQL endpoint follows the gh-configured/GH_HOST host)."""
    return os.environ.get("GH_HOST") or "github.com"


def _probe_failed_key(source: GitHubSource) -> str:
    """The meta key holding the LATEST capability-probe failure (empty when
    the last probe succeeded), keyed like the capability cache itself."""
    return (
        f"capability_probe_failed:v3:{_gh_host()}:"
        f"{_gh_version(getattr(source, 'gh_bin', 'gh'))}"
    )


def _attach_capabilities(conn: sqlite3.Connection, source: GitHubSource, repo: str) -> bool:
    """Hydrate or run the capability probe for sources that support it
    (issue #23). Returns True when the latest probe FAILED (the run then
    fails open with the full field set and the failure is reported in the
    sync result and status.coverage — issue #65 AC9), False otherwise."""
    probe_failed = False
    """Hydrate or run the capability probe for sources that support it
    (issue #23). Duck-typed on BOTH members: every source without the probe
    pair (fakes, REST-only, replay) skips the whole step - no meta I/O, no
    subprocess. Meta reads/writes run OUTSIDE every per-item transaction. A
    probe failure fails open: the sync proceeds with the full field set and
    the failure is recorded in fetch_log."""
    probe = getattr(source, "probe_capabilities", None)
    attach = getattr(source, "attach_capabilities", None)
    if not callable(probe) or not callable(attach):
        return
    # Omissions accumulate across every page query of this run; the list is
    # reset here (once per sync), never per page (issue #23).
    source.omitted_fields = []
    source._capabilities_from_cache = False
    # v3: the selection gained the five issue #27 fields and the timeline
    # enum introspection; v2/v1 caches cannot know them, and a stale map
    # would strip the new sections by the per-field drop rule instead of
    # degrading honestly. Versioning invalidates old caches instead.
    key = f"capability_probe:v3:{_gh_host()}:{_gh_version(getattr(source, 'gh_bin', 'gh'))}"
    cached = get_meta(conn, key)
    if cached:
        try:
            attach(json.loads(cached))
            # A cached map is a snapshot: if a later fatal error turns out to
            # be schema drift the snapshot cannot see, sync_repo invalidates
            # the row so the next run re-probes (PR #37 review — no
            # failure-triggered re-probe otherwise exists).
            source._capabilities_from_cache = True
            source._capabilities_meta_key = key
            # Clearing the latest-failure state is a write, so it commits
            # here like the fresh-probe path does: leaving it open would
            # hand the per-item loop a connection already in a transaction,
            # and its BEGIN IMMEDIATE would fail with "cannot start a
            # transaction within a transaction".
            set_meta(conn, _probe_failed_key(source), "")
            conn.commit()
            return probe_failed
        except ValueError:
            pass  # corrupt meta row: fall through to a fresh probe
    try:
        caps = probe()
    except GitHubError as exc:
        conn.execute("BEGIN IMMEDIATE")
        try:
            log_fetch(conn, repo, "graphql_probe", None, note=f"probe failed: {exc}")
            # The LATEST probe state, so status.coverage reads recovery-safe
            # (a later successful probe clears it) — issue #65 AC9: the
            # failure is reported, not only logged.
            set_meta(conn, _probe_failed_key(source), str(exc))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return True
    attach(caps)
    # A successful probe clears the latest-failure state (recovery-safe).
    set_meta(conn, _probe_failed_key(source), "")
    set_meta(
        conn,
        key,
        json.dumps(
            {
                k: (sorted(v) if isinstance(v, (set, frozenset, list)) else v)
                for k, v in caps.items()
            },
            sort_keys=True,
        ),
    )
    conn.commit()
    return probe_failed


def sync_repo(
    conn: sqlite3.Connection,
    source: GitHubSource,
    repo: str,
    *,
    force: bool = False,
    include_patches: bool = False,
    jsonl_path: Path | None = None,
    numbers: list[int] | None = None,
    fetch_source: str | None = None,
) -> dict:
    """Incremental sync. Each item is one IMMEDIATE transaction.

    With `numbers` (sync --numbers, issue #23) this is a TARGETED refresh of
    exactly those items: the watermark never moves, no full sync starts or
    completes, the deletion oracle never runs, and a targeted finalizer
    clears last_error without touching freshness stamps. `fetch_source`
    (issue #48) stamps the source EXPLICITLY requested for this run —
    targeted runs stamp too when the operator stated a source; the
    watermark still never moves for them. None (the default) rewrites
    nothing: callers that construct the source directly (the MCP surface)
    or run on the CLI default leave the recorded profile exactly as the
    operator set it, so a deliberately-rest store never silently flips
    (cubic re-review)."""
    # Everything that touches the database lives inside the try: the recorder
    # must see prologue failures too (force reset, state-row ensure, pending
    # marker) — a lock or disk fault landing there is still a failed sync and
    # may not escape with state looking clean. validate_slug is pure and stays
    # out; set_last_error upserts the state row, so recording works even when
    # the row was never created.
    repo = validate_slug(repo)  # canonical lowercase identity for every key
    ingested = 0
    ingested_numbers: set[int] = set()
    last_number: int | None = None
    jsonl_handle: TextIO | None = None
    try:
        if force and numbers is None:
            # The force reset runs first so the pending marker below is
            # computed from the watermark this run will actually use.
            # An active recovery floor is NOT cleared here: a forced run
            # drains from the beginning (a NULL watermark subsumes the
            # floor's window — the floor read below only ever RAISES a
            # non-NULL watermark to the floor), and if this run then fails
            # for an unrelated reason the floor must still be there for the
            # next one. It clears on a clean finish like any other run's.
            conn.execute(
                "UPDATE sync_state SET issues_since = NULL, last_error = NULL WHERE repo = ?",
                (repo,),
            )
            conn.commit()
        _ensure_state_row(conn, repo, include_patches, fetch_source)

        row = conn.execute(
            "SELECT issues_since, full_sync_pending, last_full_sync_at FROM sync_state WHERE repo = ?",
            (repo,),
        ).fetchone()
        since = None if row is None else row["issues_since"]
        # Recovery floor (issue #65 AC1): a previous run that could not
        # quiesce the listing persisted this floor; the run re-drains from
        # the earliest of it and the watermark, so an item the churn skipped
        # can never be sealed by the advancing per-item watermark bumps. A
        # NULL floor with the flag active is a full run's floor: from the
        # beginning. The columns arrive with migration v12; a pre-v12 store
        # simply has no floor.
        floor_cols = "listing_floor_active" in {
            r[1] for r in conn.execute("PRAGMA table_info(sync_state)")
        }
        if floor_cols:
            frow = conn.execute(
                "SELECT listing_floor, listing_floor_active FROM sync_state WHERE repo = ?",
                (repo,),
            ).fetchone()
            if frow is not None and frow["listing_floor_active"]:
                floor = frow["listing_floor"]
                if floor is None:
                    since = None
                elif since is not None and floor < since:
                    # Raise a stale watermark to the floor so the window the
                    # last run could not quiesce is re-drained. A NULL
                    # watermark already means "from the beginning", which
                    # SUBSUMES the floor's window (the plan's min() rule) —
                    # narrowing a from-the-beginning run to the floor would
                    # silently skip everything older than it.
                    since = floor
        pending_at_start = bool(row is not None and row["full_sync_pending"])
        if numbers is not None:
            # Targeted refresh (issue #23): never starts or completes a full
            # sync, and the deletion oracle never runs. A corpus that has
            # never completed a full sync stays incomplete - a numbers-only
            # store must not read as a complete graph - while an interrupted
            # full sync keeps its pending marker for the next real run.
            if row is None or (row["last_full_sync_at"] is None and not pending_at_start):
                conn.execute(
                    "UPDATE sync_state SET full_sync_pending = 1 WHERE repo = ?", (repo,)
                )
                conn.commit()
            full = False
            started_full = False
        else:
            if since is None:
                # Beginning a full sync (fresh repo or --force): mark it pending
                # so a crash mid-run leaves complete=false, and the resuming run
                # can stamp last_full_sync_at when it drains the listing.
                conn.execute(
                    "UPDATE sync_state SET full_sync_pending = 1 WHERE repo = ?", (repo,)
                )
                conn.commit()
                pending_at_start = True
            # "full" in the result means: this run completes a full sync (it
            # started one, or it resumed an interrupted one to a clean finish).
            full = since is None or pending_at_start
            started_full = since is None
        seen_numbers: set[int] = set()
        rate_window: tuple[int, str] | None = None
        # Sidecar setup is a failure path too (e.g. the target path is a
        # regular file). The finally guard tolerates a half-failed setup
        # because jsonl_handle is None until it opens. It opens BEFORE the
        # pending retry below: a torn number that turns out to be deleted
        # records its gone bundle through the same seam every other
        # deletion uses, and that needs the handle.
        if jsonl_path is not None:
            jsonl_path.mkdir(parents=True, exist_ok=True)
            jsonl_handle = (jsonl_path / "events.jsonl").open("a", encoding="utf-8")
        # Pending retry (issue #66 AC5): numbers an earlier run could not
        # resolve are refreshed first, through the exact-number seam, so
        # the advancing watermark can never seal them.
        if numbers is None:
            pending_torn = pending_numbers(conn, repo, "torn")["torn"]
            if pending_torn:
                ingested += _retry_torn_pending(
                    conn, source, repo, pending_torn, include_patches,
                    seen_numbers, jsonl_handle,
                )
                for n in pending_torn:
                    if n in seen_numbers and n not in ingested_numbers:
                        ingested_numbers.add(n)

        # Capability probe (issue #23): hydrate the cached field map or run
        # the introspection once per host+gh-version, so a schema drift never
        # reaches the page query. Duck-typed: sources without the probe pair
        # skip the whole step. Outside every per-item transaction.
        probe_failed = _attach_capabilities(conn, source, repo)
        # The listing is consumed in pages and each item commits with its
        # own watermark bump, so everything delivered before a failure is
        # durable (issue #5 AC1). A rate-limit window is slept out on the
        # injectable clock and the listing restarts from a fresh generator;
        # re-ingesting committed items is a no-op upsert and the watermark
        # never regresses. Attr-bearing errors only: anything else fails
        # fast exactly as before. A source that exposes its live budget
        # (GraphQLSource) is also checked proactively at the top of each
        # attempt, so a budget already at the floor sleeps BEFORE burning
        # the attempt (issue #5 AC4's "reaches the floor" clause).
        #
        # Pass loop (issue #65 AC1): the attempt loop below is ONE pass over
        # the window. After a pass completes, the loop runs again unless the
        # pass delivered exactly the same item set as its predecessor (the
        # quiescence condition); a boundary shift detected inside the source
        # (ListingShiftedError) ends the pass early and forces another. The
        # run's coverage is the union of every pass's deliveries, and the
        # cap (_MAX_LISTING_PASSES) fails the run incomplete rather than
        # letting a churning listing look complete.
        attempts = 0
        listing_passes = 0
        prev_pass_numbers: set[int] | None = None
        pass_numbers: set[int] = set()
        # How many rows each pass delivered per number (issue #65 AC1). A
        # source may legitimately serve several versions of one number in a
        # single walk — a recorded replay does exactly that — so "already
        # ingested" cannot be decided by the row alone: a re-delivery is a
        # verification only when the PREVIOUS pass served at least as many
        # rows for that number (see _already_covered).
        pass_counts: dict[int, int] = {}
        prev_pass_counts: dict[int, int] = {}
        # REST degradation (issue #65 AC3): set once, when a qualifying
        # GraphQL failure proves the endpoint unavailable; the run then
        # finishes over the per-item REST path and reports it.
        rest_degraded = False
        # Items this run already skipped as torn (issue #65 AC6): a later
        # pass re-delivers them from the listing, but re-attempting the
        # resolution would burn the refresh budget twice and re-log the
        # same fault — the durable pending set already holds them.
        torn_skipped: set[int] = set()
        # The updated_at each number was ingested with this run, so a
        # verification pass can skip an unchanged re-delivery (no children
        # re-fetch, no re-ingest) while still re-ingesting a NEWER row and
        # ingesting anything the first pass skipped.
        ingested_versions: dict[int, str] = {}
        # walk_since is the floor the CURRENT walk uses; a rate-window restart
        # advances it to the committed watermark (issue #65 AC2), while every
        # new pass resets it to the run's floor so the pass loop's
        # completeness argument (re-walk the whole window) still holds.
        walk_since = since
        while True:
            try:
                # Proactive floor check (issue #5 AC4 "reaches the floor"):
                # a source tracking its live budget sleeps BEFORE the attempt
                # instead of burning it on a guaranteed 429. Each floor sleep
                # counts toward the window cap, so this is bounded.
                floor = _source_budget_floor(source)
                if floor is not None and attempts < _MAX_RATE_WINDOWS:
                    rate_window = floor
                    attempts += 1
                    _rate_sleep(_BudgetWindow(*floor))
                if numbers is not None:
                    # Targeted refresh (issue #23): exactly the requested
                    # items, never the whole listing.
                    payloads = _numbered_payloads(
                        conn, source, repo, {int(n) for n in numbers}, include_patches,
                        jsonl_handle,
                    )
                else:
                    payloads = source.list_issues(walk_since)
                for payload in payloads:
                    batch = _batch(payload)
                    # One bulk children call per listing page when the source
                    # offers it (issue #5 AC2); items the source did not
                    # answer fall through to the per-item REST path. A page
                    # every row of which this run already covered (the
                    # verification pass re-listing a stable window) is NOT
                    # re-fetched: the coverage proof needs the listing page
                    # again, never its children a second time.
                    provided_map: dict[int, dict] = {}
                    fetch = getattr(source, "fetch_children", None)
                    if (
                        callable(fetch)
                        and batch
                        and not rest_degraded
                        and not _batch_fully_covered(
                            batch, ingested_versions, ingested_numbers,
                            prev_pass_counts,
                        )
                    ):
                        try:
                            provided_map = fetch(batch) or {}
                        except GitHubError as exc:
                            if _rate_attrs(exc) is not None:
                                raise  # a window: the attempt loop sleeps it out
                            if not _degradable_error(exc):
                                raise
                            # GraphQL is unavailable: degrade ONCE, log the
                            # one row, and let this and every later item take
                            # the per-item REST path.
                            rest_degraded = True
                            _log_graphql_unavailable(conn, repo, exc)
                    for list_raw in batch:
                        if not isinstance(list_raw, dict) or "number" not in list_raw:
                            continue
                        try:
                            number = int(list_raw["number"])
                        except (TypeError, ValueError):
                            conn.execute("BEGIN IMMEDIATE")
                            try:
                                log_fetch(
                                    conn, repo, "item", None,
                                    note=f"skipped malformed listing number: "
                                    f"{str(list_raw['number'])[:100]!r}",
                                )
                                conn.commit()
                            except Exception:
                                conn.rollback()
                                raise
                            continue
                        seen_numbers.add(number)
                        pass_numbers.add(number)
                        pass_counts[number] = pass_counts.get(number, 0) + 1
                        if _already_covered(
                            number,
                            ingested_versions.get(number),
                            list_raw.get("updated_at"),
                            ingested_numbers,
                            pass_counts[number],
                            prev_pass_counts,
                        ):
                            # A verification pass re-delivered a row this
                            # run already covered: coverage is proven, so
                            # nothing is re-fetched and no second
                            # observation is recorded.
                            last_number = number
                            continue
                        kind = item_kind(list_raw)
                        provided = provided_map.get(number)
                        if isinstance(provided, dict) and provided.get("gone"):
                            # A recorded deletion replaying from a sidecar
                            # (issue #23): the same first-sighting ingest-and-
                            # mark path as a live 404, with the note carried
                            # verbatim from the bundle (which recorded exactly
                            # what the live run wrote) so state_reason
                            # reproduces byte-identically for every deletion
                            # shape — HTTP 404/410, the oracle, or a targeted
                            # refresh.
                            carried_note = provided.get("gone_note")
                            carried_verdict = provided.get("gone_verdict")
                            status = provided.get("gone_status")
                            # EVERY gone observation is applied — the mark
                            # records that observation's verdict, so a later
                            # bundle (410/transferred after 404/deleted) must
                            # still land — while the run's `ingested` count
                            # stays DISTINCT per number (C2's restarted-pass
                            # contract). Only the counting is once per number.
                            marked = _ingest_and_mark_gone(
                                conn, repo, list_raw, number,
                                status if isinstance(status, int) else 404,
                                include_patches=include_patches,
                                jsonl_handle=jsonl_handle,
                                advance_watermark=numbers is None,
                                note=carried_note if isinstance(carried_note, str) else None,
                                verdict=(
                                    carried_verdict
                                    if isinstance(carried_verdict, str)
                                    and carried_verdict in ("deleted", "transferred")
                                    else "deleted"
                                ),
                            )
                            if number not in ingested_numbers:
                                ingested_numbers.add(number)
                                ingested += marked
                            ingested_versions[number] = list_raw.get("updated_at")
                            last_number = number
                            continue
                        try:
                            (pull_raw, issue_comments, review_comments, reviews,
                             files, timeline, flags, children_complete,
                             links, links_incomplete, pr_commits) = (
                                _resolve_children(
                                    source, list_raw, kind, include_patches,
                                    provided=provided,
                                )
                            )
                        except GitHubError as exc:
                            if exc.status in (404, 410):
                                # Same rule as the replayed-gone branch: the
                                # mark applies to every live 404/410
                                # observation, the distinct-item count moves
                                # once per number.
                                marked = _ingest_and_mark_gone(
                                    conn, repo, list_raw, number, exc.status,
                                    include_patches=include_patches,
                                    jsonl_handle=jsonl_handle,
                                    advance_watermark=numbers is None,
                                )
                                if number not in ingested_numbers:
                                    ingested_numbers.add(number)
                                    ingested += marked
                                ingested_versions[number] = list_raw.get("updated_at")
                                last_number = number
                                continue
                            raise

                        # Torn-row resolution (issue #65 AC6): the pull
                        # payload's updated_at strictly newer than the
                        # listing row's means the two snapshots disagree —
                        # re-fetch the listing row until it catches up. A
                        # listing that stays skewed skips the item into the
                        # durable pending set (never the forbidden mixture,
                        # and never the ABSENT-preservation path that would
                        # keep a stored row's old pull-owned trio).
                        pull_ts = (
                            pull_raw.get("updated_at")
                            if isinstance(pull_raw, dict) else None
                        )
                        list_ts = list_raw.get("updated_at")
                        if number in torn_skipped:
                            last_number = number
                            continue
                        if (
                            isinstance(pull_ts, str) and isinstance(list_ts, str)
                            and pull_ts > list_ts
                        ):
                            refreshed = _resolve_torn_row(source, list_raw, pull_ts)
                            if refreshed is None:
                                # The listing never caught up: skip the item,
                                # leave its stored row byte-unchanged, and
                                # record the durable retry.
                                torn_skipped.add(number)
                                write_pending(conn, repo, [number], "torn")
                                conn.execute("BEGIN IMMEDIATE")
                                try:
                                    log_fetch(
                                        conn, repo, "item", str(number),
                                        note=(
                                            "torn_row_unresolved: the listing row"
                                            " stayed older than the pull payload"
                                            f" (listing {list_ts}, pull {pull_ts});"
                                            " the item is pending retry"
                                        ),
                                    )
                                    conn.commit()
                                except Exception:
                                    conn.rollback()
                                    raise
                                last_number = number
                                continue
                            list_raw = refreshed

                        conn.execute("BEGIN IMMEDIATE")
                        try:
                            ingest_item(
                                conn,
                                repo,
                                list_raw,
                                pull_raw=pull_raw,
                                issue_comments=issue_comments,
                                review_comments=review_comments,
                                reviews=reviews,
                                files=files,
                                include_patches=include_patches,
                                timeline=timeline,
                                children_complete=children_complete,
                                links=links,
                                links_incomplete=links_incomplete,
                                pr_commits=pr_commits,
                                advance_watermark=numbers is None,
                            )
                            # The note covers the fetch-fallback sections
                            # only. The v10 connection gates (links_*/,
                            # pr_commits) are NOT fallback signals: an absent
                            # connection is recorded ONCE per run by the
                            # probe's omitted_fields row, and repeating it
                            # per item would drown the real degradation
                            # notes (the PR #37 noise rule).
                            incomplete = sorted(
                                s
                                for s, ok in children_complete.items()
                                if not ok and s in _LOGGABLE_SECTIONS
                            )
                            if incomplete:
                                log_fetch(
                                    conn,
                                    repo,
                                    "item",
                                    str(number),
                                    note="children incomplete: " + ", ".join(incomplete),
                                    status=None,
                                )
                            for section, etype, emsg in (
                                getattr(source, "last_fetch_drops", None) or {}
                            ).get(number, []):
                                log_fetch(
                                    conn,
                                    repo,
                                    "item",
                                    str(number),
                                    note=f"dropped {section}: {etype} {emsg}",
                                    status=None,
                                )
                            if flags.get("timeline_incomplete"):
                                log_fetch(
                                    conn,
                                    repo,
                                    "item",
                                    str(number),
                                    note=(
                                        f"timeline truncated: retained newest {len(timeline or [])} events"
                                    ),
                                    status=None,
                                )
                            for flag_name, note in sorted(
                                (f, n)
                                for f, n in (
                                    (
                                        "pr_commits_incomplete",
                                        "commits truncated: retained first"
                                        f" {len(pr_commits or [])} messages"
                                        " (union-only)",
                                    ),
                                )
                                if flags.get(f)
                            ):
                                log_fetch(
                                    conn,
                                    repo,
                                    "item",
                                    str(number),
                                    note=note,
                                    status=None,
                                )
                            for flag_name in sorted(
                                f for f in flags if f.startswith("links_overflow_")
                            ):
                                log_fetch(
                                    conn,
                                    repo,
                                    "item",
                                    str(number),
                                    note=(
                                        "links connection overflowed"
                                        f" ({flag_name.removeprefix('links_overflow_')}):"
                                        " union-only"
                                    ),
                                    status=None,
                                )
                            if flags.get("closing_refs_incomplete"):
                                closing_refs = (pull_raw or {}).get(
                                    "closing_issues_references"
                                ) or []
                                log_fetch(
                                    conn,
                                    repo,
                                    "item",
                                    str(number),
                                    note=(
                                        "closing references truncated: retained "
                                        f"first {len(closing_refs)}"
                                    ),
                                    status=None,
                                )
                            if len(files) >= 3000:
                                log_fetch(
                                    conn,
                                    repo,
                                    "pr_files",
                                    str(number),
                                    note="truncated at GitHub 3000-file cap",
                                )
                            conn.commit()
                        except Exception:
                            conn.rollback()
                            raise
                        if number not in ingested_numbers:
                            ingested_numbers.add(number)
                            ingested += 1
                        ingested_versions[number] = list_raw.get("updated_at")
                        last_number = number
                        # One replayable item_bundle per ingested item
                        # (issue #23): the bundle carries the owning number
                        # and the exact section lists ingest saw, so a replay
                        # reproduces the store row for row. A pull payload
                        # without its own number (GraphQL-shaped) is stamped
                        # on the bundle copy only - the raw dicts stay
                        # untouched so raw_json reproduces byte-identically.
                        pull_for_bundle = pull_raw
                        if isinstance(pull_for_bundle, dict) and "number" not in pull_for_bundle:
                            pull_for_bundle = {**pull_for_bundle, "number": number}
                        _write_jsonl(
                            jsonl_handle,
                            "item_bundle",
                            {
                                "number": number,
                                "item": list_raw,
                                "pull": pull_for_bundle,
                                "issue_comments": issue_comments,
                                "review_comments": review_comments,
                                "reviews": reviews,
                                "files": files,
                                "timeline": timeline,
                                # v10 sections (issue #27): the exact link
                                # and commit payloads ingest saw, with the
                                # same presence semantics (None = never
                                # carried) so replay reproduces the store.
                                "links": links,
                                "links_incomplete": links_incomplete,
                                "pr_commits": pr_commits,
                                # The exact completeness map ingest saw
                                # (issue #25): replay applies it verbatim so
                                # it can never fabricate tombstones the
                                # original run suppressed. Pre-#25 bundles
                                # lack the key; the reader fails them closed.
                                "children_complete": children_complete,
                                "gone": False,
                                "gone_status": None,
                            },
                        )
                # Pass-loop control (issue #65 AC1): this pass is done. A
                # targeted refresh is one walk and never re-drains; otherwise
                # the loop runs again unless this pass delivered exactly the
                # same item set as its predecessor (quiescence), and the cap
                # fails the run incomplete with the recovery floor persisted.
                if numbers is not None:
                    break
                listing_passes += 1
                if pass_numbers == prev_pass_numbers:
                    break
                if listing_passes >= _MAX_LISTING_PASSES:
                    raise _fail_not_quiescent(conn, repo, since, listing_passes)
                prev_pass_numbers = pass_numbers
                pass_numbers = set()
                prev_pass_counts = pass_counts
                pass_counts = {}
                attempts = 0  # a fresh pass gets its own rate-window budget
                walk_since = since  # ...and re-walks from the run's floor
                continue
            except ListingShiftedError:
                # The source's boundary verification saw the order shift
                # mid-walk: this pass ends early (its delivered items are
                # durable) and the next pass re-walks the window.
                if numbers is not None:
                    raise
                listing_passes += 1
                if listing_passes >= _MAX_LISTING_PASSES:
                    raise _fail_not_quiescent(conn, repo, since, listing_passes)
                pass_numbers = set()
                prev_pass_counts = pass_counts
                pass_counts = {}
                attempts = 0
                walk_since = since
                continue
            except GitHubError as exc:
                window = _rate_attrs(exc)
                if window is None or attempts >= _MAX_RATE_WINDOWS:
                    raise
                rate_window = window
                attempts += 1
                _rate_sleep(exc)
                # Resume the interrupted walk from the watermark the items
                # committed so far have already advanced to (issue #65 AC2) —
                # never from the run-start binding, which would re-list
                # everything the pass already delivered. The pass loop resets
                # walk_since to the run floor on the next pass.
                if numbers is None:
                    committed_since = _committed_watermark(conn, repo)
                    if committed_since is not None and (
                        walk_since is None or committed_since > walk_since
                    ):
                        walk_since = committed_since

        # Deletion oracle: only a drained full listing may mark items gone
        # (issue #5 AC5). Incremental absence marks nothing. Runs under the
        # bounded retry so a window cannot fail an otherwise-complete run.
        # A targeted refresh never runs it (issue #23): absence from its
        # filtered view is meaningless for every other item.
        if started_full:
            _deletion_pass(conn, source, repo, seen_numbers, jsonl_handle)

        # Fields the capability probe dropped from the page queries
        # (issue #23): one fetch_log row per distinct omitted field.
        omitted_fields = sorted(set(getattr(source, "omitted_fields", None) or []))
        if omitted_fields:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for field in omitted_fields:
                    log_fetch(conn, repo, "graphql_probe", None, note=f"omitted {field}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        if numbers is not None:
            # Targeted refresh: releases are corpus-level, not item-level;
            # a targeted run does not refresh them (issue #23).
            releases: list[dict] = []
        else:
            try:
                releases = _call_with_rate_retry(source.list_releases)
            except GitHubError as exc:
                conn.execute("BEGIN IMMEDIATE")
                set_last_error(conn, repo, str(exc))
                conn.commit()
                raise SyncError(str(exc)) from exc

        if rate_window is None:
            # A clean run that slept no window still reports the last budget
            # the source observed (GraphQLSource tracks it per page query).
            rate_window = _source_budget_floor(source)

        conn.execute("BEGIN IMMEDIATE")
        try:
            if numbers is None:
                replace_releases(conn, repo, releases)
            mark_sync_finished(
                conn,
                repo,
                rate_limit_remaining=None if rate_window is None else rate_window[0],
                rate_limit_reset_at=None if rate_window is None else rate_window[1],
                targeted=numbers is not None,
                ingested_count=ingested,
            )
            if numbers is None and floor_cols:
                # The listing quiesced and the run finished clean: the
                # recovery floor is resolved and cleared (a targeted refresh
                # never re-drains the listing, so it never clears it).
                conn.execute(
                    "UPDATE sync_state SET listing_floor = NULL,"
                    " listing_floor_active = 0 WHERE repo = ?",
                    (repo,),
                )
            recount(conn, repo)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        for rec in releases:
            _write_jsonl(jsonl_handle, "release", rec)
        # Best-effort planner maintenance (issue #22): SQLite decides what to
        # analyze (observed: the FTS shadow tables on this schema). The data,
        # counts, and finish stamps above are already durably committed, and
        # the hot read paths use forced indexes or explicit UNION ALL equality
        # seeks in the per-item/per-call paths — so a maintenance failure must
        # never turn a committed sync into a reported one. Only lock
        # contention is swallowed: disk-full or corruption during the ANALYZE
        # write is a storage fault and still propagates, per the
        # report-don't-swallow rule.
        try:
            conn.execute("PRAGMA optimize")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
    except BaseException as exc:
        # Every failure leaves a truthful trail. A BaseException (decode
        # error, AttributeError on a dead reader thread, KeyboardInterrupt)
        # can escape between BEGIN IMMEDIATE and commit, so roll any open
        # transaction back before recording, or the recorder itself would
        # hit "cannot start a transaction within a transaction".
        if conn.in_transaction:
            conn.rollback()
        if isinstance(exc, (GitHubError, SyncError)) and getattr(
            source, "_capabilities_from_cache", False
        ):
            # Self-heal the capability cache (PR #37 review): a fatal error
            # while running on a CACHED capability map may be server-side
            # schema drift the snapshot cannot see, and nothing else
            # invalidates the row. Drop it best-effort so the next sync
            # re-probes; the current run still fails and reports.
            try:
                key = getattr(
                    source, "_capabilities_meta_key",
                    f"capability_probe:v2:{_gh_host()}:{_gh_version(getattr(source, 'gh_bin', 'gh'))}",
                )
                conn.execute("DELETE FROM meta WHERE key = ?", (key,))
                conn.commit()
                source._capabilities_from_cache = False
            except sqlite3.Error:
                pass  # a dying database cannot heal its cache either
        try:
            conn.execute("BEGIN IMMEDIATE")
            set_last_error(conn, repo, _error_text(exc))
            conn.commit()
        except sqlite3.Error:
            pass  # a dying database cannot record; propagate the original
        if isinstance(exc, (GitHubError, sqlite3.Error)):
            raise SyncError(str(exc)) from exc
        raise
    finally:
        if jsonl_handle is not None:
            # Every record is flushed as it is written, so a failing close
            # loses nothing; and an exception raised in finally is not caught
            # by the sibling except, so it must not escape unrecorded.
            try:
                jsonl_handle.close()
            except OSError:
                pass

    state = conn.execute(
        "SELECT * FROM sync_state WHERE repo = ?", (repo,)
    ).fetchone()
    return {
        "repo": repo,
        "ingested": ingested,
        "last_number": last_number,
        "full": full,
        "finished_at": utcnow(),
        "issues_since": None if state is None else state["issues_since"],
        "item_count": None if state is None else state["item_count"],
        "comment_count": None if state is None else state["comment_count"],
        "edge_count": None if state is None else state["edge_count"],
        "last_error": None if state is None else state["last_error"],
        "rate_limit_remaining": None if rate_window is None else rate_window[0],
        "rate_limit_reset_at": None if rate_window is None else rate_window[1],
        "source": "rest (graphql unavailable)" if rest_degraded else "graphql",
        "graphql_probe_failed": bool(probe_failed),
    }


# --- whole-run sync lock (issue #2 AC5) -------------------------------------
#
# Layout: byte 0 is a sentinel kept under an OS byte-range lock; the JSON
# payload {"pid", "host", "started_at"} lives at offset >= 1. Windows range
# locks are MANDATORY, so per-reader rules differ: the acquirer reads through
# its own locked descriptor (exempt from the lock) including byte 0 and
# parses from the first "{", which also tolerates hand-written files whose
# payload starts at 0; unlocked observers (e.g. `where`) read from offset 1
# best-effort and degrade when no "{" is found. The file is never unlinked
# by a process that does not own it: takeover rewrites the payload in place
# while holding the range lock, which makes concurrent stale-recovery atomic.


def lock_path_for(db_path: Path) -> Path:
    # Resolve first (PRR-002): raw --db spellings of the same file (relative
    # forms, trailing dots, symlink aliases) must share one lock.
    return Path(str(Path(db_path).resolve()) + ".sync.lock")


def _lock_range(fd: int) -> None:
    """Exclusive non-blocking lock on byte 0; raises OSError when held."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_range(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _write_payload_locked(fd: int, payload: dict) -> None:
    """Truncate-then-write at the sentinel layout (byte 0 = NUL sentinel,
    payload at offset 1). Truncation matters: an in-place shorter write over
    a longer hand-written payload would leave tail residue and a stale `{`
    at byte 0 that breaks parse-from-first-`{` for every later reader."""
    blob = b"\x00" + json.dumps(payload).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, blob)


def _read_payload_locked(fd: int) -> dict | None:
    """Acquirer-side read through the locked descriptor: from byte 0, parse
    from the first '{' (covers both the sentinel layout and hand-written
    files). None = no payload (free lock)."""
    os.lseek(fd, 0, os.SEEK_SET)
    data = os.read(fd, 65536)
    idx = data.find(b"{")
    if idx < 0:
        return None
    try:
        payload = json.loads(data[idx:].decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _observe_lock_file(lock_path: Path) -> dict | str | None:
    """Read an ALREADY-DERIVED lock file path (offset 1, best-effort)."""
    try:
        with open(lock_path, "rb") as fh:
            fh.seek(1)
            data = fh.read(65536)
    except OSError:
        return None
    idx = data.find(b"{")
    if idx < 0:
        if data:
            return "held (holder unreadable)"
        return None
    try:
        payload = json.loads(data[idx:].decode("utf-8", errors="replace"))
    except ValueError:
        return "held (holder unreadable)"
    return payload if isinstance(payload, dict) else "held (holder unreadable)"


def read_lock_observer(db_path: Path) -> dict | str | None:
    """Observer-side read (`where`): derives the lock path from the db."""
    return _observe_lock_file(lock_path_for(db_path))


def _pid_alive(pid: int) -> bool:
    """Liveness of a recorded lock pid. Windows never uses os.kill(pid, 0)
    for this (it can terminate the process); OpenProcess failure with
    ERROR_ACCESS_DENIED means ALIVE - fail closed, never steal."""
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 0x103
        ERROR_ACCESS_DENIED = 5
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ctypes.GetLastError() == ERROR_ACCESS_DENIED
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        if not ok:
            return False
        return code.value == STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SyncLock:
    """A held whole-run lock. `release_owned` only on the acquired path;
    `abandon` on join/held paths so a joining sync never wipes the holder's
    payload."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self.fd = fd

    def release_owned(self) -> None:
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            os.ftruncate(self.fd, 0)
        finally:
            self._abandon()

    def abandon(self) -> None:
        """Unlock + close without touching the payload. The join/held paths
        in acquire_sync_lock call this; only the owner may truncate."""
        self._abandon()

    def _abandon(self) -> None:
        try:
            _unlock_range(self.fd)
        except OSError:
            pass
        finally:
            os.close(self.fd)
            self.fd = -1


_FAULT_REPROBE_PAUSE = 0.02  # bounded re-probe pause (PRR-008)


def acquire_sync_lock(
    db_path: Path, *, wait: bool = False, poll_s: float = 0.2
) -> SyncLock | None:
    """Acquire the whole-run lock for db_path. None = held (join).

    Same-host holders with a dead pid are taken over atomically under the
    range lock; foreign-host holders are never stolen. wait=True polls until
    the lock frees instead of returning None.
    """
    path = lock_path_for(db_path)
    while True:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            _lock_range(fd)
        except OSError as lock_fault:
            os.close(fd)
            # Distinguish contention from an environmental fault (PRR-008).
            # The first instant is ambiguous: a genuine holder may hold the
            # range lock with its payload not yet (re)written, while a
            # transient environmental fault fails identically. Bounded
            # re-probe (reviewer prescription): after one short pause,
            # a holder still locks the file AND its payload is observable;
            # a persistent fault keeps failing with nothing to observe -
            # report it (a silent joined:true here was the original
            # PRR-008 silent false-success).
            time.sleep(_FAULT_REPROBE_PAUSE)
            fd2 = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                _lock_range(fd2)
            except OSError:
                os.close(fd2)
                holder = _observe_lock_file(path)
                if holder is None:
                    raise lock_fault
                if not wait:
                    return None
                time.sleep(poll_s)
                continue
            # The re-probe ACQUIRED the range lock: the earlier failure was
            # transient (and any prior holder died within the window, where
            # our payload legitimately replaces theirs). Take ownership.
            _write_payload_locked(
                fd2, {"pid": os.getpid(), "host": socket.gethostname(), "started_at": utcnow()}
            )
            return SyncLock(path, fd2)
        payload = _read_payload_locked(fd)
        if payload is None:
            _write_payload_locked(
                fd, {"pid": os.getpid(), "host": socket.gethostname(), "started_at": utcnow()}
            )
            return SyncLock(path, fd)
        holder_host = str(payload.get("host", ""))
        try:
            holder_pid = int(payload.get("pid", -1))
        except (TypeError, ValueError):
            holder_pid = -1
        if holder_host == socket.gethostname() and not _pid_alive(holder_pid):
            _write_payload_locked(
                fd, {"pid": os.getpid(), "host": socket.gethostname(), "started_at": utcnow()}
            )
            return SyncLock(path, fd)
        SyncLock(path, fd).abandon()
        if not wait:
            return None
        time.sleep(poll_s)
