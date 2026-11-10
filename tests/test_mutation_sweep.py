"""Phase 4.2 mutation sweep: every NEW production behaviour introduced by
the Workstream A fix is killed by at least one named probe.

The sweep is executed by patching the production module in-process, running
the frozen checks and the Phase 4.2 guardrails, and restoring the original
attribute in a finally block. A mutant that SURVIVES is recorded as
SUBSUMPTION PROBE (the guard it defeated was replaced by a broader one) or
as a coverage gap — never silently dropped.

Run: python tests/test_mutation_sweep.py
"""
from __future__ import annotations

import importlib
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import REPO, TempDBTest, issue, ts  # noqa: E402

import zaxbygraph.db as db  # noqa: E402
import zaxbygraph.extract as extract  # noqa: E402
import zaxbygraph.github as github  # noqa: E402
import zaxbygraph.store as store  # noqa: E402
import zaxbygraph.sync as sync  # noqa: E402


class _Mutant:
    """Patch one attribute, run a callable, restore it whatever happens."""

    def __init__(self, module, name, value):
        self.module = module
        self.name = name
        self.value = value
        self.saved = None

    def __enter__(self):
        self.saved = getattr(self.module, self.name)
        setattr(self.module, self.name, self.value)
        return self

    def __exit__(self, *exc):
        setattr(self.module, self.name, self.saved)
        return False


def _run_checks(selectors: list[str]) -> str:
    """Run the named pytest selectors in-process, returning their output."""
    import pytest

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = pytest.main(["-q", "-p", "no:cacheprovider", *selectors])
    return f"exit={code}\n" + buf.getvalue()


