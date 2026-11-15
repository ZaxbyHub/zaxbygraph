"""Backtest-harness acceptance checks for issues #68/#69, authored against
the traces' acceptance tables BEFORE any implementation (issue-tracer check
author role; base 36d4bb4111995a5ce59d844984e6619fde384321).

Frozen check names (#68 acceptance table):

    C1  test_eval_bundle_replays_real_bundle          (AC1, DISCRIMINATING)
    C2  test_keyword_ground_truth_on_rest_store       (AC2, DISCRIMINATING)
    C3  test_every_metric_has_a_baseline              (AC3, NEW-SURFACE)
    C4  test_eval_triage_next_fix_hit_rate            (AC4, NEW-SURFACE)
    C5  test_localize_ranking_is_not_alphabetical     (AC5, DISCRIMINATING)
    C6  test_no_ground_truth_is_an_error              (AC6, DISCRIMINATING)
    C7  test_as_of_views                              (AC7, NEW-SURFACE)
    C8  PRESERVING, lives in tests/test_similar.py:
        python -m pytest tests/test_similar.py -q -k
        test_derived_tier_never_writes                  (GREEN at base)
    C9  AC9 is NON-EXECUTABLE(PRODUCT_DECISION): the maintainer records the
        floor values on issue #68 once AC1-AC5 produce numbers on the
        recorded corpus. No test here.

AUTHOR-PINNED CONTRACTS the fix must satisfy (the shapes are decisions made
by the check author at freeze time; the implementation must match them):

- C1: ``zaxbygraph.evaluate`` imports the production bundler as
  ``from zaxbygraph.derive import bundle as derive_bundle`` and eval bundle
  mode drives it through ONE prepare function (amendment wave 3's
  VIEW-OWNERSHIP pin): ``evaluate.prepare_snapshot(conn, repo, as_of=T,
  exclude_pr=<fix PR>, reopen=...)`` returns a prepared connection, the
  eval replays ONE SNAPSHOT PER MERGED MULTI-ISSUE FIX PR at
  ``T == that PR's created_at`` and calls ``derive_bundle`` on the returned
  PREPARED CONNECTION WITHOUT ``as_of`` (the prepared view owns the
  snapshot - the as_of kwarg belongs to the prepare call, never to the
  derive call). C1's direct oracle uses the same two calls. The eval bundle
  result payload carries ``proposed_pairs`` (a list of two-element int
  lists, each pair sorted ascending - the DISTINCT union of the per-case
  proposals, an audit view) and ``per_fix_pr`` - a list with one entry per
  such PR, each entry ``{"pr": <fix PR number>, "proposed_pairs":
  [[a, b], ...]}`` whose proposed_pairs are exactly the co-member pairs of
  the bundles the production bundler proposed on that PR's prepared
  snapshot (every 2-combination of each bundle's member numbers, each pair
  sorted ascending). The aggregate metrics are MICRO-aggregates (amendment
  wave 3): per-case numerators and denominators are SUMMED, never unioned,
  so a pair proposed at two snapshots counts in BOTH cases' denominators -
  ``pair_precision`` = sum(per-case hits)/``proposed_case_total`` and
  ``pair_recall`` = sum(per-case hits)/``truth_case_total`` (None only
  when the relevant denominator is empty), while ``truth_pairs`` stays the
  AUDIT count of distinct closed-by-one-PR pairs, distinct from the micro
  denominators the metrics actually divide by. Since amendment wave 5 the
  payload's TOP-LEVEL ``ground_truth`` label is 'github' on this store:
  every truth edge is GitHub-reported (closing_ref) and each case is
  gh-alone >= 2 (the top-level vocabulary is 'github'/'keyword'/'mixed';
  the PER-CASE provenance field stays 'closes'/'keyword' - C17 pins
  'mixed' here, C2 pins 'keyword' on the all-keyword store).
- C2: the eval payload carries ``ground_truth: "keyword"`` when the
  PR-description/commit-message ``closes_keyword`` fallback supplied the
  truth, and the pair metrics are non-NULL on such a store.
- C3/C4: the eval payload carries ``baselines`` with, per mode:
  bundle -> ``random_same_size`` (``pair_precision``/``pair_recall``),
  triage -> ``lowest_number_first`` (``top1_next_fix_rate``/
  ``top3_next_fix_rate``), localize -> ``most_touched`` (``acc_at_5``/
  ``precision_at_5``). Every baseline metric is a real number, or None
  alongside a non-empty ``reason`` string in the same baseline dict.
  Since amendment wave 4, C4 also mirrors C1's prepare-chain contract:
  the eval triage path drives the SAME ONE prepare chain - one prepared
  snapshot per merged fix PR that closed at least one issue, prepared at
  that PR's ``created_at`` with ``exclude_pr`` == that PR (amendment
  wave 5: the eligibility is MODE-SPECIFIC - triage admits merged PRs
  closing >= 1 issue, while bundle's per-PR cases need >= 2) - and the
  production triage driver, imported into evaluate as ``derive_triage``
  (``from zaxbygraph.derive import triage as derive_triage``, the mirror
  of C1's ``derive_bundle`` alias), runs on the PREPARED connection
  WITHOUT ``as_of`` (an as_of riding the triage call would be a second,
  competing view authority). Since amendment wave 6 the ground-truth
  provenance SUFFICIENCY threshold is MODE-SPECIFIC: a triage case is
  'closes'-provenanced at gh-alone >= 1, where bundle's label needs
  gh-alone >= 2 per case, so C4's all-GitHub triage store (two
  closing_ref-provenanced cases, no keyword edges) labels its TOP-LEVEL
  ``ground_truth`` 'github', not 'mixed'.
- C6: ``evaluate.run_eval`` on a mode with zero ground truth RAISES a
  ``ValueError`` subclass whose instance carries ``code == 
  "no_ground_truth"`` and a non-empty message naming what the store lacks
  (the CLI layer translates it to a non-zero exit).
- C7: ``derive.triage`` and ``derive.bundle`` accept an ``as_of=<ISO-Z
  string>`` keyword answering from a temp-table as-of view. As-of
  suppression rules (amendment wave 2, leakage legs): a ``mentions`` edge
  whose evidence names a comment created after the stamp is invisible;
  comments created after the stamp never count into ``participants``; the
  item's text features (resolved paths, lexical/title evidence) come from
  the body revision current AT the stamp (the ``revisions`` pre-image
  whose ``valid_from <= stamp < valid_to``); and the UNDATED child tables
  (``labels``, ``assignees``, ``issue_field_values`` carry no timestamps)
  are suppressed wholesale in as-of views - ``label_weight`` and
  ``field_weight`` report null and an undated assignee row cannot gate
  ('assigned') an as-of row - because a row whose arrival time the store
  cannot express must never change the as-of answer.

Class split across the two frozen modules (pre-implementation amendment
wave, critic-approved): 13 DISCRIMINATING (C1, C2, C5, C6, C9, C10, C11,
C12, C13, C14, C15, C16, C17) + 3 NEW-SURFACE (C3, C4, C7) + 2
PRESERVING (C8, C18). The frozen argv cells and test names are unchanged
- the amendments added assertions inside the same test methods and
updated docstrings/fixture geometry only.

Hermetic: FakeGitHubSource-derived sources only; no network, no live
GitHub, no gh; stdlib only; unittest (also collectable by pytest). Every
store's sync_state stamps are pinned so triage stage-2 arithmetic never
depends on the wall clock.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fixtures import REPO, FakeGitHubSource, comment, issue, pr_file, pull
from fixtures_eval import _insert_edge

from zaxbygraph.db import connect, init_schema, open_existing
from zaxbygraph import derive, evaluate
from zaxbygraph.sync import sync_repo


def _merged_pull(number: int, merged_at: str) -> dict:
    """pull() with an explicit merge stamp (the eval geometry needs control
    over merged_at, which the fixture clock derives from the number)."""
    p = pull(number, merged=True)
    p["merged_at"] = merged_at
    return p


def _pin_synced_at(conn, repo: str = REPO, stamp: str = "2027-03-01T00:00:00Z") -> None:
    """Freeze the store's freshness stamps so stage-2 hotspot recency (and
    therefore triage ORDER) is deterministic (tests/test_triage_bundle.py
    PINNED_SYNCED_AT pattern)."""
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
    (the len-based ids in comment_on collide across numbers;
    tests/test_triage_bundle.py _add_comment note)."""
    rec = comment(cid, body, author=author, created_at=created_at)
    src.issue_comments.setdefault(number, []).append(rec)
    src.issues[number]["comments"] = len(src.issue_comments[number])


class _StoreTestCase(unittest.TestCase):
    """One temp store per test class (subclasses override ``_build``); one
    read-only connection per test (tests/test_similar.py DerivedTierTest
    pattern)."""

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


