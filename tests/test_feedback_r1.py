"""Swarm PR-feedback round 1 regression tests (PR #79, branch
fix/68-69-eval-selection-quality).

Five of the six r1 findings have a code path to pin (FB-006 is a
measurement correction: the exact-set baseline row in the PR body; its
measured value is re-derived by the C17 replay, not by a test here):

- FB-001/ASM-1: ``actors.is_bot`` is a v12-only column, and the triage
  bot-exclusion reads (``SELECT login FROM actors WHERE is_bot = 1`` and
  the inbound-mentions ``sa.is_bot`` join) crashed with OperationalError
  on a pre-v12 store. Reads never migrate and must not route through the
  migration-forcing ``_require_bot_flags``: the fix probes the column
  (PRAGMA table_info) and degrades bot-free — no login counted as a bot.
- FB-002/CS-2: prepare_snapshot's items shadow copied the EXTRACTED
  current-state columns (``sub_issues_*`` / ``dependencies_*``)
  verbatim, so a container that completed AFTER T gated 'tracking'
  inside the as-of view at T. The fix NULLs the four columns once
  ``updated_at > T`` (the existing undatable rule) and discloses the
  class via ``current_state_tables``.
- FB-003/TF-1: tracking-gate branch (c) — body-mentions all closed —
  gated ordinary citing issues. Removed; the epic-leg re-pin lives in
  tests/test_tracking_gate.py. Pinned here: an issue citing ONE closed
  issue stays rankable (the negative leg the critic named).
- FB-004/CUB-9678: the similar-mode random baseline drew from a pool
  that EXCLUDED the partners, so ``random_recall_at_5`` was identically
  0.0 — a baseline that cannot fire. The fix draws from the full visible
  candidate set.
- FB-005/ASM-2: prepare_snapshot on a store below the v10 floor died
  mid-build on a bare OperationalError. The fix refuses at entry with
  the coded ``store_too_old`` error naming sync as the remedy.

Hermetic: FakeGitHubSource through the real sync path; no network, no
credentials; unittest (also collectable by pytest). Sync stamps pinned
where stage-2 arithmetic runs so nothing depends on the wall clock.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, issue

from zaxbygraph import derive, evaluate
from zaxbygraph.db import connect, init_schema, open_existing
from zaxbygraph.sync import sync_repo


def _pin_synced_at(
    conn, repo: str = REPO, stamp: str = "2027-03-01T00:00:00Z"
) -> None:
    """Freeze the store's freshness stamps so triage stage-2 arithmetic
    (and therefore the gates/ranking) is deterministic (the
    tests/test_eval_backtest.py _pin_synced_at pattern)."""
    conn.execute(
        "UPDATE sync_state SET last_full_sync_at = ?, last_incr_sync_at = ?"
        " WHERE repo = ?",
        (stamp, stamp, repo),
    )
    conn.commit()


class _StoreTestCase(unittest.TestCase):
    """One temp store per test class (subclasses override ``_build``); one
    read-only connection per test (tests/test_selection_quality.py
    _StoreTestCase pattern)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._td = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._td.name) / "store.db"
        conn = connect(cls.db_path)
        try:
            init_schema(conn)
            cls._build(conn)
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._td.cleanup()

    @classmethod
    def _build(cls, conn) -> None:  # pragma: no cover - overridden
        raise AssertionError("_build must be overridden")

    def setUp(self) -> None:
        # TI-1/CUB-9739/CUB-9647: in-process triage/bundle/eval calls
        # resolve .zaxbygraph.toml from the CWD — run inside an empty temp
        # cwd so an ambient config can never reweight a check.
        self._prior_cwd = os.getcwd()
        self._hermetic_cwd = tempfile.TemporaryDirectory(
            prefix="zaxbygraph-hermetic-"
        )
        os.chdir(self._hermetic_cwd.name)
        self.conn = open_existing(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        os.chdir(self._prior_cwd)
        self._hermetic_cwd.cleanup()


# ==== FB-001/ASM-1: pre-v12 stores degrade bot-free ==========================


class PreV12BotColumnTests(unittest.TestCase):
    """A v11-shaped store (v12 schema minus ``actors.is_bot``) must serve
    triage and bundle — the two crash sites of FB-001 — with the bot
    exclusion degrading to bot-free (no login counted as a bot), never an
    OperationalError and never a silent migration."""

    def setUp(self) -> None:
        # TI-1/CUB-9739/CUB-9647: in-process triage/bundle calls resolve
        # .zaxbygraph.toml from the CWD — empty temp cwd, never the
        # operator's repo root.
        self._prior_cwd = os.getcwd()
        self._hermetic_cwd = tempfile.TemporaryDirectory(
            prefix="zaxbygraph-hermetic-"
        )
        os.chdir(self._hermetic_cwd.name)

    def tearDown(self) -> None:
        os.chdir(self._prior_cwd)
        self._hermetic_cwd.cleanup()

    def test_triage_and_bundle_succeed_without_is_bot(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db_path = Path(td.name) / "store.db"
        src = FakeGitHubSource()
        # One open issue authored by alice with a bot-authored comment:
        # on the v12 store the bot is flagged and excluded from
        # participants; after the column is dropped nobody is an
        # established bot, so the SAME comment counts as human signal
        # (bot-free degradation — the honest direction: FB-001 never
        # wants reads to guess bots without the verdict column).
        src.add_issue(issue(
            500, title="crash on submit", body="stack trace in body",
            created_at="2027-06-01T00:00:00Z",
        ))
        src.comment_on(500, "posting the logs", author="ci-bot[bot]")
        conn = connect(db_path)
        try:
            init_schema(conn)
            sync_repo(conn, src, REPO)
            _pin_synced_at(conn)
            v12_participants = None
            ro = open_existing(db_path)
            try:
                rows = {
                    r["number"]: r
                    for r in derive.triage(ro, REPO, include_gated=True)["rows"]
                }
                v12_participants = rows[500]["scores"]["features_raw"][
                    "participants"
                ]
            finally:
                ro.close()
            # Degrade to the v11 shape: drop the v12-only column and stamp
            # the old user_version (the finding's prescribed construction —
            # ALTER TABLE DROP COLUMN needs the 3.35 SQLite floor the repo
            # already documents and tests elsewhere).
            conn.execute("ALTER TABLE actors DROP COLUMN is_bot")
            conn.execute("PRAGMA user_version = 11")
            conn.commit()
        finally:
            conn.close()

        # v12 control leg: the flagged bot is excluded (1 = alice only) —
        # proves the fixture really exercises the bot-exclusion path.
        self.assertEqual(
            v12_participants, 1,
            f"on the v12 store the bot comment must NOT count as a"
            f" participant; got {v12_participants!r}",
        )

        ro = open_existing(db_path)
        try:
            # RED pre-fix: both reads died on 'no such column: is_bot'.
            out = derive.triage(ro, REPO, include_gated=True)
            rows = {r["number"]: r for r in out["rows"]}
            self.assertIn(500, rows)
            self.assertEqual(
                rows[500]["scores"]["features_raw"]["participants"], 2,
                f"without the is_bot column no login is an established bot"
                f" (bot-free degradation: alice + the bot author both"
                f" count); got"
                f" {rows[500]['scores']['features_raw']['participants']!r}",
            )
            payload = derive.bundle(ro, REPO)
            self.assertIsInstance(payload["pool"], list)
        finally:
            ro.close()


# ==== FB-002/CS-2: extracted columns stop contributing once updated_at > T ===


class AsOfExtractedColumnsTests(_StoreTestCase):
    """A container whose sub-issue summary completed AFTER T must not gate
    'tracking' in the as-of view at T: the extracted columns are
    current-state, and prepare_snapshot NULLs them under the existing
    undatable rule (updated_at > T) instead of leaking the post-T
    completion into the replay. RED pre-fix: the columns leaked verbatim
    and the epic gated in the as-of view exactly as at head."""

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        # Epic 900: created 06-01, updated 06-10 — the summary's
        # completion (total 2, completed 2) is only knowable from the
        # 06-10 state. At T = 06-05 the as-of view cannot certify the
        # columns and must not let them gate.
        epic = issue(
            900, title="Epic: finish the migration",
            body="tracking epic",
            created_at="2027-06-01T00:00:00Z",
            updated_at="2027-06-10T00:00:00Z",
        )
        epic["sub_issues_summary"] = {"total": 2, "completed": 2}
        src.add_issue(epic)
        src.add_issue(issue(
            901, title="ordinary open work", body="plain",
            created_at="2027-06-01T00:00:00Z",
        ))
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)

    def test_post_t_completion_does_not_gate_as_of(self) -> None:
        """The as-of view at T=2027-06-05: epic 900 is NOT gated
        'tracking' (the completion postdates T), stays rankable, and the
        payload discloses the four extracted columns as current-state."""
        out = derive.triage(
            self.conn, REPO, include_gated=True, as_of="2027-06-05T00:00:00Z",
        )
        rows = {r["number"]: r for r in out["rows"]}
        self.assertIn(900, rows)
        self.assertNotIn(
            "tracking", rows[900]["gates"],
            f"a container whose sub-issue completion postdates T must not"
            f" gate 'tracking' in the as-of view at T — the extracted"
            f" columns are current-state and are NULLed once"
            f" updated_at > T; got {rows[900]['gates']!r}",
        )
        self.assertIs(
            rows[900]["ranked"], True,
            f"the as-of view at T ranks the then-unfinished epic; got"
            f" {rows[900]!r}",
        )
        # The disclosure names the four extracted columns.
        disclosures = out.get("asof_disclosures", {}).get("disclosures", {})
        current_state = disclosures.get("current_state_tables", [])
        for col in (
            "sub_issues_total", "sub_issues_completed",
            "dependencies_blocked_by", "dependencies_blocking",
        ):
            self.assertIn(
                col, current_state,
                f"the as-of payload must disclose {col} as a current-state"
                f" extraction; got {current_state!r}",
            )

    def test_live_view_still_gates_the_finished_epic(self) -> None:
        """Control leg: at head (no as_of) the same store DOES gate the
        epic — the fix nulls only the as-of copy, never the live
        columns."""
        out = derive.triage(self.conn, REPO, include_gated=True)
        rows = {r["number"]: r for r in out["rows"]}
        self.assertIn(
            "tracking", rows[900]["gates"],
            f"the live view sees the completed summary and must gate"
            f" 'tracking'; got {rows[900]['gates']!r}",
        )
        self.assertIs(rows[900]["ranked"], False)


# ==== FB-003/TF-1: an issue citing one closed issue stays rankable ===========


class TrackingCiteNegativeTests(_StoreTestCase):
    """The negative leg TF-1 named: an ordinary open issue whose body
    cites ONE closed issue in PROSE must stay rankable — a prose citation
    is not a task-list reference, so the checkbox-aware branch (c) never
    sees it (the original any-mention branch gated this exact shape)."""

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        # 610's body cites closed 611 ("regression of #611"): the ingest
        # derives the body-origin mentions edge 610 -> 611.
        src.add_issue(issue(
            610, title="submit broke again", body="regression of #611",
            created_at="2027-06-01T00:00:00Z",
        ))
        src.add_issue(issue(
            611, title="the original fix", body="plain",
            state="closed", created_at="2027-06-01T00:00:00Z",
        ))
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)

    def test_citing_issue_stays_rankable(self) -> None:
        out = derive.triage(self.conn, REPO, include_gated=True)
        rows = {r["number"]: r for r in out["rows"]}
        self.assertIn(610, rows)
        self.assertNotIn(
            "tracking", rows[610]["gates"],
            f"an open issue citing one closed issue is ordinary rankable"
            f" work — body mentions never gate 'tracking' (branch (c)"
            f" removed, FB-003/TF-1); got {rows[610]['gates']!r}",
        )
        self.assertIs(rows[610]["ranked"], True)
        payload = derive.bundle(self.conn, REPO)
        self.assertIn(
            610, payload["pool"],
            f"the citing issue stays in the bundle pool; got"
            f" {payload['pool']!r}",
        )


