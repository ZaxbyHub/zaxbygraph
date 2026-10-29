"""Derived read tier tests (issue #29): similar / explain / coupling /
eval, the never-write invariant, and the leakage-controlled eval floors.

Frozen acceptance checks C1-C9 (anchor
issue-tracer-checkpoint-v1 slug=29-derived-read-tier, published on issue
#29) pin one test name per AC; every other test here is edge or guardrail
coverage the Full-Resolution Contract requires.

SYNTHETIC FLOOR DERIVATION (hand-computed from the fixture geometry in
tests/fixtures_eval.py BEFORE the floors were asserted — every number below
is derived, not measured):

- Duplicate ground truth: edges 102->101. Eval target 101: its partner 102
  was created one hour LATER (t(4,1) > t(4)), so the AC6 candidate cutoff
  correctly hides it and the target is not measurable (skipped, not a
  zero). Eval target 102: partner 101 (created t(4) <= t(4,1)) is visible
  alongside the pre-cutoff PRs 10 and 11 (both share src/feature.py, both
  created t(0)). 101 shares the planted anchor (resolved src/feature.py)
  plus the identical title/body, scoring 5.75 vs the PRs' path-only
  scores, so 101 ranks FIRST among visible candidates.
  => recall@5 = 1/1 = 1.0, MRR = 1/1 = 1.0, eligible = 1 of 2 targets.
- Localize: closing PR 120 (files src/feature.py, src/fix_patch.py) is
  excluded per target; src/feature.py stays reachable through PRs 10/11
  (pre-cutoff, non-closing), src/fix_patch.py does not — reachable truth
  is non-empty for both targets 101 and 105, and each target's own
  resolved ref scores 1.0, so src/feature.py ranks first.
  => Acc@5 = 2/2 = 1.0.
- Bundle: truth pair {101, 105} (PR 120 closes both). Per-target cutoffs:
  from 101 nothing is visible (both 102 and 105 are younger), from 105
  both 101 and 102 are visible, so proposed = {{101,105}, {102,105}}.
  => pair_recall = 1/1 = 1.0, pair_precision = 1/2 = 0.5.

REAL-CORPUS EMPIRICAL GOALS (regression pins, NOT hand-derived oracles —
the recording in tests/eval_fixture/ is replayed and the pinned values are
what the current harness measures on it; no random-baseline comparison is
claimed; re-measured after the pr43 round-2 metric fixes — untruncated
candidate pools, visible-partner recall denominators, scoreable-pair bundle
denominators — and again after the round-3 cochange symmetry fix, whose
honest min-confidence scoring moved this corpus's bundle numbers from
recall 1.0/precision 0.105 to 0.5/0.053): localize Acc@5 = 1.0 (9 of 15
targets eligible), bundle pair_recall >= 0.5, pair_precision >= 0.05.
The real corpus has zero duplicate_of edges, so the similar mode reports
zero eligible targets there by construction; duplicate detection floors
live on the synthetic store only.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, TempDBTest, scrubbed_env
from fixtures_eval import build_eval_store, record_real_corpus

from zaxbygraph.db import connect, init_schema, open_existing
from zaxbygraph.derive import (
    ConfigError,
    coupling,
    explain,
    localize_files,
    load_weights,
    origin_timestamp,
    similar,
)
from zaxbygraph.evaluate import run_eval, _visible_candidates

_EVAL_FIXTURE_DIR = Path(__file__).resolve().parent / "eval_fixture"
#: corpus_sha256 of the REPLAYED real-corpus store (default include set).
#: Re-pinned after the marker-scrub normalization of the recording (see
#: tests/fixtures_eval.py `_MARKER_SCRUBS`); eval metrics unchanged.
REPLAYED_CORPUS_SHA256 = (
    "71a15ac9be1ffe6ffee9318f44b39b00def59e7f57c97588ce5e6dc2ae7ebbaf"
)

_REAL_STORE: Path | None = None
_REAL_TEMPDIR: tempfile.TemporaryDirectory | None = None


def _real_store_path() -> Path:
    """Replay the committed real-corpus recording once per test run."""
    global _REAL_STORE, _REAL_TEMPDIR
    if _REAL_STORE is None:
        from replay import JsonlReplaySource
        from zaxbygraph.sync import sync_repo

        _REAL_TEMPDIR = tempfile.TemporaryDirectory()
        conn = connect(Path(_REAL_TEMPDIR.name) / "real.db")
        init_schema(conn)
        try:
            sync_repo(conn, JsonlReplaySource(_EVAL_FIXTURE_DIR / "events.jsonl"), "zaxbyhub/zaxbygraph")
        finally:
            conn.close()
        _REAL_STORE = Path(_REAL_TEMPDIR.name) / "real.db"
    return _REAL_STORE


class DerivedTierTest(unittest.TestCase):
    """Shared synthetic store, opened read-only per test."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._td = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._td.name) / "eval.db"
        conn = connect(cls.db_path)
        try:
            init_schema(conn)
            build_eval_store(conn)
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._td.cleanup()

    def setUp(self) -> None:
        self.conn = open_existing(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()


class SimilarCommandTests(DerivedTierTest):
    def test_similar_ranks_planted_partner(self) -> None:
        """AC1: the planted near-duplicate ranks first with full evidence."""
        out = similar(self.conn, REPO, 101, limit=10)
        self.assertTrue(out["derived"])
        self.assertNotIn(101, [c["number"] for c in out["candidates"]])
        first = out["candidates"][0]
        self.assertEqual(first["number"], 102)
        self.assertIsNotNone(first["anchor"])
        self.assertIn(first["anchor"], {"signature", "files_direct", "structural", "cochange"})
        for candidate in out["candidates"]:
            self.assertIn("components", candidate)
            for name, comp in candidate["components"].items():
                self.assertIn("score", comp)
                self.assertIn("evidence", comp)
        self.assertIn("weights", out)
        # the planted partner carries the shared resolved path as evidence
        shared = first["components"]["files_direct"]["evidence"]["shared_paths"]
        self.assertIn("src/feature.py", shared)
        self.assertTrue(out["weights"])

    def test_affinity_is_symmetric(self) -> None:
        """AC2: affinity(A,B) == affinity(B,A), including no-hit pairs."""
        for a, b in ((101, 102), (101, 104), (102, 105), (103, 199)):
            e1 = explain(self.conn, REPO, a, b)
            e2 = explain(self.conn, REPO, b, a)
            self.assertEqual(e1["total_score"], e2["total_score"], (a, b))
            for name, comp in e1["components"].items():
                self.assertEqual(comp["score"], e2["components"][name]["score"], (a, b, name))

    def test_lexical_only_pairs_have_no_edge(self) -> None:
        """AC3: vocabulary-only and label-only decoys never pair."""
        out = similar(self.conn, REPO, 101, limit=50)
        numbers = [c["number"] for c in out["candidates"]]
        self.assertNotIn(103, numbers, "vocabulary-only decoy must not pair")
        self.assertNotIn(104, numbers, "label-only decoy must not pair")
        # 106 pairs (its keyword mention edge is GitHub-reported adjacency,
        # a structural anchor), but only through that anchor: its lexical
        # overlap with 101 is near zero, so the pair is anchor-created.
        # and the label-sharing decoy shares 'bug' with 101 — prove the label
        # alone was the ONLY overlap by checking 104's own components carry a
        # context score while no anchor fired for it anywhere.
        for candidate in out["candidates"]:
            fired = [
                name
                for name in ("signature", "files_direct", "structural", "cochange")
                if candidate["components"][name]["score"] > 0
            ]
            self.assertTrue(fired, candidate)

    def test_explain_matches_similar(self) -> None:
        """AC4: explain returns the exact breakdown similar used."""
        sim = similar(self.conn, REPO, 101, limit=50)
        entry = next(c for c in sim["candidates"] if c["number"] == 102)
        exp = explain(self.conn, REPO, 101, 102)
        self.assertEqual(exp["total_score"], entry["total_score"])
        for name, comp in entry["components"].items():
            self.assertEqual(exp["components"][name]["score"], comp["score"])
            self.assertEqual(exp["components"][name]["evidence"], comp["evidence"])
        self.assertEqual(exp["anchor"], entry["anchor"])
        # parity under --ls-files: both commands filter identically
        sim_ls = similar(self.conn, REPO, 101, limit=50, ls_files=["src/feature.py"])
        entry_ls = next(c for c in sim_ls["candidates"] if c["number"] == 102)
        exp_ls = explain(self.conn, REPO, 101, 102, ls_files=["src/feature.py"])
        self.assertEqual(exp_ls["total_score"], entry_ls["total_score"])
        self.assertEqual(
            exp_ls["components"]["files_direct"]["evidence"],
            entry_ls["components"]["files_direct"]["evidence"],
        )

    def test_explain_identity_pair(self) -> None:
        exp = explain(self.conn, REPO, 101, 101)
        self.assertEqual(exp["total_score"], 0.0)
        self.assertTrue(exp["derived"])

    def test_similar_unknown_item_is_coded(self) -> None:
        with self.assertRaises(LookupError):
            similar(self.conn, REPO, 99999)


class CouplingTests(DerivedTierTest):
    def test_coupling_support_lift(self) -> None:
        """AC5: support/confidence/lift over merged PRs touching 2-50 files.

        Hand-derived: population = {PR 10, PR 11, PR 120} (PR 12 has one
        file... no: PR 12 touches src/hub.py only => 1 file, excluded; PRs
        141 (1 file) and 142 (51 files) excluded). support(src/feature.py,
        src/hub.py) = 1 (PR 10); support(src/feature.py) = 3; confidence =
        1/3; lift = 3*1/(3*1) = 1.0."""
        out = coupling(self.conn, REPO, "src/feature.py", limit=20)
        self.assertTrue(out["derived"])
        self.assertEqual(out["population_prs"], 3)
        by_path = {r["path"]: r for r in out["results"]}
        self.assertEqual(by_path["src/hub.py"]["support"], 1)
        self.assertAlmostEqual(by_path["src/hub.py"]["confidence"], 1 / 3, places=6)
        self.assertAlmostEqual(by_path["src/hub.py"]["lift"], 1.0, places=6)
        self.assertEqual(by_path["src/feature_test.py"]["support"], 1)
        self.assertEqual(by_path["src/fix_patch.py"]["support"], 1)
        # unmerged PRs cannot exist here, but single-file and 51-file PRs
        # are outside the population by construction: their files never
        # appear as results.
        self.assertNotIn("src/solo.py", by_path)
        self.assertNotIn("src/bulk/file_00.py", by_path)
        self.assertNotIn("docs/leak.md", by_path)
        # unmerged PRs never count (mutation probe M5's target)
        self.assertNotIn("src/open_work.py", by_path)
        self.assertEqual(out["population_prs"], 3)

    def test_coupling_zero_support_answers_with_reason(self) -> None:
        # src/solo.py exists in a MERGED pr (PR 141, one file — outside the
        # 2-50 population): exists-but-excluded, not unknown.
        excluded = coupling(self.conn, REPO, "src/solo.py")
        self.assertEqual(excluded["results"], [])
        self.assertEqual(excluded["reason"], "excluded_from_population")
        # 51-file PR 142's files are the same class.
        bulk = coupling(self.conn, REPO, "src/bulk/file_00.py")
        self.assertEqual(bulk["reason"], "excluded_from_population")
        # a path no merged PR ever touched is genuinely unknown
        nothing = coupling(self.conn, REPO, "no/such/path.py")
        self.assertEqual(nothing["reason"], "unknown_path")
        # an unmerged PR's path is unknown too (never merged)
        open_pr = coupling(self.conn, REPO, "src/open_work.py")
        self.assertEqual(open_pr["reason"], "unknown_path")

    def test_coupling_empty_population(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "empty.db")
            try:
                init_schema(conn)
                out = coupling(conn, REPO, "src/x.py")
                self.assertEqual(out["results"], [])
                self.assertEqual(out["reason"], "no_merged_prs")
            finally:
                conn.close()


class LocalizeTests(DerivedTierTest):
    def test_localize_ranks_own_ref_first(self) -> None:
        out = localize_files(self.conn, REPO, 101, cutoff="2026-06-05T00:00:00Z")
        self.assertEqual(out["files"][0]["path"], "src/feature.py")
        self.assertEqual(out["files"][0]["score"], 1.0)

    def test_localize_comment_cutoff_excludes_late_refs(self) -> None:
        # docs/leak.md reaches the target through a comment written AFTER
        # the fix PR's creation: with the comment cutoff it is gone from the
        # target's evidence, without the cutoff it would rank at 1.0 (the
        # far-future item cutoff keeps PR 130's vocabulary in the pool so
        # the ref resolves in both legs).
        with_cut = localize_files(
            self.conn,
            REPO,
            101,
            cutoff="2027-01-01T00:00:00Z",
            comment_cutoff="2026-06-07T00:00:00Z",
        )
        without_cut = localize_files(
            self.conn, REPO, 101, cutoff="2027-01-01T00:00:00Z"
        )
        self.assertNotIn(
            "docs/leak.md", [f["path"] for f in with_cut["files"]]
        )
        self.assertIn("docs/leak.md", [f["path"] for f in without_cut["files"]])

    def test_localize_excluded_prs(self) -> None:
        out = localize_files(
            self.conn,
            REPO,
            105,
            cutoff="2026-06-05T00:00:00Z",
            excluded_prs={120},
        )
        paths = [f["path"] for f in out["files"]]
        self.assertIn("src/feature.py", paths)
        self.assertNotIn("src/fix_patch.py", paths)


class OriginJoinTests(unittest.TestCase):
    def test_origin_timestamp_joins_per_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "origins.db")
            try:
                init_schema(conn)
                conn.execute(
                    "INSERT INTO items (id, repo, number, kind, title, body,"
                    " state, author, created_at, updated_at, raw_json)"
                    " VALUES (10101, ?, 101, 'issue', 't', '', 'open', 'a',"
                    " '2026-06-01T00:00:00Z', '2026-06-01T00:00:00Z', '{}')",
                    (REPO,),
                )
                conn.execute(
                    "INSERT INTO comments (pk, github_id, repo, number, kind, author,"
                    " created_at, updated_at, body, raw_json) VALUES"
                    " (9001, 555, ?, 101, 'issue_comment', 'alice',"
                    " '2026-06-05T00:00:00Z', '2026-06-05T00:00:00Z', 'x', '{}')",
                    (REPO,),
                )
                conn.execute(
                    "INSERT INTO comments (pk, github_id, repo, number, kind, author,"
                    " created_at, updated_at, body, raw_json) VALUES"
                    " (9002, 556, ?, 101, 'review_comment', 'bob',"
                    " '2026-06-06T00:00:00Z', '2026-06-06T00:00:00Z', 'x', '{}')",
                    (REPO,),
                )
                conn.execute(
                    "INSERT INTO reviews (id, repo, number, author, state,"
                    " submitted_at, body, raw_json) VALUES"
                    " (777, ?, 101, 'bob', 'APPROVED',"
                    " '2026-06-07T00:00:00Z', 'x', '{}')",
                    (REPO,),
                )
                ts, ok = origin_timestamp(conn, REPO, 101, "issue_comment:555")
                self.assertTrue(ok)
                self.assertEqual(ts, "2026-06-05T00:00:00Z")
                # review comments are COMMENTS rows, never reviews rows (N6)
                ts, ok = origin_timestamp(conn, REPO, 101, "review_comment:556")
                self.assertTrue(ok)
                self.assertEqual(ts, "2026-06-06T00:00:00Z")
                ts, ok = origin_timestamp(conn, REPO, 101, "review:777")
                self.assertTrue(ok)
                self.assertEqual(ts, "2026-06-07T00:00:00Z")
                ts, ok = origin_timestamp(conn, REPO, 101, "body")
                self.assertTrue(ok)
                self.assertIsNone(ts)
                # a missing source row suppresses conservatively
                ts, ok = origin_timestamp(conn, REPO, 101, "issue_comment:999999")
                self.assertFalse(ok)
                self.assertIsNone(ts)
            finally:
                conn.close()