# ==== C1: eval bundle replays the REAL bundle ================================


class EvalBundleReplayTests(_StoreTestCase):
    """AC1 (#68). RED at base: evaluate.py never calls the production
    bundler - it scores similar()'s top-5 candidates (source-traced in the
    issue) - so ``zaxbygraph.evaluate`` has neither ``prepare_snapshot``
    nor ``derive_bundle`` and the pins below fail on the missing
    attributes.

    Store geometry (T0 = 2027-01-01; amendment wave 3):
      - PR 10 merged 01-02: [src/feature.py]                    (vocabulary)
      - filler PRs 11/12/13 merged 01-03/01-03/01-04, two DISJOINT
        unrelated files each, referenced by no issue: hub dilution. At the
        wave-2 geometry every merged PR touched src/feature.py (share 1.0),
        and the post-fix anchor rules (C11: anchors need non-hub evidence)
        would have made every required pair ILLEGAL; with the fillers the
        shared path sits at 1/4 at T1 and 2/5 at T2, both < 0.5 - non-hub
        in every snapshot view.
      - issue 101 created 01-03, body refs src/feature.py
      - issue 105 created 01-03T02:00, body refs src/feature.py
        => {101, 105} is anchored by its OWN evidence (shared resolved path
        + lexical/title overlap) at EVERY snapshot - the
        DUPLICATE-ACROSS-SNAPSHOT pair: its evidence predates both fix PRs
        so it is proposed at BOTH T1 and T2.
      - fix PR 1: PR 120 created 01-05 (T1), merged 01-06:
        [src/feature.py, src/fix_patch.py]; GitHub-reported closes edges
        120 -> 101 and 120 -> 105 (source='closing_ref') => truth pair
        {101, 105}.
      - issue 111 created 01-08 and issue 112 created 01-08T02:00 - the
        LEAK pair: overlapping title/body vocabulary, deliberately NO path
        references and no shared neighbour, so {111, 112} is anchored ONLY
        through fix PR 2's own closes edges (structural common neighbour)
        and sits below the affinity floor on its own (verified arithmetic:
        title 0.8 + lexical ~0.9 + context 0.5 = 2.2 < 3.0). Without
        exclude_pr the fix PR's own evidence proposes its truth pair
        trivially; with exclude_pr the pair has no anchor and is never
        proposed.
      - fix PR 2 (created LATER): PR 130 created 01-10 (T2), merged 01-11:
        [src/fix_patch2.py, src/other2.py]; closes edges 130 -> 111 and
        130 -> 112 (source='closing_ref') => truth pair {111, 112}.
    The bundle ground truth is exactly two distinct pairs, one per fix PR;
    the pair {101, 105} is propose-able at BOTH T1 and T2, which is what
    makes MICRO and UNION aggregation differ on this store.
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        src.add_pr(
            issue(10, title="Add feature module", body="", kind="pr",
                  created_at="2027-01-01T00:00:00Z"),
            _merged_pull(10, "2027-01-02T00:00:00Z"),
            files=[pr_file("src/feature.py")],
        )
        # Hub dilution (amendment wave 3): three independent two-file merged
        # PRs over files no issue references. They close nothing (no new fix
        # PRs), anchor nothing (no issue references their files), and their
        # only effect is the merged-PR population N: 4 at T1, 5 at T2, so
        # src/feature.py's hub share stays below 0.5 in every snapshot view.
        for n, (fa, fb, merged) in {
            11: ("src/filla.py", "src/fillb.py", "2027-01-03T00:00:00Z"),
            12: ("src/fillc.py", "src/filld.py", "2027-01-03T00:00:00Z"),
            13: ("src/fille.py", "src/fillf.py", "2027-01-04T00:00:00Z"),
        }.items():
            src.add_pr(
                issue(n, title=f"filler {n}", body="", kind="pr",
                      created_at="2027-01-01T00:00:00Z"),
                _merged_pull(n, merged),
                files=[pr_file(fa), pr_file(fb)],
            )
        src.add_issue(issue(
            101, title="Login crash on submit",
            body="crash in `src/feature.py` on submit",
            created_at="2027-01-03T00:00:00Z",
        ))
        src.add_issue(issue(
            105, title="Submit handler duplicates work",
            body="submit path in `src/feature.py:10:2` duplicates work",
            created_at="2027-01-03T02:00:00Z",
        ))
        src.add_pr(
            issue(120, title="Fix login crash", body="", kind="pr",
                  created_at="2027-01-05T00:00:00Z"),
            _merged_pull(120, "2027-01-06T00:00:00Z"),
            files=[pr_file("src/feature.py"), pr_file("src/fix_patch.py")],
        )
        # The leak pair (amendment wave 3): near-twin vocabulary, NO path
        # refs, no other neighbour - anchored only through PR 130's closes
        # edges, and below the affinity floor on its own.
        src.add_issue(issue(
            111, title="Reload fault slows the page",
            body="the reload path duplicates work badly here",
            created_at="2027-01-08T00:00:00Z",
        ))
        src.add_issue(issue(
            112, title="Fault reload handling is broken",
            body="the reload path duplicates work badly there",
            created_at="2027-01-08T02:00:00Z",
        ))
        src.add_pr(
            issue(130, title="Fix reload fault", body="", kind="pr",
                  created_at="2027-01-10T00:00:00Z"),
            _merged_pull(130, "2027-01-11T00:00:00Z"),
            files=[pr_file("src/fix_patch2.py"), pr_file("src/other2.py")],
        )
        sync_repo(conn, src, REPO)
        _insert_edge(conn, REPO, 120, "closes", 101, source="closing_ref")
        _insert_edge(conn, REPO, 120, "closes", 105, source="closing_ref")
        _insert_edge(conn, REPO, 130, "closes", 111, source="closing_ref")
        _insert_edge(conn, REPO, 130, "closes", 112, source="closing_ref")
        _pin_synced_at(conn)

    def test_eval_bundle_replays_real_bundle(self) -> None:
        """RED at base, GREEN after the fix.

        Discriminating leg: the fix imports the production bundler into
        evaluate.py as ``derive_bundle`` and drives it through ONE prepare
        function ``evaluate.prepare_snapshot`` (the amendment-wave-3
        VIEW-OWNERSHIP pin). Patching prepare_snapshot with a spying
        wrapper and derive_bundle with a stub whose payload mirrors
        derive.bundle's head shape (repo, bundles with members as
        {number, ...} dicts, merge_candidates, related_not_bundleable,
        excluded, pool, dropped, weights, config_source, formula_version,
        derived) must fully control the proposal set. The stub cannot see
        any as_of (the derive call must not carry one), so it dispatches on
        the prepared connection's VISIBLE ISSUE NUMBERS: it proposes
        {101, 105} on every snapshot plus {111, 112} on T2's, making the
        wave-3 MICRO arithmetic exact: precision == 2/3 (NOT the union's
        1.0 - the pair proposed at both snapshots counts in BOTH cases'
        denominators), recall == 1.0, audit truth_pairs == 2 distinct
        pairs, micro denominators proposed_case_total == 3 and
        truth_case_total == 2.

        Unpatched legs: (i) per_fix_pr equals a directly-computed
        prepare+derive_bundle oracle per fix PR (derive_bundle on the
        prepared connection, without as_of); (ii) the exclusion leg -
        preparing T2 WITHOUT exclude_pr leaves fix PR 130's closes edges in
        the view, structural adjacency anchors {111, 112}, and the proposal
        set DIFFERS from the excluded one (the trivial leak the exclusion
        exists to remove); (iii) the duplication precondition ({101, 105}
        proposed at BOTH snapshots) plus the micro identities - a
        union-aggregating implementation fails the identities; (iv) the
        TOP-LEVEL ground_truth label is 'github' on this all-GitHub
        store, under the stub and unpatched (amendment wave 5)."""
        # --- store-derived case table (pure SQL; holds at base) -----------
        created = {
            int(row["number"]): row["created_at"]
            for row in self.conn.execute(
                "SELECT number, created_at FROM items"
                " WHERE repo = ? AND number IN (120, 130)", (REPO,)
            )
        }
        pr_by_created = {stamp: pr for pr, stamp in created.items()}
        truth_by_pr: dict[int, set[tuple[int, int]]] = {}
        for pr in (120, 130):
            closed = sorted(
                int(row[0]) for row in self.conn.execute(
                    "SELECT dst_id FROM edges WHERE repo = ?"
                    " AND rel IN ('closes', 'closes_keyword')"
                    " AND src_id = ?", (REPO, str(pr)),
                )
            )
            truth_by_pr[pr] = {
                tuple(sorted((a, b)))
                for i, a in enumerate(closed) for b in closed[i + 1:]
            }

        # --- view-ownership pin: ONE prepare function ---------------------
        real_prepare = getattr(evaluate, "prepare_snapshot", None)
        self.assertIsNotNone(
            real_prepare,
            "evaluate must expose prepare_snapshot(conn, repo, as_of,"
            " exclude_pr, reopen) - the ONE prepare function the eval and"
            " the direct oracle share",
        )

        def _stub_bundle(members: list[int]) -> dict:
            return {
                "bundle_id": f"stub-fixture-bundle-{members[0]}",
                "members": [
                    {"number": n, "title": f"stub {n}",
                     "triage_total": 1.0, "stage1": 1.0, "gates": []}
                    for n in members
                ],
                "pairs": [],
                "files": {"direct": [], "linked": [], "analog": []},
                "cochanged_tests": [],
                "open_pr_collisions": [],
                "order": members,
                "risk_summary": {},
                "cohesion": 1.0,
                "why_not_bigger": "membership cap (reached target size)",
            }

        def _visible_issues(prepared) -> set[int]:
            return {
                int(row[0]) for row in prepared.execute(
                    "SELECT number FROM items WHERE repo = ?"
                    " AND kind = 'issue'", (REPO,),
                )
            }

        def _stub(*args, **kwargs):
            # No as_of may ride the derive call (view ownership); the
            # prepared connection's own content identifies the snapshot.
            visible = _visible_issues(args[0])
            member_lists: list[list[int]] = [[101, 105]]
            if 111 in visible and 112 in visible:
                member_lists.append([111, 112])
            return {
                "repo": REPO,
                "bundles": [_stub_bundle(m) for m in member_lists],
                "merge_candidates": [],
                "related_not_bundleable": [],
                "excluded": [],
                "pool": sorted(visible),
                "dropped": [],
                "weights": {},
                "config_source": "defaults",
                "formula_version": "triage.v1",
                "derived": True,
            }

        prepare_calls: list[dict] = []

        def _prepare_spy(*args, **kwargs):
            returned = real_prepare(*args, **kwargs)
            prepare_calls.append({"args": args, "kwargs": kwargs,
                                  "returned": returned})
            return returned

        stub = mock.Mock(side_effect=_stub)
        with mock.patch("zaxbygraph.evaluate.prepare_snapshot", _prepare_spy), \
                mock.patch("zaxbygraph.evaluate.derive_bundle", stub):
            result = evaluate.run_eval(self.conn, REPO, "bundle")

        self.assertEqual(
            len(prepare_calls), 2,
            f"eval bundle must prepare ONE snapshot per merged multi-issue"
            f" fix PR (two here); prepared {len(prepare_calls)}",
        )
        for call in prepare_calls:
            kwargs = call["kwargs"]
            for key in ("as_of", "exclude_pr", "reopen"):
                self.assertIn(
                    key, kwargs,
                    f"prepare_snapshot must be called with the {key}"
                    f" keyword (the pinned (T, exclude_pr, reopen)"
                    f" signature); got {kwargs!r}",
                )
            stamp = kwargs["as_of"]
            self.assertIn(
                stamp, pr_by_created,
                f"prepare_snapshot's as_of must be a fix PR's created_at;"
                f" got {stamp!r}",
            )
            self.assertEqual(
                kwargs["exclude_pr"], pr_by_created[stamp],
                f"the snapshot at {stamp} must exclude that stamp's own fix"
                f" PR {pr_by_created[stamp]} (the trivial-leak control);"
                f" got exclude_pr={kwargs['exclude_pr']!r}",
            )
        self.assertEqual(
            {c["kwargs"]["as_of"] for c in prepare_calls},
            {created[120], created[130]},
            "one prepared snapshot per fix PR, at each PR's created_at",
        )
        self.assertEqual(
            stub.call_count, 2,
            f"derive_bundle is called once per prepared snapshot; got"
            f" {stub.call_count}",
        )
        for call in stub.call_args_list:
            self.assertNotIn(
                "as_of", call.kwargs,
                "derive_bundle runs on the PREPARED connection without"
                " as_of - the prepared view owns the snapshot",
            )
        for call, prepare in zip(stub.call_args_list, prepare_calls):
            self.assertIs(
                call.args[0], prepare["returned"],
                "each derive_bundle call receives the connection its"
                " prepare_snapshot returned (one shared view per case)",
            )

        # --- MICRO-aggregate pins under the stub --------------------------
        # Stub proposals: T1 {{101, 105}}; T2 {{101, 105}, {111, 112}}.
        # Per case: hits 1/1 and 1/2 => micro precision (1+1)/(1+2) = 2/3
        # (a union would report 1.0) and micro recall 2/2 = 1.0; the audit
        # count truth_pairs = 2 distinct pairs while the micro denominators
        # are proposed_case_total = 3 and truth_case_total = 2.
        metrics = result["metrics"]
        self.assertAlmostEqual(
            metrics["pair_precision"], 2 / 3,
            msg=f"micro precision sums per-case numerators/denominators - a"
                f" pair proposed at both snapshots counts twice; union"
                f" aggregation would wrongly report 1.0: {metrics}",
        )
        self.assertEqual(metrics["pair_recall"], 1.0, metrics)
        self.assertEqual(
            metrics.get("truth_pairs"), 2,
            f"truth_pairs is the AUDIT count of distinct closed-by-one-PR"
            f" pairs: {metrics}",
        )
        self.assertEqual(
            metrics.get("proposed_case_total"), 3,
            f"the micro proposed denominator counts per-case proposals"
            f" (1 + 2), not the 2-pair distinct union: {metrics}",
        )
        self.assertEqual(
            metrics.get("truth_case_total"), 2,
            f"the micro truth denominator counts per-case truth pairs"
            f" (1 + 1): {metrics}",
        )

        # --- top-level ground-truth label (amendment wave 5): this store's
        # truth is ALL GitHub-reported (every closes edge
        # source='closing_ref', gh-alone >= 2 per case), so the TOP-LEVEL
        # ground_truth label is 'github' - asserted here under the stub
        # because the label describes the STORE's truth streams, not the
        # bundler's proposals (vocabulary: 'github' = every case
        # gh-alone >= 2, 'keyword' = every case needs the keyword stream,
        # 'mixed' = both kinds; per-case provenance stays
        # 'closes'/'keyword').
        self.assertEqual(
            result.get("ground_truth"), "github",
            f"an all-GitHub store (closing_ref edges only, gh-alone >= 2"
            f" per case) must label its top-level ground_truth 'github';"
            f" got {result.get('ground_truth')!r}",
        )

        # --- unpatched leg: the proposal set is reported ------------------
        result = evaluate.run_eval(self.conn, REPO, "bundle")
        self.assertEqual(
            result.get("ground_truth"), "github",
            f"the top-level ground_truth label stays 'github' on this"
            f" all-GitHub store in the unpatched leg too; got"
            f" {result.get('ground_truth')!r}",
        )
        self.assertIn(
            "proposed_pairs", result,
            "the eval bundle payload must report proposed_pairs",
        )
        pairs = result["proposed_pairs"]
        self.assertIsInstance(pairs, list)
        for pair in pairs:
            self.assertEqual(len(pair), 2, pair)
            a, b = pair
            self.assertIsInstance(a, int)
            self.assertIsInstance(b, int)
            self.assertLess(a, b, "each reported pair is sorted ascending")

        # --- one entry per fix PR ------------------------------------------
        self.assertIn(
            "per_fix_pr", result,
            "the eval bundle payload must report per_fix_pr (one entry per"
            " merged multi-issue fix PR)",
        )
        per_fix = result["per_fix_pr"]
        self.assertIsInstance(per_fix, list)
        self.assertEqual(
            len(per_fix), 2,
            f"the store holds exactly two merged multi-issue fix PRs, so"
            f" per_fix_pr has two entries; got {per_fix!r}",
        )
        self.assertEqual(
            sorted(int(e["pr"]) for e in per_fix), [120, 130],
            f"per_fix_pr names the fix PRs under 'pr'; got {per_fix!r}",
        )

        def _bundle_pairs(snapshot: dict) -> set[tuple[int, int]]:
            extracted: set[tuple[int, int]] = set()
            for b in snapshot["bundles"]:
                numbers = [
                    int(m["number"]) if isinstance(m, dict) else int(m)
                    for m in b["members"]
                ]
                for i, a in enumerate(numbers):
                    for c in numbers[i + 1:]:
                        extracted.add(tuple(sorted((a, c))))
            return extracted

        def _oracle_pairs(pr: int) -> set[tuple[int, int]]:
            prepared = real_prepare(
                self.conn, REPO, as_of=created[pr], exclude_pr=pr,
                reopen=True,
            )
            return _bundle_pairs(evaluate.derive_bundle(prepared, REPO))

        case_pairs: dict[int, set[tuple[int, int]]] = {}
        for entry in per_fix:
            pr = int(entry["pr"])
            got = {tuple(int(x) for x in p) for p in entry["proposed_pairs"]}
            expected = _oracle_pairs(pr)
            self.assertEqual(
                got, expected,
                f"per_fix_pr[{pr}].proposed_pairs must equal exactly the"
                f" co-member pairs derive_bundle proposes on the connection"
                f" prepared for that PR's stamp ({created[pr]}), computed"
                f" without as_of; got {sorted(got)}, expected"
                f" {sorted(expected)}",
            )
            case_pairs[pr] = got

        # the top-level proposed_pairs is the DISTINCT-pair audit view
        self.assertEqual(
            {tuple(int(x) for x in p) for p in result["proposed_pairs"]},
            set().union(*case_pairs.values()),
            "the top-level proposed_pairs reports the DISTINCT union of the"
            " per-case proposals (an audit view) - the metric denominators"
            " are the per-case MICRO sums, not this union",
        )

        # --- duplication precondition: the shared-evidence pair is
        # proposed at BOTH snapshots (this is what separates micro from
        # union aggregation on this store) --------------------------------
        self.assertTrue(
            case_pairs[120] & case_pairs[130],
            f"the shared-evidence pair {{101, 105}} must be proposed at"
            f" BOTH snapshots (its evidence predates both fix PRs); got"
            f" T1 {sorted(case_pairs[120])} vs T2"
            f" {sorted(case_pairs[130])}",
        )

        # --- micro identities over the payload's own per-case data -------
        hits_total = sum(
            len(case_pairs[pr] & truth_by_pr[pr]) for pr in case_pairs
        )
        proposed_total = sum(len(p) for p in case_pairs.values())
        truth_total = sum(len(truth_by_pr[pr]) for pr in case_pairs)
        metrics = result["metrics"]
        self.assertEqual(
            metrics.get("truth_pairs"), 2,
            f"truth_pairs is the AUDIT count of distinct closed-by-one-PR"
            f" pairs (2 here, from the edges): {metrics}",
        )
        self.assertEqual(
            metrics.get("proposed_case_total"), proposed_total,
            f"proposed_case_total must equal the sum of per-case proposal"
            f" counts ({proposed_total}): {metrics}",
        )
        self.assertEqual(
            metrics.get("truth_case_total"), truth_total,
            f"truth_case_total must equal the sum of per-case truth-pair"
            f" counts ({truth_total}): {metrics}",
        )
        if proposed_total:
            self.assertAlmostEqual(
                metrics["pair_precision"], hits_total / proposed_total,
                msg=f"pair_precision must be the MICRO aggregate"
                    f" sum(hits)/proposed_case_total ="
                    f" {hits_total}/{proposed_total}: {metrics}",
            )
        if truth_total:
            self.assertAlmostEqual(
                metrics["pair_recall"], hits_total / truth_total,
                msg=f"pair_recall must be the MICRO aggregate"
                    f" sum(hits)/truth_case_total ="
                    f" {hits_total}/{truth_total}: {metrics}",
            )

        # --- the exclusion leg: without exclude_pr the fix PR's own
        # closes edges anchor its truth pair (the trivial leak) -----------
        leak_prepared = real_prepare(
            self.conn, REPO, as_of=created[130], exclude_pr=None,
            reopen=True,
        )
        leak_pairs = _bundle_pairs(
            evaluate.derive_bundle(leak_prepared, REPO)
        )
        self.assertNotEqual(
            leak_pairs, case_pairs[130],
            f"excluding the fix PR must change the snapshot's proposal set"
            f" (without exclusion PR 130's closes edges leak the truth"
            f" pair); got {sorted(leak_pairs)} vs excluded"
            f" {sorted(case_pairs[130])}",
        )
        self.assertIn(
            (111, 112), leak_pairs,
            f"without exclusion, PR 130's closes edges make 111/112"
            f" structural neighbours and the truth pair is proposed"
            f" trivially; got {sorted(leak_pairs)}",
        )
        self.assertNotIn(
            (111, 112), case_pairs[130],
            f"with exclude_pr=130 the leak is gone: {{111, 112}} has no"
            f" anchor of its own and must not be proposed; got"
            f" {sorted(case_pairs[130])}",
        )


# ==== C2: keyword ground truth on a REST-shaped store ========================


class _CommitsBulkSource(FakeGitHubSource):
    """FakeGitHubSource whose listing yields one PAGE and whose children
    carry the GraphQL-only ``pr_commits`` section (the tests/
    test_triage_bundle.py _BulkSource pattern plus commits), so commit
    messages reach pr_commits through the real sync path."""

    def __init__(self) -> None:
        super().__init__()
        self.commits: dict[int, list[dict]] = {}

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
            if n in self.commits:
                children["pr_commits"] = deepcopy(self.commits[n])
            out[n] = children
        return out


class KeywordGroundTruthTests(_StoreTestCase):
    """AC2 (#68). RED at base: the store has NO rel='closes' edges, so eval
    bundle reports labels ['no_ground_truth'] with null metrics and ok true
    (observed at base). The PR-description keywords must take over as
    ground truth, labelled ``ground_truth: 'keyword'``.

    REST-store shape (T0 = 2027-01-01):
      - PR 10 merged 01-02: [src/fix.py]                          (vocabulary)
      - issue 1 created 01-03, body refs src/fix.py
      - issue 2 created 01-03T01:00, body refs src/fix.py
      - PR 20 created 01-05, merged 01-06, body "Fixes #1. Fixes #2."
        -> the real ingest path derives rel='closes_keyword',
           source='keyword' edges 20->1 and 20->2 (verified at base: the
           keyword regex needs the keyword adjacent to each #N, hence two
           sentences). No closes edges anywhere in the store.

    Commit-message fallback leg (amendment wave): issue 7/8 created 01-03,
    and PR 30 created 01-05, merged 01-06 with an EMPTY description but a
    stored pr_commits section whose two commit messages say "Fixes #7" and
    "Fixes #8" -> the real ingest path derives closes_keyword edges with
    commit-sha evidence (verified at base). The keyword ground truth must
    cover BOTH streams: the description pair {1, 2} AND the commit-message
    pair {7, 8} - pinned via truth_pairs >= 2 and eligible targets >= 4.
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = _CommitsBulkSource()
        src.add_pr(
            issue(10, title="vocab", body="", kind="pr",
                  created_at="2027-01-01T00:00:00Z"),
            _merged_pull(10, "2027-01-02T00:00:00Z"),
            files=[pr_file("src/fix.py")],
        )
        src.add_issue(issue(
            1, title="crash one", body="refs src/fix.py:1:1",
            created_at="2027-01-03T00:00:00Z",
        ))
        src.add_issue(issue(
            2, title="crash two", body="refs src/fix.py:2:1",
            created_at="2027-01-03T01:00:00Z",
        ))
        src.add_issue(issue(
            7, title="crash seven", body="refs src/fix.py:7:1",
            created_at="2027-01-03T00:00:00Z",
        ))
        src.add_issue(issue(
            8, title="crash eight", body="refs src/fix.py:8:1",
            created_at="2027-01-03T01:00:00Z",
        ))
        src.add_pr(
            issue(20, title="Fix both crashes", body="Fixes #1. Fixes #2.",
                  kind="pr", created_at="2027-01-05T00:00:00Z"),
            _merged_pull(20, "2027-01-06T00:00:00Z"),
            files=[pr_file("src/fix.py")],
        )
        src.add_pr(
            issue(30, title="Fix via commits", body="", kind="pr",
                  created_at="2027-01-05T00:00:00Z"),
            _merged_pull(30, "2027-01-06T00:00:00Z"),
            files=[pr_file("src/fix.py")],
        )
        src.commits[30] = [
            {"sha": "a" * 40, "message": "Fixes #7"},
            {"sha": "b" * 40, "message": "Fixes #8"},
        ]
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)

    def test_keyword_ground_truth_on_rest_store(self) -> None:
        """RED at base, GREEN after the fix. run_eval 'bundle' on a store
        with only keyword-stream closures from merged PRs reports
        ``ground_truth == 'keyword'`` and NON-NULL pair metrics, and the
        keyword ground truth covers BOTH the PR-description pair {1, 2}
        and the commit-message pair {7, 8} (truth_pairs >= 2, eligible
        targets >= 4)."""
        result = evaluate.run_eval(self.conn, REPO, "bundle")
        self.assertEqual(
            result.get("ground_truth"), "keyword", result.get("ground_truth")
        )
        metrics = result["metrics"]
        self.assertIsNotNone(
            metrics.get("pair_recall"),
            f"pair_recall must be measurable on keyword ground truth: {metrics}",
        )
        self.assertIsNotNone(
            metrics.get("pair_precision"),
            f"pair_precision must be measurable on keyword ground truth:"
            f" {metrics}",
        )
        self.assertGreaterEqual(
            metrics.get("truth_pairs", 0), 2,
            f"the keyword ground truth must cover the commit-message"
            f" fallback pair {{7, 8}} alongside the description pair"
            f" {{1, 2}}: {metrics}",
        )
        self.assertGreaterEqual(
            metrics.get("eligible_targets", 0), 4,
            f"all four keyword-truth targets must be eligible: {metrics}",
        )


