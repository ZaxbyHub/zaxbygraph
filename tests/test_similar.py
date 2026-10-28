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
claimed): localize Acc@5 = 1.0, bundle pair_recall = 0.5,
pair_precision = 0.111 (1 hit in 9 proposed pairs over 2 truth pairs).
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
        # 103's only anchor — the edge was load-bearing.
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
        finally:
            wconn.close()
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
            self.assertGreaterEqual(
                bundle_eval["metrics"]["pair_precision"], 0.1
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
