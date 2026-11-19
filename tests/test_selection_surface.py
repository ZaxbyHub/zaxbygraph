"""Arm's-length acceptance checks for issue #70 (the work-selection surface:
scale, one-call selection, slim rows, brief decision fields, informative
evidence, MCP default, skill teaching), authored BEFORE any implementation.

Frozen check names (issue #70 acceptance table):

    C6  test_derived_commands_scale
    C7  test_one_call_work_selection
    C8  test_default_rows_are_slim
    C9  test_brief_carries_decision_fields
    C10 test_evidence_lists_are_informative
    C11 test_mcp_bundle_default_is_one_call
    C12 test_skill_teaches_one_call

C13 is the PRESERVING pin test_triage_matches_hand_computed_formula in
tests/test_triage_bundle.py (verify-only there; never edited here).

Base calibration for C6 (measured at 5b41510): a 2000-path vocabulary with
300 stored path references runs build_context in ~0.06 s wall clock but
performs ~600k vocabulary-element visits — every reference that misses the
exact and unique-suffix lanes scans the WHOLE vocabulary once
(derive._resolve_ref's reverse-tail comprehension). Wall clock is therefore
NOT the discriminator; the deterministic proxy is the per-resolution
vocabulary-scan count, measured with a counting set at the
``derive._resolve_ref(ref, vocab, by_suffix)`` seam (the module-level
function the fix is expected to keep as the path-resolution entry point,
with the reverse direction indexed instead of scanned). The wall-clock leg
(< 10 s, the AC's CI budget) stays as the whole-call regression guard.

Hermetic: FakeGitHubSource stores only; no network, no gh. Stdlib only;
unittest (pytest runs the file). New-surface rule: post-fix symbols are
reached inside test bodies, never at import time.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, issue, pr_file, pull, scrubbed_env

from zaxbygraph.db import connect, init_schema, open_existing
from zaxbygraph.sync import sync_repo

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Hermeticity (the test_triage_bundle _LEG_CWD pattern): derived-tier CLI
#: legs resolve the [triage]/[derive] config chain from the cwd, so every
#: subprocess leg runs in a dedicated temp cwd.
_LEG_CWD = tempfile.mkdtemp(prefix="zaxbygraph-selsurf-")
atexit.register(shutil.rmtree, _LEG_CWD, ignore_errors=True)


# ==== shared plumbing ========================================================


def _run_cli(*argv: str) -> tuple[int, bytes, bytes]:
    """One CLI leg: subprocess python -m zaxbygraph with the repo's src and
    tests on PYTHONPATH and a scrubbed child environment."""
    env = scrubbed_env()
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(REPO_ROOT / "src"),
            str(REPO_ROOT / "tests"),
            env.get("PYTHONPATH", ""),
        ]
    )
    proc = subprocess.run(
        [sys.executable, "-m", "zaxbygraph", *argv],
        capture_output=True,
        env=env,
        cwd=_LEG_CWD,
        timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _cli_json(*argv: str) -> dict:
    rc, out, err = _run_cli(*argv)
    if rc != 0:
        raise AssertionError(
            f"CLI {argv!r} exited {rc}; stderr:\n{err.decode('utf-8', 'replace')}"
        )
    return json.loads(out.decode("utf-8"))


def _merged_pull(number: int, merged_at: str) -> dict:
    p = pull(number, merged=True)
    p["merged_at"] = merged_at
    return p


class _StoreTestCase(unittest.TestCase):
    """One temp store per test class; subclasses override ``_build``. The
    in-process legs run in an empty temp cwd (config-chain hermeticity)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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

    def setUp(self) -> None:
        self._prior_cwd = os.getcwd()
        self._hermetic_cwd = tempfile.TemporaryDirectory(
            prefix="zaxbygraph-selsurf-cwd-", ignore_cleanup_errors=True
        )
        os.chdir(self._hermetic_cwd.name)

    def tearDown(self) -> None:
        os.chdir(self._prior_cwd)
        self._hermetic_cwd.cleanup()

    @classmethod
    def _build(cls, conn) -> None:  # pragma: no cover - overridden
        raise AssertionError("_build must be overridden")


class _CountingSet(set):
    """A set whose full-vocabulary scans are countable: ``__iter__`` counts
    every ITERATION and every yielded element. The base implementation's
    reverse-tail resolution walks ``for p in vocab`` once per miss; an
    indexed fix never iterates the vocabulary at all."""

    def __init__(self, it=()):
        super().__init__(it)
        self.iterations = 0
        self.yields = 0

    def __iter__(self):
        self.iterations += 1
        for item in super().__iter__():
            self.yields += 1
            yield item


def _suffix_index(vocab: set[str]) -> dict[str, list[str]]:
    """The documented unique-suffix lane, rebuilt exactly the way
    build_context builds it: every component-boundary tail of every
    vocabulary path maps to the paths carrying it."""
    by_suffix: dict[str, list[str]] = {}
    for path in vocab:
        parts = path.split("/")
        for i in range(len(parts)):
            by_suffix.setdefault("/".join(parts[i:]), []).append(path)
    return by_suffix


# ==== C6: derived commands scale (issue #70 AC: operation counts) ===========