# ==== FB-004/CUB-9678: the random baseline can actually hit a partner ========


class SimilarBaselinePoolTests(_StoreTestCase):
    """The similar-mode random baseline must draw from the FULL visible
    candidate set: the old pool excluded the partners, so a draw could
    never hit one and ``random_recall_at_5`` was identically 0.0. On this
    small fixture (every visible candidate drawn when the pool is <= 5)
    the baseline recall must be NON-ZERO. RED pre-fix (0.0).

    Geometry (the fixtures_eval pattern): a merged PR before T0 supplies
    the resolved-path vocabulary, 201/202 are text-identical duplicates
    (202 created one hour after 201, so 201 is the visible partner at
    202's cutoff), and the duplicate_of edge is inserted with link-stream
    provenance exactly where GitHub itself reports duplicates."""

    @classmethod
    def _build(cls, conn) -> None:
        from fixtures import pull, pr_file

        src = FakeGitHubSource()
        merged = pull(210, merged=True)
        merged["merged_at"] = "2027-05-30T00:00:00Z"
        src.add_pr(
            issue(210, title="Add feature module", body="", kind="pr",
                  created_at="2027-05-29T00:00:00Z"),
            merged,
            files=[pr_file("src/feature.py")],
        )
        src.add_issue(issue(
            201, title="login crash on submit",
            body="crash in `src/feature.py` on submit",
            created_at="2027-06-01T00:00:00Z",
        ))
        src.add_issue(issue(
            202, title="login crash on submit",
            body="crash in `src/feature.py` on submit",
            created_at="2027-06-01T01:00:00Z",
        ))
        src.add_issue(issue(
            203, title="unrelated docs work", body="typo in readme",
            created_at="2027-06-01T00:00:00Z",
        ))
        sync_repo(conn, src, REPO)
        # GitHub-reported duplicate_of edge 202 -> 201 (the fixtures_eval
        # _insert_edge pattern).
        conn.execute(
            "INSERT OR IGNORE INTO edges (repo, src_type, src_id, rel,"
            " dst_type, dst_id, source, evidence, confidence)"
            " VALUES (?, 'item', '202', 'duplicate_of', 'item', '201',"
            " 'link', 'fixture-reported', 'EXTRACTED')",
            (REPO,),
        )
        conn.commit()

    def test_random_recall_baseline_is_nonzero(self) -> None:
        result = evaluate.run_eval(self.conn, REPO, "similar")
        baseline = result["baselines"]["random_recall_at_5"]
        recall = baseline["recall_at_5"]
        self.assertIsNotNone(recall)
        self.assertGreater(
            recall, 0.0,
            f"the random baseline draws from the full visible candidate"
            f" set (partners included — FB-004/CUB-9678), so it can and"
            f" does hit partners on this small pool; got {baseline!r}",
        )
        # And the real metric still outranks nothing here — this check
        # pins the baseline's geometry only.


