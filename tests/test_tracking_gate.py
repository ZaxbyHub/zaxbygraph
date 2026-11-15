"""Regression tests for the tracking gate's body-reference behaviour and
branch (d) (configured ``tracking_labels``) — the final critic's F1
findings (.agents/issue-traces/68-69-eval-selection-quality/
09-final-critic.md), AMENDED by swarm-pr-feedback round 1 (FB-003/TF-1)
and RE-AMENDED by swarm-pr-feedback round 3 (closeout item 1).

History of the body-mention branch (c): the final critic's F1 found the
gate's map keyed by the MENTIONED item — the inverted reading "everyone
who mentions N is done" — which gated ordinary work referenced by a
closed item. The r1 fix corrected the direction, but the PR-feedback
round-1 critic then ruled the branch unsalvageable (TF-1): even
direction-correct, it gates ANY issue whose body-mentions all resolve
closed — an ordinary citing issue (a bug report referencing closed work)
is hidden exactly like a finished container. Round 1 removed the branch
entirely, which also dropped #69 AC1's task-list case. Round 3 restores
branch (c) CHECKBOX-AWARE: a body reference counts as tracked-child work
only when the container's own body shows it as a TASK-LIST reference —
a checkbox line ('- [x] #N' / '- [ ] #N') whose referenced item is a
closed issue — with a non-empty set and every member known-closed (an
unknown child state never satisfies a completeness branch). Prose
citations ('regression seen in #800') never match the checkbox marker
and stay rankable, so TF-1's negative case remains fixed (pinned here
and by tests/test_feedback_r1.py TrackingCiteNegativeTests). The gate is
four branches again: (a) sub-issues complete, (b) child_of children
closed, (c) completed task-list references in the body, (d) configured
tracking_labels; closeable_trackers = (a)+(b)+(c).

Branch (d) (configured ``[triage] tracking_labels``) had no test at all
before this file: an open issue carrying a configured label must gate
'tracking' when (and only when) the label is configured.

Hermetic: FakeGitHubSource through the real sync path; no network, no
credentials; unittest (also collectable by pytest). Sync stamps pinned so
stage-2 arithmetic never depends on the wall clock.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, issue

from zaxbygraph.db import connect, init_schema, open_existing
from zaxbygraph import derive
from zaxbygraph.sync import sync_repo


def _pin_synced_at(
    conn, repo: str = REPO, stamp: str = "2027-03-01T00:00:00Z"
) -> None:
    """Freeze the store's freshness stamps so triage stage-2 arithmetic (and
    therefore the gates/ranking) is deterministic (the
    tests/test_eval_backtest.py _pin_synced_at pattern)."""
    conn.execute(
        "UPDATE sync_state SET last_full_sync_at = ?, last_incr_sync_at = ?"
        " WHERE repo = ?",
        (stamp, stamp, repo),
    )
    conn.commit()


def _tracker_numbers(entry) -> list[int]:
    """closeable_trackers entries may be numbers or {number, ...} dicts
    (the tests/test_selection_quality.py helper)."""
    if entry is None:
        return []
    return [
        int(e) if not isinstance(e, dict) else int(e["number"]) for e in entry
    ]


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
        # TI-1/CUB-9739/CUB-9647: the [triage]/[derive] config chain
        # resolves .zaxbygraph.toml from the CWD, and these checks call
        # triage/bundle in-process without an explicit config — run them
        # in an empty temp cwd so an operator's repo-root config can never
        # reweight a gate assertion (the tests/test_triage_bundle.py
        # _LEG_CWD pattern, applied to in-process calls).
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


# ==== branch (c) restored, checkbox-aware (round 3 / #69 AC1) ================


class TrackingMentionDirectionTests(_StoreTestCase):
    """The final critic's two-leg probe, RE-PINNED at round 3: the closed
    mentioner's inbound edge never gates bug 600 (the direction defect F1
    named), while the task-list epic 700 (body mentions only, no sub-issue
    columns, no child_of links) gates 'tracking' through the restored
    CHECKBOX-AWARE branch (c) — its body is a completed task list whose
    referenced children are closed issues (#69 AC1's "every task-list or
    mentioned child closed" case, restored without re-admitting TF-1's
    prose-citation gating)."""

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        # Leg 1 bait: an ordinary OPEN bug whose only body mentioner is a
        # CLOSED item. The real ingest path derives the body-origin mentions
        # edge 601 -> 600 (evidence 'body@<offset>') from 601's body.
        src.add_issue(issue(
            600, title="ordinary bug", body="plain report, no references",
            created_at="2027-06-01T00:00:00Z",
        ))
        src.add_issue(issue(
            601, title="closed work that saw the bug", body="see #600",
            state="closed", created_at="2027-06-01T00:00:00Z",
        ))
        # Leg 2: an open epic whose OWN body is a finished task list; the
        # ingest derives body-origin mentions edges 700 -> 701 and 700 -> 702
        # from the epic's body. Under round 1 (branch removed) this stayed
        # rankable; round 3 restores branch (c) checkbox-aware, so the
        # completed task list gates 'tracking' again.
        src.add_issue(issue(
            700, title="Epic: task list all done",
            body="- [x] #701\n- [x] #702",
            created_at="2027-06-01T00:00:00Z",
        ))
        for n in (701, 702):
            src.add_issue(issue(
                n, title=f"child {n}", body="child work", state="closed",
                created_at="2027-06-01T00:00:00Z",
            ))
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)

    def test_two_leg_mention_direction_probe(self) -> None:
        """Leg 1 stays rankable (an inbound body mention from a closed item
        never gates the mentioned issue); leg 2 gates through the restored
        checkbox-aware branch (c) — a completed task list in the epic's own
        body is closeable-container evidence, reported under
        closeable_trackers and excluded from the bundle."""
        out = derive.triage(self.conn, REPO, include_gated=True)
        rows = {r["number"]: r for r in out["rows"]}
        self.assertIn(600, rows)
        self.assertIn(700, rows)

        # --- leg 1: the bait is ordinary rankable work -------------------
        self.assertNotIn(
            "tracking", rows[600]["gates"],
            f"an ordinary open bug referenced only by a CLOSED item's body"
            f" must NOT gate 'tracking' — the mention points AT 600; 600's"
            f" own body tracks nothing; got {rows[600]['gates']!r}",
        )
        self.assertIs(
            rows[600]["ranked"], True,
            f"ungated ordinary work stays rankable; got ranked="
            f"{rows[600]['ranked']!r} with gates {rows[600]['gates']!r}",
        )

        # --- leg 2: the completed task list gates (round-3 branch (c)) ---
        self.assertIn(
            "tracking", rows[700]["gates"],
            f"an open epic whose OWN body is a task list of checkbox"
            f" references to closed issues must gate 'tracking' (round-3"
            f" checkbox-aware branch (c), #69 AC1); got"
            f" {rows[700]['gates']!r}",
        )
        self.assertIs(
            rows[700]["ranked"], False,
            f"a completed task-list epic is closeable, not rankable; got"
            f" ranked={rows[700]['ranked']!r} with gates"
            f" {rows[700]['gates']!r}",
        )
        self.assertIn(
            700, _tracker_numbers(out.get("closeable_trackers")),
            "a completed task-list epic is reported under closeable_trackers"
            " (branch (c) joins (a) and (b))",
        )

        # --- the pool consequence: leg 1 in, leg 2 out --------------------
        payload = derive.bundle(self.conn, REPO)
        self.assertNotIn(
            700, payload["pool"],
            f"a tracking-gated task-list epic must not enter the bundle"
            f" pool; got {payload['pool']!r}",
        )
        self.assertIn(
            600, payload["pool"],
            f"the ungated ordinary bug stays in the bundle pool; got"
            f" {payload['pool']!r}",
        )


class TrackingTasklistIncompleteTests(_StoreTestCase):
    """The completeness legs of the checkbox-aware branch (c): the gate
    needs EVERY checkbox-referenced child a known-closed ISSUE. An
    unchecked box over an open child is incomplete work; a reference to a
    number the store has no item for is an UNKNOWN state, and unknown
    never satisfies a completeness branch (fail-closed)."""

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        # 710's task list: one closed child plus one reference (#719) to an
        # item the store never saw — unknown state, so the set is not
        # all-closed and the gate must not fire.
        src.add_issue(issue(
            710, title="Epic: one child unknown",
            body="- [x] #711\n- [x] #719",
            created_at="2027-06-01T00:00:00Z",
        ))
        src.add_issue(issue(
            711, title="child 711", body="child work", state="closed",
            created_at="2027-06-01T00:00:00Z",
        ))
        # 720's task list: an UNCHECKED box whose child is still open —
        # incomplete by both the mark's meaning and the child's state.
        src.add_issue(issue(
            720, title="Epic: one child open",
            body="- [ ] #721",
            created_at="2027-06-01T00:00:00Z",
        ))
        src.add_issue(issue(
            721, title="child 721", body="child work",
            created_at="2027-06-01T00:00:00Z",
        ))
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)

    def test_incomplete_tasklists_stay_rankable(self) -> None:
        out = derive.triage(self.conn, REPO, include_gated=True)
        rows = {r["number"]: r for r in out["rows"]}
        self.assertIn(710, rows)
        self.assertIn(720, rows)
        self.assertNotIn(
            "tracking", rows[710]["gates"],
            f"a task list with one UNKNOWN-state child (#719 absent from the"
            f" store) must not gate 'tracking' — unknown never satisfies a"
            f" completeness branch; got {rows[710]['gates']!r}",
        )
        self.assertIs(rows[710]["ranked"], True)
        self.assertNotIn(
            "tracking", rows[720]["gates"],
            f"an unchecked checkbox ('- [ ] #721') over an OPEN child is"
            f" incomplete work and must not gate 'tracking'; got"
            f" {rows[720]['gates']!r}",
        )
        self.assertIs(rows[720]["ranked"], True)
        trackers = _tracker_numbers(out.get("closeable_trackers"))
        self.assertNotIn(710, trackers)
        self.assertNotIn(720, trackers)
        payload = derive.bundle(self.conn, REPO)
        self.assertIn(710, payload["pool"])
        self.assertIn(720, payload["pool"])


# ==== branch (d): configured tracking_labels gate ===========================


class TrackingLabelTests(_StoreTestCase):
    """Branch (d) coverage (untested before this file): an open issue
    carrying a configured ``[triage] tracking_labels`` label gates
    'tracking'; the same label gates nothing when unconfigured."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # The explicit config path is written into the store's temp dir but
        # NOT under the name config.toml — that name is the STORE-level
        # config slot (<store>/config.toml), and it must stay absent so the
        # unconfigured control leg really reads the defaults.
        cls.cfg_path = Path(cls._td.name) / "tracking_cfg.toml"
        cls.cfg_path.write_text(
            "[triage]\ntracking_labels = [\"epic\"]\n", encoding="utf-8"
        )

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        src.add_issue(issue(
            800, title="labelled epic-shaped work", body="plain",
            labels=[{"name": "epic", "color": "331889"}],
            created_at="2027-06-01T00:00:00Z",
        ))
        src.add_issue(issue(
            801, title="unlabelled work", body="plain",
            created_at="2027-06-01T00:00:00Z",
        ))
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)

    def test_configured_tracking_label_gates(self) -> None:
        """With ``[triage] tracking_labels = ["epic"]`` the labelled open
        issue gates 'tracking' (ranked false) while the unlabelled one does
        not; with no config in the chain the same label gates nothing (the
        branch is configuration-driven, never a built-in default)."""
        out = derive.triage(
            self.conn, REPO, include_gated=True, config=str(self.cfg_path)
        )
        rows = {r["number"]: r for r in out["rows"]}
        self.assertIn(800, rows)
        self.assertIn(801, rows)
        self.assertIn(
            "tracking", rows[800]["gates"],
            f"an open issue carrying a configured tracking label must gate"
            f" 'tracking'; got {rows[800]['gates']!r}",
        )
        self.assertIs(rows[800]["ranked"], False)
        self.assertNotIn(
            "tracking", rows[801]["gates"],
            f"an unlabelled open issue must not gate 'tracking'; got"
            f" {rows[801]['gates']!r}",
        )
        # The echo names the configured labels (the payload stays honest
        # about which labels gated).
        self.assertEqual(out["weights"].get("tracking_labels"), ["epic"])

        # Control leg: no config anywhere in the chain -> the label gates
        # nothing (the branch never fires on built-in defaults).
        plain = derive.triage(self.conn, REPO, include_gated=True)
        plain_rows = {r["number"]: r for r in plain["rows"]}
        self.assertNotIn(
            "tracking", plain_rows[800]["gates"],
            f"without the configured tracking_labels the same labelled issue"
            f" must not gate 'tracking'; got {plain_rows[800]['gates']!r}",
        )
        self.assertIs(plain_rows[800]["ranked"], True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
