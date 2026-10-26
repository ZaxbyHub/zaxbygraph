"""Supplementary coverage for issue #23 (non-frozen; ships with the fix).

Where the frozen checks in test_sync_plumbing.py pin each acceptance
criterion's headline behavior, these tests pin the surrounding contract the
plan committed to: probe fail-open + fetch_log trails, the persisted
capability cache and its invalidation, the null-section REST fallback
guardrail, targeted-sync state rules (fresh/incomplete, interrupted-full,
stale-error clearing, watermarkless fallback, stamps untouched), gone-bundle
replay fidelity, and duplicated-sidecar dedup. RED at base is implied by the
frozen checks; here every test is GREEN on the fixed tree and each names the
mutation that would kill it in its docstring.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from fixtures import REPO, FakeGitHubSource, TempDBTest, comment, issue, pr_file, pull, ts
from fixtures_bulk import PageShapedBulkSource, add_page_prs
from zaxbygraph.db import connect, get_meta, init_schema
from zaxbygraph.github import GitHubError
from zaxbygraph.graphql import GraphQLSource
from zaxbygraph.sync import _gh_host, _gh_version, sync_repo
from test_sync_plumbing import (
    _FULL_SCHEMA,
    _BundleSource,
    _FakeGhHost,
    _listing_item,
    _page_payload,
)


def _capabilities_key(version: str | None = None) -> str:
    """The meta key sync derives, or an explicitly different one."""
    if version is None:
        version = _gh_version(sys.executable)
    return f"capability_probe:{_gh_host()}:{version}"


class _GoneBulkSource(PageShapedBulkSource):
    """Bulk source whose fetch_children reports item 2 as a recorded
    deletion, exactly as a sidecar replay serves a gone bundle."""

    def fetch_children(self, items) -> dict:
        out = super().fetch_children(items)
        if any(int(r["number"]) == 2 for r in items if isinstance(r, dict)):
            out[2] = {"gone": True, "gone_status": 404}
        return out


class ProbeTrailTests(TempDBTest):
    """Probe fail-open, omitted-field rows, and drop notes in fetch_log."""

    def test_probe_failure_fails_open(self) -> None:
        """A probe that raises must not fail the sync: the run completes with
        the full field set and one fetch_log graphql_probe row names the
        failure. Killed by: propagating the probe error out of sync_repo."""
        host = _FakeGhHost()
        host.write_schema(_FULL_SCHEMA)
        host.write_graphql_responses([_page_payload([1])])
        host.write_rest_responses(
            [[issue(1, title="one", comments=1, updated_at=ts(10))], [], []]
        )
        with host:
            src = host.source()

            def boom(self):
                raise GitHubError("introspection denied", status=403)

            with patch.object(GraphQLSource, "probe_capabilities", boom):
                result = sync_repo(self.conn, src, REPO)
        self.assertIsNone(result["last_error"])
        self.assertEqual(result["ingested"], 1)
        notes = [
            r["note"]
            for r in self.conn.execute(
                "SELECT note FROM fetch_log WHERE resource = 'graphql_probe'"
            ).fetchall()
        ]
        self.assertTrue(
            any("probe failed" in (n or "") for n in notes),
            "the probe failure must be recorded in fetch_log",
        )

    def test_omitted_fields_recorded(self) -> None:
        """A schema missing timelineItems must leave one fetch_log row per
        omitted field (resource graphql_probe, note 'omitted <field>') and
        still complete. Killed by: dropping the omission logging block."""
        host = _FakeGhHost()
        host.write_schema(
            {"Issue": ["databaseId", "comments"], "PullRequest": _FULL_SCHEMA["PullRequest"]}
        )
        host.write_graphql_responses([_page_payload([1])])
        host.write_rest_responses(
            [[issue(1, title="one", comments=1, updated_at=ts(10))], [], []]
        )
        with host:
            src = host.source()
            result = sync_repo(self.conn, src, REPO)
        self.assertIsNone(result["last_error"])
        notes = [
            r["note"]
            for r in self.conn.execute(
                "SELECT note FROM fetch_log WHERE resource = 'graphql_probe'"
            ).fetchall()
        ]
        # exact: one row per omitted field, no spurious rows (a mutant that
        # records omissions for fields it still selects would add rows here)
        self.assertEqual(notes, ["omitted timelineItems"])

    def test_drop_note_recorded(self) -> None:
        """A section-scoped FORBIDDEN must leave a fetch_log note naming the
        dropped section (mirrors frozen C2's scenario). Killed by: removing
        the per-drop fetch_log write in sync_repo."""
        host = _FakeGhHost()
        host.write_schema(_FULL_SCHEMA)
        host.write_graphql_responses(
            [
                {
                    "data": {
                        "rateLimit": {"remaining": 4321, "resetAt": "2026-10-03T13:00:00Z"},
                        "repository": {
                            "i1": {
                                "databaseId": 10_001,
                                "comments": None,
                            },
                        },
                    },
                    "errors": [
                        {
                            "type": "FORBIDDEN",
                            "message": "mocked per-field FORBIDDEN on i1 comments",
                            "path": ["repository", "i1", "comments"],
                        }
                    ],
                }
            ]
        )
        host.write_rest_responses(
            [
                [issue(1, title="one", comments=1, updated_at=ts(10))],
                [comment(50_001, "fallback")],
                [],
            ]
        )
        with host:
            result = sync_repo(self.conn, host.source(), REPO)
        self.assertIsNone(result["last_error"])
        notes = [
            r["note"]
            for r in self.conn.execute("SELECT note FROM fetch_log").fetchall()
        ]
        self.assertTrue(
            any(
                (n or "").startswith("dropped comments: FORBIDDEN")
                for n in notes
            ),
            "the dropped section must be named in fetch_log",
        )


class NullSectionGuardTests(TempDBTest):
    """The presence-vs-nullness guardrail (issue #23 localization finding)."""

    def test_null_section_falls_back_to_rest(self) -> None:
        """A GraphQL node whose comments key is present but None (permission
        mask) must degrade to the per-item REST fallback instead of storing a
        silently empty complete section. Killed by: reverting the null guard
        to a bare `'comments' in node` presence check."""
        host = _FakeGhHost()
        host.write_schema(_FULL_SCHEMA)
        host.write_graphql_responses(
            [
                {
                    "data": {
                        "rateLimit": {"remaining": 4321, "resetAt": "2026-10-03T13:00:00Z"},
                        "repository": {
                            "i1": {"databaseId": 10_001, "comments": None},
                        },
                    }
                }
            ]
        )
        host.write_rest_responses(
            [
                [issue(1, title="one", comments=1, updated_at=ts(10))],
                [comment(50_001, "fallback comment")],
                [],
            ]
        )
        with host:
            result = sync_repo(self.conn, host.source(), REPO)
            rest_paths = host.rest_paths()
        self.assertIsNone(result["last_error"])
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM comments WHERE repo = ?", (REPO,)
            ).fetchone()[0],
            1,
            "the masked section must be completed over REST, not stored empty",
        )
        self.assertTrue(
            any(p.startswith("repos/acme/forgegate/issues/1/comments") for p in rest_paths)
        )