# ==== FB-005/ASM-2: prepare_snapshot refuses below the v10 floor =============


class SnapshotFloorTests(unittest.TestCase):
    """A v9-shaped store (no item_events/pr_commits, user_version 9) gets
    the coded ``store_too_old`` refusal at prepare_snapshot entry — never
    a mid-build OperationalError — naming sync as the remedy. RED
    pre-fix: OperationalError('no such table: main.item_events')."""

    def setUp(self) -> None:
        # TI-1/CUB-9739/CUB-9647: the as-of refusal leg resolves the
        # config chain from the CWD — empty temp cwd.
        self._prior_cwd = os.getcwd()
        self._hermetic_cwd = tempfile.TemporaryDirectory(
            prefix="zaxbygraph-hermetic-"
        )
        os.chdir(self._hermetic_cwd.name)

    def tearDown(self) -> None:
        os.chdir(self._prior_cwd)
        self._hermetic_cwd.cleanup()

    def test_v9_shaped_store_gets_coded_refusal(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db_path = Path(td.name) / "store.db"
        src = FakeGitHubSource()
        src.add_issue(issue(
            400, title="plain", body="plain",
            created_at="2027-06-01T00:00:00Z",
        ))
        conn = connect(db_path)
        try:
            init_schema(conn)
            sync_repo(conn, src, REPO)
            # Degrade to the v9 shape: the v10 floor tables gone, the
            # version stamped back.
            conn.execute("DROP TABLE item_events")
            conn.execute("DROP TABLE IF EXISTS pr_commits")
            conn.execute("PRAGMA user_version = 9")
            conn.commit()
        finally:
            conn.close()

        ro = open_existing(db_path)
        try:
            with self.assertRaises(evaluate.StoreTooOldError) as caught:
                evaluate.prepare_snapshot(
                    ro, REPO, as_of="2027-06-05T00:00:00Z"
                )
            self.assertEqual(
                getattr(caught.exception, "code", None), "store_too_old",
                f"the refusal must carry the store_too_old code (the"
                f" export/snapshot floor shape); got {caught.exception!r}",
            )
            self.assertNotIsInstance(
                caught.exception, sqlite3.OperationalError,
                "the floor refusal is a coded error, never the mid-build"
                " OperationalError",
            )
            self.assertIn(
                "sync", str(caught.exception),
                f"the refusal names sync as the remedy; got"
                f" {str(caught.exception)!r}",
            )
            # The user-facing surface answers with the same coded error.
            with self.assertRaises(ValueError) as triage_caught:
                derive.triage(ro, REPO, as_of="2027-06-05T00:00:00Z")
            self.assertEqual(
                getattr(triage_caught.exception, "code", None),
                "store_too_old",
                f"derive.triage(as_of=...) must surface the same coded"
                f" refusal; got {triage_caught.exception!r}",
            )
        finally:
            ro.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
