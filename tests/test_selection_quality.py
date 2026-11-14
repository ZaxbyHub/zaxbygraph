"""Work-selection quality acceptance checks for issue #69, authored against
the trace's acceptance table BEFORE any implementation (issue-tracer check
author role; base 36d4bb4111995a5ce59d844984e6619fde384321).

Frozen check names (#69 acceptance table):

    C9  test_tracking_epics_are_gated                 (AC1, DISCRIMINATING)
    C10 test_demand_excludes_bots_and_children        (AC2, DISCRIMINATING)
    C11 test_anchor_requires_non_hub_evidence        (AC3, DISCRIMINATING)
    C12 test_greedy_selection_rules                   (AC4, DISCRIMINATING)
    C13 test_gated_and_unbundled_are_reported         (AC5, DISCRIMINATING)
    C14 test_linked_pr_gate_is_narrow                 (AC6, DISCRIMINATING)
    C15 test_default_priority_labels_and_relative_hotspot (AC7, DISCRIMINATING)
    C16 test_analog_filters_before_truncating         (AC8, DISCRIMINATING)
    C17 test_beats_baselines_on_recorded_corpus       (AC9, DISCRIMINATING -
        the acceptance gate for the whole PR)
    C18 PRESERVING, lives in tests/test_similar.py:
        python -m pytest tests/test_similar.py -q -k
        test_affinity_is_symmetric                      (GREEN at base)
    AC11 NON-EXECUTABLE(PRODUCT_DECISION): the chosen default weights,
        thresholds and the built-in label map are recorded on issue #69
        with the results table. No test here.

AUTHOR-PINNED CONTRACTS the fix must satisfy (decisions made by the check
author at freeze time; the implementation must match them):

- C9: the tracking gate works from stored evidence. The fixture populates
  BOTH signals the store can carry - the link-stream ``child_of`` edges
  (child -> parent, source='link', the real sync path) and the
  ``items.sub_issues_total`` / ``sub_issues_completed`` columns (from the
  listing payload's ``sub_issues_summary``) - so the gate may read either.
  The gate name starts with ``tracking``; ``closeable_trackers`` rides the
  TRIAGE payload as a list of numbers or ``{number, ...}`` dicts.
- C10: bot exclusion uses ``actors.is_bot`` (a ``[bot]``-suffixed login
  flags itself through the real ingest path). The ``comments`` count
  feature is deliberately NOT pinned: it reads the stored
  ``items.comment_count`` column, not the actors at read time.
- C12(c): the new why_not_bigger reason string for the cohesion stop is
  pinned as ``"below cohesion threshold"``. The cohesion fraction's
  default is taken as 0.5; the fixture's margins (2.58 vs 0.5 * 9.77)
  stay RED/GREEN-correct for any fraction in (0.27, 1.0).
- C13: ``gated`` is ``{count: int, by_gate: {gate name: [numbers]}, 
  numbers: [numbers]}`` and ``unbundled`` is ``[{number, reason}, ...]``
  on the bundle payload.
- C15: with no config file anywhere in the chain, ``config_source``
  contains "built-in" or "builtin" (case-insensitive) naming the built-in
  label map that supplied the priority weights. Since amendment waves
  4+5, C15 also pins the hotspot CLOCK: an explicit ``as_of=T`` must
  anchor hotspot at T even when the store's sync_state stamps differ
  from T - the as-of payload carries ``hotspot_asof == T`` and the
  as-of answer must equal, per row, the answer a store whose OWN stamps
  are T produces. The NORMALIZED hotspot is uniform-offset invariant BY
  DESIGN (under a uniform clock shift every raw recency sum scales by
  the same factor exp(-delta/45) and the max normalization cancels it),
  so the offset-sensitivity pin rides the additive per-row
  ``hotspot_raw`` diagnostic (the unnormalized recency sum): it must
  DIFFER between the T-anchored and stored-stamp-anchored runs (probe
  merge ages 1/77 days under the stored stamps vs 11/87 under T).
- C16: the analog contract is asserted through ``derive.brief``'s packet
  (``analog`` block carries the PR's ``number``); the filter is to MERGED
  PRs (an open non-merged PR ranking above the merged one is bait).
- C13: each bundle pair carries ``anchors`` - every fired anchor signal,
  strongest first by the signal's weighted contribution
  (affinity-weight x component score from the pair's own ``components``
  and the payload's ``weights.affinity`` echo, ties by name) - and
  ``anchor`` equals ``anchors[0]``. The triage payload carries the same
  ``gated`` block shape as the bundle payload.
- C14: a legacy comment-derived ``closes_keyword`` edge (evidence
  ``'comment:<id> closing keyword'``) must NOT gate - the fix
  distinguishes it from PR-description/commit keyword edges by its
  evidence provenance.
- C17: the floors are HARD, not placeholders: pair precision > 0.18 and
  pair recall > 0.24, strictly above the issue's measured pre-change
  values (18% / 24%). A later AMEND may RAISE these floors but never
  lower them. Since amendment wave 2 the corpus is the recorded
  NEIGHBORHOOD fixture (tests/eval_fixture_backtest/) and the GT
  enumeration is PINNED from the replayed store: 10 fix PRs
  (20/41/45/49/50/51/54/63/76/78) under closes UNION closes_keyword with
  body@/commit evidence, 14 distinct truth pairs, 22 eligible targets,
  and per-fix-PR ``ground_truth`` provenance ('closes' vs 'keyword';
  PR 49 is the keyword case - at base, closes-only counting drops it to
  13 pairs / 20 targets). Since amendment wave 3 the fixture also
  carries the source store's revisions rows (50, replayed through
  tests/fixtures_backtest.py - the GT pins are unchanged, they come from
  edges) and the CHECK pins the per-case disclose+exclude rule the
  partial revisions coverage demands: each per_fix_pr entry carries a
  ``disclosures`` list, and a case whose truth endpoints lack an
  item_title/item_body pre-image chain reaching before the fix PR's
  created_at discloses ``text_as_of_unknown`` - 9 of the 10 pinned cases
  must fire it; only PR 63's case (both endpoints 61/62 covered, the
  fixture's honest coverage is 3 of 22 endpoints: 61, 62, 72) must not.
  Since amendment wave 4 the TOP-LEVEL ``ground_truth`` label is pinned
  to the exclusive ``'mixed'``: nine closes-provenanced cases plus PR 49's
  keyword case cover the corpus jointly, so neither single-kind label may
  appear there. The top-level vocabulary (amendment wave 5) is 'github'
  (every case's truth is gh-alone-recoverable - gh closes >= 2 issues per
  case), 'keyword' (every case needs the keyword stream), 'mixed' (both
  kinds); the PER-CASE provenance stays 'closes'/'keyword' (C2 pins the
  same top-level field as 'keyword' on the all-keyword store, C1 pins it
  'github' on the all-GitHub store).

Class split across the two frozen modules (pre-implementation amendment
wave, critic-approved): 13 DISCRIMINATING (C1, C2, C5, C6, C9, C10, C11,
C12, C13, C14, C15, C16, C17) + 3 NEW-SURFACE (C3, C4, C7) + 2
PRESERVING (C8, C18). The frozen argv cells and test names are unchanged
- the amendments added assertions inside the same test methods and
updated docstrings/fixture geometry only.

Hermetic: FakeGitHubSource-derived sources plus (C17 only) the COMMITTED
recorded corpus replayed offline via tests/fixtures_backtest.py's
BacktestReplaySource (tests/replay.py's JsonlReplaySource plus the
fixture-side revisions mapping) - no network, no live GitHub, no gh;
stdlib only; unittest (also collectable by pytest). All sync_state
stamps are pinned so stage-2 arithmetic never sees the wall clock.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, comment, issue, pr_file, pull
from fixtures_eval import _insert_edge

from zaxbygraph.db import connect, init_schema, open_existing
from zaxbygraph import derive
from zaxbygraph.sync import sync_repo


def _merged_pull(number: int, merged_at: str) -> dict:
    p = pull(number, merged=True)
    p["merged_at"] = merged_at
    return p


def _pin_synced_at(conn, repo: str = REPO, stamp: str = "2027-03-01T00:00:00Z") -> None:
    """Freeze the store's freshness stamps so stage-2 hotspot recency (and
    therefore triage ORDER and bundle seeding) is deterministic."""
    conn.execute(
        "UPDATE sync_state SET last_full_sync_at = ?, last_incr_sync_at = ?"
        " WHERE repo = ?",
        (stamp, stamp, repo),
    )
    conn.commit()


def _add_comment(
    src: FakeGitHubSource,
    number: int,
    cid: int,
    body: str,
    author: str,
    created_at: str | None = None,
) -> None:
    """Append one issue comment with an EXPLICIT globally-unique github id
    (comment_on's len-based ids collide across numbers)."""
    rec = comment(cid, body, author=author, created_at=created_at)
    src.issue_comments.setdefault(number, []).append(rec)
    src.issues[number]["comments"] = len(src.issue_comments[number])


def _node(number: int, repo: str = REPO) -> dict:
    """One link-connection endpoint in the house {"number", "repo"} shape."""
    return {"number": number, "repo": repo}


def _links(
    *,
    parent: dict | None = None,
) -> dict:
    """A complete ``links`` children section carrying only the parent
    connection (the tests/test_triage_bundle.py _links pattern)."""
    return {
        "blocked_by": [],
        "parent": dict(parent) if parent else None,
        "duplicate_of": None,
        "open_prs": [],
    }


class _BulkSource(FakeGitHubSource):
    """FakeGitHubSource + the GraphQL-only links section: list_issues yields
    one PAGE and fetch_children returns per-item children dicts so the
    parent connection reaches the store as child_of edges through the real
    sync path (tests/test_triage_bundle.py _BulkSource pattern)."""

    def __init__(self) -> None:
        super().__init__()
        self.links: dict[int, dict] = {}

    def list_issues(self, since):
        from copy import deepcopy

        page = [
            deepcopy(rec)
            for rec in sorted(
                self.issues.values(),
                key=lambda r: (r["updated_at"], r["number"]),
            )
            if since is None or rec["updated_at"] >= since
        ]
        if page:
            yield page

    def fetch_children(self, items) -> dict[int, dict]:
        from copy import deepcopy

        out: dict[int, dict] = {}
        for raw in items:
            n = int(raw["number"])
            children: dict = {
                "issue_comments": deepcopy(self.issue_comments.get(n, [])),
                "review_comments": deepcopy(self.review_comments.get(n, [])),
                "reviews": deepcopy(self.reviews.get(n, [])),
                "files": deepcopy(self.files.get(n, [])),
            }
            if n in self.pulls:
                children["pull"] = deepcopy(self.pulls[n])
            if n in self.links:
                children["links"] = deepcopy(self.links[n])
            out[n] = children
        return out


class _StoreTestCase(unittest.TestCase):
    """One temp store per test class (subclasses override ``_build``); one
    read-only connection per test."""

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


def _member_numbers(bundle: dict) -> list[int]:
    """Members may be dicts ({number, title, ...}) or bare numbers."""
    return [
        int(m["number"]) if isinstance(m, dict) else int(m) for m in bundle["members"]
    ]


def _tracker_numbers(entry) -> list[int]:
    """closeable_trackers entries may be numbers or {number, ...} dicts."""
    if entry is None:
        return []
    return [
        int(e) if not isinstance(e, dict) else int(e["number"]) for e in entry
    ]


# ==== C9: finished tracking epics are gated ==================================


class TrackingEpicTests(_StoreTestCase):
    """AC1 (#69), DISCRIMINATING. RED at base: the epic is ranked (gates
    [], ranked true) and sits in the bundle pool - exactly the observed
    defect (#21 with sub_issues 13/13 ranked as top work).

    Store: open epic 400 with sub_issues_summary {total: 2, completed: 2}
    (the items columns the reviewer observed on the real store) AND
    link-stream child_of edges from its two CLOSED children 401/402 (the
    other stored parentage signal) - both populated through the real sync
    path, so the gate can read either. The children carry timeline
    mentions edges at the epic (the phantom demand). 403 is an unrelated
    open issue so the pool is non-trivial.
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        rec_epic = issue(
            400, title="Epic: migrate the parser",
            body="tracking epic for the migration",
            created_at="2027-06-01T00:00:00Z",
        )
        rec_epic["sub_issues_summary"] = {"total": 2, "completed": 2}
        src.add_issue(rec_epic)
        for n in (401, 402):
            src.add_issue(issue(
                n, title=f"child {n}", body="child work", state="closed",
                created_at="2027-06-01T00:00:00Z",
            ))
            src.links[n] = _links(parent=_node(400))
        src.add_issue(issue(
            403, title="other open work", body="plain",
            created_at="2027-06-01T00:00:00Z",
        ))
        sync_repo(conn, src, REPO)
        _insert_edge(conn, REPO, 401, "mentions", 400, source="timeline")
        _insert_edge(conn, REPO, 402, "mentions", 400, source="timeline")
        _pin_synced_at(conn)

    def test_tracking_epics_are_gated(self) -> None:
        """RED at base, GREEN after the fix. The all-children-closed epic
        is gated 'tracking' (ranked false), reported under
        closeable_trackers, and excluded from every bundle (not even in
        the pool)."""
        out = derive.triage(self.conn, REPO, include_gated=True)
        rows = {r["number"]: r for r in out["rows"]}
        self.assertIn(400, rows)
        gates = rows[400]["gates"]
        self.assertTrue(
            any(g == "tracking" or g.startswith("tracking") for g in gates),
            f"the finished tracking epic must carry a 'tracking' gate, got"
            f" {gates!r}",
        )
        self.assertIs(rows[400]["ranked"], False)
        trackers = _tracker_numbers(out.get("closeable_trackers"))
        self.assertIn(
            400, trackers,
            "an epic whose children are all closed is reported under"
            " closeable_trackers",
        )

        payload = derive.bundle(self.conn, REPO)
        self.assertNotIn(
            400, payload["pool"],
            "a tracking-gated epic must not enter the bundle pool",
        )
        for bundle in payload["bundles"]:
            self.assertNotIn(
                400, _member_numbers(bundle),
                "a tracking-gated epic must not appear in any bundle",
            )


# ==== C10: demand counts people, not bots or children ========================


class DemandExclusionTests(_StoreTestCase):
    """AC2 (#69), DISCRIMINATING. RED at base: participants counts the bot
    (2 at base) and inbound_mentions counts every source item regardless
    of state or parentage (3 at base).

    Bot leg: issue 500 (author bob) carries TWO comments from
    'dependabot[bot]' and ONE from bob. The [bot] suffix flags the actor
    through the real ingest path (actors.is_bot = 1), so post-fix
    participants must be 1 (bob only). The ``comments`` COUNT feature is
    deliberately not pinned here: it reads the stored comment_count
    column, which no read-time bot filter touches.

    Child leg: open epic 510 with sub_issues {total: 2, completed: 1}
    (INCOMPLETE - one child still open - so the epic stays rankable under
    the C9 gate). Its closed child 511 and open child 512 (both via
    child_of links) each mention it, plus the unrelated open issue 520
    mentions it. Post-fix inbound_mentions counts only 520 (1, not 3):
    closed sources and the epic's own children are excluded.
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        src.add_issue(issue(
            500, title="bot-noise target", body="plain", author="bob",
            created_at="2027-06-01T00:00:00Z",
        ))
        _add_comment(src, 500, 70001, "automated note one", "dependabot[bot]",
                     created_at="2027-06-02T00:00:00Z")
        _add_comment(src, 500, 70002, "automated note two", "dependabot[bot]",
                     created_at="2027-06-02T01:00:00Z")
        _add_comment(src, 500, 70003, "human note", "bob",
                     created_at="2027-06-02T02:00:00Z")
        rec_epic = issue(
            510, title="epic partially done", body="tracking",
            created_at="2027-06-01T00:00:00Z",
        )
        rec_epic["sub_issues_summary"] = {"total": 2, "completed": 1}
        src.add_issue(rec_epic)
        src.add_issue(issue(
            511, title="child one", body="c1", state="closed",
            created_at="2027-06-01T00:00:00Z",
        ))
        src.add_issue(issue(
            512, title="child two", body="c2",
            created_at="2027-06-01T00:00:00Z",
        ))
        src.links[511] = _links(parent=_node(510))
        src.links[512] = _links(parent=_node(510))
        src.add_issue(issue(
            520, title="stranger", body="s",
            created_at="2027-06-01T00:00:00Z",
        ))
        sync_repo(conn, src, REPO)
        _insert_edge(conn, REPO, 511, "mentions", 510, source="timeline")
        _insert_edge(conn, REPO, 512, "mentions", 510, source="timeline")
        _insert_edge(conn, REPO, 520, "mentions", 510, source="timeline")
        _pin_synced_at(conn)

    def test_demand_excludes_bots_and_children(self) -> None:
        """RED at base, GREEN after the fix. (a) participants excludes bot
        actors; (b) inbound_mentions counts only open non-child sources."""
        out = derive.triage(self.conn, REPO, include_gated=True)
        rows = {r["number"]: r for r in out["rows"]}
        self.assertIn(500, rows)
        self.assertIn(510, rows)

        participants = rows[500]["scores"]["features_raw"]["participants"]
        self.assertEqual(
            participants, 1,
            f"participants must exclude bot actors (bob only, got"
            f" {participants})",
        )

        mentions = rows[510]["scores"]["features_raw"]["inbound_mentions"]
        self.assertEqual(
            mentions, 1,
            f"inbound_mentions must count only open non-child sources"
            f" (issue 520 only; got {mentions})",
        )


# ==== C11: anchors need non-hub evidence above a threshold ===================


class AnchorThresholdTests(_StoreTestCase):
    """AC3 (#69), DISCRIMINATING. RED at base via leg 1 (a support-1
    hub-file co-change anchors an edge); leg 2 is GREEN at base and pins
    the surviving positive case so the fix cannot over-tighten.

    Merged-PR population (N = 8, all 2-file PRs, all in the coupling
    population; hub share = touches/N, DEFAULT_HUB_SHARE = 0.5):
      PR 1  [src/hx.py, src/hy.py]     <- the ONLY hx-hy co-occurrence
      PR 2  [src/hx.py, src/f2.py]
      PR 3  [src/hx.py, src/f3.py]
      PR 4  [src/hx.py, src/f4.py]     <- hx in 4/8 PRs = 0.5 share: HUB
      PR 5  [src/la.py, src/lb.py]
      PR 6  [src/la.py, src/lb.py]     <- support(la, lb) = 2
      PR 7  [src/z7.py, src/z7b.py]
      PR 8  [src/z8.py, src/z8b.py]

    Issues (open, bodies resolve their refs against the vocabulary):
      600 -> src/hx.py (hub), 601 -> src/hy.py: their ONLY shared evidence
        is the co-change pair (hx, hy) with support 1 through the hub file
        hx (verified at base: anchor fires, affinity 4.625 >= floor 3.0,
        and the pair is co-bundled).
      602 -> src/la.py, 603 -> src/lb.py: co-change support 2, symmetric
        confidence 1.0, lift = 8*2/(2*2) = 2 > 1, both files non-hub
        (2/8 = 0.25 share) - the strong evidence the fix must keep
        (verified at base: affinity 6.875, co-bundled).
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        prs = {
            1: ["src/hx.py", "src/hy.py"],
            2: ["src/hx.py", "src/f2.py"],
            3: ["src/hx.py", "src/f3.py"],
            4: ["src/hx.py", "src/f4.py"],
            5: ["src/la.py", "src/lb.py"],
            6: ["src/la.py", "src/lb.py"],
            7: ["src/z7.py", "src/z7b.py"],
            8: ["src/z8.py", "src/z8b.py"],
        }
        for n, files in prs.items():
            src.add_pr(
                issue(n, title=f"pr{n}", body="", kind="pr",
                      created_at="2027-01-01T00:00:00Z"),
                _merged_pull(n, "2027-01-02T00:00:00Z"),
                files=[pr_file(f) for f in files],
            )
        src.add_issue(issue(
            600, title="Hub fault alpha report",
            body="crash near src/hx.py:1:1 in the hub area",
            created_at="2027-02-01T00:00:00Z",
        ))
        src.add_issue(issue(
            601, title="Hub fault beta report",
            body="crash near src/hy.py:1:1 in the hub area",
            created_at="2027-02-01T00:00:00Z",
        ))
        src.add_issue(issue(
            602, title="Lone fault alpha report",
            body="crash near src/la.py:1:1 in the lone area",
            created_at="2027-02-01T00:00:00Z",
        ))
        src.add_issue(issue(
            603, title="Lone fault beta report",
            body="crash near src/lb.py:1:1 in the lone area",
            created_at="2027-02-01T00:00:00Z",
        ))
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)

    def test_anchor_requires_non_hub_evidence(self) -> None:
        """RED at base (leg 1), GREEN after the fix; leg 2 is GREEN at base
        by design. Leg 1: the pair whose only shared evidence is a
        support-1 co-change through a hub file gets NO anchor, so it is
        never co-bundled and is explained elsewhere (related_not_bundleable
        or the C13 unbundled list). Leg 2: co-change with support >= 2 and
        lift > 1 on non-hub files still fires and still bundles."""
        payload = derive.bundle(self.conn, REPO)

        def _cobundled(a: int, b: int) -> bool:
            return any(
                a in numbers and b in numbers
                for numbers in (_member_numbers(x) for x in payload["bundles"])
            )

        # Leg 1 (RED at base: [600, 601] is a bundle).
        self.assertFalse(
            _cobundled(600, 601),
            "a support-1 hub-file co-change must not anchor a bundle edge"
            f" (bundles: {[_member_numbers(x) for x in payload['bundles']]})",
        )
        reported = (
            any(
                sorted(pair) == [600, 601]
                for pair in (r["numbers"] for r in payload["related_not_bundleable"])
            )
            or {e.get("number") for e in (payload.get("unbundled") or [])}
            >= {600, 601}
        )
        self.assertTrue(
            reported,
            "the anchor-less pair must be explained: related_not_bundleable"
            " or the unbundled list names 600/601",
        )

        # Leg 2 (GREEN at base; must stay GREEN after the fix).
        self.assertTrue(
            _cobundled(602, 603),
            "co-change with support 2 and lift > 1 on non-hub files must"
            f" still anchor a bundle (bundles:"
            f" {[_member_numbers(x) for x in payload['bundles']]})",
        )


# ==== C12: greedy selection rules ============================================


class GreedySelectionTests(unittest.TestCase):
    """AC4 (#69), DISCRIMINATING. Legs (a), (b), (c) are RED at base; leg
    (d) is RED at base (only one bundle forms). Three purpose-built stores
    (built once per class; every geometry number below was verified
    against the base implementation):

    AFFINITY store (legs a+b): PRs 1/2 [p, q], PRs 3/4 [s, r] (all 2-file,
    merged). Issues:
      - 700 (seed S): reactions +6, five comments (triage total 0.6500,
        the highest); refs p+q+s.
      - 701 (partner A): no engagement (triage total 0.1083); refs p+q.
      - 702 (partner B): reactions +3, two comments (triage total 0.5050);
        refs s+r.
      Base affinities (explain): aff(S,A) = 9.167 (anchored), aff(S,B) =
      6.167 (anchored), aff(A,B) = 2.667 with NO anchor. At base the
      partner choice multiplies by triage total (0.1083*9.167 < 0.5050*
      6.167), so B wins and A then joins: bundle [700, 702, 701]. The fix
      chooses by affinity alone -> [700, 701], and B is barred from joining
      because its pair with the existing member A has no anchor (below the
      floor), even though aff(S,B) clears it.

    COHESION store (leg c): PRs 1/2 [g1, g1b], PR 3 [g5, g6], PR 4 [g7
    only], PRs 5/6 fillers. Issues:
      - 750 (seed): reactions +6, three comments; refs g1+g1b+g5.
      - 751 (strong partner): refs g1+g1b+g5. aff(750, 751) = 9.774.
      - 752 (weak partner): refs g5+g7. aff(750, 752) = 2.583 and
        aff(751, 752) = 2.560 - both anchored (files_direct) and both
        above the test's lowered affinity_floor 1.5, so NO pair-floor rule
        can be what excludes 752; only the cohesion stop can.
      Ran with size "2-3", allow_shared_files=True, and an explicit
      [triage] affinity_floor = 1.5 config. At base the bundle grows to
      the maximum: [750, 751, 752] with why_not_bigger "membership cap
      (reached target size)" (the string pinned by
      tests/test_triage_paging.py). The fix stops at [750, 751] with the
      pinned new reason "below cohesion threshold" (2.583 < fraction *
      9.774 for any fraction > 0.27; the default is taken as 0.5).

    HUB-EXEMPTION store (leg d): PR 1 [h, gd1], PRs 2/3 [h, x_i] (h in
      3/6 = 0.5 share: HUB), PR 4 [pd1, y4], PRs 5/6 fillers. Issues:
      - 760 (seed, +6 reactions) refs gd1; 761 refs gd1+h (pair one).
      - 762 (seed, +5 reactions) refs pd1; 763 refs pd1+h (pair two).
      Both pairs anchor on their non-hub file. At base, after bundle one
      consumes gd1 AND h, 763 is blocked by the shared-file disjointness
      (h is in used_files), so only ONE bundle forms. The fix exempts hub
      files from cross-bundle disjointness -> two bundles.

    PAIR-FLOOR store (leg e, amendment wave): PRs 1/2 [p1, p2], PRs 3/4
    [q1, q2] (support 2 each), PRs 5/6 fillers (N = 6; every file's hub
    share 2/6, non-hub). Issues (the TWIN geometry):
      - 710 (seed S, +6 reactions, five comments; triage total 0.6500)
        refs p1+p2+q1+q2, title "Twin fault alpha beta report".
      - 711 (twin A) refs p1+p2, title "Twin fault alpha report".
      - 712 (twin C) refs q1+q2, title "Twin fault beta report".
      The two pairs are EXACTLY symmetric (verified at base):
      aff(S,A) == aff(S,C) == 8.416667 (files_direct 0.5, cochange 1.0
      with support 2 and lift 3, identical title/lexical/context by
      construction - the p*/q* filename fragments are too short to
      tokenize), while aff(A,C) = 3.3 with NO anchor (disjoint file
      families, no shared PRs). Ran with size "2-3", count 2.
      At base the third member joins on mean affinity alone: bundle
      [710, 711, 712].
      Post-fix, whichever twin is chosen first (they tie; the tiebreak is
      the implementation's), the OTHER twin's MEAN affinity with the
      group is aff/2 == 0.5 x cohesion - it does NOT fall below the
      cohesion stop under a mean-OR-min cohesion definition, so ONLY the
      per-pair floor rule can exclude it: its pair with the seated twin
      has no anchor (bundle affinity 0, below any positive floor - and
      deliberately above the raw floor 3.0 at 3.3, so a raw-score check
      that ignores the anchor guard would wrongly admit it). It must be
      reported under ``unbundled`` with a below-floor reason naming
      "floor" (author-pinned wording).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._td = tempfile.TemporaryDirectory()
        root = Path(cls._td.name)

        # --- affinity store (legs a+b) ---
        cls.affinity_db = root / "affinity.db"
        conn = connect(cls.affinity_db)
        try:
            init_schema(conn)
            src = FakeGitHubSource()
            for n, files in {
                1: ["src/p.py", "src/q.py"],
                2: ["src/p.py", "src/q.py"],
                3: ["src/s.py", "src/r.py"],
                4: ["src/s.py", "src/r.py"],
            }.items():
                src.add_pr(
                    issue(n, title=f"v{n}", body="", kind="pr",
                          created_at="2027-01-01T00:00:00Z"),
                    _merged_pull(n, f"2027-01-0{n + 1}T00:00:00Z"),
                    files=[pr_file(f) for f in files],
                )
            rec_s = issue(
                700, title="Seed crash on the submit path",
                body="fault at src/p.py:1:1 and src/q.py:2:1 and src/s.py:3:1",
                created_at="2027-02-01T00:00:00Z",
            )
            rec_s["reactions"] = {"+1": 6, "total_count": 6}
            src.add_issue(rec_s)
            for cid, author in (
                (71001, "carol"), (71002, "dave"), (71003, "erin"),
                (71004, "frank"), (71005, "grace"),
            ):
                _add_comment(src, 700, cid, "me too", author,
                             created_at="2027-02-02T00:00:00Z")
            src.add_issue(issue(
                701, title="Seed crash on the submit path",
                body="fault at src/p.py:1:1 and src/q.py:2:1",
                created_at="2027-02-01T00:00:00Z",
            ))
            rec_b = issue(
                702, title="Seed crash partial view",
                body="partial fault at src/s.py:3:1 and src/r.py:4:1",
                created_at="2027-02-01T00:00:00Z",
            )
            rec_b["reactions"] = {"+1": 3, "total_count": 3}
            src.add_issue(rec_b)
            for cid, author in ((72001, "heidi"), (72002, "ivan")):
                _add_comment(src, 702, cid, "also here", author,
                             created_at="2027-02-02T00:00:00Z")
            sync_repo(conn, src, REPO)
            _pin_synced_at(conn)
        finally:
            conn.close()

        # --- cohesion store (leg c) + its low-floor config ---
        cls.cohesion_db = root / "cohesion.db"
        cls.cohesion_cfg = root / "lowfloor.toml"
        cls.cohesion_cfg.write_text("[triage]\naffinity_floor = 1.5\n",
                                    encoding="utf-8")
        conn = connect(cls.cohesion_db)
        try:
            init_schema(conn)
            src = FakeGitHubSource()
            for n, files in {
                1: ["src/g1.py", "src/g1b.py"],
                2: ["src/g1.py", "src/g1b.py"],
                3: ["src/g5.py", "src/g6.py"],
                4: ["src/g7.py"],
                5: ["src/z1.py", "src/z2.py"],
                6: ["src/z3.py", "src/z4.py"],
            }.items():
                src.add_pr(
                    issue(n, title=f"v{n}", body="", kind="pr",
                          created_at="2027-01-01T00:00:00Z"),
                    _merged_pull(n, f"2027-01-0{n + 1}T00:00:00Z"),
                    files=[pr_file(f) for f in files],
                )
            rec_seed = issue(
                750, title="Cohesion seed fault alpha",
                body="fault at src/g1.py:1:1 and src/g1b.py:2:1 and"
                     " src/g5.py:3:1",
                created_at="2027-02-01T00:00:00Z",
            )
            rec_seed["reactions"] = {"+1": 6, "total_count": 6}
            src.add_issue(rec_seed)
            for cid, author in ((73001, "carol"), (73002, "dave"),
                                (73003, "erin")):
                _add_comment(src, 750, cid, "me too", author,
                             created_at="2027-02-02T00:00:00Z")
            src.add_issue(issue(
                751, title="Cohesion seed fault beta",
                body="fault at src/g1.py:1:1 and src/g1b.py:2:1 and"
                     " src/g5.py:3:1 too",
                created_at="2027-02-01T00:00:00Z",
            ))
            src.add_issue(issue(
                752, title="Weak partner fault",
                body="weak fault at src/g5.py:5:1 and src/g7.py:6:1",
                created_at="2027-02-01T00:00:00Z",
            ))
            sync_repo(conn, src, REPO)
            _pin_synced_at(conn)
        finally:
            conn.close()

        # --- hub-exemption store (leg d) ---
        cls.hubexempt_db = root / "hubexempt.db"
        conn = connect(cls.hubexempt_db)
        try:
            init_schema(conn)
            src = FakeGitHubSource()
            for n, files in {
                1: ["src/h.py", "src/gd1.py"],
                2: ["src/h.py", "src/x2.py"],
                3: ["src/h.py", "src/x3.py"],
                4: ["src/pd1.py", "src/y4.py"],
                5: ["src/q5.py", "src/q5b.py"],
                6: ["src/q6.py", "src/q6b.py"],
            }.items():
                src.add_pr(
                    issue(n, title=f"v{n}", body="", kind="pr",
                          created_at="2027-01-01T00:00:00Z"),
                    _merged_pull(n, f"2027-01-0{n + 1}T00:00:00Z"),
                    files=[pr_file(f) for f in files],
                )
            rec_s1 = issue(
                760, title="Pair one seed fault",
                body="fault at src/gd1.py:1:1",
                created_at="2027-02-01T00:00:00Z",
            )
            rec_s1["reactions"] = {"+1": 6, "total_count": 6}
            src.add_issue(rec_s1)
            src.add_issue(issue(
                761, title="Pair one partner fault",
                body="fault at src/gd1.py:1:1 and src/h.py:2:1",
                created_at="2027-02-01T00:00:00Z",
            ))
            rec_s2 = issue(
                762, title="Pair two seed fault",
                body="fault at src/pd1.py:1:1",
                created_at="2027-02-01T00:00:00Z",
            )
            rec_s2["reactions"] = {"+1": 5, "total_count": 5}
            src.add_issue(rec_s2)
            src.add_issue(issue(
                763, title="Pair two partner fault",
                body="fault at src/pd1.py:1:1 and src/h.py:2:1",
                created_at="2027-02-01T00:00:00Z",
            ))
            sync_repo(conn, src, REPO)
            _pin_synced_at(conn)
        finally:
            conn.close()

        # --- pair-floor store (leg e, amendment wave) ---
        cls.pairfloor_db = root / "pairfloor.db"
        conn = connect(cls.pairfloor_db)
        try:
            init_schema(conn)
            src = FakeGitHubSource()
            for n, files in {
                1: ["src/p1.py", "src/p2.py"],
                2: ["src/p1.py", "src/p2.py"],
                3: ["src/q1.py", "src/q2.py"],
                4: ["src/q1.py", "src/q2.py"],
                5: ["src/z1.py", "src/z2.py"],
                6: ["src/z3.py", "src/z4.py"],
            }.items():
                src.add_pr(
                    issue(n, title=f"v{n}", body="", kind="pr",
                          created_at="2027-01-01T00:00:00Z"),
                    _merged_pull(n, f"2027-01-0{n + 1}T00:00:00Z"),
                    files=[pr_file(f) for f in files],
                )
            rec_seed = issue(
                710, title="Twin fault alpha beta report",
                body="fault at src/p1.py:1:1 and src/p2.py:2:1 and"
                     " src/q1.py:3:1 and src/q2.py:4:1",
                created_at="2027-02-01T00:00:00Z",
            )
            rec_seed["reactions"] = {"+1": 6, "total_count": 6}
            src.add_issue(rec_seed)
            for cid, author in (
                (74001, "carol"), (74002, "dave"), (74003, "erin"),
                (74004, "frank"), (74005, "grace"),
            ):
                _add_comment(src, 710, cid, "me too", author,
                             created_at="2027-02-02T00:00:00Z")
            src.add_issue(issue(
                711, title="Twin fault alpha report",
                body="fault at src/p1.py:1:1 and src/p2.py:2:1",
                created_at="2027-02-01T00:00:00Z",
            ))
            src.add_issue(issue(
                712, title="Twin fault beta report",
                body="fault at src/q1.py:1:1 and src/q2.py:2:1",
                created_at="2027-02-01T00:00:00Z",
            ))
            sync_repo(conn, src, REPO)
            _pin_synced_at(conn)
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._td.cleanup()

    def test_greedy_selection_rules(self) -> None:
        """RED at base on legs (a), (b), (c), (d), (e); GREEN after the
        fix."""
        # ---- legs (a) + (b): partner by affinity; every pair clears the
        # floor ----------------------------------------------------------
        conn = open_existing(self.affinity_db)
        try:
            payload = derive.bundle(conn, REPO, size="2-3", count=2)
            bundles = [_member_numbers(x) for x in payload["bundles"]]
            self.assertTrue(
                bundles, "the affinity store must yield at least one bundle"
            )
            first = sorted(bundles[0])
            self.assertEqual(
                first, [700, 701],
                f"(a) the partner is chosen by AFFINITY alone (A=701 at"
                f" 9.167 beats B=702 at 6.167); triage totals only pick the"
                f" seed. At base triage multiplication picks B first. Got"
                f" {bundles}",
            )
            self.assertNotIn(
                702, first,
                "(b) B must not join [S, A]: its pair with the existing"
                " member A has no anchor (affinity 0 in edge terms, below"
                " the floor) even though aff(S, B) clears the floor",
            )
        finally:
            conn.close()

        # ---- leg (c): cohesion stop --------------------------------------
        conn = open_existing(self.cohesion_db)
        try:
            payload = derive.bundle(
                conn, REPO, size="2-3", count=1, allow_shared_files=True,
                config=str(self.cohesion_cfg),
            )
            bundles = [_member_numbers(x) for x in payload["bundles"]]
            self.assertEqual(
                bundles, [[750, 751]],
                f"(c) growth must stop at the strong pair (cohesion 9.774;"
                f" the weak partner's 2.583 is below the fraction of it)"
                f" - got {bundles}",
            )
            self.assertEqual(
                bundles and payload["bundles"][0]["why_not_bigger"],
                "below cohesion threshold",
                f"(c) the cohesion stop is reported as the pinned reason"
                f" string (at base the bundle grows to the membership cap)"
                f" - got {payload['bundles'][0]['why_not_bigger']!r}",
            )
        finally:
            conn.close()

        # ---- leg (d): hub files are exempt from disjointness -------------
        conn = open_existing(self.hubexempt_db)
        try:
            payload = derive.bundle(conn, REPO, size="2-2", count=2)
            bundles = [sorted(_member_numbers(x)) for x in payload["bundles"]]
            self.assertEqual(
                bundles, [[760, 761], [762, 763]],
                f"(d) a candidate whose only file overlap with a PRIOR"
                f" bundle is a hub file (src/h.py, 0.5 share) must be"
                f" admitted - at base the shared-file conflict collapses"
                f" the output to one bundle; got {bundles}",
            )
        finally:
            conn.close()

        # ---- leg (e) (amendment wave): the per-pair floor rule, isolated
        # from the cohesion stop ------------------------------------------
        conn = open_existing(self.pairfloor_db)
        try:
            payload = derive.bundle(conn, REPO, size="2-3", count=2)
            bundles = [_member_numbers(x) for x in payload["bundles"]]
            self.assertTrue(bundles, "the twin store must yield a bundle")
            members = sorted(bundles[0])
            self.assertEqual(
                members, [710, 711] if 711 in members else [710, 712],
                f"(e) exactly ONE twin joins the seed - the other twin's"
                f" MEAN affinity equals 0.5 x cohesion (the cohesion stop"
                f" cannot exclude it), so only the per-pair floor rule"
                f" (its pair with the seated twin has no anchor) can;"
                f" got {bundles}",
            )
            self.assertEqual(len(members), 2)
            excluded = 712 if 711 in members else 711
            unbundled = payload.get("unbundled") or []
            entries = {e["number"]: e for e in unbundled}
            self.assertIn(
                excluded, entries,
                f"(e) the pair-floor-excluded twin {excluded} must be"
                f" reported under unbundled (got {unbundled})",
            )
            reason = str(entries[excluded].get("reason", ""))
            self.assertIn(
                "floor", reason.lower(),
                f"(e) {excluded}'s unbundled reason must name the floor"
                f" (author-pinned wording), got {reason!r}",
            )
        finally:
            conn.close()


# ==== C13: gated and unbundled are reported ==================================


class GatedUnbundledReportingTests(_StoreTestCase):
    """AC5 (#69), DISCRIMINATING. RED at base: the bundle payload has no
    ``gated`` and no ``unbundled`` key (verified at base: keys are
    bundles/config_source/derived/dropped/excluded/formula_version/
    merge_candidates/pool/related_not_bundleable/repo/weights).

    Store: PRs 1/2 [src/w1.py, src/w2.py] merged, PLUS three independent
    two-file filler PRs 3/4/5 (files disjoint from w1/w2; amendment wave 2)
    raising the coupling population from N=2 to N=5 so the cochange anchor
    survives the NEW rule (non-hub AND support >= 2 AND lift > 1). At the
    wave-1 geometry the pair sat at lift EXACTLY 1.0 (2*2/(2*2), probed at
    base via derive.coupling) and would have stopped firing after the fix;
    with the fillers lift = 5*2/(2*2) = 2.5 > 1 and w1/w2's hub share is
    2/5 = 0.4 < 0.5 (non-hub), while the pair's component scores are
    unchanged (probed at base: files_direct 1.0, cochange 1.0). Open
    issues: 851/852 (anchored, co-bundled: both ref w1+w2; 851 carries the
    demand so it seeds), 853 (loner: no path refs, no edges, no anchor with
    anyone - a pool member left out), 854 (assignee row -> gated
    'assigned'). At base: pool = [851, 852, 853], one bundle [851, 852],
    and 853's exclusion is explained nowhere.

    Amendment-wave legs: the co-bundled pair (851, 852) fires TWO anchors
    (verified at base: files_direct 1.0 and cochange 1.0; weighted
    contributions 4.0 vs 3.0 under the payload's affinity weights), while
    the base ``anchor`` field reports the alphabetically first fired
    anchor ('cochange') - the claim-3 defect. The fix adds a per-pair
    ``anchors`` list, strongest first, with ``anchor`` == anchors[0]; the
    triage payload must carry the same ``gated`` block shape as the
    bundle payload (base triage payload has no such key).
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        for n in (1, 2):
            src.add_pr(
                issue(n, title=f"v{n}", body="", kind="pr",
                      created_at="2027-01-01T00:00:00Z"),
                _merged_pull(n, f"2027-01-0{n + 1}T00:00:00Z"),
                files=[pr_file("src/w1.py"), pr_file("src/w2.py")],
            )
        # Amendment wave 2: three independent two-file PRs, files disjoint
        # from w1/w2, lifting the coupling population N from 2 to 5 so the
        # (w1, w2) pair's lift is 5*2/(2*2) = 2.5 > 1 under the NEW cochange
        # anchor rule (at the wave-1 geometry lift was exactly 1.0 and the
        # anchor would not have fired post-fix; hub share 2/5 < 0.5 stays
        # non-hub, and the pair's component scores are unchanged).
        for n, files in {
            3: ["src/k1.py", "src/k2.py"],
            4: ["src/k3.py", "src/k4.py"],
            5: ["src/k5.py", "src/k6.py"],
        }.items():
            src.add_pr(
                issue(n, title=f"v{n}", body="", kind="pr",
                      created_at="2027-01-01T00:00:00Z"),
                _merged_pull(n, f"2027-01-0{n + 1}T00:00:00Z"),
                files=[pr_file(f) for f in files],
            )
        rec_s = issue(
            851, title="Pairable seed fault",
            body="fault at src/w1.py:1:1 and src/w2.py:2:1",
            created_at="2027-02-01T00:00:00Z",
        )
        rec_s["reactions"] = {"+1": 4, "total_count": 4}
        src.add_issue(rec_s)
        _add_comment(src, 851, 86001, "here too", "bob",
                     created_at="2027-02-02T00:00:00Z")
        src.add_issue(issue(
            852, title="Pairable partner fault",
            body="fault at src/w1.py:1:1 and src/w2.py:2:1",
            created_at="2027-02-01T00:00:00Z",
        ))
        src.add_issue(issue(
            853, title="Loner entirely unrelated prose",
            body="nothing relevant here at all",
            created_at="2027-02-01T00:00:00Z",
        ))
        rec_g = issue(
            854, title="Assigned gated fault", body="plain body",
            created_at="2027-02-01T00:00:00Z",
        )
        rec_g["assignees"] = [{"login": "erin"}]
        src.add_issue(rec_g)
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)

    def test_gated_and_unbundled_are_reported(self) -> None:
        """RED at base, GREEN after the fix. The bundle payload reports
        ``gated: {count, by_gate, numbers}`` (the assigned issue 854 under
        its gate) and ``unbundled: [{number, reason}]`` (the below-floor
        loner 853 with a non-empty reason; the bundled pair is not listed
        as unbundled). Amendment-wave legs: each co-bundled pair carries
        ``anchors`` - all fired anchor signals strongest-first by weighted
        contribution - with ``anchor`` == anchors[0], and the TRIAGE
        payload carries the same ``gated`` block shape."""
        # Geometry precondition (passes at base; documents why the three
        # filler PRs exist): the (w1, w2) co-change pair sits at support 2
        # with lift 2.5 > 1 in the coupling table the production cochange
        # component reads, so it clears the NEW anchor rule (support >= 2
        # AND lift > 1 AND non-hub, share 2/5 < 0.5). At the wave-1 geometry
        # (population 2) lift was exactly 1.0 and the anchor would not have
        # fired post-fix.
        coupling = derive.coupling(self.conn, REPO, "src/w1.py")
        entry = next(
            (r for r in coupling["results"] if r["path"] == "src/w2.py"), None
        )
        self.assertIsNotNone(entry, "src/w2.py must couple with src/w1.py")
        assert entry is not None  # narrow None for the lines below
        self.assertEqual(entry["support"], 2, entry)
        self.assertIsNotNone(entry["lift"], entry)
        self.assertGreater(float(entry["lift"]), 1.0, entry)

        payload = derive.bundle(self.conn, REPO)

        # ---- per-pair anchors list, strongest first (amendment wave) ----
        anchor_signals = {
            "signature", "files_direct", "structural", "cochange",
        }
        affinity_weights = payload["weights"]["affinity"]
        pair = next(
            p for p in payload["bundles"][0]["pairs"]
            if sorted((p["a"], p["b"])) == [851, 852]
        )
        self.assertIn(
            "anchors", pair,
            "each bundle pair must list its fired anchors (strongest first)",
        )
        anchors = pair["anchors"]
        fired = sorted(
            name
            for name in anchor_signals
            if pair["components"].get(name, {}).get("score", 0) > 0
        )
        self.assertEqual(
            sorted(anchors), fired,
            f"anchors must list EVERY fired anchor signal, got {anchors!r}"
            f" (fired: {fired})",
        )
        expected_order = sorted(
            fired,
            key=lambda name: (
                -affinity_weights.get(name, 0.0)
                * pair["components"][name]["score"],
                name,
            ),
        )
        self.assertEqual(
            anchors, expected_order,
            f"anchors must be ordered strongest first by weighted"
            f" contribution, got {anchors!r} (expected {expected_order})",
        )
        self.assertEqual(
            pair["anchor"], anchors[0],
            "the singular anchor field equals the strongest fired anchor",
        )

        self.assertIn("gated", payload, "the bundle payload must report gated")
        gated = payload["gated"]
        self.assertIsInstance(gated, dict)
        for key in ("count", "by_gate", "numbers"):
            self.assertIn(key, gated, f"gated must carry {key}")
        self.assertEqual(gated["count"], 1)
        self.assertIn(854, gated["numbers"])
        by_gate = gated["by_gate"]
        self.assertIsInstance(by_gate, dict)
        self.assertIn("assigned", by_gate)
        self.assertIn(854, by_gate["assigned"])

        self.assertIn(
            "unbundled", payload, "the bundle payload must report unbundled"
        )
        unbundled = payload["unbundled"]
        self.assertIsInstance(unbundled, list)
        entries = {e["number"]: e for e in unbundled}
        self.assertIn(
            853, entries,
            "the below-floor pool member left out must be explained with a"
            " reason",
        )
        self.assertTrue(
            isinstance(entries[853].get("reason"), str)
            and entries[853]["reason"].strip(),
            f"853's unbundled reason must be a non-empty string, got"
            f" {entries[853]!r}",
        )
        self.assertNotIn(851, entries)
        self.assertNotIn(852, entries)

        # ---- the triage payload carries the same gated block -------------
        triage = derive.triage(self.conn, REPO, include_gated=True)
        self.assertIn(
            "gated", triage,
            "the triage payload must report the same gated block",
        )
        triage_gated = triage["gated"]
        self.assertIsInstance(triage_gated, dict)
        for key in ("count", "by_gate", "numbers"):
            self.assertIn(key, triage_gated, f"triage gated must carry {key}")
        self.assertEqual(triage_gated["count"], 1)
        self.assertIn(854, triage_gated["numbers"])
        self.assertIn(
            854, triage_gated["by_gate"].get("assigned", []),
            "the assigned issue is reported under its gate by name",
        )


# ==== C14: the linked_pr gate is narrow =======================================


class LinkedPRGateTests(_StoreTestCase):
    """AC6 (#69), DISCRIMINATING. Legs (1), (2), (4), (5), (6) are RED at
    base; leg (3) is GREEN at base and pins the SURVIVING keyword gate,
    honestly labelled.

    Keyword-closers legs (1)-(3): open issues 401/402/403 and three OPEN
    PRs:
      - 404: draft (items.draft = 1 via the pull payload's draft flag),
        state 'open', with a GitHub-reported closes edge 404 -> 401.
      - 405: state 'open' (stale) but merged_at SET (pull merged=True with
        an explicit stamp), with a closes edge 405 -> 402.
      - 406: plain open non-draft PR whose DESCRIPTION says "This change
        fixes #403." -> the real ingest path derives a closes_keyword edge
        (source='keyword').
    At base all three fire the gate through the keyword_closers fallback
    (state='open' + closes/closes_keyword): every issue carries
    'linked_pr (append-only evidence; may be stale)'.

    Direct-link legs (4)-(5, amendment wave): open issues 421/422 whose
    links.open_prs connection (the real sync path, _BulkSource) writes
    rel='linked_pr' source='link' edges at:
      - 424: an OPEN DRAFT PR (421 -> 424),
      - 425: a stale-state PR with merged_at SET (422 -> 425).
    At base the DIRECT gate fires on both (state='open', kind='pr'):
    gates ['linked_pr'].

    Legacy comment-keyword leg (6, amendment wave): issue 423 with a
    directly-inserted closes_keyword edge from OPEN PR 426 whose evidence
    names a COMMENT ('comment:12345 closing keyword' - the pre-#67 shape;
    since #67 the ingest path records comments as mentions, so this row
    can only exist as a legacy survivor). At base it gates through the
    keyword fallback; per AC6's first clause a comment-derived keyword
    must NOT gate - the fix distinguishes it from description/commit
    keyword edges by its evidence provenance.
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        for n in (401, 402, 403, 421, 422, 423):
            src.add_issue(issue(
                n, title=f"gate issue {n}", body="plain",
                created_at="2027-06-01T00:00:00Z",
            ))
        draft_p = pull(404, merged=False)
        draft_p["draft"] = True
        src.add_pr(
            issue(404, title="draft pull", body="", kind="pr",
                  created_at="2027-06-02T00:00:00Z"),
            draft_p,
            files=[pr_file("src/d.py")],
        )
        stale_p = pull(405, merged=True)
        stale_p["merged_at"] = "2027-06-03T00:00:00Z"
        src.add_pr(
            issue(405, title="stale state pull", body="", kind="pr",
                  state="open", created_at="2027-06-02T00:00:00Z"),
            stale_p,
            files=[pr_file("src/e.py")],
        )
        src.add_pr(
            issue(406, title="fix in progress", body="This change fixes #403.",
                  kind="pr", created_at="2027-06-02T00:00:00Z"),
            pull(406, merged=False),
            files=[pr_file("src/f.py")],
        )
        draft2 = pull(424, merged=False)
        draft2["draft"] = True
        src.add_pr(
            issue(424, title="draft direct link", body="", kind="pr",
                  created_at="2027-06-02T00:00:00Z"),
            draft2,
            files=[pr_file("src/g.py")],
        )
        stale2 = pull(425, merged=True)
        stale2["merged_at"] = "2027-06-04T00:00:00Z"
        src.add_pr(
            issue(425, title="stale direct link", body="", kind="pr",
                  state="open", created_at="2027-06-02T00:00:00Z"),
            stale2,
            files=[pr_file("src/h.py")],
        )
        src.add_pr(
            issue(426, title="legacy comment keyword pull", body="",
                  kind="pr", created_at="2027-06-02T00:00:00Z"),
            pull(426, merged=False),
            files=[pr_file("src/i.py")],
        )
        src.links[421] = _links(parent=None) | {"open_prs": [_node(424)]}
        src.links[422] = _links(parent=None) | {"open_prs": [_node(425)]}
        sync_repo(conn, src, REPO)
        _insert_edge(conn, REPO, 404, "closes", 401, source="closing_ref")
        _insert_edge(conn, REPO, 405, "closes", 402, source="closing_ref")
        _insert_edge(
            conn, REPO, 426, "closes_keyword", 423, source="keyword",
            evidence="comment:12345 closing keyword",
        )
        _pin_synced_at(conn)

    def test_linked_pr_gate_is_narrow(self) -> None:
        """RED at base (legs 1, 2, 4, 5, 6 gate), GREEN after the fix;
        leg 3 is GREEN at base and must stay GREEN."""
        # geometry preconditions (pass at base; they document the fixture)
        rows_geo = {
            int(r["number"]): (r["draft"], r["state"], r["merged_at"])
            for r in self.conn.execute(
                "SELECT number, draft, state, merged_at FROM items"
                " WHERE number IN (404, 405, 424, 425)"
            )
        }
        self.assertEqual(rows_geo[404][0], 1, "PR 404 must be stored as draft")
        self.assertEqual(rows_geo[405][1], "open")
        self.assertIsNotNone(rows_geo[405][2], "PR 405 must carry merged_at")
        self.assertEqual(rows_geo[424][0], 1, "PR 424 must be stored as draft")
        self.assertEqual(rows_geo[425][1], "open")
        self.assertIsNotNone(rows_geo[425][2], "PR 425 must carry merged_at")
        linked_edges = {
            (r["src_id"], r["dst_id"])
            for r in self.conn.execute(
                "SELECT src_id, dst_id FROM edges WHERE repo = ?"
                " AND rel = 'linked_pr'", (REPO,)
            )
        }
        self.assertIn(("421", "424"), linked_edges)
        self.assertIn(("422", "425"), linked_edges)

        out = derive.triage(self.conn, REPO, include_gated=True)
        rows = {r["number"]: r for r in out["rows"]}

        def _linked(n: int) -> list[str]:
            return [g for g in rows[n]["gates"] if "linked_pr" in g]

        # Leg 1 (RED at base): an open DRAFT PR's closes edge does not gate.
        self.assertEqual(
            _linked(401), [],
            f"(1) a draft PR must not gate its target as linked_pr, got"
            f" {rows[401]['gates']!r}",
        )
        # Leg 2 (RED at base): a PR whose merged_at is set never gates even
        # with a stale open state.
        self.assertEqual(
            _linked(402), [],
            f"(2) a merged PR (merged_at set) must not gate its target as"
            f" linked_pr regardless of the stale state, got"
            f" {rows[402]['gates']!r}",
        )
        # Leg 3 (GREEN at base): a PR-DESCRIPTION closes_keyword from an
        # open non-draft PR still gates, with the append-only label.
        self.assertTrue(
            any("append-only" in g for g in _linked(403)),
            f"(3) the PR-description keyword gate must survive with its"
            f" append-only label, got {rows[403]['gates']!r}",
        )
        # Leg 4 (RED at base): a DIRECT linked_pr edge at an open DRAFT PR
        # does not gate.
        self.assertEqual(
            _linked(421), [],
            f"(4) a draft PR must not gate its target even through a direct"
            f" linked_pr edge, got {rows[421]['gates']!r}",
        )
        # Leg 5 (RED at base): a DIRECT linked_pr edge at a stale-state PR
        # whose merged_at is set does not gate.
        self.assertEqual(
            _linked(422), [],
            f"(5) a merged PR (merged_at set) must not gate its target even"
            f" through a direct linked_pr edge, got {rows[422]['gates']!r}",
        )
        # Leg 6 (RED at base): a legacy COMMENT-derived closes_keyword edge
        # does not gate (the AC6 first clause).
        self.assertEqual(
            _linked(423), [],
            f"(6) a comment-derived keyword edge must not gate its target"
            f" as linked_pr, got {rows[423]['gates']!r}",
        )


# ==== C15: built-in priority labels + relative hotspot ========================


class BuiltinLabelHotspotTests(_StoreTestCase):
    """AC7 (#69), DISCRIMINATING. RED at base on all three legs:
    label_weight is null on an issue labelled 'high' (the [triage.labels]
    map is empty at base), config_source is 'defaults', and hotspot is the
    absolute recency/10 scheme under which a 1-day-old merge and a
    77-day-old merge BOTH score 0.1 (and a hub-file issue scores higher).

    Merged PRs (N = 5, hub share = touches/N; DEFAULT_HUB_SHARE = 0.5):
      PR 1 [src/hot_new.py, n1] merged 2027-06-30 (1 day before the pinned
           as-of 2027-07-01)
      PR 2 [src/hot_old.py, n2] merged 2027-04-15 (77 days before: still
           inside the base 90-day window, so base recency is EQUAL)
      PR 3/4/5 [src/hubfile.py, n_i] merged 06-28..06-30 -> hubfile in 3/5
           = 0.6 share: HUB
    Issues (open; hotspot verified at base): 801 labelled 'high', 802
    labelled 'wibble', 811 refs hot_new (base hotspot 0.1), 812 refs
    hot_old (base hotspot 0.1 - EQUAL at base), 813 refs hubfile only
    (base hotspot 0.3 - HIGHER at base through raw touch count).

    Post-fix contracts: 'high' hits the built-in map (label_weight > 0)
    while 'wibble' stays missing-or-zero (the missing-feature rule);
    config_source names the built-in map; hotspot is relative to the
    repository's own distribution (1 day beats 77 days) and hub-discounted
    (the hubfile issue drops below the fresh non-hub one).

    Amendment waves 4+5 - the hotspot CLOCK pin: an explicit as_of=T (T =
    2027-07-11, ten days past the pinned stamps S = 2027-07-01) must
    anchor the hotspot arithmetic at T even though the store's
    sync_state stamps say S. Under S the two probe merges sit at ages
    1/77 days; under T at 11/87 - a uniform +10-day shift. The check
    computes the T-clock answer two ways (the as_of call, and a plain
    call on a store whose own stamps were moved to T) and requires them
    EQUAL per row, and the as_of payload must carry hotspot_asof == T.
    The NORMALIZED hotspot is uniform-offset invariant BY DESIGN (every
    raw recency sum scales by the same exp(-delta/45) under the shift
    and the max cancels), so amendment wave 5 removed the wave-4
    normalized-pair inequality - it could only ever pass by float noise
    - and moved the offset-sensitivity pin onto the additive per-row
    hotspot_raw diagnostic (the unnormalized recency sum), which MUST
    differ between the T-anchored and stored-stamp-anchored runs.
    """


    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        for n, (files, merged) in {
            1: (["src/hot_new.py", "src/n1.py"], "2027-06-30T00:00:00Z"),
            2: (["src/hot_old.py", "src/n2.py"], "2027-04-15T00:00:00Z"),
            3: (["src/hubfile.py", "src/n3.py"], "2027-06-30T00:00:00Z"),
            4: (["src/hubfile.py", "src/n4.py"], "2027-06-29T00:00:00Z"),
            5: (["src/hubfile.py", "src/n5.py"], "2027-06-28T00:00:00Z"),
        }.items():
            src.add_pr(
                issue(n, title=f"v{n}", body="", kind="pr",
                      created_at="2027-01-01T00:00:00Z"),
                _merged_pull(n, merged),
                files=[pr_file(f) for f in files],
            )
        rec_h = issue(
            801, title="high labelled fault",
            body="fault at src/hot_new.py:1:1",
            created_at="2027-06-20T00:00:00Z",
        )
        rec_h["labels"] = [{"name": "high", "color": "ff0000"}]
        src.add_issue(rec_h)
        rec_w = issue(
            802, title="wibble labelled fault",
            body="fault at src/hot_new.py:2:1",
            created_at="2027-06-20T00:00:00Z",
        )
        rec_w["labels"] = [{"name": "wibble", "color": "00ff00"}]
        src.add_issue(rec_w)
        src.add_issue(issue(
            811, title="recent file fault",
            body="fault at src/hot_new.py:3:1",
            created_at="2027-06-20T00:00:00Z",
        ))
        src.add_issue(issue(
            812, title="older file fault",
            body="fault at src/hot_old.py:4:1",
            created_at="2027-06-20T00:00:00Z",
        ))
        src.add_issue(issue(
            813, title="hub file fault",
            body="fault at src/hubfile.py:5:1",
            created_at="2027-06-20T00:00:00Z",
        ))
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn, stamp="2027-07-01T00:00:00Z")

    def test_default_priority_labels_and_relative_hotspot(self) -> None:
        """RED at base, GREEN after the fix. (a) built-in label weights with
        no config file; (b) hotspot relative to the repo's own recency
        distribution; (c) hotspot hub-discounted; (d) an explicit as_of
        anchors the hotspot CLOCK at T even though the store's sync_state
        stamps differ from T (amendment waves 4+5: the payload carries
        hotspot_asof == T, the normalized answer equals the T-stamps
        oracle, and the additive hotspot_raw diagnostic - unlike the
        normalized value, which is uniform-offset invariant BY DESIGN -
        differs between the T-anchored and stored-stamp runs)."""
        out = derive.triage(self.conn, REPO, include_gated=True)
        rows = {r["number"]: r for r in out["rows"]}
        for n in (801, 802, 811, 812, 813):
            self.assertIn(n, rows)

        # (a) built-in priority labels (RED at base: label_weight is None).
        high = rows[801]["scores"]["features_raw"]["label_weight"]
        self.assertIsNotNone(
            high, "an issue labelled 'high' must hit a built-in label weight"
        )
        self.assertGreater(high, 0)
        wibble = rows[802]["scores"]["features_raw"]["label_weight"]
        self.assertTrue(
            wibble is None or wibble == 0,
            f"a non-conventional label stays missing-or-zero, got {wibble!r}",
        )
        source = out["config_source"]
        self.assertTrue(
            "built-in" in source.lower() or "builtin" in source.lower(),
            f"config_source must name the built-in map, got {source!r}",
        )

        # (b) relative hotspot: 1-day-old merge beats 77-day-old (RED at
        # base: both are 0.1 under the absolute 90-day-window scheme).
        self.assertGreater(
            rows[811]["hotspot"], rows[812]["hotspot"],
            f"hotspot must be relative to the repository's own recency"
            f" distribution (got {rows[811]['hotspot']} vs"
            f" {rows[812]['hotspot']})",
        )

        # (c) hub discount: the hubfile-only issue (0.6 share) drops below
        # the fresh non-hub issue (RED at base: 0.3 vs 0.1).
        self.assertGreater(
            rows[811]["hotspot"], rows[813]["hotspot"],
            f"a hub file's hotspot contribution must be discounted (got"
            f" {rows[811]['hotspot']} vs {rows[813]['hotspot']})",
        )

        # (d) hotspot CLOCK (amendment waves 4+5): an explicit as_of
        # anchors the hotspot clock at T even when the store's sync_state
        # stamps say otherwise. The stamps sit at S = 2027-07-01 (under S:
        # hot_new merged 1 day ago, hot_old 77 days ago); T = 2027-07-11
        # shifts every merge age by a uniform +10 days (11 and 87). The
        # NORMALIZED hotspot is uniform-offset invariant BY DESIGN - every
        # row's raw recency sum scales by the same factor exp(-delta/45)
        # under the shift and the max normalization cancels it - so the
        # clock pin cannot rest on normalized values differing (wave 5
        # removed that inequality: it could only ever pass by float
        # noise). Three legs instead:
        t = "2027-07-11T00:00:00Z"
        asof = derive.triage(self.conn, REPO, as_of=t)
        asof_rows = {r["number"]: r for r in asof["rows"]}
        for n in (811, 812, 813):
            self.assertIn(
                n, asof_rows,
                f"every fixture issue predates {t} and must stay in the"
                f" as-of view; got {sorted(asof_rows)}",
            )

        # (d.i) the as-of payload names the clock the hotspot arithmetic
        # was anchored at.
        self.assertEqual(
            asof.get("hotspot_asof"), t,
            f"an as_of triage run must carry hotspot_asof == the as_of"
            f" stamp so the hotspot clock is auditable; got"
            f" {asof.get('hotspot_asof')!r}",
        )

        # (d.ii) the normalized as-of answer equals the T-stamps oracle -
        # computed here by moving the stamps to T on a writable connection
        # and running the plain call (the stamps ARE the plain call's
        # hotspot clock, base behavior the pinned fixtures already rely
        # on), restoring S afterwards.
        writer = connect(self.db_path)
        try:
            _pin_synced_at(writer, stamp=t)
            clock_t = derive.triage(writer, REPO)
        finally:
            _pin_synced_at(writer, stamp="2027-07-01T00:00:00Z")
            writer.close()
        clock_t_rows = {r["number"]: r for r in clock_t["rows"]}
        for n in (811, 812, 813):
            self.assertEqual(
                asof_rows[n]["hotspot"], clock_t_rows[n]["hotspot"],
                f"as_of={t} must anchor the hotspot clock at T: issue"
                f" {n}'s hotspot under as_of"
                f" ({asof_rows[n]['hotspot']!r}) must equal the value a"
                f" store whose own stamps are T produces"
                f" ({clock_t_rows[n]['hotspot']!r}) - an implementation"
                f" anchored at the stored stamps (2027-07-01) answers the"
                f" S-clock value instead",
            )

        # (d.iii) the additive per-row hotspot_raw diagnostic (the
        # UNNORMALIZED recency sum) is offset-sensitive where the
        # normalized value is not: under the stored stamps the probe
        # merges sit at ages 1/77 days, under as_of={t} at 11/87, and
        # exp(-1/45) != exp(-11/45) - the raw sums MUST move with the
        # clock on every probe row (rows = the stored-stamp-anchored run
        # from the top of this test).
        for n in (811, 812, 813):
            for run, label in ((rows, "stored-stamp"), (asof_rows, "as_of")):
                self.assertIn(
                    "hotspot_raw", run[n],
                    f"every triage row must carry the additive hotspot_raw"
                    f" diagnostic (the unnormalized recency sum); the"
                    f" {label} run's row {n} lacks it (keys:"
                    f" {sorted(run[n])})",
                )
            self.assertNotEqual(
                asof_rows[n]["hotspot_raw"], rows[n]["hotspot_raw"],
                f"issue {n}'s hotspot_raw must move with the clock: the"
                f" raw recency sum is offset-sensitive (probe merge ages"
                f" 1/77 days under the stored stamps vs 11/87 under"
                f" as_of={t}), unlike the normalized hotspot which is"
                f" offset-invariant by design; got"
                f" {asof_rows[n]['hotspot_raw']!r} under as_of vs"
                f" {rows[n]['hotspot_raw']!r} under the stored stamps",
            )


# ==== C16: the analog filters to PRs before truncating =======================


class AnalogFilterTests(_StoreTestCase):
    """AC8 (#69), DISCRIMINATING. RED at base: _analog_for truncates the
    candidate list to 20 BEFORE filtering to PRs, so with 21 issue
    candidates ranked above the only PR candidate the analog comes back
    null (verified at base).

    Store: PR 1 merged 01-02 [src/tgt.py] (vocabulary only - its body
    carries no path ref, so it is never an anchored candidate); issue 900
    (open, body refs src/tgt.py); issues 901..921 near-clones of 900 (all
    score 7.444 - verified at base); PR 950 merged 01-20 with body
    "updates src/tgt.py:1:1" and files [src/tgt.py] (files_direct anchor,
    score 5.604); and the amendment-wave OPEN-PR BAIT PR 951 (open, never
    merged, same file/anchor shape, score 6.625 - ABOVE the merged PR but
    still below the top-20 cut; verified at base the two PRs sit at ranks
    22 and 23 of 23 anchored candidates). At base neither PR reaches the
    capped list, so the analog is null.
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        src.add_pr(
            issue(1, title="vocab pr", body="", kind="pr",
                  created_at="2027-01-01T00:00:00Z"),
            _merged_pull(1, "2027-01-02T00:00:00Z"),
            files=[pr_file("src/tgt.py")],
        )
        src.add_issue(issue(
            900, title="Target submit crash report",
            body="crash when submitting near src/tgt.py:1:1",
            created_at="2027-02-01T00:00:00Z",
        ))
        for k in range(1, 22):
            src.add_issue(issue(
                900 + k, title="Target submit crash report",
                body=f"crash when submitting near src/tgt.py:1:1 variant {k}",
                created_at="2027-02-01T00:00:00Z",
            ))
        src.add_pr(
            issue(950, title="Chore: touch target file",
                  body="updates src/tgt.py:1:1", kind="pr",
                  created_at="2027-01-15T00:00:00Z"),
            _merged_pull(950, "2027-01-20T00:00:00Z"),
            files=[pr_file("src/tgt.py")],
        )
        src.add_pr(
            issue(951, title="Crash report for the target area",
                  body="adjusts src/tgt.py:2:1 now", kind="pr",
                  created_at="2027-01-16T00:00:00Z"),
            pull(951, merged=False),
            files=[pr_file("src/tgt.py")],
        )
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)

    def test_analog_filters_before_truncating(self) -> None:
        """RED at base (analog: null), GREEN after the fix. The issue's PR
        analog is the MERGED PR 950 even though 21 issue candidates AND the
        open non-merged bait PR 951 outrank it: the candidate scan filters
        to MERGED PRs BEFORE applying the cap (a kind-only filter would
        return the open bait)."""
        packet = derive.brief(self.conn, REPO, 900)
        analog = packet.get("analog")
        self.assertTrue(
            isinstance(analog, dict) and analog.get("number") == 950,
            f"the MERGED PR at rank 23 must still be the analog (filter to"
            f" merged PRs before truncating - the open bait 951 ranks above"
            f" it at 22); got {analog!r}",
        )


# ==== C17: the acceptance gate - beat the baselines on the recorded corpus ====


#: The NEIGHBORHOOD backtest corpus (amendment wave 2): recorded from the
#: REAL user-level store - the multi-issue merged fix PRs of
#: ZaxbyHub/zaxbygraph plus their closed issues and open-at-the-time pool
#: - with each fix PR's pr_commits section so the commit-message keyword
#: stream replays. tests/eval_fixture/ stays anchored by the #29 traces;
#: the neighborhood rule and the disclosed recording limitations live in
#: tests/eval_fixture_backtest/README.md.
_EVAL_FIXTURE_DIR = Path(__file__).resolve().parent / "eval_fixture_backtest"
_REAL_REPO = "zaxbyhub/zaxbygraph"

#: HARD floors (amendment wave; critic-approved): strictly above the
#: issue's measured pre-change values (18% precision / 24% recall on the
#: hand backtest). A later AMEND may RAISE these floors from a measured
#: post-change run but may NEVER lower them - they are a ratchet, not a
#: placeholder.
_PRECISION_FLOOR = 0.18
_RECALL_FLOOR = 0.24

#: PINNED ground-truth enumeration for the recorded corpus (amendment
#: wave 2), computed BY HAND from the REPLAYED store under the POST-FIX
#: GT semantics (merged multi-issue closers under closes UNION
#: closes_keyword with body@/commit evidence): 10 fix PRs, 14 distinct
#: truth pairs, 22 eligible target endpoints. Per-PR ground-truth
#: provenance: 'closes' where the GitHub-reported stream alone closes >=
#: 2 issues, else 'keyword' - PR 49 is the corpus's keyword case (its
#: #32 link rides the commit-message stream; its timeline closes edge is
#: one of the recording's disclosed losses). At BASE the same store
#: enumerates 13 truth pairs / 20 eligible targets (PR 49 drops out -
#: closes-only counting), which is exactly the widening the AC2 keyword
#: fallback must deliver.
_PINNED_FIX_PRS = [20, 41, 45, 49, 50, 51, 54, 63, 76, 78]
_PINNED_TRUTH_PAIRS = 14
_PINNED_ELIGIBLE_TARGETS = 22
_PINNED_PROVENANCE = {
    20: "closes", 41: "closes", 45: "closes", 49: "keyword",
    50: "closes", 51: "closes", 54: "closes", 63: "closes",
    76: "closes", 78: "closes",
}

#: Revisions coverage on the recorded corpus (amendment wave 3), computed
#: from the REPLAYED store: the truth endpoints whose item_title/item_body
#: pre-image chain reaches before their fix PR's created_at, and the fix-PR
#: cases whose endpoints are ALL covered (the only cases whose text-as-of
#: is certifiable from the store). These drive the per-case
#: ``text_as_of_unknown`` disclosure pins: every case NOT in
#: _PINNED_FULLY_COVERED_FIX_PRS must disclose; a fully covered case must
#: not.
_PINNED_COVERED_ENDPOINTS = [61, 62, 72]
_PINNED_FULLY_COVERED_FIX_PRS = [63]


class RecordedCorpusBacktestTests(unittest.TestCase):
    """AC9 (#69), DISCRIMINATING - the acceptance gate for the whole PR.
    RED at base: the recorded real-corpus fixture replays fine and the
    bundle metrics exist (precision 0.05 / recall 0.3077 at base on this
    corpus), but the payload carries no ``baselines`` key, so the
    comparison cannot even be made.

    The corpus is the committed replayable NEIGHBORHOOD recording
    (tests/eval_fixture_backtest/events.jsonl): 45 items = 10 multi-issue
    merged fix PRs + their 22 closed issues + the 35-issue
    open-at-the-time pool, replayed through the real sync path by
    tests/fixtures_backtest.py::replay_backtest_store (amendment wave 3:
    the base JsonlReplaySource re-ingests the item bundles, and the
    fixture-side mapping additionally applies the recorded revisions rows
    - 50, the source store's own text pre-images - so as-of text
    features have the same history to resolve against as the live store).
    Determinism requires the fix's random same-size baseline to use a
    FIXED SEED; this check cannot force that, it only fails if the seeded
    baseline is not computed.

    The pinned GT numbers above were computed from the REPLAYED store
    (probe at amendment time; re-verified after the wave-3 revisions
    recording - the GT comes from edges and is unchanged by it) and must
    hold POST-FIX: they describe the per-fix-PR ground-truth enumeration
    the fix itself performs, so a mismatch means the fix's GT semantics -
    not the fixture - moved.

    DISCLOSED LIMITATION carried by this CHECK (not the fixture,
    amendment wave 3): the recording's revisions coverage is 3 of the 22
    truth endpoints (61, 62, 72), and only PR 63's case has both
    endpoints covered - pre-images exist solely for edits the source
    store's sync OBSERVED. The plan's per-case disclose+exclude rule
    follows: a case whose truth endpoints lack an item_title/item_body
    pre-image chain reaching before the fix PR's created_at must disclose
    ``text_as_of_unknown`` (and must not consume post-stamp text it
    cannot certify), which is asserted below; the floors above stay
    pinned as the ratchet they are and are re-derived only from a
    measured post-change run by a later AMEND.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from fixtures_backtest import replay_backtest_store

        cls._td = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._td.name) / "real.db"
        conn = connect(cls.db_path)
        try:
            init_schema(conn)
            replay_backtest_store(
                conn, _EVAL_FIXTURE_DIR / "events.jsonl", _REAL_REPO,
            )
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._td.cleanup()

    def setUp(self) -> None:
        self.conn = open_existing(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()

    def test_beats_baselines_on_recorded_corpus(self) -> None:
        """RED at base, GREEN after the fix. On the recorded corpus:
        (a) the eval bundle backtest's pair precision AND pair recall are
        STRICTLY above the runtime-computed random same-size baseline;
        (b) both clear the HARD floors (precision > 0.18, recall > 0.24 -
        strictly above the issue's measured pre-change values; a later
        AMEND may raise these floors but never lower them); (c) eval
        triage's top1 next-fix rate is at or above the
        lowest-number-first baseline on the same corpus; (d) the
        ground-truth enumeration matches the PINNED numbers (10 fix PRs /
        14 truth pairs / 22 eligible targets, PR 49 keyword-provenanced)
        and every fix PR discloses its ground_truth source, with the
        TOP-LEVEL ground_truth label pinned to the exclusive 'mixed'
        (nine closes cases + PR 49's keyword case, amendment wave 4);
        (e) every
        per-case entry carries a ``disclosures`` list and
        ``text_as_of_unknown`` fires for exactly the cases whose truth
        endpoints lack covering revisions pre-images (9 of 10; only PR
        63's fully-covered case stays silent) - the honest-backtest pin
        (amendment wave 3)."""
        from zaxbygraph import evaluate

        result = evaluate.run_eval(self.conn, _REAL_REPO, "bundle")
        metrics = result["metrics"]
        self.assertIn("baselines", result)
        baselines = result["baselines"]
        self.assertIn("random_same_size", baselines)
        random_baseline = baselines["random_same_size"]

        precision = metrics["pair_precision"]
        recall = metrics["pair_recall"]
        base_precision = random_baseline["pair_precision"]
        base_recall = random_baseline["pair_recall"]
        self.assertIsNotNone(precision)
        self.assertIsNotNone(recall)
        self.assertIsNotNone(base_precision)
        self.assertIsNotNone(base_recall)
        self.assertGreater(
            precision, base_precision,
            f"pair precision {precision} must beat the random same-size"
            f" baseline {base_precision}",
        )
        self.assertGreater(
            recall, base_recall,
            f"pair recall {recall} must beat the random same-size baseline"
            f" {base_recall}",
        )
        self.assertGreater(
            precision, _PRECISION_FLOOR,
            f"pair precision {precision} must be strictly above the hard"
            f" floor {_PRECISION_FLOOR} (the pre-change value)",
        )
        self.assertGreater(
            recall, _RECALL_FLOOR,
            f"pair recall {recall} must be strictly above the hard floor"
            f" {_RECALL_FLOOR} (the pre-change value)",
        )

        # ---- pinned ground-truth enumeration (amendment wave 2) ----------
        # These numbers were computed by hand from the REPLAYED store under
        # the post-fix GT semantics (see _PINNED_* above); post-fix they
        # must hold exactly. A failure here means the fix's GT enumeration
        # drifted, not that the fixture changed (the fixture is committed).
        self.assertIn(
            "per_fix_pr", result,
            "the eval bundle payload must report per_fix_pr so the GT"
            " enumeration is auditable",
        )
        per_fix = result["per_fix_pr"]
        self.assertEqual(
            sorted(int(e["pr"]) for e in per_fix),
            _PINNED_FIX_PRS,
            f"the corpus holds exactly {_PINNED_FIX_PRS} merged multi-issue"
            f" closers under closes UNION closes_keyword (body@/commit"
            f" evidence); got {sorted(int(e['pr']) for e in per_fix)}",
        )
        self.assertEqual(
            metrics.get("truth_pairs"), _PINNED_TRUTH_PAIRS,
            f"truth pairs must enumerate to {_PINNED_TRUTH_PAIRS} distinct"
            f" pairs over the ten fix PRs; got {metrics.get('truth_pairs')}"
            f" (at base, closes-only counting yields 13 - PR 49 drops out)",
        )
        self.assertEqual(
            metrics.get("eligible_targets"), _PINNED_ELIGIBLE_TARGETS,
            f"eligible targets must enumerate to"
            f" {_PINNED_ELIGIBLE_TARGETS} truth-pair endpoints; got"
            f" {metrics.get('eligible_targets')}"
            f" (at base, closes-only counting yields 20)",
        )
        # ---- per-case ground-truth provenance (amendment wave 2) ---------
        provenance = {int(e["pr"]): e.get("ground_truth") for e in per_fix}
        self.assertEqual(
            provenance, _PINNED_PROVENANCE,
            f"every fix PR must disclose which stream supplied its ground"
            f" truth ('closes' where the GitHub-reported stream alone closes"
            f" >= 2 issues, else 'keyword' - PR 49's #32 link rides the"
            f" commit-message stream); got {provenance}",
        )
        # the exact PR-49-keyword expectation, singled out: its #32 closure
        # survives only through the commit-message keyword stream
        entry_49 = next(e for e in per_fix if int(e["pr"]) == 49)
        self.assertEqual(
            entry_49.get("ground_truth"), "keyword",
            f"PR 49's ground truth must be keyword-provenanced (its #32"
            f" link exists only as a timeline closes edge plus the"
            f" commit-message keyword edge of the recorded commit"
            f" stream); got {entry_49!r}",
        )

        # ---- top-level ground-truth label (amendment waves 4+5) --------
        # Nine closes-provenanced cases + PR 49's keyword case cover the
        # corpus JOINTLY, so the payload's TOP-LEVEL ground_truth label is
        # the exclusive 'mixed'. The top-level vocabulary is
        # 'github'/'keyword'/'mixed' (amendment wave 5): 'github' = every
        # case's truth is gh-alone-recoverable (gh closes >= 2 per case),
        # 'keyword' = every case needs the keyword stream, 'mixed' = both
        # kinds; the PER-CASE provenance stays 'closes'/'keyword' (C2 pins
        # the same top-level field as 'keyword' on the all-keyword store,
        # C1 pins it 'github' on the all-GitHub store).
        self.assertEqual(
            result.get("ground_truth"), "mixed",
            f"the top-level ground_truth label is the exclusive 'mixed'"
            f" here: nine closes-provenanced cases plus PR 49's keyword"
            f" case cover the corpus jointly, neither stream alone; got"
            f" {result.get('ground_truth')!r}",
        )

        # ---- per-case text-as-of disclosure (amendment wave 3) -----------
        # The fixture's honest revisions coverage is 3 of the 22 truth
        # endpoints (61, 62, 72) and only PR 63's case has both endpoints
        # covered, so every OTHER case must disclose text_as_of_unknown -
        # the plan's per-case disclose+exclude rule: a case whose truth
        # endpoints lack an item_title/item_body pre-image chain reaching
        # before the fix PR's created_at cannot certify its text-as-of and
        # must say so instead of silently scoring post-stamp text. The
        # fully covered case must stay silent (the rule keys on actual
        # coverage, not a blanket disclosure).
        disclosures: dict[int, list] = {}
        for entry in per_fix:
            pr = int(entry["pr"])
            self.assertIn(
                "disclosures", entry,
                f"per_fix_pr[{pr}] must carry a disclosures list (the"
                f" per-case disclose+exclude surface)",
            )
            value = entry["disclosures"]
            self.assertIsInstance(
                value, list,
                f"per_fix_pr[{pr}].disclosures must be a list of strings;"
                f" got {value!r}",
            )
            for item in value:
                self.assertIsInstance(
                    item, str,
                    f"per_fix_pr[{pr}].disclosures entries are strings;"
                    f" got {item!r}",
                )
            disclosures[pr] = value
        firing = sorted(
            pr for pr, ds in disclosures.items()
            if "text_as_of_unknown" in ds
        )
        expected_firing = sorted(
            pr for pr in _PINNED_FIX_PRS
            if pr not in _PINNED_FULLY_COVERED_FIX_PRS
        )
        self.assertEqual(
            firing, expected_firing,
            f"text_as_of_unknown must fire for exactly the cases whose"
            f" truth endpoints lack covering pre-images (all of"
            f" {_PINNED_FIX_PRS} except {_PINNED_FULLY_COVERED_FIX_PRS},"
            f" whose endpoints {_PINNED_COVERED_ENDPOINTS} supply the"
            f" coverage); got firing {firing}, expected"
            f" {expected_firing}",
        )

        triage_eval = evaluate.run_eval(self.conn, _REAL_REPO, "triage")
        top1 = triage_eval["metrics"]["top1_next_fix_rate"]
        lowest_first = triage_eval["baselines"]["lowest_number_first"]
        base_top1 = lowest_first["top1_next_fix_rate"]
        self.assertIsNotNone(top1)
        self.assertIsNotNone(base_top1)
        self.assertGreaterEqual(
            top1, base_top1,
            f"eval triage's top1 next-fix rate {top1} must be at or above"
            f" the lowest-number-first baseline {base_top1}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
