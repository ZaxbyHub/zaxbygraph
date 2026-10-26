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
        self.assertIn("omitted timelineItems", notes)

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

    def test_second_source_reuses_persisted_probe(self) -> None:
        """After one sync probed, a SECOND source over the same store must
        hydrate from meta: no additional introspection call. Killed by:
        ignoring the cached meta row (always probing)."""
        schema = {"Issue": ["databaseId", "comments"], "PullRequest": _FULL_SCHEMA["PullRequest"]}
        responses = [
            _page_payload([1]),
            [issue(1, title="one", comments=1, updated_at=ts(10))],
            [],
            [],
        ]
        host = _FakeGhHost()
        host.write_schema(schema)
        host.write_graphql_responses([_page_payload([1])])
        # two syncs x (listing + comments fallback + releases)
        listing = [issue(1, title="one", comments=1, updated_at=ts(10))]
        host.write_rest_responses([listing, [], [], listing, [], []])
        with host:
            first = sync_repo(self.conn, host.source(), REPO)
            probes_after_first = sum(1 for c in host.calls() if c["probe"])
            second = sync_repo(self.conn, host.source(), REPO)
            probes_after_second = sum(1 for c in host.calls() if c["probe"])
        self.assertIsNone(first["last_error"])
        self.assertIsNone(second["last_error"])
        self.assertEqual(probes_after_first, 1)
        self.assertEqual(
            probes_after_second,
            1,
            "the second source must hydrate capabilities from the meta cache",
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
    numbers (or None for a number GitHub does not have)."""

    def get_issues(self, numbers):
        out: dict[int, dict | None] = {}
        for n in numbers:
            rec = self.issues.get(int(n))
            out[int(n)] = deepcopy(rec) if rec is not None else None
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
        deleted (no watermark movement); a never-stored number only leaves a
        fetch_log note. Killed by: aborting the run on the 404."""
        src = _NumberedSource()
        src.add_issue(issue(1, title="kept", updated_at=ts(10)))
        self._seed_state(since="2026-01-01T00:00:00Z")
        # pre-store item 2 so the 404 marks it gone
        from zaxbygraph.store import ingest_item

        self.conn.execute("BEGIN IMMEDIATE")
        ingest_item(
            self.conn, REPO, issue(2, title="gone", updated_at=ts(5)),
            pull_raw=None, issue_comments=[], review_comments=[],
            reviews=[], files=[], include_patches=False,
            advance_watermark=False,
        )
        self.conn.commit()
        code = self._run_numbers(src, "1,2,9")
        self.assertEqual(code, 0)
        states = {
            r["number"]: r["state"]
            for r in self.conn.execute("SELECT number, state FROM items").fetchall()
        }
        self.assertEqual(states.get(1), "open")
        self.assertEqual(states.get(2), "deleted")
        self.assertNotIn(9, states)
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


if __name__ == "__main__":
    unittest.main()