# ==== C3 + C4: baselines beside every metric; eval triage mode ==============


class EvalBacktestMiniTests(_StoreTestCase):
    """AC3 + AC4 (#68), NEW-SURFACE. The mini backtest corpus:

      - PR 5 merged 01-03: [src/one.py, src/two.py]      (vocabulary/pool)
      - issue 11 created 01-04, body refs src/one.py
      - issue 12 created 01-04T01:00, body refs src/two.py
      - issue 13 created 01-04T02:00, no path refs (triage filler)
      - PR 21 created 01-06, merged 01-07: [src/one.py, src/patch.py];
        closes edges 21 -> 11 and 21 -> 12 (source='closing_ref').
      - issue 14 created 01-05, body refs src/one.py (single-issue truth
        target); PR 22 created 01-08, merged 01-09: [src/fix22.py]; closes
        edge 22 -> 14 (source='closing_ref') - the TRIAGE eligibility case
        (amendment wave 5): triage admits merged PRs closing >= 1 issue,
        while bundle's per-PR cases still need >= 2, so PR 22 opens a
        triage case but NO bundle case.

    Ground truth available per mode at base: bundle truth pair {11, 12};
    localize targets 11/12 whose reachable truth file is src/one.py; and a
    next-fix signal (issues 11/12 were fixed by the later merged PR 21,
    issue 14 by the later merged PR 22) for the new eval triage mode.
    RED at base: 'triage' is an unknown mode (ValueError) and no payload
    carries a ``baselines`` key.
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        src.add_pr(
            issue(5, title="vocab", body="", kind="pr",
                  created_at="2027-01-01T00:00:00Z"),
            _merged_pull(5, "2027-01-03T00:00:00Z"),
            files=[pr_file("src/one.py"), pr_file("src/two.py")],
        )
        src.add_issue(issue(
            11, title="fault one", body="refs src/one.py:1:1",
            created_at="2027-01-04T00:00:00Z",
        ))
        src.add_issue(issue(
            12, title="fault two", body="refs src/two.py:1:1",
            created_at="2027-01-04T01:00:00Z",
        ))
        src.add_issue(issue(
            13, title="fault three", body="unrelated notes",
            created_at="2027-01-04T02:00:00Z",
        ))
        src.add_issue(issue(
            14, title="fault four", body="refs src/one.py:2:1",
            created_at="2027-01-05T00:00:00Z",
        ))
        src.add_pr(
            issue(21, title="Fix both faults", body="", kind="pr",
                  created_at="2027-01-06T00:00:00Z"),
            _merged_pull(21, "2027-01-07T00:00:00Z"),
            files=[pr_file("src/one.py"), pr_file("src/patch.py")],
        )
        # Single-issue merged fix PR (amendment wave 5): eval TRIAGE
        # eligibility is >= 1 closed issue (mode-specific - bundle's per-PR
        # cases still need >= 2), so PR 22 must open a triage case but no
        # bundle case. Its file is fresh so it perturbs no coupling math.
        src.add_pr(
            issue(22, title="Fix fault four", body="", kind="pr",
                  created_at="2027-01-08T00:00:00Z"),
            _merged_pull(22, "2027-01-09T00:00:00Z"),
            files=[pr_file("src/fix22.py")],
        )
        sync_repo(conn, src, REPO)
        _insert_edge(conn, REPO, 21, "closes", 11, source="closing_ref")
        _insert_edge(conn, REPO, 21, "closes", 12, source="closing_ref")
        _insert_edge(conn, REPO, 22, "closes", 14, source="closing_ref")
        _pin_synced_at(conn)

    def _assert_metric_value(self, container: dict, key: str, label: str) -> None:
        """A baseline/metric value is a real number, or None WITH a
        non-empty ``reason`` string in the same dict (the None-with-reason
        rule this check pins)."""
        self.assertIn(key, container, f"{label} must carry {key}")
        value = container[key]
        if value is None:
            self.assertIn(
                "reason", container,
                f"{label}.{key} is None without a reason",
            )
            reason = container["reason"]
            self.assertTrue(
                isinstance(reason, str) and reason.strip(),
                f"{label}.{key} is None with an empty reason",
            )
        else:
            self.assertIsInstance(value, (int, float), f"{label}.{key}")
            self.assertNotIsInstance(value, bool, f"{label}.{key}")

    def test_every_metric_has_a_baseline(self) -> None:
        """RED at base, GREEN after the fix. Each required mode's payload
        gains a ``baselines`` key with the mode-appropriate named baseline:
        bundle -> random_same_size (fixed seed), triage ->
        lowest_number_first, localize -> most_touched. Every baseline
        metric is a number or None-with-reason (author-pinned rule)."""
        cases = {
            "bundle": ("random_same_size", ("pair_precision", "pair_recall")),
            "triage": ("lowest_number_first",
                       ("top1_next_fix_rate", "top3_next_fix_rate")),
            "localize": ("most_touched", ("acc_at_5", "precision_at_5")),
        }
        for mode, (baseline_name, metric_keys) in cases.items():
            result = evaluate.run_eval(self.conn, REPO, mode)
            self.assertIn(
                "baselines", result, f"eval {mode} payload must carry baselines"
            )
            baselines = result["baselines"]
            self.assertIn(
                baseline_name, baselines,
                f"eval {mode} must report the {baseline_name} baseline,"
                f" got {sorted(baselines)}",
            )
            baseline = baselines[baseline_name]
            self.assertIsInstance(baseline, dict)
            for key in metric_keys:
                self._assert_metric_value(
                    baseline, key, f"{mode}.{baseline_name}.{key}"
                )

    def test_eval_triage_next_fix_hit_rate(self) -> None:
        """RED at base ('triage' is an unknown eval mode -> ValueError),
        GREEN after the fix. run_eval 'triage' reports how often the
        top-ranked issue (and any of the top three) was among the next
        issues fixed: ``metrics.top1_next_fix_rate`` and
        ``metrics.top3_next_fix_rate`` are real numbers >= 0.0 (0.0
        allowed), and ``baselines.lowest_number_first`` carries the same
        two rates as comparable numbers.

        Amendment wave 4 adds the prepare-chain pin mirroring C1's: the
        triage backtest drives the ONE prepare chain (one snapshot per
        merged fix PR, prepared at that PR's created_at with
        exclude_pr == that PR) and the production triage driver NEVER
        receives as_of on the prepared connection (the prepared view owns
        the snapshot, exactly as in bundle mode). Amendment wave 5 pins
        the MODE-SPECIFIC eligibility: triage admits merged PRs closing
        >= 1 issue, so the single-issue closer PR 22 opens a triage case
        alongside the two-issue closer PR 21 (bundle's per-PR cases still
        need >= 2).

        Amendment wave 6 pins the MODE-SPECIFIC sufficiency threshold
        behind the ground-truth provenance labels: a triage case is
        'closes'-provenanced when the GitHub-reported stream alone closes
        >= 1 issue (threshold 1), where bundle's 'closes' provenance
        needs gh-alone >= 2 per case (a bundle case itself needs >= 2
        issues). This store's triage truth is ALL GitHub-reported (PR 21
        closes 11/12, PR 22 closes 14 - every edge source='closing_ref';
        no keyword edges anywhere), so BOTH cases are 'closes'-provenanced
        and the TOP-LEVEL ground_truth label is 'github', NOT 'mixed'."""
        result = evaluate.run_eval(self.conn, REPO, "triage")
        metrics = result["metrics"]
        for key in ("top1_next_fix_rate", "top3_next_fix_rate"):
            self.assertIn(key, metrics, f"eval triage metrics must carry {key}")
            value = metrics[key]
            self.assertIsInstance(value, (int, float), key)
            self.assertNotIsInstance(value, bool, key)
            self.assertGreaterEqual(value, 0.0, key)
        baselines = result["baselines"]
        self.assertIn("lowest_number_first", baselines)
        for key in ("top1_next_fix_rate", "top3_next_fix_rate"):
            value = baselines["lowest_number_first"][key]
            self.assertIsInstance(value, (int, float), f"baseline.{key}")
            self.assertNotIsInstance(value, bool, f"baseline.{key}")
            self.assertGreaterEqual(value, 0.0, f"baseline.{key}")

        # --- prepare-chain contract (amendment wave 4, mirrors C1's
        # view-ownership pin): the triage backtest drives the SAME ONE
        # prepare chain - one prepared snapshot per merged fix PR that
        # closed at least one issue, prepared at that PR's created_at with
        # the PR itself excluded (the same trivial-leak control) - and the
        # production triage driver runs on the PREPARED connection without
        # as_of. The store holds exactly two such fix PRs (amendment wave
        # 5): 21 (created 2027-01-06, closing 11 and 12) and 22 (created
        # 2027-01-08, closing JUST 14 - triage eligibility is >= 1 closed
        # issue, mode-specific: bundle's per-PR cases need >= 2); merged
        # PR 5 closes nothing and must not open a case.
        real_prepare = getattr(evaluate, "prepare_snapshot", None)
        self.assertIsNotNone(
            real_prepare,
            "evaluate must expose prepare_snapshot - the ONE prepare chain"
            " every eval mode shares (C1's view-ownership pin)",
        )
        real_triage = getattr(evaluate, "derive_triage", None)
        self.assertIsNotNone(
            real_triage,
            "evaluate must import the production triage driver as"
            " derive_triage (from zaxbygraph.derive import triage as"
            " derive_triage - the mirror of C1's derive_bundle alias)",
        )

        prepare_calls: list[dict] = []
        triage_calls: list[dict] = []

        def _prepare_spy(*args, **kwargs):
            returned = real_prepare(*args, **kwargs)
            prepare_calls.append({"kwargs": kwargs, "returned": returned})
            return returned

        def _triage_spy(*args, **kwargs):
            triage_calls.append({"args": args, "kwargs": kwargs})
            return real_triage(*args, **kwargs)

        with mock.patch("zaxbygraph.evaluate.prepare_snapshot", _prepare_spy), \
                mock.patch("zaxbygraph.evaluate.derive_triage", _triage_spy):
            evaluate.run_eval(self.conn, REPO, "triage")

        fix_created = {
            int(row["number"]): row["created_at"]
            for row in self.conn.execute(
                "SELECT number, created_at FROM items"
                " WHERE repo = ? AND number IN (21, 22)", (REPO,)
            )
        }
        created_to_pr = {stamp: pr for pr, stamp in fix_created.items()}
        self.assertEqual(
            len(prepare_calls), 2,
            f"eval triage must prepare ONE snapshot per merged fix PR that"
            f" closed at least one issue (two here: PR 21 closing 11/12 and"
            f" PR 22 closing JUST 14 - triage admits merged single-issue"
            f" closers, unlike bundle's multi-issue-only cases); prepared"
            f" {len(prepare_calls)} snapshots:"
            f" {[c['kwargs'] for c in prepare_calls]}",
        )
        for call in prepare_calls:
            prepare_kwargs = call["kwargs"]
            stamp = prepare_kwargs.get("as_of")
            self.assertIn(
                stamp, created_to_pr,
                f"the triage snapshot's as_of must be a fix PR's created_at"
                f" ({sorted(fix_created.values())}); got {stamp!r}",
            )
            pr = created_to_pr[stamp]
            self.assertEqual(
                prepare_kwargs.get("exclude_pr"), pr,
                f"the triage snapshot at {stamp} must exclude its own fix"
                f" PR {pr} (the same trivial-leak control as bundle mode);"
                f" got {prepare_kwargs!r}",
            )
        self.assertEqual(
            {c["kwargs"].get("as_of") for c in prepare_calls},
            set(fix_created.values()),
            "one prepared snapshot per fix PR, at each PR's created_at"
            " (the two-issue closer AND the single-issue closer)",
        )
        self.assertEqual(
            len(triage_calls), len(prepare_calls),
            f"the production triage driver runs once per prepared snapshot;"
            f" got {len(triage_calls)} triage calls for"
            f" {len(prepare_calls)} snapshots",
        )
        for call in triage_calls:
            self.assertNotIn(
                "as_of", call["kwargs"],
                "production triage must NEVER receive as_of on the prepared"
                " connection - the prepared view owns the snapshot",
            )
        for call, prepare in zip(triage_calls, prepare_calls):
            self.assertIs(
                # call is the spy's recorded {"args": ..., "kwargs": ...}
                # dict (C1's equivalent leg reads .args off mock call
                # objects; this hand-rolled spy stores a dict, so the
                # subscript is the intended access — corrected by the
                # implementer, reported to the tracer).
                call["args"][0], prepare["returned"],
                "each triage call must receive the connection its"
                " prepare_snapshot returned (one shared view per case)",
            )

        # --- ground-truth provenance labels (amendment wave 6): the
        # sufficiency threshold is MODE-SPECIFIC. Triage's per-case
        # threshold is 1 - a case whose GitHub-reported closes stream
        # alone covers it is 'closes'-provenanced - where bundle's label
        # needs gh-alone >= 2 per case (its cases need >= 2 issues
        # anyway). This triage store's truth is ALL GitHub-reported (PR
        # 21 closes 11/12, PR 22 closes JUST 14; every edge
        # source='closing_ref', no keyword edges anywhere), so BOTH cases
        # - PR 21's two-edge case and PR 22's single-edge case, the one
        # that only clears the bar at threshold 1 - are
        # 'closes'-provenanced with no keyword stream needed, and the
        # TOP-LEVEL ground_truth label is 'github', NOT 'mixed' (the
        # top-level vocabulary is 'github'/'keyword'/'mixed'; the
        # PER-CASE provenance field stays 'closes'/'keyword' - C17 pins
        # 'mixed' on the mixed corpus, C2 pins 'keyword' on the
        # all-keyword store, C1 pins 'github' on the all-GitHub BUNDLE
        # store with its >= 2 threshold).
        self.assertEqual(
            result.get("ground_truth"), "github",
            f"an all-GitHub triage store (closing_ref edges only, gh"
            f" closes >= 1 per case under the triage sufficiency"
            f" threshold) must label its top-level ground_truth 'github',"
            f" not 'mixed'; got {result.get('ground_truth')!r}",
        )

        # --- per-case leg (conditional, amendment wave 6): if the triage
        # payload exposes per-case entries (C1/C17's per_fix_pr surface
        # or an equivalent), PR 22's entry - the single-issue case that
        # only clears the sufficiency bar at the MODE-SPECIFIC triage
        # threshold (1) - must carry per-case provenance 'closes': an
        # implementation that reuses bundle's >= 2 threshold here would
        # mislabel exactly this case.
        per_case: list[dict] | None = None
        for key in ("per_fix_pr", "per_case", "cases"):
            value = result.get(key)
            if isinstance(value, list) and value and all(
                isinstance(e, dict) for e in value
            ):
                per_case = value
                break
        if per_case is not None:
            by_pr: dict[int, dict] = {}
            for entry in per_case:
                pr_id = entry.get(
                    "pr", entry.get("pr_number", entry.get("fix_pr"))
                )
                if isinstance(pr_id, int) and not isinstance(pr_id, bool):
                    by_pr[pr_id] = entry
            if by_pr:
                self.assertIn(
                    22, by_pr,
                    f"the triage payload's per-case entries must name PR"
                    f" 22 (the single-issue closer that opens a triage"
                    f" case under the >= 1 eligibility); got {per_case!r}",
                )
                prov = by_pr[22].get(
                    "ground_truth", by_pr[22].get("provenance")
                )
                self.assertEqual(
                    prov, "closes",
                    f"PR 22's per-case provenance must be 'closes' (its"
                    f" #14 closure is GitHub-reported via closing_ref and"
                    f" the triage sufficiency threshold is 1 gh closes"
                    f" edge per case - bundle's >= 2 threshold must not"
                    f" leak into triage labelling); got {prov!r} in"
                    f" {by_pr[22]!r}",
                )


# ==== C5: localize ties break by evidence, not by path name ==================


class LocalizeTieBreakTests(_StoreTestCase):
    """AC5 (#68), DISCRIMINATING. RED at base: the base sort key is
    (-score, path), so an equal-score tie is broken alphabetically.

    Fixture geometry (the tie is deliberate, the support differs):
      - PR 1 merged 01-02: [src/tgt.py, src/aaa.txt]
      - PR 2 merged 01-03: [src/tgt.py, src/bbb.txt]
      - PR 3 merged 01-04: [src/tgt.py, src/bbb.txt]
      - issue 300 created 01-05, body refs src/tgt.py (resolves: src/tgt.py
        is in the merged vocabulary).

    Hand-derived pair arithmetic inside localize_files (verified at base):
    file_support = {tgt: 3, aaa: 1, bbb: 2}; pair_support = {(aaa, tgt): 1,
    (bbb, tgt): 2}. Both co-visible candidates offer their files at pair
    confidence 1.0 (aaa: 1/1; bbb: 2/2), so src/aaa.txt and src/bbb.txt tie
    at score 1.0 while their SUPPORT differs (1 vs 2). The base order is
    alphabetical (aaa first); the fix must rank by the file's own evidence
    - (score, then support, then path) - putting src/bbb.txt (support 2)
    ahead of src/aaa.txt (support 1).
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        src.add_pr(
            issue(1, title="a", body="", kind="pr", created_at="2027-01-01T00:00:00Z"),
            _merged_pull(1, "2027-01-02T00:00:00Z"),
            files=[pr_file("src/tgt.py"), pr_file("src/aaa.txt")],
        )
        src.add_pr(
            issue(2, title="b", body="", kind="pr", created_at="2027-01-01T00:00:00Z"),
            _merged_pull(2, "2027-01-03T00:00:00Z"),
            files=[pr_file("src/tgt.py"), pr_file("src/bbb.txt")],
        )
        src.add_pr(
            issue(3, title="c", body="", kind="pr", created_at="2027-01-01T00:00:00Z"),
            _merged_pull(3, "2027-01-04T00:00:00Z"),
            files=[pr_file("src/tgt.py"), pr_file("src/bbb.txt")],
        )
        src.add_issue(issue(
            300, title="target fault", body="fault at src/tgt.py:1:1",
            created_at="2027-01-05T00:00:00Z",
        ))
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)

    def test_localize_ranking_is_not_alphabetical(self) -> None:
        """RED at base, GREEN after the fix. src/bbb.txt (pair support 2)
        must rank ABOVE src/aaa.txt (pair support 1) despite the equal 1.0
        confidence and the alphabetical advantage of "aaa"."""
        out = derive.localize_files(self.conn, REPO, 300)
        paths = [f["path"] for f in out["files"]]
        self.assertIn("src/aaa.txt", paths)
        self.assertIn("src/bbb.txt", paths)
        self.assertLess(
            paths.index("src/bbb.txt"),
            paths.index("src/aaa.txt"),
            f"equal-confidence ties must break by evidence strength (support"
            f" 2 before support 1), not by path name; got {paths}",
        )