class CapabilityCacheTests(TempDBTest):
    """The persisted meta cache: reuse across sources, invalidation by
    host/gh-version, and no probe machinery for sources without the pair."""

    def test_non_lead_field_omitted_from_query(self) -> None:
        """Per-FIELD omission (issue #23 review finding 1): a probe reporting
        a NON-lead PullRequest field absent (closingIssuesReferences — not a
        fragment lead, buried mid-scalars at base granularity) must remove
        exactly that field from the page query and record the omission.
        Killed by: gating whole field blocks on their lead field only."""
        pr_fields = [f for f in _FULL_SCHEMA["PullRequest"] if f != "closingIssuesReferences"]
        host = _FakeGhHost()
        host.write_schema({"Issue": _FULL_SCHEMA["Issue"], "PullRequest": pr_fields})
        pr_node = {
            "databaseId": 10_001,
            "baseRefName": "main",
            "mergedAt": None,
            "additions": 1,
            "deletions": 0,
            "changedFiles": 0,
            "commits": {"totalCount": 1},
            "mergeCommit": None,
            "headRefName": "feature",
            "isDraft": False,
            "mergedBy": None,
        }
        host.write_graphql_responses(
            [
                {
                    "data": {
                        "rateLimit": {"remaining": 4321, "resetAt": "2026-10-03T13:00:00Z"},
                        "repository": {"p1": pr_node},
                    }
                }
            ]
        )
        pr_item = _listing_item(1)
        pr_item["pull_request"] = {"url": "https://api.github.com/repos/x"}  # PR-shaped alias
        with host:
            src = host.source()
            src.probe_capabilities()
            children = src.fetch_children([pr_item])
            calls = host.calls()
        self.assertIn(1, children)
        pages = [c for c in calls if not c["probe"]]
        self.assertGreaterEqual(len(pages), 1)
        for page in pages:
            self.assertNotIn(
                "closingIssuesReferences",
                page["query"],
                "a probed-absent NON-lead field must be omitted from the page query",
            )
            self.assertIn("isDraft", page["query"], "sibling fields must stay selected")
        self.assertIn("closingIssuesReferences", src.omitted_fields)

    def test_second_source_reuses_persisted_probe(self) -> None:
        """After one sync probed, a SECOND source over the same store must
        hydrate from meta: no additional introspection call, AND the hydrated
        map must actually filter the second source's page query (behavioral
        pin — a no-op attach_capabilities must fail this test; PR #37 review
        round 2)."""
        schema = {"Issue": ["databaseId", "comments"], "PullRequest": _FULL_SCHEMA["PullRequest"]}
        host = _FakeGhHost()
        host.write_schema(schema)
        # one GraphQL page per sync (the fake serves them by call counter)
        host.write_graphql_responses([_page_payload([1]), _page_payload([1])])
        # per sync: (since-filtered listing, releases) — 2 REST calls each
        listing = [issue(1, title="one", comments=1, updated_at=ts(10))]
        host.write_rest_responses([listing, [], listing, []])
        with host:
            first = sync_repo(self.conn, host.source(), REPO)
            probes_after_first = sum(1 for c in host.calls() if c["probe"])
            second = sync_repo(self.conn, host.source(), REPO)
            probes_after_second = sum(1 for c in host.calls() if c["probe"])
            pages = [c for c in host.calls() if not c["probe"]]
        self.assertIsNone(first["last_error"])
        self.assertIsNone(second["last_error"])
        self.assertEqual(first["ingested"], 1)
        self.assertEqual(second["ingested"], 1, "the second sync must really re-ingest")
        self.assertEqual(probes_after_first, 1)
        self.assertEqual(
            probes_after_second,
            1,
            "the second source must hydrate capabilities from the meta cache",
        )
        # BEHAVIORAL pin: the second sync's page query was built from the
        # hydrated map (timelineItems absent from the schema) without any
        # second probe — an attach no-op would leave timelineItems selected.
        self.assertGreaterEqual(len(pages), 2)
        for page in pages:
            self.assertNotIn(
                "timelineItems",
                page["query"],
                "the hydrated capability map must filter the page query",
            )
        cached = get_meta(self.conn, _capabilities_key())
        self.assertIsNotNone(cached)
        self.assertIn("Issue", json.loads(cached))

    def test_version_change_forces_reprobe(self) -> None:
        """A cached row keyed by a DIFFERENT gh version must not be reused:
        the sync re-probes and writes the new key. Killed by: keying the meta
        row without the version (or ignoring the key mismatch)."""
        host = _FakeGhHost()
        host.write_schema({"Issue": ["databaseId", "comments"], "PullRequest": _FULL_SCHEMA["PullRequest"]})
        host.write_graphql_responses([_page_payload([1])])
        host.write_rest_responses(
            [[issue(1, title="one", comments=1, updated_at=ts(10))], [], []]
        )
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            (
                _capabilities_key(version="stale-version-string"),
                json.dumps({"Issue": ["databaseId"], "PullRequest": []}),
            ),
        )
        self.conn.commit()
        with host:
            result = sync_repo(self.conn, host.source(), REPO)
            probe_calls = sum(1 for c in host.calls() if c["probe"])
        self.assertIsNone(result["last_error"])
        self.assertEqual(probe_calls, 1)
        self.assertIsNotNone(get_meta(self.conn, _capabilities_key()))

    def test_sources_without_probe_pair_skip_metadata(self) -> None:
        """A source with neither probe_capabilities nor attach_capabilities
        must complete with NO capability meta row and NO graphql_probe rows.
        Killed by: calling the metadata path unguarded."""
        src = PageShapedBulkSource()
        add_page_prs(src, 1)
        result = sync_repo(self.conn, src, REPO)
        self.assertIsNone(result["last_error"])
        self.assertIsNone(get_meta(self.conn, _capabilities_key()))
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE resource = 'graphql_probe'"
        ).fetchone()[0]
        self.assertEqual(rows, 0)


