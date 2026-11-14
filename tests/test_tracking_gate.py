"""Regression tests for the tracking gate's branch (c) direction and branch
(d) (configured ``tracking_labels``) — the final critic's F1 findings
(.agents/issue-traces/68-69-eval-selection-quality/09-final-critic.md).

Branch (c) is the body-mention children branch of the tracking gate (issue
#69 AC1): a task list lives in the ITEM'S OWN BODY, so the tracked children
are the items THAT body references, restricted to issue-kind targets with
known state. The defect this file pins: the gate's map was keyed by the
MENTIONED item (``body_mention_srcs[dst] = {mentioners}``), so the gate
fired on N when every item whose body mentions N was closed — the inverted
reading "everyone who mentions N is done" — which gated ordinary work
referenced by a closed item (the critic's live probe gated 7 of 9 open
issues, including follow-ups whose only body mentioner was the merged fix
PR) while a real finished task-list epic stayed ranked.

Two legs from the critic's synthetic probe (scratch store, real sync path):

- open bug 600, referenced only by CLOSED issue 601 ("see #600"): an
  inbound body mention from a closed item is not 600's tracked children
  (600's own body references nothing), so 600 must NOT gate 'tracking' —
  it is ordinary rankable work;
- open epic 700 whose OWN body is a finished task list
  ("- [x] #701 / - [x] #702") with both children CLOSED: the container's
  own body-origin outbound mentions, all closed issue-kind children, so
  700 MUST gate 'tracking' (ranked false, out of every bundle pool).

Branch (d) (configured ``[triage] tracking_labels``) had no test at all:
an open issue carrying a configured label must gate 'tracking' when (and
only when) the label is configured.

RED before the direction fix (both probe legs fail: 600 IS gated and 700
is NOT), GREEN after; the branch (d) test passes before and after — it is
coverage for the untested branch, not a defect pin.

Hermetic: FakeGitHubSource through the real sync path; no network, no
credentials; unittest (also collectable by pytest). Sync stamps pinned so
stage-2 arithmetic never depends on the wall clock.
"""

from __future__ import annotations

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
        self.conn = open_existing(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()


# ==== branch (c): the container's OWN body mentions are the children ========


class TrackingMentionDirectionTests(_StoreTestCase):
    """The final critic's two-leg probe. RED at 09b4643 (the inverted
    direction): the closed mentioner's inbound edge gates ordinary bug 600,
    and epic 700 — whose own body lists only closed children — stays
    ranked."""

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
        # from the epic's body.
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
        """RED before the direction fix, GREEN after. An inbound body
        mention from a closed item must NOT gate the mentioned issue; a
        container whose own body lists only closed issue-kind children MUST
        gate 'tracking', stay unranked, and stay out of every bundle."""
        out = derive.triage(self.conn, REPO, include_gated=True)
        rows = {r["number"]: r for r in out["rows"]}
        self.assertIn(600, rows)
        self.assertIn(700, rows)

        # --- leg 1: the bait is ordinary rankable work -------------------
        self.assertNotIn(
            "tracking", rows[600]["gates"],
            f"an ordinary open bug referenced only by a CLOSED item's body"
            f" must NOT gate 'tracking' — the mention points AT 600; 600's"
            f" own body tracks nothing (the inverted direction gated"
            f" exactly this shape); got {rows[600]['gates']!r}",
        )
        self.assertIs(
            rows[600]["ranked"], True,
            f"ungated ordinary work stays rankable; got ranked="
            f"{rows[600]['ranked']!r} with gates {rows[600]['gates']!r}",
        )

        # --- leg 2: the finished task-list epic gates --------------------
        self.assertIn(
            "tracking", rows[700]["gates"],
            f"an open epic whose OWN body lists only CLOSED children (a"
            f" finished task list) must gate 'tracking' — the tracked"
            f" children are the items the epic's own body references; got"
            f" {rows[700]['gates']!r}",
        )
        self.assertIs(
            rows[700]["ranked"], False,
            f"a tracking-gated epic must not be rankable work; got"
            f" {rows[700]!r}",
        )

        # --- the pool consequence: gated epic out, ordinary bug in -------
        payload = derive.bundle(self.conn, REPO)
        self.assertNotIn(
            700, payload["pool"],
            "a tracking-gated epic must not enter the bundle pool",
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