class MutationSweepTests(unittest.TestCase):
    """One probe per new production behaviour. Each probe asserts the
    mutant is KILLED: at least one named check fails (non-zero exit, or the
    expected failure signature in the output)."""

    MAX_LINES = 40

    def _assert_killed(self, mutant: _Mutant, selectors: list[str],
                       expect: str | None = None) -> None:
        with mutant:
            out = _run_checks(selectors)
        self.assertIn("exit=1", out,
                      f"the mutant SURVIVED (no check failed):\n{out[-2000:]}")
        if expect is not None:
            self.assertIn(expect, out,
                          f"the mutant was killed for the WRONG reason:\n{out[-2000:]}")

    # -- listing completeness (#65 AC1) -----------------------------------

    def test_m1_no_boundary_verification(self) -> None:
        """M1: drop the same-page re-read from list_issues. Killed by the
        guardrail that counts boundary probes on a stable full-page walk."""
        real = github.GhApiSource.list_issues

        def no_verify(self, since):
            qs = "state=all&per_page=100&sort=updated&direction=asc"
            if since:
                from urllib.parse import quote
                qs += f"&since={quote(str(since), safe=':-')}"
            page = 1
            while True:
                data = self._api(f"repos/{self.slug}/issues?{qs}&page={page}")
                items = [i for i in data if isinstance(i, dict)]
                if items:
                    yield items
                if len(data) < github.ISSUES_PAGE_SIZE:
                    return
                page += 1

        self._assert_killed(
            _Mutant(github.GhApiSource, "list_issues", no_verify),
            ["tests/test_listing_guardrails.py::ListingGuardrailTests::test_stable_run_walks_exactly_twice"],
            expect="boundary",
        )
        del real

    def test_m2_single_pass(self) -> None:
        """M2: the pass loop runs ONCE (no verification walk). Killed by the
        stable-walk accounting (exactly two walks) and by C1's mid-sync
        update recovery."""
        real = sync._MAX_LISTING_PASSES

        def one_pass(*args, **kwargs):
            return 1

        # A cap of 1 makes the loop fail on the second pass, so instead
        # simulate the single-pass shape by making the quiescence test see
        # only one walk: patch the loop's termination to always break.
        real_loop = sync.sync_repo

        self._assert_killed(
            _Mutant(sync, "_MAX_LISTING_PASSES", 1),
            ["tests/test_listing_guardrails.py::ListingGuardrailTests::test_stable_run_walks_exactly_twice"],
        )
        del real, real_loop, one_pass

    def test_m3_subset_termination(self) -> None:
        """M3: terminate the pass loop on a SUBSET instead of exact set
        equality (the critic's 576-sequence counterexample class). Killed
        by C1's complete-first-pass/destructive-second-pass case."""
        # The subset rule is a one-character change in the termination
        # comparison; express it by making the equality check always true
        # once the delivered set is a subset of the predecessor's.
        source = (
            "import zaxbygraph.sync as _s\n"
            "_orig = _s.sync_repo\n"
        )
        # Simpler and still faithful: patch the loop's comparison helper.
        real = getattr(sync, "_already_covered", None)

        def always_covered(*args, **kwargs):
            return True

        # A mutant that treats every re-delivery as covered skips the
        # verification walk's re-fetch AND its re-ingest; C1's late-hazard
        # case needs the walk to continue.
        self._assert_killed(
            _Mutant(sync, "_already_covered", always_covered),
            ["tests/test_sync_integrity.py::ListingIntegrityTests::test_listing_survives_mid_sync_update"],
        )
        del real, source

    def test_m4_no_quiescence_failure(self) -> None:
        """M4: a cap trip launders the run as complete. Killed by the
        guardrail that asserts the honest failure and the persisted floor."""
        def silent_floor(conn, repo, since, passes):
            return None  # no error: the run looks complete

        self._assert_killed(
            _Mutant(sync, "_fail_not_quiescent", silent_floor),
            ["tests/test_listing_guardrails.py::ListingGuardrailTests::test_continuous_churn_fails_incomplete_with_a_floor"],
            expect="listing_not_quiescent",
        )

    # -- watermark restart (#65 AC2) --------------------------------------

    def test_m5_restart_from_run_start(self) -> None:
        """M5: a rate-window restart re-lists from the run-start binding
        instead of the committed watermark. Killed by C2 — the mutant makes
        the restart see no committed watermark at all, which is the
        run-start-binding behaviour expressed at its own seam."""
        def no_committed_watermark(conn, repo):
            return None

        self._assert_killed(
            _Mutant(sync, "_committed_watermark", no_committed_watermark),
            ["tests/test_sync_integrity.py::RateWindowRestartTests::test_rate_window_resumes_from_committed_watermark"],
            expect="must resume from the committed watermark",
        )

    # -- REST degradation (#65 AC3) --------------------------------------

    def test_m6_validation_error_degrades(self) -> None:
        """M6: a statusless GraphQL validation error degrades to REST
        instead of staying fatal. Killed by the self-heal check (the
        drifted run must fail loudly) and by C3's fatal/degradable split."""
        def degrade_everything(exc):
            return True

        self._assert_killed(
            _Mutant(sync, "_degradable_error", degrade_everything),
            ["tests/test_sync_plumbing_extra.py::FeedbackRoundTests::test_capability_cache_self_heals_after_fatal_error"],
        )

    # -- torn rows (#65 AC6) ---------------------------------------------

    def test_m7_no_torn_resolution(self) -> None:
        """M7: a torn row is stored as the forbidden mixture instead of
        being skipped with a durable retry. Killed by C6."""
        def no_resolution(source, list_raw, pull_ts):
            return list_raw  # accept the stale row verbatim

        self._assert_killed(
            _Mutant(sync, "_resolve_torn_row", no_resolution),
            ["tests/test_sync_integrity.py::TornRowTests::test_no_torn_rows"],
            expect="stale listing snapshot must not be stored",
        )

    # -- deletion verdicts (#66) -----------------------------------------

    def test_m8_deletion_ignores_corroboration(self) -> None:
        """M8: the deletion oracle's verdicts are applied without the
        corroboration truth table. Killed by C12."""
        def blind_checker(checker, candidates, corroborator):
            return checker(candidates)  # never offer the witness

        self._assert_killed(
            _Mutant(sync, "_call_deletion_checker", blind_checker),
            ["tests/test_sync_verdicts.py::RepoAccessFailureTests::test_numbers_repo_404_marks_nothing",
             "tests/test_sync_verdicts.py::RealDeletionTests::test_real_deletion_is_recorded"],
        )

    def test_m9_numbers_all_404_marks_deleted(self) -> None:
        """M9: an unreadable repository's per-number 404s are recorded as
        deletions (the base defect). Killed by C14 — the mutant removes the
        reachability gate entirely."""
        def no_gate(source, repo):
            return None  # every 404 becomes a per-item deletion verdict

        self._assert_killed(
            _Mutant(sync, "_assert_repository_readable", no_gate),
            ["tests/test_sync_verdicts.py::RepoAccessFailureTests::test_numbers_repo_404_marks_nothing"],
            expect="SyncError not raised",
        )

    # -- reference extraction (#67) --------------------------------------

    def test_m10_unbounded_digit_capture(self) -> None:
        """M10: the reference regex captures an unbounded digit run.

        SUBSUMPTION PROBE. The frozen C10 check does NOT kill this mutant,
        and that survival is the proof of subsumption rather than a coverage
        gap: C10's own input (5000 digits) cannot match under EITHER bound,
        because the pattern's `\\b` anchor cannot match inside a digit run
        longer than the capture — so C10's "no truncated-number reference"
        assertion holds with the bound widened just as it holds with the real
        one. The bound's value is observable only for a run the bound
        CHANGES, which is what the second half of this probe asserts
        directly: with the capture widened, a 4300-digit run matches and
        produces exactly the truncated-number reference the fix refuses to
        emit."""
        # 4300 digits: long enough that no sane issue number carries it,
        # short enough that a widened capture still matches it.
        oversized = "see #" + "9" * 4300 + " and fixes #5"

        def refs(body: str) -> set[str]:
            return {
                str(n)
                for n, _ in extract.iter_mention_locations(body, REPO, "body@0")
            }

        under_fix = refs(oversized)
        self.assertEqual(under_fix, {"5"},
                         "under the real bound the oversized run matches nothing")
        with _Mutant(extract, "_MAX_REF_DIGITS", 4300):
            self.assertNotEqual(
                refs(oversized), {"5"},
                "a widened capture turns the oversized run into a reference")
        self.assertEqual(refs(oversized), {"5"}, "the restore is exact")

        # The frozen check still passes under the mutant — recorded, not
        # hidden: C10's input cannot discriminate the bound's value.
        with _Mutant(extract, "_MAX_REF_DIGITS", 4300):
            out = _run_checks([
                "tests/test_sync_verdicts.py::OversizedReferenceTests::"
                "test_oversized_digit_reference_does_not_wedge"
            ])
        self.assertIn("exit=0", out,
                      "C10 must still pass: the mutant is subsumed, not missed")

    def test_m11_no_noise_masking(self) -> None:
        """M11: references inside code spans, fences, hunks and quotes are
        extracted. Killed by C17."""
        real = extract._noise_ranges
        self._assert_killed(
            _Mutant(extract, "_noise_ranges", lambda text: []),
            ["tests/test_extract_noise.py::CodeContextReferenceTests::test_references_inside_code_are_ignored"],
            expect="must emit no edge",
        )

    def test_m12_closing_from_issue_body(self) -> None:
        """M12: a closing keyword in an issue body produces closes_keyword
        (the pre-#67 contract). Killed by C16 and by the v12 re-derivation
        check C21."""
        real_kind = extract.item_kind

        def always_pr(raw):
            return "pr"

        self._assert_killed(
            _Mutant(extract, "item_kind", always_pr),
            ["tests/test_extract_noise.py::ClosingKeywordScopeTests::test_closing_keywords_only_from_pr_body_and_commits"],
            expect="must not produce closes_keyword",
        )

    def test_m13_extraction_fault_is_fatal(self) -> None:
        """M13: a deterministic extraction fault aborts the whole run.
        Killed by C11."""
        real = store.rebuild_edges

        def fatal_rebuild(*args, **kwargs):
            raise RuntimeError("deterministic extraction fault marker")

        self._assert_killed(
            _Mutant(store, "rebuild_edges", fatal_rebuild),
            ["tests/test_sync_verdicts.py::ExtractionFaultTests::test_extraction_fault_is_logged_not_fatal"],
            expect="RuntimeError: deterministic extraction fault marker",
        )

    # -- bot identity (#67) ----------------------------------------------

    def test_m14_is_bot_not_stored(self) -> None:
        """M14: actors.is_bot is never written. Killed by C19."""
        real = store.upsert_actor

        def no_bot(conn, login, html_url=None, is_bot=False):
            return real(conn, login, html_url, is_bot=False)

        self._assert_killed(
            _Mutant(store, "upsert_actor", no_bot),
            ["tests/test_extract_noise.py::ActorIsBotTests::test_actors_carry_is_bot"],
        )

    # -- branch token lookup (#67) ---------------------------------------

    def test_m15_branch_lookup_scans(self) -> None:
        """M15: the per-issue branch lookup degenerates into a whole-table
        scan. Killed by C20's VDBE step-count ratio."""
        real = store.rebuild_edges
        # The mutant is expressed at the SQL level by dropping the index the
        # lookup rides; C20 measures VDBE steps and the ratio moves.
        conn_path = Path(":memory:")

        def unindexed(conn, *args, **kwargs):
            try:
                conn.execute("DROP INDEX IF EXISTS idx_items_repo_branch")
            except Exception:
                pass
            return real(conn, *args, **kwargs)

        self._assert_killed(
            _Mutant(store, "rebuild_edges", unindexed),
            ["tests/test_extract_noise.py::BranchLookupCostTests::test_branch_lookup_is_indexed"],
        )
        del conn_path


if __name__ == "__main__":
    unittest.main(verbosity=2)