class _NumberedSource(FakeGitHubSource):
    """A numbers-capable fake: get_issues serves exactly the requested
    numbers as (payload, status) tuples; missing numbers carry the status
    the caller states (404/410) or None when unknown."""

    def __init__(self) -> None:
        super().__init__()
        self.missing_status: dict[int, int | None] = {}

    def get_issues(self, numbers):
        out: dict[int, tuple[dict | None, int | None]] = {}
        for n in numbers:
            rec = self.issues.get(int(n))
            if rec is not None:
                out[int(n)] = (deepcopy(rec), 200)
            else:
                out[int(n)] = (None, self.missing_status.get(int(n), 404))
        return out


class NumbersModeStateTests(TempDBTest):
    """Targeted-sync state rules (plan rev 4, R2/R3/R5/R6)."""

    def _seed_state(self, *, since=None, pending=0, last_full=None, last_incr=None, error=None):
        self.conn.execute(
            "INSERT INTO sync_state(repo, issues_since, full_sync_pending, "
            "last_full_sync_at, last_incr_sync_at, last_error) VALUES (?, ?, ?, ?, ?, ?)",
            (REPO, since, pending, last_full, last_incr, error),
        )
        self.conn.commit()

    def _run_numbers(self, src, numbers):
        from zaxbygraph.cli import cmd_sync

        args = type(
            "Args",
            (),
            {
                "repo": REPO,
                "db": str(self.db_path),
                "force": False,
                "include_patches": False,
                "jsonl": None,
                "jsonl_flag": False,
                "format": "json",
                "source": "graphql",
                "wait": False,
                "numbers": numbers,
            },
        )()
        with patch("zaxbygraph.cli.GhApiSource", return_value=src):
            return cmd_sync(args)

    def test_fresh_targeted_store_stays_incomplete(self) -> None:
        """sync --numbers on a fresh store must leave full_sync_pending=1, so
        the corpus never reads complete without a real full sync. Killed by:
        skipping the never-full pending rule."""
        src = _NumberedSource()
        src.add_issue(issue(1, title="one", updated_at=ts(10)))
        code = self._run_numbers(src, "1")
        self.assertEqual(code, 0)
        row = self.conn.execute(
            "SELECT full_sync_pending, last_full_sync_at, issues_since FROM sync_state WHERE repo = ?",
            (REPO,),
        ).fetchone()
        self.assertEqual(row["full_sync_pending"], 1)
        self.assertIsNone(row["last_full_sync_at"])
        self.assertIsNone(row["issues_since"])

    def test_interrupted_full_sync_state_preserved(self) -> None:
        """A targeted run on an interrupted-full store (pending=1) must keep
        the pending marker and null last_full_sync_at while clearing
        last_error. Killed by: routing numbers mode through the shared
        finalizer (which stamps and clears pending)."""
        src = _NumberedSource()
        src.add_issue(issue(1, title="one", updated_at=ts(10)))
        self._seed_state(since="2026-01-01T00:00:00Z", pending=1, error="boom")
        code = self._run_numbers(src, "1")
        self.assertEqual(code, 0)
        row = self.conn.execute(
            "SELECT full_sync_pending, last_full_sync_at, last_error, issues_since "
            "FROM sync_state WHERE repo = ?",
            (REPO,),
        ).fetchone()
        self.assertEqual(row["full_sync_pending"], 1)
        self.assertIsNone(row["last_full_sync_at"])
        self.assertIsNone(row["last_error"])
        self.assertEqual(row["issues_since"], "2026-01-01T00:00:00Z")

    def test_stale_error_cleared_and_stamps_untouched(self) -> None:
        """A successful targeted run clears last_error but leaves BOTH
        freshness stamps and the watermark exactly as found. Killed by:
        stamping last_incr_sync_at (fakes whole-corpus freshness) or leaving
        last_error set."""
        src = _NumberedSource()
        src.add_issue(issue(1, title="one", updated_at=ts(10)))
        self._seed_state(
            since="2026-01-01T00:00:00Z",
            pending=0,
            last_full="2026-01-01T09:00:00Z",
            last_incr="2026-01-02T09:00:00Z",
            error="previous failure",
        )
        code = self._run_numbers(src, "1")
        self.assertEqual(code, 0)
        row = self.conn.execute(
            "SELECT issues_since, full_sync_pending, last_full_sync_at, "
            "last_incr_sync_at, last_error FROM sync_state WHERE repo = ?",
            (REPO,),
        ).fetchone()
        self.assertIsNone(row["last_error"])
        self.assertEqual(row["issues_since"], "2026-01-01T00:00:00Z")
        self.assertEqual(row["full_sync_pending"], 0)
        self.assertEqual(row["last_full_sync_at"], "2026-01-01T09:00:00Z")
        self.assertEqual(row["last_incr_sync_at"], "2026-01-02T09:00:00Z")

    def test_numbers_404_marks_stored_and_skips_missing(self) -> None:
        """A requested number GitHub no longer has: a STORED item is marked
        deleted with the REAL reported status (no watermark movement); a
        never-stored number only leaves a fetch_log note; an unknown status
        (None) never fabricates an HTTP code in state_reason. Killed by:
        hardcoding 404 into the mark note regardless of the carried status."""
        src = _NumberedSource()
        src.add_issue(issue(1, title="kept", updated_at=ts(10)))
        src.missing_status = {2: 410, 3: None, 9: 404, 8: None}
        self._seed_state(since="2026-01-01T00:00:00Z")
        # pre-store items 2 and 3 so their absences mark them gone
        from zaxbygraph.store import ingest_item

        self.conn.execute("BEGIN IMMEDIATE")
        ingest_item(
            self.conn, REPO, issue(2, title="gone-410", updated_at=ts(5)),
            pull_raw=None, issue_comments=[], review_comments=[],
            reviews=[], files=[], include_patches=False,
            advance_watermark=False,
        )
        ingest_item(
            self.conn, REPO, issue(3, title="gone-unknown", updated_at=ts(6)),
            pull_raw=None, issue_comments=[], review_comments=[],
            reviews=[], files=[], include_patches=False,
            advance_watermark=False,
        )
        self.conn.commit()
        code = self._run_numbers(src, "1,2,3,8,9")
        self.assertEqual(code, 0)
        rows = {
            r["number"]: (r["state"], r["state_reason"])
            for r in self.conn.execute(
                "SELECT number, state, state_reason FROM items"
            ).fetchall()
        }
        self.assertEqual(rows[1][0], "open")
        self.assertEqual(rows[2][0], "deleted")
        self.assertIn("HTTP 410", rows[2][1], "the carried status must reach state_reason")
        self.assertEqual(rows[3][0], "open", "an unknown status must not mark a deletion")
        self.assertNotIn(8, rows)
        self.assertNotIn(9, rows)
        notes = [
            r["note"] for r in self.conn.execute("SELECT note FROM fetch_log").fetchall()
        ]
        self.assertTrue(any("skipped missing number" in (n or "") for n in notes))
        row = self.conn.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()
        self.assertEqual(row["issues_since"], "2026-01-01T00:00:00Z")

    def test_fallback_listing_ignores_watermark(self) -> None:
        """Without get_issues, the fallback listing must run UNFILTERED
        (since=None) so a requested item OLDER than the stored watermark is
        still refreshed; the watermark itself stays untouched. Killed by:
        passing the stored watermark to the fallback listing."""
        src = PageShapedBulkSource()  # no get_issues: fallback path
        add_page_prs(src, 2)
        self._seed_state(since="2099-01-01T00:00:00Z")
        from zaxbygraph.cli import cmd_sync

        args = type(
            "Args",
            (),
            {
                "repo": REPO,
                "db": str(self.db_path),
                "force": False,
                "include_patches": False,
                "jsonl": None,
                "jsonl_flag": False,
                "format": "json",
                "source": "graphql",
                "wait": False,
                "numbers": "1",
            },
        )()
        with patch("zaxbygraph.cli.GhApiSource", return_value=src):
            code = cmd_sync(args)
        self.assertEqual(code, 0)
        numbers = [r[0] for r in self.conn.execute("SELECT number FROM items").fetchall()]
        self.assertEqual(numbers, [1])
        row = self.conn.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()
        self.assertEqual(row["issues_since"], "2099-01-01T00:00:00Z")


