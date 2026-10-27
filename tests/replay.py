"""JsonlReplaySource: replay a recorded `sync --jsonl` sidecar through the
real sync/ingest path (issue #23 AC7; observation fidelity extended by #25).

The sidecar's `item_bundle` record (written per ingested item by
zaxbygraph.sync) carries the owning number, the exact list payload, the
exact section lists ingest saw, and — since #25 — the exact
`children_complete` map. Feeding bundles back through sync_repo reproduces
items, children, edges, AND revision history row for row.

Recording contract (changed by #25; do NOT key observations by
`(number, item.updated_at)` — that pair is NOT unique and a LAST-WINS rule
silently discards same-timestamp history):

- Bundles are observations in append order. ADJACENT records for one number
  whose COMPLETE bundle payload (item, pull, every section list, timeline,
  children_complete, gone, gone_status, gone_note, gone_verdict) compares
  equal collapse — a resumed sync re-records the same observation.
  Nonconsecutive repetitions NEVER collapse: a byte-identical A→B→A keeps
  all three observations, so the revert revision survives.
- `list_issues` yields pages with at most one version of each number, and
  every page producer (`list_issues` AND `get_issues`, the targeted-refresh
  path) registers the exact bundles behind it on a page cursor;
  `fetch_children` consumes that cursor so each version re-ingests with its
  OWN children — including two versions whose item payloads are
  byte-identical but whose children differ. The cursor is replay-only
  machinery: live sources have none, and `_resolve_children` never sees it.
- Bundles recorded before #25 lack `children_complete`; the reader injects
  an all-False map (fail closed) so replay can never fabricate deletion
  evidence the original run did not observe. `_resolve_children` applies a
  present recorded map verbatim and treats an absent key exactly as before,
  so live bulk responses are unaffected.

A gone bundle is served as `{"gone": True, ...}` with its recorded deletion
metadata (status, note, verdict): sync routes it through the same
first-sighting 404/410 ingest-and-mark path a live 404 takes, so `state`,
`state_reason`, and `raw_json` reproduce exactly. This source has no probe
methods and no `check_deleted`: sync's duck-typed guards skip both.
"""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path

#: The bulk child sections a bundle records, plus the GraphQL-only keys the
#: recorded completeness map also covers.
_RECORDED_SECTIONS = ("issue_comments", "review_comments", "reviews", "files")
_RECORDED_COMPLETENESS_KEYS = (*_RECORDED_SECTIONS, "pull", "timeline")


def _absent_completeness() -> dict[str, bool]:
    """Fail-closed map for bundles recorded before #25: nothing the original
    run did not observe may become deletion evidence on replay."""
    return {name: False for name in _RECORDED_COMPLETENESS_KEYS}


