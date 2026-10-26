"""JsonlReplaySource: replay a recorded `sync --jsonl` sidecar through the
real sync/ingest path (issue #23 AC7).

The sidecar's `item_bundle` record (written per ingested item by
zaxbygraph.sync) carries the owning number, the exact list payload, and the
exact section lists ingest saw, so feeding bundles back through sync_repo
reproduces items, children, and edges row for row. Bundles are deduplicated
by `(number, item.updated_at)` LAST-WINS — the sidecar opens in append mode,
so a resumed sync may duplicate lines and the newest record for a key is the
truthful one. Release records deduplicate by id the same way (one
replace_releases call must never see the same id twice).

A gone bundle is served as `{"gone": True, "gone_status": <status>}` with
empty sections: sync routes it through the same first-sighting 404/410
ingest-and-mark path a live 404 takes, so `state`, `state_reason`, and
`raw_json` reproduce exactly. This source has no probe methods and no
`check_deleted`: sync's duck-typed guards skip both.
"""
from __future__ import annotations

import json
from pathlib import Path


class JsonlReplaySource:
    """A GitHubSource over a recorded events.jsonl sidecar."""

    def __init__(self, events_path: Path | str) -> None:
        self._path = Path(events_path)
        bundles: dict[tuple[int, str], dict] = {}
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
                    item = payload.get("item") or {}
                    key = (payload.get("number"), item.get("updated_at"))
                    bundles[key] = payload
                elif resource == "release" and isinstance(payload, dict):
                    releases[payload.get("id")] = payload
        # (updated_at, number) order, per the replay contract.
        self._bundles = [
            bundles[key] for key in sorted(bundles, key=lambda k: (k[1], k[0]))
        ]
        self._by_number: dict[int, dict] = {
            int(bundle["number"]): bundle for bundle in self._bundles
        }
        self._releases = list(releases.values())

    def list_issues(self, since: str | None):
        page = [
            bundle["item"]
            for bundle in self._bundles
            if since is None or (bundle["item"].get("updated_at") or "") >= since
        ]
        if page:
            yield page

    def get_issues(self, numbers: list[int]) -> dict[int, dict | None]:
        """Exact-number shells from the recorded bundles (numbers mode)."""
        out: dict[int, dict | None] = {}
        for number in numbers:
            bundle = self._by_number.get(int(number))
            out[int(number)] = None if bundle is None else bundle["item"]
        return out

    def fetch_children(self, items) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for raw in items:
            if not isinstance(raw, dict) or "number" not in raw:
                continue
            try:
                number = int(raw["number"])
            except (TypeError, ValueError):
                continue
            bundle = self._by_number.get(number)
            if bundle is None:
                continue
            if bundle.get("gone"):
                out[number] = {
                    "gone": True,
                    "gone_status": bundle.get("gone_status") or 404,
                }
                continue
            child: dict = {}
            for section in ("pull", "issue_comments", "review_comments", "reviews", "files"):
                if bundle.get(section) is not None:
                    child[section] = bundle[section]
            child["timeline"] = list(bundle.get("timeline") or [])
            out[number] = child
        return out

    def list_releases(self) -> list[dict]:
        return list(self._releases)