class DerivedScaleTests(_StoreTestCase):
    """A wide store: 100 merged PRs x 20 files = 2000 vocabulary paths, and
    200 open issues whose bodies each reference one path that is in NO
    vocabulary (``docs/nowhere/modNNNN.md``) plus the 100 PR bodies
    referencing their own ``pkg/modXXX/file00.py`` (exact hits). At base
    every miss walks all 2000 vocabulary entries once."""

    VOCAB = 2000
    REF_ISSUES = 200

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        per_pr = cls.VOCAB // 100
        for n in range(1, 101):
            files = [pr_file(f"pkg/mod{n:03d}/file{k:02d}.py") for k in range(per_pr)]
            src.add_pr(
                issue(
                    n,
                    title=f"plumbing {n}",
                    body=f"updates pkg/mod{n:03d}/file00.py:1:1",
                    kind="pr",
                    state="closed",
                    created_at="2027-06-01T00:00:00Z",
                ),
                _merged_pull(n, "2027-06-10T00:00:00Z"),
                files=files,
            )
        for m in range(1, cls.REF_ISSUES + 1):
            src.add_issue(
                issue(
                    1000 + m,
                    title=f"issue {m}",
                    body=f"crash at docs/nowhere/mod{m:04d}.md:3:1",
                    created_at="2027-06-05T00:00:00Z",
                )
            )
        sync_repo(conn, src, REPO)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # The AC's scale store: 20,000 items, 4,000 open issues (16,000
        # merged PRs x 1 file each = 16,000 vocabulary paths; the 4,000 open
        # issues each reference one off-vocabulary path). Built once per
        # class so the four-command legs stay inside the AC's CI budget.
        cls._td_scale = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.scale_db_path = Path(cls._td_scale.name) / "scale.db"
        conn = connect(cls.scale_db_path)
        try:
            init_schema(conn)
            src = FakeGitHubSource()
            for n in range(1, 16001):
                src.add_pr(
                    issue(
                        n,
                        title=f"scale pr {n}",
                        body=f"updates pkg/scale/f{n:05d}.py:1:1",
                        kind="pr",
                        state="closed",
                        created_at="2027-06-01T00:00:00Z",
                    ),
                    _merged_pull(n, "2027-06-10T00:00:00Z"),
                    files=[pr_file(f"pkg/scale/f{n:05d}.py")],
                )
            for m in range(1, 4001):
                src.add_issue(
                    issue(
                        20000 + m,
                        title=f"scale issue {m}",
                        body=f"crash at docs/missing/m{m:04d}.md:3:1",
                        created_at="2027-06-05T00:00:00Z",
                    )
                )
            sync_repo(conn, src, REPO)
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls) -> None:
        # PR #81 review F-23: clean the 20k scale store too — the inherited
        # teardown only cleans _td, leaving the scale database on disk for
        # the rest of the suite run.
        cls._td_scale.cleanup()
        super().tearDownClass()

    def test_derived_commands_scale(self) -> None:
        """RED at base, GREEN after the fix. The AC's operation-count budget
        ("within a fixed budget corresponding to under 10s on CI") as a
        DETERMINISTIC proxy: the per-resolution vocabulary-scan count at the
        derive._resolve_ref seam is O(1) (<= 64 yielded vocabulary elements
        per resolution) instead of O(vocabulary); the whole build_context
        call over the 200-ref/2000-path probe stays under the AC's 10 s wall
        budget and resolves/unresolves every reference exactly once."""
        # ---- leg 3a: deterministic operation budgets at 20k scale --------
        # Issue #70 AC1: triage/bundle/similar/brief on a 20,000-item /
        # 4,000-open synthetic store within DETERMINISTIC operation budgets.
        # In-process CLI runs (config-hermetic cwd from setUp) under two
        # instruments: (a) a counting sqlite progress handler (VDBE
        # instruction proxy at 1000-instruction granularity, so handler
        # FIRES x 1000 = approximate instructions) installed on every
        # connection the command opens, and (b) sys.setprofile counting
        # every Python call. Base calibration (5b41510, measured
        # 2026-11-16): triage 40.5M calls / 3,553 fires (~3.55M
        # instructions), bundle 42.7M / 3,937 fires, similar 1.29M /
        # 1,884 fires, brief 41.2M / 3,553 fires. The pathology
        # is Python-side (the O(refs x vocab) scan), so the PYTHON-CALL
        # budgets discriminate and the VDBE budgets are regression guards
        # (the fix's SQL work is comparable; its Python work collapses).
        # The 30s wall ceilings in leg 3b are SECONDARY guards.
        import io as _io
        import sqlite3 as _sq
        from contextlib import redirect_stdout as _rso

        from zaxbygraph import cli as _cli

        # VDBE guard budgets are in HANDLER FIRES at 1000-instruction
        # granularity: 6,000 fires ~= 6M VDBE instructions (base measured
        # 1,884-3,937 fires). Round-5 correction: the first draft compared
        # raw fires against a 6,000,000 budget - 1000x too loose.
        #
        # Python-call budgets (issue #70 AC1's operation-count contract):
        # the base measured 40.5M (triage), 42.7M (bundle), 41.2M (brief),
        # 1.29M (similar) - the dominant residual cost is the stage-2
        # analog scan (pair_components over every candidate), which is the
        # algorithm's design, not the O(refs x vocab) path-resolution
        # defect this issue fixes. That defect is discriminated by leg 1's
        # seam counter (<=64 vocabulary visits per resolution) and by the
        # wall ceilings in leg 3b. These budgets therefore pin the
        # algorithm's FIXED cost with headroom: 1.5x the measured base
        # catches a regression (a re-introduced vocabulary scan multiplies
        # the count) while tolerating machine-shape variance.
        budgets = {
            "triage": (60_000_000, 6_000),
            "bundle": (64_000_000, 6_000),
            "similar": (2_000_000, 6_000),
            "brief": (62_000_000, 6_000),
        }
        vdbe = {"n": 0}
        py_calls = {"n": 0}

        def _progress():
            vdbe["n"] += 1

        def _profile(frame, event, arg):
            if event == "call":
                py_calls["n"] += 1
            return _profile

        real_connect = _sq.connect

        def _wrapped_connect(*a, **k):
            c = real_connect(*a, **k)
            try:
                c.set_progress_handler(_progress, 1000)
            except Exception:
                pass
            return c

        for name, (call_budget, vdbe_budget) in budgets.items():
            argv = [name, "--db", str(self.scale_db_path), "--repo", REPO,
                    "--format", "json"]
            if name == "similar":
                argv.insert(1, "20001")
            elif name == "brief":
                argv.insert(1, "20001")
            vdbe["n"] = 0
            py_calls["n"] = 0
            buf = _io.StringIO()
            _sq.connect = _wrapped_connect
            sys.setprofile(_profile)
            try:
                with _rso(buf):
                    try:
                        _cli.main(argv)
                    except SystemExit:
                        pass
            finally:
                sys.setprofile(None)
                _sq.connect = real_connect
            envelope = json.loads(buf.getvalue())
            self.assertTrue(
                envelope.get("ok"), f"{name} instrumented leg must answer ok"
            )
            self.assertLess(
                py_calls["n"], call_budget,
                f"{name} made {py_calls['n']:,} Python calls on the 20k"
                f" store (budget {call_budget:,}; base calibration measured"
                " 40M+ — the whole-vocabulary scan class)",
            )
            self.assertLess(
                vdbe["n"], vdbe_budget,
                f"{name} fired the VDBE progress handler {vdbe['n']:,}"
                f" times on the 20k store (guard budget {vdbe_budget:,}"
                " fires at 1000-instruction granularity)",
            )

        # ---- leg 3b: wall ceilings as INFORMATIONAL (PR #81 review F-24) --
        # The 30s wall ceilings were false-red on the primary development
        # platform (measured 31.1s/31.6s on the maintainer's Windows box at
        # this same head while CI runs the same legs under 10s), so an
        # unconditional ceiling masked machine variance while allowing a 3x
        # regression through. The HARD guards for this contract are leg 3's
        # deterministic Python-call and VDBE-instruction budgets above; the
        # wall numbers are recorded, and asserted only when
        # ZAXBYGRAPH_SCALE_WALL_GUARD=1 opts into the machine-sensitive
        # check (the skip reason is stated here).
        wall_guard = os.environ.get("ZAXBYGRAPH_SCALE_WALL_GUARD") == "1"
        scale_t0 = time.perf_counter()
        for argv in (
            ["triage", "--db", str(self.scale_db_path), "--repo", REPO, "--format", "json"],
            ["bundle", "--db", str(self.scale_db_path), "--repo", REPO, "--format", "json"],
            ["similar", "20001", "--db", str(self.scale_db_path), "--repo", REPO, "--format", "json"],
            ["brief", "20001", "--db", str(self.scale_db_path), "--repo", REPO, "--format", "json"],
        ):
            name = argv[0]
            t0 = time.perf_counter()
            env = _cli_json(*argv)
            elapsed = time.perf_counter() - t0
            self.assertTrue(env.get("ok"), f"{name} leg must answer ok")
            if wall_guard:
                self.assertLess(
                    elapsed, 30.0,
                    f"{name} over the 20k scale store took {elapsed:.1f}s"
                    " (opted-in wall guard, budget 30s; the AC budget"
                    " corresponds to under 10s on CI)",
                )
            else:
                print(
                    f"CHECK_RESULT wall-info {name}={elapsed:.1f}s"
                    " (informational; set ZAXBYGRAPH_SCALE_WALL_GUARD=1 to"
                    " enforce the 30s ceiling)"
                )
        print(
            f"CHECK_RESULT scale-legs total={time.perf_counter() - scale_t0:.1f}s"
        )

        # ---- leg 4: brief computes ONE row, not the whole triage ---------
        # The AC's single-row pin: with derive._triage_compute replaced by
        # a counting wrapper, the brief packet path must NOT invoke the
        # whole-triage entry. (Row equality with the full-triage output
        # rides C13's formula pin — this leg owns the call-count half.)
        from zaxbygraph import derive as _d2

        small_conn = open_existing(self.db_path)
        try:
            real_compute = _d2._triage_compute
            counting = {"n": 0}
            derive_calls = {"n": 0}

            def _counting_compute(*a, **k):
                # Single-row mode (only=...) is the FIXED shape: brief
                # scores one row through the shared compute. Only a FULL
                # (no only=) recompute counts as the defect this leg
                # pins (issue #70 AC1: brief must not recompute the whole
                # triage for one row).
                if not k.get("only"):
                    counting["n"] += 1
                return real_compute(*a, **k)

            def _profile(frame, event, arg):
                if event == "call" and frame.f_code.co_filename.endswith(
                    ("derive.py",)
                ):
                    derive_calls["n"] += 1
                return _profile

            _d2._triage_compute = _counting_compute
            try:
                import sys as _sys

                _sys.setprofile(_profile)
                try:
                    packet = _d2.brief(
                        small_conn, REPO, 1001, budget_tokens=4000,
                    )
                finally:
                    _sys.setprofile(None)
            finally:
                _d2._triage_compute = real_compute
            self.assertEqual(
                counting["n"], 0,
                "brief must not recompute the whole triage for one row"
                f" (saw {counting['n']} full-triage calls)",
            )
            # deterministic Python-call budget (fix: one row's scoring; base
            # calibration measured 112,956 derive-module calls for the
            # whole-triage recompute). Recalibrated at the Phase 4.5
            # implementation review (finding 3): the corrected single-row
            # mode must keep the requested row's FULL-population stage-1
            # rank so its top-K eligibility matches the full triage, so the
            # population stage-1 pass now runs (it is cheap arithmetic; the
            # expensive stage-2 analog scans still run for the one row
            # only). Measured head 53,863 (deterministic call count; the
            # shared context + coupling-index build dominates it), so the
            # 70k budget sits 1.61x below the measured base and still
            # fails the whole-triage recompute it discriminates against.
            self.assertLess(
                derive_calls["n"], 70_000,
                f"single-row brief made {derive_calls['n']} derive-module"
                " calls (budget 70k; base whole-triage recompute measured"
                " 112,956, head measured 53,863)",
            )
            self.assertEqual(packet.get("number"), 1001)
            # row-equality half (plan-critic round 4): the packet's row and
            # the full-triage row differ ONLY in representation (title is
            # wrapped as {"untrusted_text": ...} in the serialized packet
            # and the internal stage1 key is omitted), so compare the
            # scoring projection with both deltas stripped on BOTH sides.
            # Preserving-green at base: the base brief picks its row from
            # the same compute, so the stripped comparison is equal there
            # too.
            cfg = _d2.load_triage_config()
            full = real_compute(small_conn, REPO, cfg, cfg["top_k"])
            full_row = next(
                (r for r in full["rows"] if r["number"] == 1001), None
            )
            self.assertIsNotNone(full_row, "full triage must rank 1001")
            packet_row = dict(packet.get("features") or {})
            expected_row = dict(full_row)

            def _scoring_projection(row: dict) -> dict:
                drop = {"title", "stage1"}
                return {k: v for k, v in row.items() if k not in drop}

            self.assertEqual(
                _scoring_projection(packet_row),
                _scoring_projection(expected_row),
                "the single-row brief packet's scoring projection must"
                " equal the full-triage row's (title untrusted-text"
                " wrapping and the internal stage1 key are representation"
                " deltas, stripped on both sides)",
            )
        finally:
            small_conn.close()



        from zaxbygraph import derive

        conn = open_existing(self.db_path)
        try:
            # ---- leg 1: deterministic per-resolution scan count -----------
            # Three probe references at the seam: a reverse-tail hit (the
            # ref is a checkout-prefixed mention of a vocab path), an
            # unresolvable miss, and an exact hit. Each must resolve in a
            # BOUNDED number of vocabulary-element visits (an indexed lookup
            # yields zero; the budget leaves 64 for any ref-derived probe
            # fan the fix legitimately keeps).
            vocab = {
                f"pkg/mod{k // 20:03d}/file{k % 20:02d}.py"
                for k in range(self.VOCAB)
            }
            by_suffix = _suffix_index(vocab)
            probes = [
                ("/abs/checkout/pkg/mod001/file05.py", "pkg/mod001/file05.py"),
                ("docs/nowhere/x.md", None),
                ("pkg/mod001/file05.py", "pkg/mod001/file05.py"),
            ]
            for ref, expected in probes:
                counting = _CountingSet(vocab)
                resolved = derive._resolve_ref(ref, counting, by_suffix)
                self.assertEqual(
                    resolved, expected, f"resolution of {ref!r} must stay correct"
                )
                self.assertLessEqual(
                    counting.iterations,
                    2,
                    f"resolving {ref!r} may scan the full vocabulary at most"
                    f" a bounded constant number of times, got"
                    f" {counting.iterations}",
                )
                self.assertLessEqual(
                    counting.yields,
                    64,
                    f"resolving {ref!r} yielded {counting.yields} vocabulary"
                    f" elements — the per-ref vocabulary scan is O(vocab),"
                    f" not O(1)",
                )

            # ---- leg 2: whole-call budget + correctness at scale ----------
            calls = {"n": 0}
            real = derive._resolve_ref

            def _counting_resolve(ref, vocab_arg, by_suffix_arg):
                calls["n"] += 1
                return real(ref, vocab_arg, by_suffix_arg)

            derive._resolve_ref = _counting_resolve
            try:
                t0 = time.perf_counter()
                ctx = derive.build_context(conn, REPO)
                elapsed = time.perf_counter() - t0
            finally:
                derive._resolve_ref = real
            self.assertLess(
                elapsed,
                10.0,
                f"build_context over {self.REF_ISSUES} unresolved refs x"
                f" {self.VOCAB} vocabulary paths took {elapsed:.1f}s — the AC"
                f" budget is under 10s",
            )
            stored_refs = int(
                conn.execute(
                    "SELECT COUNT(*) FROM item_path_refs WHERE repo = ?", (REPO,)
                ).fetchone()[0]
            )
            self.assertGreaterEqual(stored_refs, self.REF_ISSUES + 100)
            self.assertLessEqual(
                calls["n"],
                stored_refs,
                "each stored reference must be resolved exactly once (no"
                f" retry storm): {calls['n']} calls for {stored_refs} refs",
            )
            unresolved = sum(
                len(d.unresolved_refs) for d in ctx["items"].values()
            )
            self.assertEqual(
                unresolved,
                self.REF_ISSUES,
                "every off-vocabulary reference must land in the unresolved"
                " accounting (strings preserved, never dropped silently)",
            )
            resolved_pr_items = sum(
                1
                for d in ctx["items"].values()
                if any(p.startswith("pkg/mod") for p in d.paths)
            )
            self.assertEqual(
                resolved_pr_items,
                100,
                "every merged-PR body reference must resolve to its exact"
                " vocabulary path",
            )
        finally:
            conn.close()

    def test_brief_outside_top_k_matches_full_triage(self) -> None:
        """RED at the pre-fix implementation head 5fc2404, GREEN after the
        fix (Phase 4.5 implementation review finding 3). The single-row
        brief keeps the requested row's FULL-population stage-1 rank, so its
        top-K eligibility is the full triage's: a brief for an issue that
        ranks OUTSIDE top_k reports the SAME row the full ranking reports
        for it - stage2/hotspot/regression/analog None, note
        'not_computed (outside top_k)', and total_score == stage1 with no
        stage-2 bonus. The pre-fix single-row mode made the requested row
        the sole ungated member of its own population, so it always received
        stage 2 and always a bonus: a total_score the full triage would
        never give that row (the pin's failure there was
        "AssertionError: 0.0 is not None : an outside-top_k brief must not
        report stage2"). GREEN at the base too - the base brief picked its
        row from the whole-triage recompute, so the row was always the full
        ranking's; what the base got wrong was the cost, not this row."""
        from zaxbygraph import derive as _d

        conn = open_existing(self.db_path)
        try:
            cfg = _d.load_triage_config()
            full = _d._triage_compute(conn, REPO, cfg, cfg["top_k"])
            outside = full["ungated"][max(1, cfg["top_k"]):]
            self.assertTrue(
                outside,
                "the 200-open-issue fixture must have rows outside top_k"
                f" (top_k={cfg['top_k']}, ungated={len(full['ungated'])})",
            )
            target = outside[0]["number"]
            expected = next(r for r in full["rows"] if r["number"] == target)
            packet = _d.brief(conn, REPO, target, budget_tokens=4000)
        finally:
            conn.close()

        row = dict(packet.get("features") or {})
        self.assertEqual(row.get("number"), target)
        for key in ("stage2", "hotspot", "regression", "analog"):
            self.assertIsNone(
                row.get(key),
                f"an outside-top_k brief must not report {key} (the full"
                " triage leaves it uncomputed for that row)",
            )
        self.assertEqual(
            row.get("note"),
            "not_computed (outside top_k)",
            "an outside-top_k brief must carry the full triage's note",
        )
        self.assertEqual(
            row.get("total_score"),
            expected["total_score"],
            "the outside-top_k brief's total_score must equal the full"
            " triage row's (stage1, no stage-2 bonus)",
        )
        self.assertEqual(
            row.get("total_score"),
            expected["stage1"],
            "an outside-top_k row's total_score IS its stage1",
        )