class ConfigTests(DerivedTierTest):
    def test_weights_precedence_and_merge(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store.toml"
            cwd = Path(td) / "cwd.toml"
            store.write_text("[derive]\ntitle = 9.0\n", encoding="utf-8")
            cwd.write_text("[derive]\ntitle = 3.0\nlexical = 0.5\n", encoding="utf-8")
            weights = load_weights(str(store), str(cwd))
            self.assertEqual(weights["title"], 3.0, "cwd wins over store")
            self.assertEqual(weights["lexical"], 0.5)
            self.assertEqual(weights["signature"], load_weights()["signature"])
            explicit = Path(td) / "explicit.toml"
            explicit.write_text("[derive]\ntitle = 1.0\n", encoding="utf-8")
            weights = load_weights(str(store), str(cwd), str(explicit))
            self.assertEqual(weights["title"], 1.0)

    def test_config_errors_are_coded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.toml"
            bad.write_text("[derive\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_weights(explicit_config=str(bad))
            unknown = Path(td) / "unknown.toml"
            unknown.write_text("[derive]\nnope = 1\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_weights(explicit_config=str(unknown))
            negative = Path(td) / "negative.toml"
            negative.write_text("[derive]\nlexical = -1\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_weights(explicit_config=str(negative))
            hub = Path(td) / "hub.toml"
            hub.write_text("[derive]\nhub_share = 2\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_weights(explicit_config=str(hub))

    def test_weight_magnitude_bounds_are_coded(self) -> None:
        """PRR-012/013: a TOML integer too big for float raises ConfigError
        (not a raw OverflowError), and an over-magnitude weight cannot turn
        a score into JSON `Infinity`."""
        with tempfile.TemporaryDirectory() as td:
            huge = Path(td) / "huge.toml"
            huge.write_text(f"[derive]\nlexical = {10 ** 400}\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_weights(explicit_config=str(huge))
            over = Path(td) / "over.toml"
            over.write_text("[derive]\nlexical = 1e308\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_weights(explicit_config=str(over))


class EvalTests(DerivedTierTest):
    def test_eval_leakage_controls(self) -> None:
        """AC6: every control is isolatable — disabling exactly one exposes
        exactly its bait."""
        # (a) candidate cutoff: 199 (created after target 101) is invisible
        # under the control and visible without it.
        out = similar(self.conn, REPO, 101, limit=50)
        raw = out["candidates"]
        self.assertIn(199, [c["number"] for c in raw])
        visible, _unknown = _visible_candidates(out, "2026-06-05T00:00:00Z")
        self.assertNotIn(199, [c["number"] for c in visible])
        # an unparseable created_at flags leakage instead of counting
        tampered = [dict(c, created_at=None) if c["number"] == 105 else c for c in raw]
        out_t = dict(out, candidates=tampered)
        visible_t, unknown_t = _visible_candidates(out_t, "2026-06-05T00:00:00Z")
        self.assertTrue(unknown_t)
        self.assertNotIn(105, [c["number"] for c in visible_t])
        # (b) closing-PR exclusion, single knob: the item cutoff is held at
        # a far-future value (PR 120 inside the pool both ways) and ONLY the
        # exclusion toggles — deleting excluded_prs must fail this leg.
        cut = localize_files(
            self.conn, REPO, 105, cutoff="2027-01-01T00:00:00Z", excluded_prs={120}
        )
        self.assertNotIn("src/fix_patch.py", [f["path"] for f in cut["files"]])
        leaky = localize_files(self.conn, REPO, 105, cutoff="2027-01-01T00:00:00Z")
        self.assertIn("src/fix_patch.py", [f["path"] for f in leaky["files"]])
        # (c) adjacency exclusion: with every anchor but structural
        # suppressed, issue 103 pairs with 101 ONLY through the common
        # neighbor the duplicate_of edge creates (103 mentions 102, so both
        # see 102 as adjacent). Removing the (101,102) adjacency removes
        # 103's only anchor — the edge was load-bearing. The inserted edge
        # is deleted in the finally block: leaving it behind would make
        # every later test on this shared store see mutated geometry
        # (pr43 review PRR-018).
        wconn = connect(self.db_path)
        try:
            wconn.execute(
                "INSERT OR IGNORE INTO edges (repo, src_type, src_id, rel,"
                " dst_type, dst_id, source, evidence, confidence)"
                " VALUES (?, 'item', '103', 'mentions', 'item', '102',"
                " 'timeline', 'fixture', 'EXTRACTED')",
                (REPO,),
            )
            wconn.commit()
            suppressed = {"signature", "files_direct", "cochange"}
            with_edge = similar(
                self.conn, REPO, 101, limit=50, suppress_signals=suppressed
            )
            self.assertIn(103, [c["number"] for c in with_edge["candidates"]])
            without_edge = similar(
                self.conn,
                REPO,
                101,
                limit=50,
                suppress_signals=suppressed,
                exclude_adjacency={frozenset((101, 102))},
            )
            self.assertNotIn(103, [c["number"] for c in without_edge["candidates"]])
        finally:
            wconn.execute(
                "DELETE FROM edges WHERE repo = ? AND src_type = 'item'"
                " AND src_id = '103' AND rel = 'mentions' AND dst_type = 'item'"
                " AND dst_id = '102' AND source = 'timeline'",
                (REPO,),
            )
            wconn.commit()
            wconn.close()
        # (d) keyword-source filtering has its own bait: issue 106 mentions
        # 102 via a keyword-sourced edge, so 101 pairs with 106 through the
        # common neighbor 102 — and loses it when keyword edges are
        # filtered. Filtering is also conservative (never adds).
        unfiltered = similar(self.conn, REPO, 101, limit=50)
        self.assertIn(106, [c["number"] for c in unfiltered["candidates"]])
        filtered = similar(
            self.conn,
            REPO,
            101,
            limit=50,
            filtered_sources={"keyword"},
        )
        self.assertNotIn(106, [c["number"] for c in filtered["candidates"]])
        self.assertLessEqual(
            {c["number"] for c in filtered["candidates"]},
            {c["number"] for c in unfiltered["candidates"]},
        )
        # (e) signature suppression is visible in eval output as a zeroed
        # component (item_signatures has no origin column), and every
        # scored eval target carries the plan's with_leakage disclosure.
        sup = similar(self.conn, REPO, 101, limit=5, suppress_signals={"signature"})
        entry = next(c for c in sup["candidates"] if c["number"] == 102)
        self.assertEqual(entry["components"]["signature"]["score"], 0.0)
        self.assertTrue(entry["components"]["signature"]["evidence"]["suppressed"])
        result = run_eval(self.conn, REPO, "similar")
        self.assertTrue(result["with_leakage"])
        for target, reasons in result["with_leakage"].items():
            self.assertIn("signature_origin_unknown", reasons)
        bundle_result = run_eval(self.conn, REPO, "bundle")
        for target, reasons in bundle_result["with_leakage"].items():
            self.assertIn("signature_origin_unknown", reasons)
        # (f) unappliable controls label with_leakage: a duplicate target
        # whose created_at was lost cannot apply the candidate cutoff.
        wconn2 = connect(self.db_path)
        try:
            wconn2.execute("UPDATE items SET created_at = NULL WHERE number = 102")
            wconn2.commit()
        finally:
            wconn2.close()
        result = run_eval(self.conn, REPO, "similar")
        try:
            self.assertIn("102", result["with_leakage"])
            self.assertIn("missing_created_at", result["with_leakage"]["102"])
        finally:
            wconn2 = connect(self.db_path)
            try:
                wconn2.execute(
                    "UPDATE items SET created_at = '2026-06-05T01:00:00Z'"
                    " WHERE number = 102"
                )
                wconn2.commit()
            finally:
                wconn2.close()

    def test_eval_floors_synthetic(self) -> None:
        """AC7 (synthetic half): the hand-derived floors from the module
        docstring hold and the snapshot hash is present and stable."""
        similar_eval = run_eval(self.conn, REPO, "similar")
        self.assertEqual(similar_eval["metrics"]["recall_at_5"], 1.0)
        self.assertEqual(similar_eval["metrics"]["mrr"], 1.0)
        self.assertEqual(similar_eval["metrics"]["eligible_targets"], 1)
        localize_eval = run_eval(self.conn, REPO, "localize")
        self.assertEqual(localize_eval["metrics"]["acc_at_5"], 1.0)
        bundle_eval = run_eval(self.conn, REPO, "bundle")
        self.assertEqual(bundle_eval["metrics"]["pair_recall"], 1.0)
        self.assertEqual(bundle_eval["metrics"]["pair_precision"], 0.5)
        shas = {
            similar_eval["corpus_sha256"],
            localize_eval["corpus_sha256"],
            bundle_eval["corpus_sha256"],
        }
        self.assertEqual(len(shas), 1)
        self.assertTrue(next(iter(shas)))

    def test_eval_floors_real(self) -> None:
        """AC7 (real half): the replayed recording meets the empirical
        regression goals, replays faithfully against the committed fidelity
        manifest, and carries the pinned recomputed corpus hash."""
        conn = open_existing(_real_store_path())
        try:
            recorded = json.loads(
                (_EVAL_FIXTURE_DIR / "fidelity_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            from fixtures_eval import _fingerprint

            actual = _fingerprint(conn, "zaxbyhub/zaxbygraph")
            for key, expected in recorded.items():
                self.assertEqual(
                    actual[key],
                    expected,
                    f"replayed surface {key} drifted from the recording",
                )
            localize_eval = run_eval(conn, "zaxbyhub/zaxbygraph", "localize")
            self.assertGreaterEqual(localize_eval["metrics"]["acc_at_5"], 1.0)
            self.assertEqual(localize_eval["corpus_sha256"], REPLAYED_CORPUS_SHA256)
            bundle_eval = run_eval(conn, "zaxbyhub/zaxbygraph", "bundle")
            self.assertGreaterEqual(bundle_eval["metrics"]["pair_recall"], 0.5)
            # re-measured after the pr43 round-3 cochange symmetry fix:
            # the honest symmetric confidence sup/max(sup_A, sup_B) (= the
            # min of the two directional conditionals) lowers scores on
            # hub-adjacent pairs, which honestly moved this corpus's
            # numbers (recall 1.0 -> 0.5, precision 0.105 -> 0.053); the
            # floors are regression pins, not quality oracles
            self.assertGreaterEqual(
                bundle_eval["metrics"]["pair_precision"], 0.05
            )
            self.assertEqual(bundle_eval["corpus_sha256"], REPLAYED_CORPUS_SHA256)
            # the real corpus carries zero duplicate_of ground truth; the
            # similar mode reports that honestly instead of inventing zeros
            similar_eval = run_eval(conn, "zaxbyhub/zaxbygraph", "similar")
            self.assertEqual(similar_eval["metrics"]["targets_total"], 0)
            self.assertIsNone(similar_eval["metrics"]["recall_at_5"])
        finally:
            conn.close()

    def test_eval_rejects_unknown_mode(self) -> None:
        with self.assertRaises(ValueError):
            run_eval(self.conn, REPO, "nope")

    def test_corpus_sha_parity_with_export(self) -> None:
        """The eval snapshot hash and write_corpus's manifest hash are the
        same bytes on the same store (the additive export refactor's
        parity contract)."""
        import io

        from zaxbygraph.export import (
            corpus_sha256_from_lines,
            iter_corpus_content_lines,
            write_corpus,
        )

        buf = io.BytesIO()
        manifest = write_corpus(
            self.conn, REPO, buf, None, frozenset(), False, str(self.db_path)
        )
        streamed = corpus_sha256_from_lines(
            iter_corpus_content_lines(self.conn, REPO, frozenset())
        )
        self.assertEqual(manifest["corpus_sha256"], streamed)


class DegradationTests(unittest.TestCase):
    """The plan's edge-case bullets, each exercised (final-critic round 1,
    finding 2): pre-v10 eval degradation, capability-slot degradation, and
    a store-level failure answering an envelope instead of a traceback."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._td = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._td.name) / "degrade.db"
        conn = connect(cls.db_path)
        try:
            init_schema(conn)
            build_eval_store(conn)
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._td.cleanup()

    def test_eval_on_pre_v10_store_labels_store_too_old(self) -> None:
        import shutil

        old = Path(self._td.name) / "v9.db"
        shutil.copyfile(self.db_path, old)
        wconn = connect(old)
        try:
            wconn.execute("PRAGMA user_version = 9")
            wconn.commit()
        finally:
            wconn.close()
        ro = open_existing(old)
        try:
            result = run_eval(ro, REPO, "localize")
        finally:
            ro.close()
        self.assertIsNone(result["corpus_sha256"])
        self.assertIn("store_too_old", result["labels"])

    def test_missing_capability_slots_degrade_with_notes(self) -> None:
        import shutil

        old = Path(self._td.name) / "nosig.db"
        shutil.copyfile(self.db_path, old)
        wconn = connect(old)
        try:
            wconn.execute("DROP TABLE item_signatures")
            wconn.commit()
        finally:
            wconn.close()
        ro = open_existing(old)
        try:
            out = similar(ro, REPO, 101, limit=5)
            self.assertIn(
                "item_signatures absent (pre-v9 store): signature signal omitted",
                out["capabilities"],
            )
            # the rest of the tier still answers: 102 is still the top
            # candidate through the resolved shared path
            self.assertEqual(out["candidates"][0]["number"], 102)
        finally:
            ro.close()

    def test_corrupt_store_answers_envelope_not_traceback(self) -> None:
        import io
        import json as _json
        from contextlib import redirect_stderr, redirect_stdout

        from zaxbygraph.cli import main

        junk = Path(self._td.name) / "junk.db"
        junk.write_bytes(b"this is not a sqlite database at all" * 64)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(
                ["similar", "101", "--db", str(junk), "--repo", REPO,
                 "--format", "json"]
            )
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err.getvalue())
        envelope = _json.loads(out.getvalue())
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "store_unreadable")


class NeverWriteTests(DerivedTierTest):
    def _main_sha(self) -> str:
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    def test_derived_tier_never_writes(self) -> None:
        """AC8: the store's main file is byte-identical after every derived
        command, and a write attempt through the read-only handle fails."""
        before = self._main_sha()
        similar(self.conn, REPO, 101, limit=5)
        explain(self.conn, REPO, 101, 102)
        coupling(self.conn, REPO, "src/feature.py")
        localize_files(self.conn, REPO, 101)
        run_eval(self.conn, REPO, "similar")
        run_eval(self.conn, REPO, "localize")
        run_eval(self.conn, REPO, "bundle")
        self.assertEqual(self._main_sha(), before)
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute(
                "INSERT INTO items (id, repo, number, kind, title, body,"
                " state, author, created_at, updated_at)"
                " VALUES (-1, ?, -1, 'issue', 'x', '', 'open', 'a', '', '')",
                (REPO,),
            )

    def test_temp_objects_work_on_the_read_connection(self) -> None:
        """The mechanism channel: temp tables are writable on mode=ro while
        the main database is not."""
        self.conn.execute("CREATE TEMP TABLE scratch(v)")
        self.conn.execute("INSERT INTO scratch VALUES (7)")
        self.assertEqual(
            self.conn.execute("SELECT v FROM scratch").fetchone()[0], 7
        )


class ClampGuardrailTests(DerivedTierTest):
    def test_negative_limits_are_clamped_not_unlimited(self) -> None:
        """AC9's defect class, extended to the new commands: a negative
        limit must never reach SQL as unlimited (SQLite reads negative
        LIMIT as no limit — the seeded store has more anchored candidates
        than the clamp allows)."""
        from zaxbygraph.query import _clamp_limit

        clamped = similar(self.conn, REPO, 101, limit=-1)
        self.assertEqual(len(clamped["candidates"]), _clamp_limit(-1))
        coupled = coupling(self.conn, REPO, "src/feature.py", limit=-1)
        self.assertLessEqual(len(coupled["results"]), _clamp_limit(-1))


class CliContractTests(DerivedTierTest):
    """Machine-envelope legs through the real CLI (subprocess, offline)."""

    def _run(self, *argv: str) -> tuple[int, bytes, bytes]:
        import os
        import subprocess
        from sys import executable

        env = scrubbed_env()
        env["PYTHONPATH"] = os.pathsep.join(
            [
                str(Path(__file__).resolve().parents[1] / "src"),
                str(Path(__file__).resolve().parents[1] / "tests"),
                env.get("PYTHONPATH", ""),
            ]
        )
        proc = subprocess.run(
            [executable, "-m", "zaxbygraph", *argv],
            capture_output=True,
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
            timeout=120,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_similar_envelope_and_not_found(self) -> None:
        rc, out, _err = self._run(
            "similar", "101", "--db", str(self.db_path), "--repo", REPO, "--format", "json"
        )
        self.assertEqual(rc, 0, out)
        envelope = json.loads(out)
        self.assertTrue(envelope["ok"])
        self.assertTrue(envelope["data"]["derived"])
        rc, out, _err = self._run(
            "similar", "99999", "--db", str(self.db_path), "--repo", REPO, "--format", "json"
        )
        self.assertEqual(rc, 1)
        envelope = json.loads(out)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "not_found")

    def test_eval_envelope_carries_sha(self) -> None:
        rc, out, _err = self._run(
            "eval", "similar", "--db", str(self.db_path), "--repo", REPO, "--format", "json"
        )
        self.assertEqual(rc, 0, out)
        envelope = json.loads(out)
        self.assertTrue(envelope["ok"])
        self.assertTrue(envelope["data"]["corpus_sha256"])
        # PRR-021: eval metrics are tied to the [derive] config that
        # produced them via the same weights key similar/explain carry.
        self.assertTrue(envelope["data"]["weights"])

    def test_config_errors_carry_config_error_code(self) -> None:
        """PRR-014: weight-config failures are config_errors, matching the
        documented contract (previously the generic bad_request)."""
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.toml"
            bad.write_text("[derive]\nnope = 1\n", encoding="utf-8")
            rc, out, _err = self._run(
                "similar", "101", "--db", str(self.db_path), "--repo", REPO,
                "--config", str(bad), "--format", "json",
            )
            self.assertEqual(rc, 2, out)
            envelope = json.loads(out)
            self.assertFalse(envelope["ok"])
            self.assertEqual(envelope["error"]["code"], "config_error")

    def test_repo_required_on_unresolved_repo(self) -> None:
        """PRR-016: all four derived commands refuse an unresolved repo with
        the canonical code instead of answering from an empty filter."""
        for argv in (
            ("similar", "101"),
            ("explain", "101", "102"),
            ("coupling", "src/feature.py"),
            ("eval", "similar"),
        ):
            rc, out, _err = self._run(
                *argv, "--db", str(self.db_path), "--repo", "", "--format", "json"
            )
            self.assertEqual(rc, 2, argv)
            envelope = json.loads(out)
            self.assertFalse(envelope["ok"])
            self.assertEqual(envelope["error"]["code"], "repo_required", argv)

    def test_corrupt_store_reports_store_unreadable_on_legacy_reads(self) -> None:
        """PRR-015: the store_unreadable code is forwarded by every read
        runner, not only the derived tier."""
        import io
        import json as _json
        from contextlib import redirect_stderr, redirect_stdout

        from zaxbygraph.cli import main

        junk = Path(self._td.name) / "junk2.db"
        junk.write_bytes(b"still not a database" * 64)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(
                ["search", "crash", "--db", str(junk), "--repo", REPO,
                 "--format", "json"]
            )
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err.getvalue())
        envelope = _json.loads(out.getvalue())
        self.assertEqual(envelope["error"]["code"], "store_unreadable")


class PathResolutionTests(DerivedTierTest):
    """PRR-001/017/020/028: normalization, resolution direction, and the
    ls-files intersection order."""

    def test_norm_path_preserves_dotfiles(self) -> None:
        from zaxbygraph.derive import _norm_path

        self.assertEqual(_norm_path(".github/workflows/ci.yml"), ".github/workflows/ci.yml")
        self.assertEqual(_norm_path(".env"), ".env")
        self.assertEqual(_norm_path("./src/feature.py"), "src/feature.py")
        self.assertEqual(_norm_path("src\\windows\\path.py"), "src/windows/path.py")
        self.assertEqual(_norm_path(""), "")
        # a relative parent prefix survives normalization (it is data, not
        # decoration: lstrip("./") used to rename it into existence)
        self.assertEqual(_norm_path("../outside/x.py"), "../outside/x.py")

    def test_resolve_ref_matches_in_either_direction(self) -> None:
        """PRR-020: the documented bidirectional suffix match, implemented."""
        from pathlib import PurePosixPath

        from zaxbygraph.derive import _resolve_ref

        def suffix_index(vocab: set[str]) -> dict[str, list[str]]:
            by_suffix: dict[str, list[str]] = {}
            for path in vocab:
                parts = PurePosixPath(path).parts
                for i in range(len(parts)):
                    by_suffix.setdefault("/".join(parts[i:]), []).append(path)
            return by_suffix

        vocab = {"src/zaxbygraph/query.py", "docs/guide.md"}
        by_suffix = suffix_index(vocab)
        # forward: ref is a tail of a vocab path
        self.assertEqual(
            _resolve_ref("query.py", vocab, by_suffix), "src/zaxbygraph/query.py"
        )
        # reverse: a vocab path is a tail of the (longer) ref — absolute or
        # checkout-prefixed mentions resolve too
        self.assertEqual(
            _resolve_ref("checkout/src/zaxbygraph/query.py", vocab, by_suffix),
            "src/zaxbygraph/query.py",
        )
        # ambiguous in the reverse direction resolves to nothing: the ref
        # ends with BOTH vocab paths
        ambiguous = {"feature.py", "src/a/feature.py"}
        self.assertIsNone(_resolve_ref("repo/src/a/feature.py", ambiguous, {}))
        # a ref that ends with exactly one vocab path resolves uniquely —
        # even when a sibling path shares the suffix's tail shape
        self.assertEqual(
            _resolve_ref(
                "repo/src/a/feature.py",
                {"src/a/feature.py", "src/b/feature.py"},
                {},
            ),
            "src/a/feature.py",
        )

    def test_similar_explain_carry_resolved_path_count(self) -> None:
        out = similar(self.conn, REPO, 101, limit=5)
        self.assertGreater(out["resolved_path_count"], 0)
        self.assertEqual(
            explain(self.conn, REPO, 101, 102)["resolved_path_count"],
            out["resolved_path_count"],
        )
        self.assertEqual(
            explain(self.conn, REPO, 101, 101)["resolved_path_count"],
            out["resolved_path_count"],
        )

    def test_ls_files_thins_but_never_redirects_resolution(self) -> None:
        """PRR-017: the checkout filter removes paths from scoring; it must
        not change what references resolve to (the intersection is applied
        after resolution)."""
        plain = similar(self.conn, REPO, 101, limit=50)
        kept = similar(
            self.conn,
            REPO,
            101,
            limit=50,
            ls_files=["src/feature.py", "src/fix_patch.py", "docs/leak.md", "docs/other.md"],
        )
        # intersecting with the full vocabulary changes nothing
        self.assertEqual(
            [c["number"] for c in plain["candidates"]],
            [c["number"] for c in kept["candidates"]],
        )
        narrowed = similar(self.conn, REPO, 101, limit=50, ls_files=["docs/leak.md"])
        numbers = {c["number"] for c in narrowed["candidates"]}
        for cand in narrowed["candidates"]:
            self.assertEqual(cand["components"]["files_direct"]["score"], 0.0)
        # 101/102's only ANCHOR signal was the shared thinned path (labels
        # and lexical merely raise), so the pair legitimately drops out of
        # the narrowed ranking while remaining present unfiltered
        self.assertIn(102, {c["number"] for c in plain["candidates"]})
        self.assertNotIn(102, numbers)


class RevisionControlTests(unittest.TestCase):
    """PRR-003/004: the revised-after-cutoff disclosure actually fires, and
    a pre-v8 store discloses that the control is unavailable."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._td = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._td.name) / "rev.db"
        conn = connect(cls.db_path)
        try:
            init_schema(conn)
            build_eval_store(conn)
            # A post-cutoff body edit on target 101 (created T0+4d): the
            # item_title/item_body revision rows are what store.py writes
            # (valid_to is NOT NULL — a still-current revision carries its
            # supersedence stamp).
            conn.execute(
                "INSERT INTO revisions(repo, number, entity, entity_id, kind,"
                " origin, content, valid_from, valid_to)"
                " VALUES (?, '101', 'item_body', NULL, NULL, 'edit',"
                " 'rewritten after the cutoff', ?, ?)",
                (REPO, "2026-06-10T00:00:00Z", "2026-06-11T00:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._td.cleanup()

    def test_revised_target_is_disclosed_in_every_mode(self) -> None:
        ro = open_existing(self.db_path)
        try:
            for mode in ("similar", "localize", "bundle"):
                result = run_eval(ro, REPO, mode)
                self.assertIn(
                    "item_revised_after_cutoff",
                    result["with_leakage"].get("101", []),
                    mode,
                )
            # disclosure is label-only: the similar metric still scores 101
            similar_eval = run_eval(ro, REPO, "similar")
            self.assertGreater(similar_eval["metrics"]["eligible_targets"], 0)
        finally:
            ro.close()

    def test_pre_v8_store_discloses_unavailable_control(self) -> None:
        import shutil

        old = Path(self._td.name) / "prev8.db"
        shutil.copyfile(self.db_path, old)
        wconn = connect(old)
        try:
            wconn.execute("DROP TABLE revisions")
            wconn.commit()
        finally:
            wconn.close()
        ro = open_existing(old)
        try:
            for mode in ("similar", "localize", "bundle"):
                result = run_eval(ro, REPO, mode)
                self.assertIn(
                    "revisions_unavailable", result["with_leakage"].get("101", []),
                    mode,
                )
            # disclosure, not exclusion: scoring continues
            self.assertGreater(result["metrics"]["eligible_targets"], 0)
        finally:
            ro.close()

    def test_unparseable_created_at_is_excluded_and_labelled(self) -> None:
        import shutil

        broken = Path(self._td.name) / "brokenstamp.db"
        shutil.copyfile(self.db_path, broken)
        wconn = connect(broken)
        try:
            # move the planted revision out of the way so 101's only issue
            # is its stamp, then corrupt 102's created_at (non-NULL junk)
            wconn.execute("DELETE FROM revisions WHERE number = '101'")
            wconn.execute(
                "UPDATE items SET created_at = 'not-a-timestamp' WHERE number = '102'"
            )
            wconn.commit()
        finally:
            wconn.close()
        ro = open_existing(broken)
        try:
            # 102 is a duplicate-similar target only (localize/bundle target
            # issues come from closes edges: 101 and 105)
            result = run_eval(ro, REPO, "similar")
            self.assertIn(
                "created_at_unparseable", result["with_leakage"].get("102", [])
            )
        finally:
            ro.close()


class ClosingPrexclusionTests(unittest.TestCase):
    """PRR-005: eval modes exclude the target's closing-PR edges. Two
    issues sharing NOTHING but their closing PR must not pair through it:
    with the exclusion the bundle proposal set misses the truth pair, and
    similar() only proposes it when the closing-PR adjacency is left in."""

    @classmethod
    def setUpClass(cls) -> None:
        from fixtures import FakeGitHubSource, issue, pr_file, pull
        from fixtures_eval import _insert_edge
        from zaxbygraph.sync import sync_repo

        cls._td = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._td.name) / "closing.db"
        conn = connect(cls.db_path)
        try:
            init_schema(conn)
            src = FakeGitHubSource()
            src.add_issue(
                issue(
                    301,
                    title="totally unrelated alpha problem",
                    body="alpha rotor bearing wear",
                    created_at="2026-06-02T00:00:00Z",
                )
            )
            src.add_issue(
                issue(
                    302,
                    title="unrelated gamma auth flow",
                    body="gamma token refresh loop",
                    created_at="2026-06-02T00:00:00Z",
                )
            )
            merged = pull(310, merged=True)
            merged["merged_at"] = "2026-06-04T00:00:00Z"
            src.add_pr(
                issue(
                    310,
                    title="chore merge",
                    body="",
                    kind="pr",
                    created_at="2026-06-03T00:00:00Z",
                ),
                merged,
                files=[pr_file("src/a.py"), pr_file("src/b.py")],
            )
            sync_repo(conn, src, REPO)
            _insert_edge(conn, REPO, 310, "closes", 301, source="closing_ref")
            _insert_edge(conn, REPO, 310, "closes", 302, source="closing_ref")
            # the duplicate link makes {301, 302} eval-similar targets whose
            # ONLY ranking bridge is the shared closing PR 310
            _insert_edge(conn, REPO, 302, "duplicate_of", 301)
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._td.cleanup()

    def setUp(self) -> None:
        self.conn = open_existing(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()

    def test_closing_pr_alone_does_not_pair_issues_in_eval(self) -> None:
        # the shared closing PR is the only bridge: with the exclusion,
        # neither issue proposes the other, so the truth pair is missed.
        result = run_eval(self.conn, REPO, "bundle")
        self.assertGreaterEqual(result["metrics"]["truth_pairs"], 1)
        self.assertEqual(result["metrics"]["pair_recall"], 0.0)

    def test_closing_pr_alone_does_not_rank_partner_in_similar_eval(self) -> None:
        """PRR-005's discriminating leg: the duplicate pair {301, 302}
        anchors ONLY through the common closing-PR neighbor; the eval
        excludes that adjacency, so the partner must NOT rank (pre-fix this
        recalled 1.0 through the closing PR)."""
        result = run_eval(self.conn, REPO, "similar")
        # the partner never ranks, so NEITHER duplicate direction has an
        # in-scope candidate: the metric is empty (pre-fix it recalled 1.0
        # through the closing-PR adjacency)
        self.assertIsNone(result["metrics"]["recall_at_5"])
        self.assertEqual(result["metrics"]["eligible_targets"], 0)
        self.assertIn("no_ground_truth", result["labels"])

    def test_closing_pr_exclusion_is_the_only_toggle(self) -> None:
        # proving the isolation: WITHOUT the exclusion the pair anchors
        # through the closing PR (structural), so deleting exactly the
        # (301, 310) adjacency from 302's view must remove the proposal.
        with_edge = similar(self.conn, REPO, 302, limit=10)
        self.assertIn(301, [c["number"] for c in with_edge["candidates"]])
        without_edge = similar(
            self.conn,
            REPO,
            302,
            limit=10,
            exclude_adjacency={frozenset((301, 310))},
        )
        self.assertNotIn(301, [c["number"] for c in without_edge["candidates"]])


class EvalDenominatorTests(unittest.TestCase):
    """Discriminating legs for the pr43 round-2 metric fixes: each test
    FAILS against the pre-fix eval (truncation before filtering, all-partner
    recall denominators, unmerged closers in bundle truth)."""

    def _build(self, name: str) -> tuple[object, "FakeGitHubSource"]:  # type: ignore[name-defined]
        from fixtures import FakeGitHubSource

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db_path = Path(td.name) / name
        conn = connect(db_path)
        self.addCleanup(conn.close)
        init_schema(conn)
        return conn, FakeGitHubSource()

    def test_post_cutoff_leaders_cannot_crowd_the_window(self) -> None:
        """PRR-006: 25 future decoys outrank the planted partner; the eval
        must rank from the UNTRUNCATED pool and filter visibility BEFORE the
        top-5 cut (pre-fix: limit=20 truncated the partner away and the
        target was skipped, reporting no ground truth at all)."""
        from fixtures import issue, pr_file, pull
        from fixtures_eval import _insert_edge
        from zaxbygraph.sync import sync_repo

        conn, src = self._build("crowd.db")
        merged = pull(500, merged=True)
        merged["merged_at"] = "2026-05-01T00:00:00Z"
        src.add_pr(
            issue(500, title="vocab pr", body="", kind="pr", created_at="2026-05-01T00:00:00Z"),
            merged,
            files=[pr_file("src/shared.py")],
        )
        body = "alpha beta gamma delta epsilon zeta"
        src.add_issue(
            issue(501, title="submit crash", body=body, created_at="2026-06-01T00:00:00Z",
                  comments=1)
        )
        # the path ref rides in a comment (extracted like the fixture does)
        src.comment_on(501, "see src/shared.py:1:1")
        src.add_issue(
            issue(502, title="widget overview", body="widget plumbing notes entirely unrelated",
                  created_at="2026-06-01T00:00:00Z", comments=1)
        )
        src.comment_on(502, "see src/shared.py:2:2")
        for n in range(510, 535):
            src.add_issue(
                issue(n, title=f"decoy {n}", body=body, created_at="2026-07-01T00:00:00Z",
                      comments=1)
            )
            src.comment_on(n, "see src/shared.py:3:3")
        sync_repo(conn, src, REPO)
        _insert_edge(conn, REPO, 502, "duplicate_of", 501)
        conn.commit()
        ro = open_existing(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
        try:
            result = run_eval(ro, REPO, "similar")
        finally:
            ro.close()
        # BOTH duplicate directions are eligible now (each partner is visible
        # at its target's cutoff): pre-fix, target 501 was skipped entirely
        # (its partner truncated away by limit=20) leaving eligible == 1.
        self.assertEqual(result["metrics"]["eligible_targets"], 2)
        self.assertEqual(result["metrics"]["recall_at_5"], 1.0)

    def test_recall_denominator_counts_only_visible_partners(self) -> None:
        """PRR-007: partner 603 is created after the target and can never
        hit; it must not sit in the recall denominator (pre-fix: 0.5)."""
        from fixtures import issue, pr_file, pull
        from fixtures_eval import _insert_edge
        from zaxbygraph.sync import sync_repo

        conn, src = self._build("denom.db")
        merged = pull(600, merged=True)
        merged["merged_at"] = "2026-05-01T00:00:00Z"
        src.add_pr(
            issue(600, title="vocab pr", body="", kind="pr", created_at="2026-05-01T00:00:00Z"),
            merged,
            files=[pr_file("src/both.py")],
        )
        for number, stamp in (
            (601, "2026-06-01T00:00:00Z"),
            (602, "2026-06-01T00:00:00Z"),
            (603, "2026-07-01T00:00:00Z"),
        ):
            src.add_issue(
                issue(number, title=f"crash on submit {number}",
                      body="submit handler crashes on retry", created_at=stamp,
                      comments=1)
            )
            src.comment_on(number, "see src/both.py:1:1")
        sync_repo(conn, src, REPO)
        _insert_edge(conn, REPO, 602, "duplicate_of", 601)
        _insert_edge(conn, REPO, 603, "duplicate_of", 601)
        conn.commit()
        ro = open_existing(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
        try:
            result = run_eval(ro, REPO, "similar")
        finally:
            ro.close()
        # all three targets are eligible; recall is 1.0 everywhere because
        # the invisible partner 603 no longer sits in 601's denominator
        # (pre-fix: recall (0.5 + 1.0 + 1.0) / 3)
        self.assertEqual(result["metrics"]["eligible_targets"], 3)
        self.assertEqual(result["metrics"]["recall_at_5"], 1.0)

    def test_unmerged_closer_contributes_no_bundle_truth(self) -> None:
        """PRR-008: an UNMERGED PR with closing keywords/references must not
        seed bundle truth (pre-fix: truth_pairs == 1 and a real recall
        denominator)."""
        from fixtures import issue, pr_file, pull
        from fixtures_eval import _insert_edge
        from zaxbygraph.sync import sync_repo

        conn, src = self._build("unmerged.db")
        src.add_issue(
            issue(701, title="first half", body="first half of the bundle",
                  created_at="2026-06-01T00:00:00Z")
        )
        src.add_issue(
            issue(702, title="second half", body="second half of the bundle",
                  created_at="2026-06-01T00:00:00Z")
        )
        src.add_pr(
            issue(710, title="open pr", body="", kind="pr", created_at="2026-06-02T00:00:00Z"),
            pull(710, merged=False),  # never merged: no merged_at
            files=[pr_file("src/x.py"), pr_file("src/y.py")],
        )
        sync_repo(conn, src, REPO)
        _insert_edge(conn, REPO, 710, "closes", 701, source="closing_ref")
        _insert_edge(conn, REPO, 710, "closes", 702, source="closing_ref")
        conn.commit()
        ro = open_existing(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
        try:
            result = run_eval(ro, REPO, "bundle")
        finally:
            ro.close()
        self.assertEqual(result["metrics"]["truth_pairs"], 0)
        self.assertIsNone(result["metrics"]["pair_recall"])
        self.assertIn("no_ground_truth", result["labels"])

    def test_all_targets_cutoff_excluded_yields_empty_bundle_metrics(self) -> None:
        """pr43 critic round 2 (executed probe): with every bundle target's
        created_at missing, pair_recall must be EMPTY (null) with a
        no_ground_truth label — not 0.0 over an un-hittable pair."""
        import shutil

        from fixtures import FakeGitHubSource, issue, pr_file, pull
        from fixtures_eval import _insert_edge
        from zaxbygraph.sync import sync_repo

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db_path = Path(td.name) / "nulstamps.db"
        conn = connect(db_path)
        init_schema(conn)
        src = FakeGitHubSource()
        src.add_issue(
            issue(801, title="half one", body="half one text", created_at="2026-06-01T00:00:00Z")
        )
        src.add_issue(
            issue(802, title="half two", body="half two text", created_at="2026-06-01T00:00:00Z")
        )
        merged = pull(810, merged=True)
        merged["merged_at"] = "2026-06-03T00:00:00Z"
        src.add_pr(
            issue(810, title="merger", body="", kind="pr", created_at="2026-06-02T00:00:00Z"),
            merged,
            files=[pr_file("src/p.py"), pr_file("src/q.py")],
        )
        sync_repo(conn, src, REPO)
        _insert_edge(conn, REPO, 810, "closes", 801, source="closing_ref")
        _insert_edge(conn, REPO, 810, "closes", 802, source="closing_ref")
        conn.commit()
        conn.execute("UPDATE items SET created_at = NULL WHERE number IN ('801', '802')")
        conn.commit()
        conn.close()
        ro = open_existing(db_path)
        try:
            result = run_eval(ro, REPO, "bundle")
        finally:
            ro.close()
        self.assertIsNone(result["metrics"]["pair_recall"])
        self.assertEqual(result["metrics"]["eligible_targets"], 0)
        self.assertIn("no_ground_truth", result["labels"])
        self.assertIn("801", result["with_leakage"])

    def test_half_excluded_bundle_yields_fully_empty_metrics(self) -> None:
        """pr43 round-2 gate probe: ONE excluded truth endpoint must null
        BOTH metrics (precision over proposals with no scoreable truth is
        not a measurement), not pair_recall null beside pair_precision
        0.0."""
        import shutil

        from fixtures import FakeGitHubSource, issue, pr_file, pull
        from fixtures_eval import _insert_edge
        from zaxbygraph.sync import sync_repo

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db_path = Path(td.name) / "halfstamps.db"
        conn = connect(db_path)
        init_schema(conn)
        src = FakeGitHubSource()
        src.add_issue(
            issue(821, title="half one", body="half one text", created_at="2026-06-01T00:00:00Z")
        )
        src.add_issue(
            issue(822, title="half two", body="half two text", created_at="2026-06-01T00:00:00Z")
        )
        merged = pull(830, merged=True)
        merged["merged_at"] = "2026-06-03T00:00:00Z"
        src.add_pr(
            issue(830, title="merger", body="", kind="pr", created_at="2026-06-02T00:00:00Z"),
            merged,
            files=[pr_file("src/p.py"), pr_file("src/q.py")],
        )
        sync_repo(conn, src, REPO)
        _insert_edge(conn, REPO, 830, "closes", 821, source="closing_ref")
        _insert_edge(conn, REPO, 830, "closes", 822, source="closing_ref")
        conn.commit()
        conn.execute("UPDATE items SET created_at = NULL WHERE number = '821'")
        conn.commit()
        conn.close()
        ro = open_existing(db_path)
        try:
            result = run_eval(ro, REPO, "bundle")
        finally:
            ro.close()
        self.assertIsNone(result["metrics"]["pair_recall"])
        self.assertIsNone(result["metrics"]["pair_precision"])
        self.assertEqual(result["metrics"]["eligible_targets"], 0)
        self.assertIn("no_ground_truth", result["labels"])
        # 821 was excluded before scoring; 822 was scored and disclosed
        self.assertNotIn("signature_origin_unknown", result["with_leakage"].get("821", []))
        self.assertIn("signature_origin_unknown", result["with_leakage"].get("822", []))


class GitLsFilesTests(unittest.TestCase):
    """PRR-002: the ``git ls-files`` intersection is root-relative from any
    subdirectory (and quotes nothing), and degrades to (None, None) outside
    a checkout."""

    def test_root_relative_from_subdirectory(self) -> None:
        import os
        import subprocess as sp

        from zaxbygraph.cli import _git_ls_files

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src" / "pkg").mkdir(parents=True)
            (root / "src" / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")

            def git(*argv: str, cwd: Path = root) -> None:
                sp.run(
                    ["git", *argv], cwd=str(cwd), check=True, capture_output=True
                )

            git("init")
            git("-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
            git(
                "-c", "user.email=t@example.com", "-c", "user.name=t",
                "commit", "-m", "init",
            )
            old = os.getcwd()
            os.chdir(root / "src" / "pkg")
            try:
                files, head = _git_ls_files()
            finally:
                os.chdir(old)
            self.assertEqual(files, ["src/pkg/mod.py"])
            self.assertTrue(head)

    def test_outside_a_checkout_degrades(self) -> None:
        import os

        from zaxbygraph.cli import _git_ls_files

        with tempfile.TemporaryDirectory() as td:
            old = os.getcwd()
            os.chdir(td)  # plain temp dir: no .git anywhere beneath
            try:
                files, head = _git_ls_files()
            finally:
                os.chdir(old)
            self.assertIsNone(files)
            self.assertIsNone(head)


class CochangeSymmetryTests(unittest.TestCase):
    """Cubic run-2: the cochange score was max over DIRECTIONAL
    conditionals (sup/support(A) vs sup/support(B)), which differ when the
    two files' support counts differ — an AC2 violation invisible in the
    synthetic fixture (its cochange scores are all 0). The symmetric
    confidence sup/max(sup_A, sup_B) is commutative."""

    def test_cochange_score_is_symmetric_under_unequal_support(self) -> None:
        import shutil

        from fixtures import FakeGitHubSource, issue, pr_file, pull
        from zaxbygraph.sync import sync_repo
        from zaxbygraph.derive import explain

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db_path = Path(td.name) / "sym.db"
        conn = connect(db_path)
        try:
            init_schema(conn)
            src = FakeGitHubSource()
            # hub file src/hub.py: touched by PRs 900/901/902
            for n in (900, 901, 902):
                merged = pull(n, merged=True)
                merged["merged_at"] = "2026-05-0%dT00:00:00Z" % ((n % 9) + 1,)
                src.add_pr(
                    issue(n, title=f"pr {n}", body="", kind="pr",
                          created_at="2026-05-01T00:00:00Z"),
                    merged,
                    files=[pr_file("src/hub.py"), pr_file(f"src/only{n}.py")],
                )
            # rare file src/rare.py: ONLY PR 900 touches it besides hub
            merged = pull(903, merged=True)
            merged["merged_at"] = "2026-05-05T00:00:00Z"
            src.add_pr(
                issue(903, title="pr 903", body="", kind="pr",
                      created_at="2026-05-01T00:00:00Z"),
                merged,
                files=[pr_file("src/hub.py"), pr_file("src/rare.py")],
            )
            src.add_issue(
                issue(910, title="item touching hub",
                      body="see src/hub.py:1:1", created_at="2026-06-01T00:00:00Z",
                      comments=1)
            )
            src.comment_on(910, "see src/hub.py:1:1")
            src.add_issue(
                issue(911, title="item touching rare",
                      body="see src/rare.py:2:2", created_at="2026-06-01T00:00:00Z",
                      comments=1)
            )
            src.comment_on(911, "see src/rare.py:2:2")
            sync_repo(conn, src, REPO)
        finally:
            conn.close()
        ro = open_existing(db_path)
        try:
            ab = explain(ro, REPO, 910, 911)
            ba = explain(ro, REPO, 911, 910)
            self.assertEqual(
                ab["components"]["cochange"]["score"],
                ba["components"]["cochange"]["score"],
            )
            # the symmetric confidence is sup/max(sup_hub, sup_rare) =
            # 1/max(4, 1) = 0.25 (hub support 4: PRs 900-903; rare 1)
            self.assertAlmostEqual(
                ab["components"]["cochange"]["score"], 0.25, places=6
            )
            # evidence confidence is the same symmetric value
            self.assertAlmostEqual(
                ab["components"]["cochange"]["evidence"]["pairs"][0]
                ["confidence"],
                0.25,
                places=6,
            )
        finally:
            ro.close()


class EvalDegradationExtraTests(unittest.TestCase):
    """PRR-011/022/027: eval survives a pre-v9 store, refuses to mint an
    unexportable sha from a too-new store, and labels zero-ground-truth."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._td = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._td.name) / "extra.db"
        conn = connect(cls.db_path)
        try:
            init_schema(conn)
            build_eval_store(conn)
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._td.cleanup()

    def test_eval_localize_on_pre_v9_store_degrades(self) -> None:
        import shutil

        old = Path(self._td.name) / "nopaths.db"
        shutil.copyfile(self.db_path, old)
        wconn = connect(old)
        try:
            # a true pre-v9 store: the v9 tables are absent AND the version
            # stamp says so (the export sha gate keys off the version)
            wconn.execute("DROP TABLE item_path_refs")
            wconn.execute("DROP TABLE item_signatures")
            wconn.execute("PRAGMA user_version = 9")
            wconn.commit()
        finally:
            wconn.close()
        ro = open_existing(old)
        try:
            # the comment-cutoff branch used to query the dropped table
            # unconditionally and crash (PRR-011); it must degrade instead
            result = run_eval(ro, REPO, "localize")
            self.assertIn("store_too_old", result["labels"])
            self.assertIsNone(result["corpus_sha256"])
        finally:
            ro.close()

    def test_store_too_new_mints_no_sha(self) -> None:
        import shutil

        from zaxbygraph.db import CURRENT_USER_VERSION

        future = Path(self._td.name) / "future.db"
        shutil.copyfile(self.db_path, future)
        wconn = connect(future)
        try:
            wconn.execute(f"PRAGMA user_version = {CURRENT_USER_VERSION + 1}")
            wconn.commit()
        finally:
            wconn.close()
        ro = open_existing(future)
        try:
            result = run_eval(ro, REPO, "similar")
            self.assertIsNone(result["corpus_sha256"])
            self.assertIn("store_too_new", result["labels"])
        finally:
            ro.close()

    def test_zero_ground_truth_labels_no_ground_truth(self) -> None:
        import shutil

        empty = Path(self._td.name) / "empty.db"
        shutil.copyfile(self.db_path, empty)
        wconn = connect(empty)
        try:
            wconn.execute(
                "DELETE FROM edges WHERE rel = 'duplicate_of'"
            )
            wconn.commit()
        finally:
            wconn.close()
        ro = open_existing(empty)
        try:
            result = run_eval(ro, REPO, "similar")
            self.assertEqual(result["metrics"]["targets_total"], 0)
            self.assertEqual(result["metrics"]["eligible_targets"], 0)
            self.assertIsNone(result["metrics"]["recall_at_5"])
            self.assertIn("no_ground_truth", result["labels"])
        finally:
            ro.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