def _bundle_identity(payload: dict) -> bytes:
    """Byte identity of a COMPLETE bundle payload — every field, deletion
    metadata included, so two deletions with different notes/verdicts never
    compare equal."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")


class JsonlReplaySource:
    """A GitHubSource over a recorded events.jsonl sidecar."""

    def __init__(self, events_path: Path | str) -> None:
        self._path = Path(events_path)
        # Observations per number, append order, adjacent exact duplicates
        # collapsed (resume re-records); nonconsecutive repeats survive.
        observations: dict[int, list[dict]] = {}
        last_identity: dict[int, bytes] = {}
        releases: dict[object, dict] = {}
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                resource = record.get("resource")
                payload = record.get("payload")
                if resource == "item_bundle" and isinstance(payload, dict):
                    try:
                        number = int(payload.get("number"))
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(payload.get("children_complete"), dict):
                        payload = {
                            **payload,
                            "children_complete": _absent_completeness(),
                        }
                    identity = _bundle_identity(payload)
                    if last_identity.get(number) == identity:
                        continue
                    last_identity[number] = identity
                    observations.setdefault(number, []).append(payload)
                elif resource == "release" and isinstance(payload, dict):
                    releases[payload.get("id")] = payload
        # (updated_at, number, append order) — the replay contract. Equal
        # keys keep append order, so same-timestamp observations stay
        # sequenced.
        decorated = []
        for seq, (number, bundles) in enumerate(observations.items()):
            for offset, bundle in enumerate(bundles):
                decorated.append(
                    ((bundle.get("item") or {}).get("updated_at") or "", number, seq, offset, bundle)
                )
        decorated.sort(key=lambda entry: (entry[0], entry[1], entry[2], entry[3]))
        self._bundles = [entry[4] for entry in decorated]
        self._by_number: dict[int, dict] = {
            int(bundle["number"]): bundle for bundle in self._bundles
        }
        self._releases = list(releases.values())
        # Page cursor: one entry per listing/shells page, consumed by the
        # matching fetch_children call (sync fetches immediately after taking
        # a page).
        self._page_cursor: deque[dict[int, dict]] = deque()

    @staticmethod
    def _build_pages(bundles: list[dict]) -> list[list[dict]]:
        """Chunk bundles so each page carries at most one version of a
        number: sync's provided children map is keyed by number per page."""
        pages: list[list[dict]] = []
        page: list[dict] = []
        seen: set[int] = set()
        for bundle in bundles:
            number = int(bundle["number"])
            if number in seen:
                pages.append(page)
                page = []
                seen = set()
            page.append(bundle)
            seen.add(number)
        if page:
            pages.append(page)
        return pages

    def _enqueue(self, bundles: list[dict]) -> None:
        self._page_cursor.append(
            {int(bundle["number"]): bundle for bundle in bundles}
        )

    def list_issues(self, since: str | None):
        selected = [
            bundle
            for bundle in self._bundles
            if since is None or (bundle["item"].get("updated_at") or "") >= since
        ]
        for page in self._build_pages(selected):
            self._enqueue(page)
            yield [bundle["item"] for bundle in page]

    def get_issues(self, numbers: list[int]) -> dict[int, tuple[dict | None, int | None]]:
        """Exact-number shells from the recorded bundles (numbers mode).

        A number absent from the recording is `(None, None)` — unknown, not
        an HTTP 404 — so sync skips it with a note instead of marking a
        deletion reason the replay never observed. The LATEST bundle per
        number is the shell, and the same latest bundles (gone included) are
        registered as the next cursor entry so the targeted path re-ingests
        with its own children."""
        out: dict[int, tuple[dict | None, int | None]] = {}
        registered: list[dict] = []
        for number in numbers:
            bundle = self._by_number.get(int(number))
            out[int(number)] = (None, None) if bundle is None else (bundle["item"], 200)
            if bundle is not None:
                registered.append(bundle)
        if registered:
            self._enqueue(registered)
        return out

    def fetch_children(self, items) -> dict[int, dict]:
        page = self._page_cursor.popleft() if self._page_cursor else {}
        out: dict[int, dict] = {}
        for raw in items:
            if not isinstance(raw, dict) or "number" not in raw:
                continue
            try:
                number = int(raw["number"])
            except (TypeError, ValueError):
                continue
            bundle = page.get(number)
            if bundle is None:
                continue
            if bundle.get("gone"):
                served = {
                    "gone": True,
                    "gone_status": bundle.get("gone_status") or 404,
                }
                if bundle.get("gone_note"):
                    # The recorded note reproduces state_reason exactly for
                    # every deletion shape (HTTP status, oracle, targeted).
                    served["gone_note"] = bundle["gone_note"]
                if bundle.get("gone_verdict"):
                    # The oracle can report 'transferred' as well as 'deleted':
                    # the verdict travels so replay reproduces items.state.
                    served["gone_verdict"] = bundle["gone_verdict"]
                out[number] = served
                continue
            child: dict = {}
            for section in ("pull", *_RECORDED_SECTIONS):
                if bundle.get(section) is not None:
                    child[section] = bundle[section]
            child["timeline"] = list(bundle.get("timeline") or [])
            # The recorded completeness map rides the provided payload into
            # _resolve_children, which applies it verbatim (issue #25).
            child["children_complete"] = dict(
                bundle.get("children_complete") or _absent_completeness()
            )
            out[number] = child
        return out

    def list_releases(self) -> list[dict]:
        return list(self._releases)

    # -- deliberate REST-fallback absence ------------------------------------
    #
    # The recorded bundles ARE the child data; there is nothing to fall back
    # to. These stubs convert what would otherwise be a raw AttributeError
    # (sync's per-item REST fallback calling a missing method) into the
    # repo's standard GitHubError, so `--include-patches` replays and edge
    # shapes fail as a clean reported sync error instead of a traceback
    # (PR #37 review). With a recorded completeness map the resolver never
    # reaches these for recorded sections (issue #25).

    @staticmethod
    def _no_rest_fallback(name: str) -> None:
        from zaxbygraph.github import GitHubError

        raise GitHubError(
            f"replay source has no REST fallback ({name}): the recorded bundles "
            'are the child data; re-record the sidecar with the flags you need '
            "(e.g. --include-patches)"
        )

    def get_pull(self, number: int) -> dict:
        self._no_rest_fallback("get_pull")

    def list_issue_comments(self, number: int) -> list[dict]:
        self._no_rest_fallback("list_issue_comments")

    def list_reviews(self, number: int) -> list[dict]:
        self._no_rest_fallback("list_reviews")

    def list_review_comments(self, number: int) -> list[dict]:
        self._no_rest_fallback("list_review_comments")

    def list_pr_files(self, number: int) -> list[dict]:
        self._no_rest_fallback("list_pr_files")