# ==== C7: one-call work selection (bundle --with-briefs) ====================


class OneCallBundleTests(_StoreTestCase):
    """The test_triage_bundle BundlePayloadTests geometry (bundle {801, 802}
    over src/core.py), extended with the decision fields issue #70 demands:
    labels on both members, an explicit author/created/updated stamp, and an
    off-vocabulary reference (docs/nowhere.md) on 801 so files_missing /
    not_in_vocabulary have something real to carry.

    - PR 810 merged touching [src/core.py, tests/test_core.py]
    - PR 811 merged touching [src/linked.py] (population filler)
    - PR 820 OPEN touching src/core.py
    - 801 (label bug, author alice) and 802 (label bug, author bob) both
      reference src/core.py -> files_direct anchor, one bundle.
    - 801's body also references docs/nowhere.md — not in any vocabulary.
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        src.add_pr(
            issue(810, title="core fix", body="fixes the fault at src/core.py:1:1",
                  kind="pr", state="closed", created_at="2027-06-01T00:00:00Z"),
            _merged_pull(810, "2027-06-10T00:00:00Z"),
            files=[pr_file("src/core.py"), pr_file("tests/test_core.py")],
        )
        src.add_pr(
            issue(811, title="linked area", body="", kind="pr", state="closed",
                  created_at="2027-06-01T00:00:00Z"),
            _merged_pull(811, "2027-06-11T00:00:00Z"),
            files=[pr_file("src/linked.py")],
        )
        src.add_pr(
            issue(820, title="open collision work", body="", kind="pr"),
            pull(820, merged=False),
            files=[pr_file("src/core.py")],
        )
        bug = [{"name": "bug", "color": "ff0000"}]
        rec801 = issue(
            801,
            title="Fault in core path one",
            body="fault at src/core.py:5:2; see also docs/nowhere.md:1:1",
            author="alice",
            labels=bug,
            created_at="2027-06-15T00:00:00Z",
            updated_at="2027-06-20T00:00:00Z",
        )
        rec802 = issue(
            802,
            title="Fault in core path two",
            body="fault at src/core.py:6:2",
            author="bob",
            labels=bug,
            created_at="2027-06-16T00:00:00Z",
            updated_at="2027-06-21T00:00:00Z",
        )
        src.add_issue(rec801)
        src.add_issue(rec802)
        sync_repo(conn, src, REPO)

    def _member(self, env: dict, number: int) -> dict:
        bundles = env["data"]["bundles"]
        self.assertTrue(
            bundles, "expected at least one bundle over the {801, 802} pair"
        )
        for bundle in bundles:
            for m in bundle["members"]:
                if isinstance(m, dict) and int(m.get("number", -1)) == number:
                    return m
        self.fail(f"no bundle member {number}: {json.dumps(bundles)[:400]}")

    def test_one_call_work_selection(self) -> None:
        """RED at base, GREEN after the fix. ``bundle --with-briefs
        --budget-tokens B`` is the ONE-CALL selection: the parser accepts
        the flag pair, and the returned bundles' members carry the decision
        fields (labels, created/updated dates, author, html_url, a body
        excerpt, gates, and files split into existing vs not-yet-existing
        with off-vocabulary references marked not_in_vocabulary); a tiny
        budget cuts the payload with truncated=true."""
        # Base: argparse rejects --with-briefs (exit 2) -> this leg fails.
        env = _cli_json(
            "bundle", "--with-briefs", "--budget-tokens", "4000",
            "--db", str(self.db_path), "--repo", REPO, "--format", "json",
        )
        self.assertTrue(env["ok"], env)
        m801 = self._member(env, 801)
        m802 = self._member(env, 802)

        # member decision fields, verbatim from the AC's list
        for field in (
            "labels", "created_at", "updated_at", "author", "html_url",
            "excerpt", "gates", "files_existing", "files_missing",
            "not_in_vocabulary",
        ):
            self.assertIn(
                field, m801,
                f"bundle members must carry the decision field {field!r}"
                f" under --with-briefs (member 801: {sorted(m801)})",
            )

        # field values against the fixture
        self.assertIn("bug", json.dumps(m801["labels"]))
        self.assertEqual(m801["author"], "alice")
        self.assertEqual(m801["created_at"], "2027-06-15T00:00:00Z")
        self.assertEqual(m801["updated_at"], "2027-06-20T00:00:00Z")
        self.assertEqual(
            m801["html_url"], f"https://github.com/{REPO}/issues/801"
        )
        self.assertIn(
            "fault at", json.dumps(m801["excerpt"]),
            "the excerpt must carry body text (the opening of the body)",
        )
        self.assertIsInstance(m801["gates"], list)
        self.assertIn(
            "src/core.py", json.dumps(m801["files_existing"]),
            "files_existing must list the member's resolved vocabulary path",
        )
        self.assertIn(
            "docs/nowhere.md", json.dumps(m801["files_missing"]),
            "files_missing must list the referenced-but-not-yet-existing"
            " path",
        )
        self.assertIn(
            "docs/nowhere.md", json.dumps(m801["not_in_vocabulary"]),
            "the off-vocabulary reference must be marked"
            " not_in_vocabulary",
        )
        self.assertIn("bug", json.dumps(m802["labels"]))

        # truncated flag semantics: a tiny budget cuts the payload
        env_tiny = _cli_json(
            "bundle", "--with-briefs", "--budget-tokens", "200",
            "--db", str(self.db_path), "--repo", REPO, "--format", "json",
        )
        self.assertTrue(env_tiny["ok"], env_tiny)
        self.assertIs(
            env_tiny["data"].get("truncated"),
            True,
            "a 200-token one-call payload must be cut and flagged"
            f" truncated=true (got {env_tiny['data'].get('truncated')!r})",
        )
        # (round 7) the 200-token envelope must be within its OWN budget:
        # a cut payload that still exceeds its budget has not been cut to
        # the budget.
        self.assertLessEqual(
            len(json.dumps(env_tiny).encode("utf-8")),
            200 * 4,
            "the 200-token one-call payload exceeds its own budget"
            " (chars/4 convention)",
        )
        # hard budget assertions (plan-critic R1): the 4000-token payload
        # must be WITHIN budget (chars/4) and cutting must actually reduce
        # the payload.
        self.assertLessEqual(
            len(json.dumps(env).encode("utf-8")),
            4000 * 4,
            "the 4000-token one-call payload exceeds its own budget"
            " (chars/4 convention)",
        )
        self.assertLess(
            len(json.dumps(env_tiny).encode("utf-8")),
            len(json.dumps(env).encode("utf-8")),
            "the 200-token payload must be strictly shorter than the"
            " 4000-token one (cutting must have occurred)",
        )

    def test_bundle_budget_sheds_brief_fields_before_bundles(self) -> None:
        """RED at the pre-fix implementation head 5fc2404, GREEN after the
        fix (Phase 4.5 implementation review finding 4). Under --with-briefs
        a budget that binds sheds the ENRICHED member fields - the
        convenience layer - BEFORE it drops a whole bundle: a member's
        number, title and triage scores are the evidence, its
        labels/author/dates/excerpt/file lists are not. The pre-fix order
        dropped whole bundles first, so a tight budget answered an empty
        bundles list while the cheap convenience fields were still intact on
        the rows it threw away (the pin's failure there was
        "AssertionError: [] is not true : the convenience brief fields must
        be shed BEFORE any whole bundle is dropped"). The size is also
        checked BEFORE each destructive step, so a payload already inside
        budget is never stripped. ERROR at the base, where --with-briefs did
        not exist yet (TypeError: unexpected keyword argument)."""
        from zaxbygraph import derive as _d

        conn = open_existing(self.db_path)
        try:
            roomy = _d.bundle(
                conn, REPO, with_briefs=True, budget_tokens=100_000,
            )
            # PR #81 review F-18: the flag must be PRESENT-and-False when
            # the budget does not bind, not merely falsy-by-default.
            self.assertIn(
                "truncated", roomy,
                "an above-floor budget payload must carry the truncated"
                " flag (PR #81 review F-18)",
            )
            self.assertIs(
                roomy["truncated"], False,
                "a 100k-token budget must not bind on this fixture"
                f" (got truncated={roomy.get('truncated')!r})",
            )
            bundles = roomy.get("bundles") or []
            self.assertTrue(
                bundles, "the fixture must produce a bundle over {801, 802}"
            )
            members = bundles[0].get("members") or []
            self.assertTrue(members, "the bundle must carry members")
            self.assertIn(
                "labels", members[0],
                "the roomy payload's members must carry the brief fields",
            )
            roomy_chars = len(
                json.dumps(roomy, sort_keys=True, ensure_ascii=True,
                           separators=(",", ":")).encode("utf-8")
            )
            # A budget one token under the roomy payload's own width binds
            # by a hair, so exactly the convenience layer has to go.
            tight = max(1, roomy_chars // 4 - 1)
            cut = _d.bundle(
                conn, REPO, with_briefs=True, budget_tokens=tight,
            )
        finally:
            conn.close()

        self.assertIs(
            cut.get("truncated"), True,
            "a budget below the payload's width must be flagged truncated",
        )
        cut_bundles = cut.get("bundles") or []
        self.assertTrue(
            cut_bundles,
            "the convenience brief fields must be shed BEFORE any whole"
            f" bundle is dropped (bundles={cut_bundles!r})",
        )
        cut_members = cut_bundles[0].get("members") or []
        self.assertTrue(cut_members, "the surviving bundle keeps its members")
        for member in cut_members:
            for key in ("labels", "author", "created_at", "updated_at",
                        "html_url", "excerpt", "files_existing",
                        "files_missing", "not_in_vocabulary"):
                self.assertNotIn(
                    key, member,
                    f"the shed convenience field {key!r} must be gone from a"
                    " budget-cut member",
                )
            for key in ("number", "title", "triage_total", "stage1"):
                self.assertIn(
                    key, member,
                    f"the evidence field {key!r} must survive budget cutting",
                )
        cut_chars = len(
            json.dumps(cut, sort_keys=True, ensure_ascii=True,
                       separators=(",", ":")).encode("utf-8")
        )
        self.assertLessEqual(
            cut_chars, tight * 4,
            "the cut payload must respect its own budget (chars/4)",
        )


# ==== C8: default rows are slim =============================================


class SlimRowsTests(_StoreTestCase):
    """Three plain open issues with NO reactions object and no milestone —
    every one of them has missing triage features, so at base each default
    row repeats the ``requires:`` sentences under scores.features_missing
    (three copies of the same sentences across rows)."""

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        # a merged PR supplies the vocabulary path the pair shares
        src.add_pr(
            issue(310, title="core history", body="touches src/core.py:1:1",
                  kind="pr", state="closed", created_at="2027-06-01T00:00:00Z"),
            _merged_pull(310, "2027-06-10T00:00:00Z"),
            files=[pr_file("src/core.py")],
        )
        for n in (301, 302, 303):
            src.add_issue(
                issue(n, title=f"plain report {n}", body="something broke",
                      created_at="2027-06-05T00:00:00Z")
            )
        # a bundle-able open pair sharing src/core.py (both resolve the path
        # through merged PR 310's vocabulary)
        for n in (311, 312):
            src.add_issue(
                issue(n, title=f"shared-file report {n}",
                      body="crash at src/core.py:1:1",
                      created_at="2027-06-05T00:00:00Z")
            )
        sync_repo(conn, src, REPO)

    def test_default_rows_are_slim(self) -> None:
        """RED at base, GREEN after the fix. Default triage rows are slim
        (number, title, labels, age, score, group scores, gates, top
        files): no per-row features_raw/features_normalized dumps, and the
        features_missing ``requires`` sentences appear at most ONCE at
        payload level (a legend), never repeated per row. ``--explain``
        restores the per-feature dumps per row. The MCP shape registry
        stays truthful for the slim default payload (its declared data_keys
        remain a subset of what the default call actually returns)."""
        env = _cli_json(
            "triage", "--db", str(self.db_path), "--repo", REPO,
            "--format", "json",
        )
        self.assertTrue(env["ok"], env)
        data = env["data"]
        rows = data["rows"]
        self.assertEqual(len(rows), 5, "fixture sanity: five open issues")
        for row in rows:
            blob = json.dumps(row)
            self.assertNotIn(
                "features_raw", blob,
                "default rows must not repeat the per-feature raw dumps",
            )
            self.assertNotIn(
                "features_normalized", blob,
                "default rows must not repeat the per-feature normalized"
                " dumps",
            )
            self.assertNotIn(
                "features_missing", blob,
                "the requires-sentences must live at payload level (a"
                " legend), not per row",
            )
            # the slim essentials survive
            for key in ("number", "title", "gates"):
                self.assertIn(key, row, f"slim rows keep {key!r}")
            self.assertIn("total_score", row, "slim rows keep the score")
            # (round 7) slim means FEWER fields, not none: the AC8 slim
            # list must be PRESENT by default — labels, an age signal, the
            # per-row group scores (all four group names under a "scores"
            # or "groups" mapping), gates (asserted above with the
            # essentials), and a top-files key.
            self.assertIn(
                "labels", row,
                f"slim rows keep labels (row keys: {sorted(row)})",
            )
            self.assertTrue(
                "age" in row or "created_at" in row,
                "slim rows keep an age signal ('age' or 'created_at';"
                f" row keys: {sorted(row)})",
            )
            scores = row.get("scores") if isinstance(row.get("scores"), dict) else None
            groups = row.get("groups") if isinstance(row.get("groups"), dict) else None
            self.assertTrue(
                scores is not None or groups is not None,
                "slim rows keep the per-row group scores as a mapping under"
                f" 'scores' or 'groups' (row keys: {sorted(row)})",
            )
            for group in ("demand", "leverage", "risk", "priority"):
                self.assertIn(
                    group, blob,
                    f"slim rows carry the group score {group!r} somewhere"
                    " in the row (scores/groups mapping)",
                )
            self.assertTrue(
                any("files" in key for key in row),
                "slim rows keep a top-files key (files/resolved_paths/"
                f"top_files) (row keys: {sorted(row)})",
            )
        # ONE payload-level legend: across the whole default payload the
        # features_missing sentences appear at most once.
        self.assertLessEqual(
            json.dumps(data).count('"features_missing"'),
            1,
            "the requires-sentences must appear at most once at payload"
            " level, not once per row",
        )
        self.assertNotIn(
            "features_raw", json.dumps(data),
            "the default payload carries no per-feature dumps at all;"
            " --explain is the surface that adds them",
        )

        # --explain adds the per-feature dumps per row
        env_x = _cli_json(
            "triage", "--explain", "--db", str(self.db_path), "--repo", REPO,
            "--format", "json",
        )
        self.assertTrue(env_x["ok"], env_x)
        rows_x = env_x["data"]["rows"]
        self.assertEqual(len(rows_x), 5)
        for row in rows_x:
            blob = json.dumps(row)
            self.assertIn(
                "features_raw", blob, "--explain restores per-row features_raw"
            )
            self.assertIn(
                "features_normalized", blob,
                "--explain restores per-row features_normalized",
            )

        # the MCP shape registry stays truthful for the slim default: every
        # declared data key is still answered by the default call.
        from zaxbygraph import mcp_server

        declared = set(mcp_server._TOOL_SHAPES["triage"]["data_keys"])
        answered = set(data)
        self.assertFalse(
            declared - answered,
            "the triage shape registry over-advertises keys the slim"
            f" default payload no longer carries: {sorted(declared - answered)}",
        )

        # bundle slim default (plan-critic R1: bundle coverage): default
        # bundle members stay lean (no per-member feature dumps), and
        # --with-briefs is the surface that enriches them (C7 pins its
        # fields; this leg pins that the DEFAULT stays slim).
        env_b = _cli_json(
            "bundle", "--db", str(self.db_path), "--repo", REPO,
            "--format", "json",
        )
        self.assertTrue(env_b["ok"], env_b)
        for bundle in env_b["data"].get("bundles", []):
            for member in bundle.get("members", []):
                blob = json.dumps(member)
                self.assertNotIn(
                    "features_raw", blob,
                    "default bundle members must stay lean",
                )
                self.assertNotIn(
                    "excerpt", blob,
                    "default bundle members must not carry briefs (the"
                    " --with-briefs surface adds them)",
                )
                # (round 7) the AC8 slim list applies to TRIAGE rows; the
                # default bundle member stays lean-but-not-hollow — it
                # keeps labels and gates alongside its number/title/score.
                if isinstance(member, dict):
                    for key in ("labels", "gates"):
                        self.assertIn(
                            key, member,
                            f"default bundle members keep {key!r}"
                            " (lean, not hollow; member keys:"
                            f" {sorted(member)})",
                        )


# ==== C9: brief carries decision fields =====================================


class BriefDecisionFieldsTests(_StoreTestCase):
    """901 (label bug, author alice, explicit created stamp) references the
    resolved src/core.py (vocabulary from merged PR 910) AND the
    off-vocabulary docs/guide/unknown.md."""

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        src.add_pr(
            issue(910, title="core history", body="touches src/core.py:1:1",
                  kind="pr", state="closed", created_at="2027-06-01T00:00:00Z"),
            _merged_pull(910, "2027-06-10T00:00:00Z"),
            files=[pr_file("src/core.py")],
        )
        src.add_issue(
            issue(
                901,
                title="Crash in core handler",
                body="crash at src/core.py:10:2; details in"
                     " docs/guide/unknown.md:3:1",
                author="alice",
                labels=[{"name": "bug", "color": "ff0000"}],
                created_at="2027-06-15T00:00:00Z",
            )
        )
        sync_repo(conn, src, REPO)

    def test_brief_carries_decision_fields(self) -> None:
        """RED at base, GREEN after the fix. The brief packet carries the
        decision fields labels / author / created_at, and the unresolved
        path references ship as STRINGS marked not_in_vocabulary (a
        files.unresolved list of path strings with a not_in_vocabulary
        marker, or a list of {path, not_in_vocabulary: true} entries) —
        not just an unresolved_count number."""
        env = _cli_json(
            "brief", "901", "--db", str(self.db_path), "--repo", REPO,
            "--format", "json",
        )
        self.assertTrue(env["ok"], env)
        data = env["data"]

        self.assertIn("bug", json.dumps(data.get("labels", [])),
                      "brief must carry the item's labels")
        self.assertEqual(data.get("author"), "alice",
                         "brief must carry the item's author")
        self.assertEqual(data.get("created_at"), "2027-06-15T00:00:00Z",
                         "brief must carry the item's created_at")

        # PR #81 review F-08: the packet carries the STORED values of
        # updated_at and html_url — never constant nulls (the row query
        # selects both columns; the keys can no longer be silently absent).
        self.assertIn("updated_at", data, "brief always emits updated_at")
        self.assertIn("html_url", data, "brief always emits html_url")
        self.assertTrue(
            data.get("updated_at"),
            f"brief's updated_at must carry the STORED value, not null"
            f" (got {data.get('updated_at')!r}; PR #81 review F-08)",
        )
        self.assertTrue(
            data.get("html_url"),
            f"brief's html_url must carry the STORED value, not null"
            f" (got {data.get('html_url')!r}; PR #81 review F-08)",
        )

        files = data.get("files")
        self.assertIsInstance(files, dict, "brief.files must be an object")
        self.assertIn(
            "src/core.py", json.dumps(files.get("resolved", [])),
            "the resolved path stays in files.resolved",
        )
        unresolved = files.get("unresolved")
        self.assertIsInstance(
            unresolved, list,
            f"files.unresolved must be a LIST of the unresolved path"
            f" strings (issue #70: only a count survives today); got"
            f" {unresolved!r}",
        )
        self.assertTrue(
            unresolved, "the fixture's docs/guide/unknown.md must appear"
                        " as an unresolved reference")
        # the strings + the not_in_vocabulary marking, in either accepted
        # shape: plain strings with a sibling marker, or marked dicts.
        if unresolved and all(isinstance(u, str) for u in unresolved):
            self.assertIn("docs/guide/unknown.md", unresolved)
            self.assertIn(
                "not_in_vocabulary", json.dumps(files),
                "string-shaped unresolved entries must be marked"
                " not_in_vocabulary at the files level",
            )
        else:
            entries = [u for u in unresolved if isinstance(u, dict)]
            self.assertTrue(entries, f"unrecognized unresolved shape: {unresolved!r}")
            match = [e for e in entries if e.get("path") == "docs/guide/unknown.md"]
            self.assertTrue(match, f"docs/guide/unknown.md missing: {unresolved!r}")
            self.assertIs(match[0].get("not_in_vocabulary"), True,
                          "each unresolved entry must be marked"
                          " not_in_vocabulary: true")


# ==== C10: evidence lists are informative ===================================


class EvidenceListsTests(_StoreTestCase):
    """Two issues 1101/1102 both referencing src/p.py and src/q.py — the
    two files merged PR 1110 co-changes, so the pair's co-change evidence
    carries the couple — plus filler issues carrying the COMMON terms
    (alpha, crash, src, ...) so the one DISTINCTIVE shared term
    (zebraunique, present only in 1101 and 1102) has strictly the highest
    IDF of the pair's shared vocabulary while alphabetical-first (alpha)
    does not."""

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        src.add_pr(
            issue(1110, title="history", body="", kind="pr", state="closed",
                  created_at="2027-06-01T00:00:00Z"),
            _merged_pull(1110, "2027-06-10T00:00:00Z"),
            files=[pr_file("src/p.py"), pr_file("src/q.py")],
        )
        src.add_issue(
            issue(1101, title="zebraunique alpha crash p",
                  body="zebraunique alpha crash report at src/p.py:1:1 and"
                       " src/q.py:1:1",
                  created_at="2027-06-15T00:00:00Z")
        )
        src.add_issue(
            issue(1102, title="zebraunique alpha crash q",
                  body="zebraunique alpha crash leak at src/p.py:2:1 and"
                       " src/q.py:2:1",
                  created_at="2027-06-15T00:00:00Z")
        )
        # fillers raise the document frequency of every COMMON shared term
        for n, word in ((1121, "alpha"), (1122, "crash"), (1123, "report"),
                        (1124, "leak")):
            src.add_issue(
                issue(n, title=f"filler {word}",
                      body=f"{word} filler alpha crash report leak src"
                           f" handler",
                      created_at="2027-06-15T00:00:00Z")
            )
        sync_repo(conn, src, REPO)

    def test_evidence_lists_are_informative(self) -> None:
        """RED at base, GREEN after the fix. shared_terms lead with the
        HIGHEST-WEIGHT term (the distinctive zebraunique, not the
        alphabetical-first alpha), and the co-change evidence lists each
        contributing file pair ONCE — no (a,b) + (b,a) duplicates from the
        symmetric coupling index."""
        from zaxbygraph import derive

        conn = open_existing(self.db_path)
        try:
            breakdown = derive.explain(conn, REPO, 1101, 1102)
        finally:
            conn.close()

        # ---- shared_terms: highest-weight first --------------------------
        lexical = breakdown["components"]["lexical"]
        shared = lexical["evidence"]["shared_terms"]
        self.assertTrue(shared, "the pair shares terms (fixture sanity)")
        self.assertIn("alpha", shared, "alpha IS a shared term (fixture)")
        self.assertIn("zebraunique", shared,
                      "the distinctive term IS a shared term (fixture)")
        self.assertEqual(
            shared[0], "zebraunique",
            f"shared_terms must lead with the HIGHEST-WEIGHT term, not the"
            f" alphabetical first (got {shared[0]!r}; full list {shared})",
        )

        # ---- co-change pairs listed once ---------------------------------
        cochange = breakdown["components"]["cochange"]
        pairs = cochange["evidence"]["pairs"]
        self.assertTrue(
            pairs,
            "the fixture's co-change (src/p.py, src/q.py) must contribute"
            " evidence pairs",
        )
        seen: set[frozenset[str]] = set()
        for entry in pairs:
            self.assertIsInstance(entry, dict, f"pair entries are dicts: {entry!r}")
            key = frozenset((entry["a"], entry["b"]))
            self.assertNotIn(
                key,
                seen,
                f"co-change evidence lists the unordered pair"
                f" {sorted((entry['a'], entry['b']))} more than once:"
                f" {pairs}",
            )
            seen.add(key)
        self.assertIn(
            frozenset(("src/p.py", "src/q.py")), seen,
            f"the co-changed couple must appear once: {pairs}",
        )


# ==== C11: the MCP bundle tool's default is the one-call selection ==========


class _McpCtx:
    """Minimal MCP server context for direct handler calls (the
    tests/test_feedback_regressions _Ctx pattern)."""

    default_repo = REPO
    default_db = None
    root = None
    modern = False
    roots_declared = False
    stale_after_s = 10 ** 9
    sync_runner = None


class McpBundleDefaultTests(_StoreTestCase):
    """The OneCallBundleTests geometry (bundle {801, 802} over src/core.py
    with labels and an off-vocabulary reference)."""

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        src.add_pr(
            issue(810, title="core fix", body="fixes the fault at src/core.py:1:1",
                  kind="pr", state="closed", created_at="2027-06-01T00:00:00Z"),
            _merged_pull(810, "2027-06-10T00:00:00Z"),
            files=[pr_file("src/core.py"), pr_file("tests/test_core.py")],
        )
        src.add_pr(
            issue(811, title="linked area", body="", kind="pr", state="closed",
                  created_at="2027-06-01T00:00:00Z"),
            _merged_pull(811, "2027-06-11T00:00:00Z"),
            files=[pr_file("src/linked.py")],
        )
        bug = [{"name": "bug", "color": "ff0000"}]
        src.add_issue(
            issue(801, title="Fault in core path one",
                  body="fault at src/core.py:5:2; see also docs/nowhere.md:1:1",
                  author="alice", labels=bug,
                  created_at="2027-06-15T00:00:00Z",
                  updated_at="2027-06-20T00:00:00Z")
        )
        src.add_issue(
            issue(802, title="Fault in core path two",
                  body="fault at src/core.py:6:2",
                  author="bob", labels=bug,
                  created_at="2027-06-16T00:00:00Z",
                  updated_at="2027-06-21T00:00:00Z")
        )
        sync_repo(conn, src, REPO)

    def test_mcp_bundle_default_is_one_call(self) -> None:
        """RED at base, GREEN after the fix. The MCP bundle tool called
        with NO arguments returns the ONE-CALL selection under a default
        budget: the result data carries bundles whose members carry the
        decision fields (labels, author, html_url, dates) and the payload
        carries the truncated flag. At base the no-args call answers the
        lean payload (members are number/title/score only) — the member
        decision fields are the discriminator."""
        from zaxbygraph import mcp_server

        ctx = _McpCtx()
        ctx.default_db = self.db_path
        ctx.root = os.getcwd()
        ctx.sync_runner = mcp_server._default_sync_runner
        result = mcp_server._TOOL_HANDLERS["bundle"]({}, ctx)
        env = result if isinstance(result, dict) else {}
        data = env.get("data") if isinstance(env.get("data"), dict) else env
        self.assertTrue(env.get("ok", True), f"no-args call must succeed: {env}")
        bundles = data.get("bundles") or []
        self.assertTrue(
            bundles, "the no-args default must answer bundles over the"
                     " {801, 802} pair"
        )
        members = bundles[0].get("members") or []
        self.assertTrue(members, "bundle carries members")
        member = members[0]
        self.assertIsInstance(member, dict)
        for field in ("labels", "author", "html_url", "created_at"):
            self.assertIn(
                field, member,
                f"MCP bundle members must carry the decision field"
                f" {field!r} on the default one-call payload (member:"
                f" {sorted(member)})",
            )
        self.assertIn(
            "bug", json.dumps(member["labels"]) if "labels" in member else "",
            "member labels carry the fixture label",
        )
        self.assertIn(
            "truncated", data,
            "the default one-call payload carries the budget truncated flag",
        )
        self.assertIsInstance(data["truncated"], bool)
        # the CONCRETE default budget (plan-critic R1): the no-args MCP
        # bundle applies the documented 4000-token default and the payload
        # under that default respects the chars/4 byte bound.
        self.assertEqual(
            data.get("budget_tokens"), 4000,
            "the no-args MCP bundle default budget must be the documented"
            f" 4000 tokens (got {data.get('budget_tokens')!r})",
        )
        self.assertLessEqual(
            len(json.dumps(env).encode("utf-8")), 4000 * 4,
            "the default one-call payload exceeds its own default budget"
            " (chars/4)",
        )


# ==== C12: the skill teaches the one-call workflow ==========================


class SkillOneCallDocsTests(unittest.TestCase):
    SKILL = REPO_ROOT / "skills" / "zaxbygraph" / "SKILL.md"

    def test_skill_teaches_one_call(self) -> None:
        """RED at base, GREEN after the fix. skills/zaxbygraph/SKILL.md
        teaches the one-call workflow (``bundle --with-briefs``) and the
        FIRST workflow-recipe section's body names it — the one-call recipe
        comes BEFORE the per-command triage -> bundle -> brief recipe,
        which still exists further down."""
        self.assertTrue(self.SKILL.exists(), "skills/zaxbygraph/SKILL.md missing")
        text = self.SKILL.read_text(encoding="utf-8")

        # the one-call surface is mentioned at all
        self.assertIn(
            "--with-briefs", text,
            "SKILL.md must teach the one-call workflow"
            " (`bundle --with-briefs`)",
        )
        self.assertIn(
            "bundle --with-briefs", text,
            "SKILL.md must name the one-call command form verbatim"
            " (`bundle --with-briefs`)",
        )

        # structural pin: the FIRST workflow-recipe section's body names
        # --with-briefs (the one-call recipe is taught first).
        lines = text.splitlines()
        headings = [
            (i, ln)
            for i, ln in enumerate(lines)
            if ln.startswith("#") and "recipe" in ln.lower()
        ]
        self.assertTrue(
            headings, "SKILL.md must carry workflow recipe section(s)"
        )
        first_i, first_heading = headings[0]
        end = headings[1][0] if len(headings) > 1 else len(lines)
        first_body = "\n".join(lines[first_i:end])
        self.assertIn(
            "--with-briefs", first_body,
            f"the FIRST workflow recipe section ({first_heading.strip()!r})"
            f" must teach the one-call workflow (--with-briefs); the"
            f" per-command recipe follows after it",
        )

        # the per-command chain still exists, AFTER the one-call recipe:
        # the ordered per-command step "**brief N**" appears later in the
        # document than the first --with-briefs mention.
        pos_onecall = text.find("--with-briefs")
        m = re.search(r"brief N", text)
        self.assertIsNotNone(
            m, "the per-command recipe (its `brief N` step) must still exist"
        )
        self.assertGreater(
            m.start(), pos_onecall,
            "the one-call workflow must be taught BEFORE the per-command"
            " triage -> bundle -> brief recipe",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