class ReplayFidelityTests(TempDBTest):
    """Gone-bundle replay fidelity and duplicated-sidecar dedup."""

    def test_gone_bundle_replay_round_trip(self) -> None:
        """A recorded deletion must replay to the SAME state, state_reason,
        and raw_json: the gone bundle routes through the shared first-sighting
        404/410 handler with the carried status. Killed by: replaying gone
        bundles through check_deleted (excluded by seen_numbers; different
        note text)."""
        src = _GoneBulkSource()
        add_page_prs(src, 2)
        sidecar = Path(self._td.name) / "sidecar"
        original = sync_repo(self.conn, src, REPO, jsonl_path=sidecar)
        self.assertEqual(original["ingested"], 2)
        orig = self.conn.execute(
            "SELECT state, state_reason, raw_json FROM items WHERE number = 2"
        ).fetchone()
        self.assertEqual(orig["state"], "deleted")

        from replay import JsonlReplaySource

        replay_conn = connect(Path(self._td.name) / "replay.db")
        init_schema(replay_conn)
        try:
            replayed = sync_repo(
                replay_conn, JsonlReplaySource(sidecar / "events.jsonl"), REPO
            )
            self.assertEqual(replayed["ingested"], 2)
            got = replay_conn.execute(
                "SELECT state, state_reason, raw_json FROM items WHERE number = 2"
            ).fetchone()
            self.assertEqual(got["state"], orig["state"])
            self.assertEqual(got["state_reason"], orig["state_reason"])
            self.assertEqual(got["raw_json"], orig["raw_json"])
        finally:
            replay_conn.close()

    def test_duplicated_sidecar_replays_identically(self) -> None:
        """Appending duplicate bundle lines (a resumed sync's append-mode
        sidecar) must not change the replayed store. Killed by: ingesting
        duplicates as new items or raising on the release insert."""
        src = PageShapedBulkSource()
        add_page_prs(src, 2)
        sidecar = Path(self._td.name) / "sidecar"
        sync_repo(self.conn, src, REPO, jsonl_path=sidecar)
        events = sidecar / "events.jsonl"
        lines = events.read_text(encoding="utf-8").splitlines()
        events.write_text(
            "\n".join(lines + lines) + "\n", encoding="utf-8"
        )

        from replay import JsonlReplaySource

        replay_conn = connect(Path(self._td.name) / "replay.db")
        init_schema(replay_conn)
        try:
            result = sync_repo(
                replay_conn, JsonlReplaySource(events), REPO
            )
            self.assertEqual(result["ingested"], 2)
            items = replay_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            self.assertEqual(items, 2)
        finally:
            replay_conn.close()