# ==== C6: zero ground truth is a coded error =================================


class NoGroundTruthErrorTests(_StoreTestCase):
    """AC6 (#68), DISCRIMINATING. RED at base: run_eval returns a payload
    with ok metrics-null and a ``no_ground_truth`` LABEL; nothing raises
    and the CLI exits 0.

    Store: two open issues and one merged PR with NO closes and NO
    closes_keyword edges anywhere (the PR body carries no keywords).

    AUTHOR-PINNED CONTRACT (the fix must match): ``evaluate.run_eval``
    RAISES a ``ValueError`` subclass whose instance carries
    ``code == "no_ground_truth"`` and a non-empty message naming what the
    store lacks; the CLI layer translates the raise into a non-zero exit
    (the CLI translation is implementation surface, pinned here only at
    the library boundary).
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        src.add_issue(issue(
            1, title="lone issue one", body="plain",
            created_at="2027-01-03T00:00:00Z",
        ))
        src.add_issue(issue(
            2, title="lone issue two", body="plain",
            created_at="2027-01-03T01:00:00Z",
        ))
        src.add_pr(
            issue(10, title="unrelated change", body="no keywords here",
                  kind="pr", created_at="2027-01-01T00:00:00Z"),
            _merged_pull(10, "2027-01-02T00:00:00Z"),
            files=[pr_file("src/other.py")],
        )
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)

    def test_no_ground_truth_is_an_error(self) -> None:
        """RED at base, GREEN after the fix. A store with no closes and no
        closes_keyword-from-merged-PR edges makes run_eval 'bundle' raise
        the coded no_ground_truth error."""
        with self.assertRaises(ValueError) as caught:
            evaluate.run_eval(self.conn, REPO, "bundle")
        exc = caught.exception
        self.assertEqual(
            getattr(exc, "code", None),
            "no_ground_truth",
            f"the raised error must carry code='no_ground_truth', got"
            f" {exc!r}",
        )
        self.assertTrue(
            str(exc).strip(),
            "the no_ground_truth error must name what the store lacks",
        )


# ==== C7: triage/bundle answer from an as-of view ============================


class AsOfViewTests(_StoreTestCase):
    """AC7 (#68), NEW-SURFACE. RED at base: triage() and bundle() accept no
    ``as_of`` keyword (TypeError: unexpected keyword argument).

    Vocabulary (amendment wave 3 hub dilution): PR 10 merged 2027-05-29:
    [src/feature.py] (pre-T1), PLUS two independent pre-T merged PRs 11/12
    (merged 05-30/05-31) touching only unrelated files that no issue
    references. At the wave-2 geometry the fixture's ONLY merged PR left
    src/feature.py at hub share 1.0, and the post-fix anchor rules (C11:
    anchors need non-hub evidence) would have made the required
    (1001, 1002) co-bundle ILLEGAL; with the fillers the shared path sits
    at 1/3 < 0.5 in both the as-of and the full view, and the fillers
    anchor nothing (nothing references their files).

    Two waves around T1 = 2027-06-03T00:00:00Z:
      - wave 1 (created 2027-06-01): issues 1001/1002/1003 (author alice).
        1001 body refs src/feature.py; 1002 body refs src/feature.py with
        shared crash/submit vocabulary. 1001 carries ONE comment by bob
        created 2027-06-04 (AFTER T1).
      - wave 2 (created 2027-06-10, after T1): issue 1004, which has a
        timeline mentions edge 1004 -> 1001.

    Amendment wave 2 - a SECOND sync pass mutates the world AFTER T1 (all
    through the real incremental sync path; verified at base):
      - 1002's body is EDITED (pre-T "crash in `src/feature.py` on submit
        too" -> post-T "reorganize the widget inventory ledger", updated_at
        2027-06-05): ingest writes a genuine revisions pre-image
        (entity='item_body', content=the pre-T body, valid_to=2027-06-05 >
        T1) and the stored items.body flips to the new text.
      - a post-T comment (cid 88002, carol, 2027-06-05) on 1002 saying
        "see #1003" makes the real ingest derive a keyword mentions edge
        1002 -> 1003 whose evidence names that comment ('comment:88002@4')
        - a post-T mention edge between two PRE-T items.
      - 1003 gains a label ('high'), an assignee (erin) and a custom field
        value (priority=urgent) in the same post-T re-ingest.

    As-of T1 (author-pinned observable features):
      - rows contain 1001/1002/1003 and NOT 1004 (later items removed);
      - 1001's participants == 1 (the post-T1 comment is invisible; the
        full view counts alice+bob == 2);
      - 1001's inbound_mentions == 0 (the edge's source item was created
        after T1, so the post-T1 edge cannot affect the as-of answer; the
        full view counts 1);
      - 1003's inbound_mentions == 0 (the comment-backed post-T edge
        between two PRE-T items is invisible; the full view counts 1);
      - 1002's participants == 1 (carol's post-T comment is invisible) and
        its resolved_paths == ['src/feature.py'] - text features derive
        from the PRE-T body (the revision pre-image whose window covers
        T1), while the full view sees the new body and resolves nothing;
      - 1003's label_weight and field_weight are null - labels and custom
        field values are UNDATED child rows, so an as-of view must suppress
        them wholesale rather than leak possibly-post-T values - and 1003
        is NOT gated 'assigned' (an undated assignee row cannot gate an
        as-of row; the full view gates it);
      - the as-of bundle co-bundles (1001, 1002) with files_direct and
        lexical evidence from the pre-T body; the full bundle does not
        co-bundle them (the edited body lost the shared path and tokens).

    DISCLOSED LIMITATION (determinism of the undated legs): the labels,
    assignees and issue_field_values tables carry no timestamps, so the
    store cannot express "this label was added at time X". The fixture
    expresses post-T insertion the only way the store allows - the rows
    did not exist after the first (pre-T) sync and appear only in the
    second (post-T) re-ingest - and the pinned contract is correspondingly
    suppression-shaped: as-of views must not let undated rows change the
    answer at all. Nothing here can distinguish a label added one second
    before T1 from one added one second after; that resolution is beyond
    the store's recording fidelity and is not asserted.
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        src.add_pr(
            issue(10, title="vocab", body="", kind="pr",
                  created_at="2027-05-28T00:00:00Z"),
            _merged_pull(10, "2027-05-29T00:00:00Z"),
            files=[pr_file("src/feature.py")],
        )
        # Hub dilution (amendment wave 3): two independent pre-T merged PRs
        # over unrelated files no issue references. They anchor nothing and
        # participate in no item's evidence; their only effect is the
        # merged-PR population N = 3, dropping src/feature.py's hub share
        # to 1/3 < 0.5 so the required (1001, 1002) co-bundle is LEGAL
        # under the post-fix non-hub anchor rules (at N = 1 the share was
        # 1.0 and the pair could never anchor).
        for n, (fa, fb, merged) in {
            11: ("src/unrel1.py", "src/unrel2.py", "2027-05-30T00:00:00Z"),
            12: ("src/unrel3.py", "src/unrel4.py", "2027-05-31T00:00:00Z"),
        }.items():
            src.add_pr(
                issue(n, title=f"filler {n}", body="", kind="pr",
                      created_at="2027-05-28T00:00:00Z"),
                _merged_pull(n, merged),
                files=[pr_file(fa), pr_file(fb)],
            )
        src.add_issue(issue(
            1001, title="wave one A",
            body="crash in `src/feature.py` on submit", author="alice",
            created_at="2027-06-01T00:00:00Z",
        ))
        src.add_issue(issue(
            1002, title="wave one C",
            body="crash in `src/feature.py` on submit too", author="alice",
            created_at="2027-06-01T00:00:00Z",
        ))
        src.add_issue(issue(
            1003, title="wave one D", body="plain", author="alice",
            created_at="2027-06-01T00:00:00Z",
        ))
        src.add_issue(issue(
            1004, title="wave two B", body="plain", author="alice",
            created_at="2027-06-10T00:00:00Z",
        ))
        _add_comment(src, 1001, 88001, "post T1 comment", "bob",
                     created_at="2027-06-04T00:00:00Z")
        sync_repo(conn, src, REPO)

        # --- the post-T world (amendment wave 2), via a real incremental
        # re-sync: body edit (writes the revisions pre-image), a post-T
        # comment whose '#1003' mention derives a comment-evidence edge,
        # and undated label/assignee/field rows that first appear now.
        rec2 = src.issues[1002]
        rec2["body"] = "reorganize the widget inventory ledger"
        rec2["updated_at"] = "2027-06-05T00:00:00Z"
        _add_comment(src, 1002, 88002, "see #1003", "carol",
                     created_at="2027-06-05T00:00:00Z")
        rec3 = src.issues[1003]
        rec3["labels"] = [{"name": "high", "color": "ff0000"}]
        rec3["assignees"] = [{"login": "erin"}]
        rec3["issue_field_values"] = [{"name": "priority", "value": "urgent"}]
        rec3["updated_at"] = "2027-06-06T00:00:00Z"
        sync_repo(conn, src, REPO)

        _insert_edge(conn, REPO, 1004, "mentions", 1001, source="timeline")
        _pin_synced_at(conn)

    def test_as_of_views(self) -> None:
        """RED at base, GREEN after the fix. triage/bundle accept as_of and
        answer from the T1 snapshot view; the plain calls keep answering
        from the full store. Amendment wave 2 adds the leakage legs: a
        post-T comment-backed mention edge between two PRE-T items, post-T
        label/assignee/field rows, and a post-T body revision."""
        t1 = "2027-06-03T00:00:00Z"

        # Geometry precondition (documents the hub-dilution fillers): the
        # merged-PR population at T1 is exactly PR 10 plus the two fillers,
        # so the shared path src/feature.py sits at hub share 1/3 < 0.5 -
        # non-hub, and the (1001, 1002) co-bundle required below is LEGAL
        # under the post-fix anchor rules (at the wave-2 geometry the only
        # merged PR put the share at 1.0 and the pair could never anchor).
        merged_at_t1 = self.conn.execute(
            "SELECT COUNT(*) FROM items WHERE repo = ?"
            " AND merged_at IS NOT NULL AND merged_at <= ?",
            (REPO, t1),
        ).fetchone()[0]
        self.assertEqual(
            merged_at_t1, 3,
            f"the C7 store must hold exactly three pre-T merged PRs at T1"
            f" (hub share of src/feature.py = 1/3); got {merged_at_t1}",
        )

        asof = derive.triage(self.conn, REPO, as_of=t1)
        numbers = [r["number"] for r in asof["rows"]]
        self.assertIn(1001, numbers)
        self.assertIn(1002, numbers)
        self.assertNotIn(
            1004, numbers, "items created after the as-of stamp are absent"
        )
        rows = {r["number"]: r for r in asof["rows"]}
        self.assertEqual(
            rows[1001]["scores"]["features_raw"]["participants"], 1,
            "the post-T1 comment must not count in the as-of view",
        )
        self.assertEqual(
            rows[1001]["scores"]["features_raw"]["inbound_mentions"], 0,
            "an edge whose source item postdates the as-of stamp must not"
            " affect the as-of answer",
        )

        # ---- leakage leg (i): post-T mention edge between PRE-T items ---
        self.assertIn(
            1003, numbers,
            "issue 1003 existed at T1 and must stay in the as-of view",
        )
        self.assertEqual(
            rows[1003]["scores"]["features_raw"]["inbound_mentions"], 0,
            "a mentions edge whose evidence names a comment created after"
            " the as-of stamp (1002 -> 1003 via comment 88002) must not"
            " change the as-of answer",
        )

        # ---- leakage leg (ii): undated post-T label/assignee/field rows --
        self.assertEqual(
            rows[1003]["gates"], [],
            f"an undated assignee row (no arrival time in the store) must"
            f" not gate an as-of row; got {rows[1003]['gates']!r}",
        )
        self.assertIsNone(
            rows[1003]["scores"]["features_raw"]["label_weight"],
            "labels carry no timestamps, so as-of views must suppress"
            " label_weight wholesale rather than leak possibly-post-T"
            " labels",
        )
        self.assertIsNone(
            rows[1003]["scores"]["features_raw"]["field_weight"],
            "custom field values carry no timestamps, so as-of views must"
            " suppress field_weight wholesale",
        )

        # ---- leakage leg (iii): post-T body revision ---------------------
        self.assertEqual(
            rows[1002]["scores"]["features_raw"]["participants"], 1,
            "carol's post-T comment on 1002 must not count in the as-of"
            " view",
        )
        self.assertEqual(
            rows[1002]["resolved_paths"], ["src/feature.py"],
            "as-of text features derive from the pre-T body (the revision"
            " pre-image whose valid_from <= T1 < valid_to), which references"
            f" src/feature.py; got {rows[1002]['resolved_paths']!r}",
        )

        asof_bundle = derive.bundle(self.conn, REPO, as_of=t1)
        self.assertNotIn(1004, asof_bundle["pool"])
        asof_pairs = {
            tuple(sorted((p["a"], p["b"])))
            for b in asof_bundle["bundles"]
            for p in b["pairs"]
        }
        self.assertIn(
            (1001, 1002), asof_pairs,
            "the as-of view co-bundles 1001/1002 through the PRE-T body's"
            f" shared path and vocabulary; bundles:"
            f" {[sorted(m['number'] if isinstance(m, dict) else m for m in b['members']) for b in asof_bundle['bundles']]}",
        )
        asof_pair = next(
            p
            for b in asof_bundle["bundles"]
            for p in b["pairs"]
            if sorted((p["a"], p["b"])) == [1001, 1002]
        )
        self.assertGreater(
            asof_pair["components"]["files_direct"]["score"], 0,
            "the as-of pair's file evidence comes from the pre-T body's"
            " shared path reference",
        )
        self.assertGreater(
            asof_pair["components"]["lexical"]["score"], 0,
            "the as-of pair's lexical evidence comes from the pre-T body's"
            " vocabulary (the post-T body shares no tokens)",
        )

        full = derive.triage(self.conn, REPO, include_gated=True)
        full_rows = {r["number"]: r for r in full["rows"]}
        self.assertIn(1004, full_rows)
        self.assertEqual(
            full_rows[1001]["scores"]["features_raw"]["participants"], 2
        )
        self.assertEqual(
            full_rows[1001]["scores"]["features_raw"]["inbound_mentions"], 1
        )
        # the full view reads the CURRENT world: the comment edge counts,
        # the assignee gates, and the edited body resolves nothing.
        self.assertEqual(
            full_rows[1003]["scores"]["features_raw"]["inbound_mentions"], 1
        )
        self.assertIn(
            "assigned", full_rows[1003]["gates"],
            "the full view sees the assignee row and gates 1003",
        )
        self.assertIs(full_rows[1003]["ranked"], False)
        self.assertEqual(
            full_rows[1002]["scores"]["features_raw"]["participants"], 2
        )
        self.assertEqual(
            full_rows[1002]["resolved_paths"], [],
            "the full view reads the EDITED body, which carries no path"
            " reference",
        )
        full_bundle = derive.bundle(self.conn, REPO)
        self.assertIn(1004, full_bundle["pool"])
        full_pairs = {
            tuple(sorted((p["a"], p["b"])))
            for b in full_bundle["bundles"]
            for p in b["pairs"]
        }
        self.assertNotIn(
            (1001, 1002), full_pairs,
            "the full view reads the edited body: the pair lost its shared"
            " path and vocabulary and must not be co-bundled",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
