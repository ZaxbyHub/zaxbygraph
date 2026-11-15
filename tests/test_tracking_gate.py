"""Regression tests for the tracking gate's body-mention behaviour and
branch (d) (configured ``tracking_labels``) — the final critic's F1
findings (.agents/issue-traces/68-69-eval-selection-quality/
09-final-critic.md), AMENDED by swarm-pr-feedback round 1 (FB-003/TF-1).

History of the body-mention branch (c): the final critic's F1 found the
gate's map keyed by the MENTIONED item — the inverted reading "everyone
who mentions N is done" — which gated ordinary work referenced by a
closed item. The r1 fix corrected the direction, but the PR-feedback
round-1 critic then ruled the branch unsalvageable (TF-1): even
direction-correct, it gates ANY issue whose body-mentions all resolve
closed — an ordinary citing issue (a bug report referencing closed work)
is hidden exactly like a finished container — while real epics are
already covered by the sub-issue-columns and child_of branches. The
critic's ruling: REMOVE branch (c) from the gate entirely; the gate is
the three branches (sub-issues complete / child_of children closed /
configured tracking_labels label). This file now pins the REMOVED
behaviour: an epic whose body lists only closed children is RANKABLE
(not gated) — body mentions alone never gate 'tracking' again.

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


# ==== body mentions never gate 'tracking' (branch (c) REMOVED, FB-003) ======


class TrackingMentionDirectionTests(_StoreTestCase):
    """The final critic's two-leg probe, RE-PINNED at the PR-feedback r1
    ruling (FB-003/TF-1): branch (c) is removed from the gate entirely, so
    BOTH legs are ordinary rankable work — the closed mentioner's inbound
    edge never gates bug 600, and the task-list epic 700 (body mentions
    only, no sub-issue columns, no child_of links) is rankable too. Real
    finished epics still gate through branches (a)/(b) — that is pinned by
    tests/test_selection_quality.py C9, which populates the columns AND the
    child_of edges."""

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
        # from the epic's body. Under the removed branch (c) this gated
        # 'tracking'; under the r1 ruling body mentions alone gate nothing.
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
        """Both legs stay rankable: an inbound body mention from a closed
        item must NOT gate the mentioned issue, and — the FB-003/TF-1
        ruling — an epic whose own body lists only CLOSED children no
        longer gates 'tracking' either (the branch is removed; only the
        sub-issue columns, child_of children, or a configured label gate)."""
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

        # --- leg 2: body mentions alone no longer gate (FB-003/TF-1) ----
        self.assertNotIn(
            "tracking", rows[700]["gates"],
            f"an open epic whose OWN body lists only CLOSED children is no"
            f" longer gated: branch (c) is removed (the PR-feedback r1"
            f" ruling — it gated ordinary citing issues while real epics"
            f" were already covered by the columns/child_of branches); got"
            f" {rows[700]['gates']!r}",
        )
        self.assertIs(
            rows[700]["ranked"], True,
            f"with branch (c) removed the task-list epic is rankable work;"
            f" got ranked={rows[700]['ranked']!r} with gates"
            f" {rows[700]['gates']!r}",
        )

        # --- the pool consequence: both stay in --------------------------
        payload = derive.bundle(self.conn, REPO)
        self.assertIn(
            700, payload["pool"],
            f"a body-mention-only epic is rankable and stays in the bundle"
            f" pool; got {payload['pool']!r}",
        )
        self.assertIn(
            600, payload["pool"],
            f"the ungated ordinary bug stays in the bundle pool; got"
            f" {payload['pool']!r}",
        )


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