class RestGetIssuesContractTests(unittest.TestCase):
    """The PRODUCTION REST getter behind `sync --numbers` (final-critic MAJOR):
    `GhApiSource.get_issues` payload/status mapping, mocked at `_api`."""

    def _source(self):
        from zaxbygraph.github import GhApiSource

        return GhApiSource("acme", "forgegate")

    def test_found_maps_to_payload_and_200(self) -> None:
        payload = issue(7, title="seven", updated_at=ts(7))
        with patch.object(type(self._source()), "_api", return_value=payload) as api:
            out = self._source().get_issues([7])
        api.assert_called_once_with("repos/acme/forgegate/issues/7")
        self.assertEqual(out, {7: (payload, 200)})

    def test_missing_maps_to_none_and_real_status(self) -> None:
        from zaxbygraph.github import GitHubError

        src = self._source()

        def api(path):
            raise GitHubError("HTTP 410 Gone", status=410)

        with patch.object(type(src), "_api", side_effect=api):
            out = src.get_issues([7])
        self.assertEqual(out, {7: (None, 410)})

    def test_non_dict_payload_is_unknown_not_404(self) -> None:
        with patch.object(type(self._source()), "_api", return_value="null body"):
            out = self._source().get_issues([7])
        self.assertEqual(out, {7: (None, None)})

    def test_other_errors_propagate(self) -> None:
        from zaxbygraph.github import GitHubError

        src = self._source()

        def api(path):
            raise GitHubError("HTTP 403 Forbidden (SSO)", status=403)

        with patch.object(type(src), "_api", side_effect=api):
            with self.assertRaises(GitHubError):
                src.get_issues([7])


class NumbersCliEdgeTests(TempDBTest):
    """The `--numbers` parse edges at the real cmd_sync boundary
    (final-critic MINOR 3): provided-but-empty, non-numeric, and the --force
    combination all exit 2 before any store access."""

    def _cmd(self, numbers, force=False):
        from zaxbygraph.cli import cmd_sync

        args = type(
            "Args",
            (),
            {
                "repo": REPO,
                "db": str(self.db_path),
                "force": force,
                "include_patches": False,
                "jsonl": None,
                "jsonl_flag": False,
                "format": "json",
                "source": "graphql",
                "wait": False,
                "numbers": numbers,
            },
        )()
        return cmd_sync(args)

    def test_empty_string_exits_2(self) -> None:
        self.assertEqual(self._cmd(""), 2)

    def test_non_numeric_exits_2(self) -> None:
        self.assertEqual(self._cmd("abc"), 2)

    def test_force_combination_exits_2(self) -> None:
        self.assertEqual(self._cmd("1,2", force=True), 2)

class FeedbackRoundTests(TempDBTest):
    """PR #37 post-publication review fixes (swarm-pr-review + critic)."""

    def test_probe_query_uses_include_deprecated_argument(self) -> None:
        """The probe must pass includeDeprecated as the ARGUMENT to fields()
        (deprecated-but-served fields must stay selected). Killed by: the
        regressive form 'fields { name includeDeprecated }' — selecting a
        nonexistent __Field subfield, which fails real introspection on every
        run (PR #37 feedback-round critic, live-verified)."""
        src = GraphQLSource("acme", "forgegate")
        captured: list[str] = []

        def fake_graphql(query, aliases=None):
            captured.append(query)
            return {
                "i": {"fields": [{"name": "databaseId"}]},
                "p": {"fields": [{"name": "comments"}]},
            }

        with patch.object(GraphQLSource, "_graphql", side_effect=fake_graphql):
            src.probe_capabilities()
        self.assertEqual(len(captured), 1)
        self.assertIn(
            "fields(includeDeprecated: true) { name }",
            captured[0],
            "the probe must request deprecated-but-served fields by argument",
        )
        self.assertNotIn("name includeDeprecated", captured[0])

    def test_rate_limited_section_error_surfaces(self) -> None:
        """A path-scoped RATE_LIMITED error must RAISE (engaging the sleep/
        retry ladder), never drop as a section. Killed by: removing the
        rate-limit gate from the section-error classifier."""
        from zaxbygraph.github import GitHubError as GHError

        host = _FakeGhHost()
        host.write_schema(_FULL_SCHEMA)
        host.write_graphql_responses(
            [
                {
                    "data": None,
                    "errors": [
                        {
                            "type": "RATE_LIMITED",
                            "message": "API rate limit exceeded",
                            "path": ["repository", "i1", "comments"],
                        }
                    ],
                }
            ]
        )
        with host:
            src = host.source()
            with self.assertRaises(GHError) as ctx:
                src.fetch_children([_listing_item(1)])
        self.assertIn("rate limit", str(ctx.exception).lower())

    def test_timeline_alias_scoped_error_degrades(self) -> None:
        """A section-scoped runtime error on a TIMELINE CONTINUATION page must
        degrade (drop the section, no events) instead of aborting the sync.
        Killed by: removing the aliases argument from _timeline_page."""
        host = _FakeGhHost()
        host.write_schema(_FULL_SCHEMA)
        host.write_graphql_responses(
            [
                {
                    "data": {
                        "rateLimit": {"remaining": 4321, "resetAt": "2026-10-03T13:00:00Z"},
                        "repository": {"i1": {"timelineItems": None}},
                    },
                    "errors": [
                        {
                            "type": "FORBIDDEN",
                            "message": "mocked timeline continuation denial",
                            "path": ["repository", "i1", "timelineItems"],
                        }
                    ],
                }
            ]
        )
        with host:
            src = host.source()
            events, info = src._timeline_page(1, "issue", "first: 50")
        self.assertEqual(events, [])
        self.assertEqual(info, {})

    def test_attach_capabilities_strict_entries(self) -> None:
        """A corrupt meta row must degrade to UNKNOWN (full field set), never
        to a stripped query; non-string entries are ignored individually.
        Killed by: str()-ing arbitrary entries back into the capability set."""
        src = GraphQLSource("acme", "forgegate")
        src.attach_capabilities({"Issue": [None, "comments", 123], "PullRequest": []})
        self.assertEqual(src._capabilities, {"Issue": {"comments"}})
        src.attach_capabilities({"Issue": [None], "PullRequest": ["comments"]})
        self.assertEqual(src._capabilities, {"PullRequest": {"comments"}})
        src.attach_capabilities({"Issue": [None], "PullRequest": []})
        self.assertIsNone(src._capabilities, "all-unknown entries must deactivate")

    def test_no_incomplete_note_for_structurally_na_sections(self) -> None:
        """A plain issue on the bulk path must not log 'children incomplete:
        files, pull, review_comments, reviews' — structurally-N/A sections
        are complete. Killed by: reverting the N/A completeness rule."""
        src = _BundleSource()
        src.add_issue(issue(1, title="one", comments=0, updated_at=ts(10)))
        dest = Path(self._td.name) / "sidecar"
        result = sync_repo(self.conn, src, REPO, jsonl_path=dest)
        self.assertIsNone(result["last_error"])
        notes = [
            r["note"] for r in self.conn.execute("SELECT note FROM fetch_log").fetchall()
        ]
        self.assertFalse(
            any((n or "").startswith("children incomplete") for n in notes),
            f"an issue has no N/A sections to report; got {notes}",
        )

    def test_targeted_noop_keeps_last_error(self) -> None:
        """A targeted run that ingests NOTHING must not clear a previous
        failure's last_error (no readiness laundering). Killed by: removing
        the ingested_count guard from the targeted finalizer."""
        from zaxbygraph.cli import cmd_sync

        self.conn.execute(
            "INSERT INTO sync_state(repo, issues_since, full_sync_pending, last_error) "
            "VALUES (?, ?, 0, 'previous failure')",
            (REPO, "2026-01-01T00:00:00Z"),
        )
        self.conn.commit()
        src = _NumberedSource()
        src.add_issue(issue(5, title="unrelated", updated_at=ts(50)))
        args = type(
            "Args",
            (),
            {
                "repo": REPO,
                "db": str(self.db_path),
                "force": False,
                "include_patches": False,
                "jsonl": None,
                "jsonl_flag": False,
                "format": "json",
                "source": "graphql",
                "wait": False,
                "numbers": "999999",
            },
        )()
        with patch("zaxbygraph.cli.GhApiSource", return_value=src):
            code = cmd_sync(args)
        self.assertEqual(code, 0)
        row = self.conn.execute(
            "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()
        self.assertEqual(
            row["last_error"],
            "previous failure",
            "a no-op targeted run must not launder a failed sync's error",
        )

    def test_gone_bundle_for_already_stored_deletion(self) -> None:
        """A deletion of an ALREADY-STORED item must reach the sidecar and
        replay to the same state/state_reason (the HIGH finding of the PR #37
        review: replay used to resurrect the deleted item)."""
        from zaxbygraph.github import GitHubError as GHError

        class _Stored404Source(FakeGitHubSource):
            """Item 1 is stored, then its per-item pull fetch 404s: the
            exists-branch deletion with the listing payload in hand."""

            def get_pull(self, number: int) -> dict:
                if number == 1:
                    raise GHError("HTTP 410 Gone", status=410)
                return super().get_pull(number)

        src = _Stored404Source()
        src.add_pr(
            issue(1, title="gone-later", comments=0, kind="pr", updated_at=ts(10)),
            pull(1, changed_files=0),
        )
        sidecar = Path(self._td.name) / "sidecar"
        result = sync_repo(self.conn, src, REPO, jsonl_path=sidecar)
        self.assertIsNone(result["last_error"])
        orig = self.conn.execute(
            "SELECT state, state_reason FROM items WHERE number = 1"
        ).fetchone()
        self.assertEqual(orig["state"], "deleted")
        records = [
            json.loads(line)
            for line in (sidecar / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        gone = [
            r for r in records
            if r.get("resource") == "item_bundle" and r["payload"].get("gone")
        ]
        self.assertEqual(len(gone), 1, "the already-stored deletion must be sidecar'd")

        from replay import JsonlReplaySource

        replay_conn = connect(Path(self._td.name) / "replay.db")
        init_schema(replay_conn)
        try:
            sync_repo(replay_conn, JsonlReplaySource(sidecar / "events.jsonl"), REPO)
            got = replay_conn.execute(
                "SELECT state, state_reason FROM items WHERE number = 1"
            ).fetchone()
            self.assertEqual(got["state"], "deleted", "replay must not resurrect")
            self.assertEqual(got["state_reason"], orig["state_reason"])
        finally:
            replay_conn.close()

    def test_gone_bundle_for_numbers_deletion(self) -> None:
        """A targeted refresh of a number GitHub no longer has must sidecar
        the deletion (from the stored payload) and replay to deleted."""
        src = _NumberedSource()
        src.missing_status = {2: 404}
        sidecar = Path(self._td.name) / "sidecar"
        from zaxbygraph.store import ingest_item

        self.conn.execute("BEGIN IMMEDIATE")
        ingest_item(
            self.conn, REPO, issue(2, title="stored", updated_at=ts(5)),
            pull_raw=None, issue_comments=[], review_comments=[],
            reviews=[], files=[], include_patches=False,
            advance_watermark=False,
        )
        self.conn.commit()
        result = sync_repo(self.conn, src, REPO, jsonl_path=sidecar, numbers=[2])
        self.assertIsNone(result["last_error"])
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM items WHERE number = 2"
            ).fetchone()[0],
            "deleted",
        )
        orig = self.conn.execute(
            "SELECT state_reason FROM items WHERE number = 2"
        ).fetchone()["state_reason"]
        records = [
            json.loads(line)
            for line in (sidecar / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        gone = [
            r for r in records
            if r.get("resource") == "item_bundle" and r["payload"].get("gone")
        ]
        self.assertEqual(len(gone), 1)

        from replay import JsonlReplaySource

        replay_conn = connect(Path(self._td.name) / "replay.db")
        init_schema(replay_conn)
        try:
            sync_repo(replay_conn, JsonlReplaySource(sidecar / "events.jsonl"), REPO)
            got = replay_conn.execute(
                "SELECT state, state_reason FROM items WHERE number = 2"
            ).fetchone()
            self.assertEqual(got["state"], "deleted")
            self.assertEqual(got["state_reason"], orig)

            # A 'transferred' oracle verdict must survive the round trip too:
            # rewrite the bundle's verdict and confirm replay honors it
            # (PR #37 feedback-round critic — replay.py used to drop it).
            transferred_bundle = dict(gone[0])
            transferred_bundle["payload"] = {
                **gone[0]["payload"], "gone_verdict": "transferred"
            }
            transferred = Path(self._td.name) / "transferred.jsonl"
            transferred.write_text(
                json.dumps(transferred_bundle) + "\n", encoding="utf-8"
            )
            replay_conn.execute("DELETE FROM items")
            replay_conn.commit()
            sync_repo(replay_conn, JsonlReplaySource(transferred), REPO)
            got_t = replay_conn.execute(
                "SELECT state, state_reason FROM items WHERE number = 2"
            ).fetchone()
            self.assertEqual(
                got_t["state"],
                "transferred",
                "the carried verdict must reach the replayed row",
            )
            self.assertIn("marked transferred", got_t["state_reason"])
        finally:
            replay_conn.close()

    def test_capability_cache_self_heals_after_fatal_error(self) -> None:
        """A fatal GitHubError while running on a CACHED capability map must
        delete the meta row so the next sync re-probes (no permanent
        post-probe-drift abort). Killed by: removing the invalidation from
        sync_repo's recorder path."""
        schema = {"Issue": ["databaseId", "comments"], "PullRequest": _FULL_SCHEMA["PullRequest"]}
        host = _FakeGhHost()
        host.write_schema(schema)
        listing = [issue(1, title="one", comments=1, updated_at=ts(10))]
        # sync1 ok; sync2's page is a VALIDATION error (data null -> hard,
        # no rate attrs -> fatal on the first attempt); sync3 must re-probe
        # (probe count 2) and recover off the third response.
        validation_error = {
            "data": None,
            "errors": [{"message": "Field 'comments' doesn't exist"}],
        }
        host.write_graphql_responses([_page_payload([1]), validation_error, _page_payload([1])])
        host.write_rest_responses([listing, [], listing, listing, []])
        with host:
            r1 = sync_repo(self.conn, host.source(), REPO)
            self.assertIsNone(r1["last_error"])
            from zaxbygraph.sync import SyncError

            with self.assertRaises(SyncError):
                sync_repo(self.conn, host.source(), REPO)
            row = self.conn.execute(
                "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
            ).fetchone()
            self.assertIsNotNone(row["last_error"], "the drifted run must fail loudly")
            self.assertIsNone(
                get_meta(self.conn, _capabilities_key()),
                "the fatal error must have invalidated the cached map",
            )
            r3 = sync_repo(self.conn, host.source(), REPO)
            self.assertIsNone(r3["last_error"], "the next sync must re-probe and recover")
            probes = sum(1 for c in host.calls() if c["probe"])
        self.assertEqual(
            probes,
            2,
            "the fatal error must invalidate the cached map (exactly one re-probe)",
        )
        self.assertIsNotNone(get_meta(self.conn, _capabilities_key()))

    def test_mcp_runner_forwards_numbers(self) -> None:
        """The MCP default sync runner must forward `numbers` to sync_repo
        (an ignored numbers argument ran a FULL sync with the deletion
        oracle — the opposite of a targeted refresh)."""
        from zaxbygraph import mcp_server

        captured: dict = {}

        def fake_sync_repo(conn, source, repo, **kw):
            captured.update(kw)

        fake_conn = unittest.mock.MagicMock()
        with patch.object(mcp_server, "sync_repo", side_effect=fake_sync_repo), \
                patch.object(mcp_server, "init_schema"), \
                patch.object(mcp_server, "connect", return_value=fake_conn):
            mcp_server._default_sync_runner(
                Path(self._td.name) / "x.db", REPO, numbers=[2, 3]
            )
        self.assertEqual(captured.get("numbers"), [2, 3])


if __name__ == "__main__":
    unittest.main()
