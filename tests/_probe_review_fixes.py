"""Phase-4.5 review-fix mutation probes: revert each fix in the working
tree, confirm the pin that covers it FAILS, then restore the exact bytes.

Each probe is (label, path, shipped_form, reverted_form, occurrences,
selectors): the script asserts the shipped form occurs exactly that many
times, writes the revert, runs the selectors, restores the original bytes,
and finally re-runs every pin to prove the restore.

One shipped refinement is deliberately NOT probed here: the batch-level
newest-duplicate stamp fold in `_batch_fully_covered` (a cost-only
decision — whether the page's bulk children call is skipped — whose
per-ROW counterpart, `_already_covered`, is pinned by the replay tests).
Disclosed in the trace rather than silently dropped.

Run: python tests/_probe_review_fixes.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (label, file, shipped_form, reverted_form, occurrences, selectors)
PROBES = [
    (
        "floor only raises a non-NULL watermark",
        "src/zaxbygraph/sync.py",
        "elif since is not None and floor < since:",
        "elif since is None or floor < since:",
        1,
        [
            "tests/test_listing_guardrails.py::ListingGuardrailTests::"
            "test_next_run_drains_from_a_named_floor",
            "tests/test_listing_guardrails.py::ListingGuardrailTests::"
            "test_force_drains_from_the_beginning_over_an_active_floor",
        ],
    ),
    (
        "targeted finalizer consults the pending set",
        "src/zaxbygraph/store.py",
        "last_error = CASE WHEN :ingested > 0 AND :pending_open = 0\n"
        "                            THEN NULL ELSE last_error END,",
        "last_error = CASE WHEN :ingested > 0 THEN NULL ELSE last_error END,",
        1,
        [
            "tests/test_sync_plumbing_extra.py::FeedbackRoundTests::"
            "test_targeted_ingest_keeps_open_deletion_error",
        ],
    ),
    (
        "transport failures are typed (github.py)",
        "src/zaxbygraph/github.py",
        "raise GitHubTransportError(",
        "raise GitHubError(",
        3,  # the launch site, the timeout site, and the OSError catch-all
        [
            "tests/test_github.py::TransportTypingTests::"
            "test_launch_failure_is_typed_on_the_rest_path",
        ],
    ),
    (
        "transport failures are typed (graphql.py)",
        "src/zaxbygraph/graphql.py",
        "raise GitHubTransportError(",
        "raise GitHubError(",
        3,  # the launch site, the timeout site, and the OSError catch-all
        [
            "tests/test_github.py::TransportTypingTests::"
            "test_launch_failure_is_typed_on_the_graphql_path",
        ],
    ),
    (
        "doctor folds the recovery floor",
        "src/zaxbygraph/doctor.py",
        "active = store_floor_active or cand_floor_active",
        "active = False  # PROBE: the fold never fires",
        1,
        ["tests/test_doctor_v12_carry.py::FloorCarryTests"],
    ),
    (
        "doctor never downgrades is_bot",
        "src/zaxbygraph/doctor.py",
        '" is_bot = MAX(COALESCE(actors.is_bot, 0), excluded.is_bot)",',
        '" is_bot = excluded.is_bot",',
        1,
        [
            "tests/test_doctor_v12_carry.py::ActorBotCarryTests::"
            "test_consolidation_never_downgrades_a_known_bot",
        ],
    ),
    (
        "pass termination is exact set equality",
        "src/zaxbygraph/sync.py",
        "if pass_numbers == prev_pass_numbers:",
        "if prev_pass_numbers is not None and pass_numbers <= prev_pass_numbers:",
        1,
        ["tests/test_listing_guardrails.py::PassTerminationTests"],
    ),
    (
        "a pending 404 resolves the obligation",
        "src/zaxbygraph/sync.py",
        "if status in (404, 410):",
        "if False:  # PROBE: the pending 404 is never resolved",
        1,
        [
            "tests/test_sync_plumbing_extra.py::FeedbackRoundTests::"
            "test_torn_pending_number_gone_resolves_the_obligation",
        ],
    ),
    (
        "the PRODUCTION oracle consults the corroborator",
        "src/zaxbygraph/graphql.py",
        "if callable(corroborator):",
        "if False and callable(corroborator):  # PROBE: base behaviour",
        1,
        ["tests/test_deletion_oracle.py::RealOracleTruthTableTests"],
    ),
    (
        "a later run retries only the pending set",
        "src/zaxbygraph/sync.py",
        "                    derive_new=started_full,",
        "                    derive_new=True,  # PROBE: re-derive from absence",
        1,
        [
            "tests/test_deletion_oracle.py::PlainRunRetryShapeTests::"
            "test_plain_run_never_derives_candidates_from_window_absence",
        ],
    ),
]


def run(selectors: list[str]) -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         *selectors],
        capture_output=True, cwd=str(ROOT),
    )
    return proc.returncode


def main() -> int:
    # These probes shell out to pytest: without it every revert would
    # "kill" on a missing-module exit, which is how a no-pytest run once
    # reported false KILLs (PR #76 review PRR-002). Fail closed with a
    # named reason instead.
    import importlib.util

    if importlib.util.find_spec("pytest") is None:
        print(
            "RESULT: BLOCKED — pytest is not installed; the probes need it "
            "to run the pinned tests (pip install pytest)"
        )
        return 2
    problems: list[str] = []
    for label, rel, shipped, reverted, occurrences, selectors in PROBES:
        path = ROOT / rel
        original = path.read_bytes()
        text = original.decode("utf-8")
        found = text.count(shipped)
        if found != occurrences:
            print(f"SKIP    {label}: shipped form count={found},"
                  f" expected {occurrences}")
            problems.append(f"{label} (pattern)")
            continue
        try:
            path.write_bytes(text.replace(shipped, reverted).encode("utf-8"))
            code = run(selectors)
        finally:
            path.write_bytes(original)
        status = "KILLED  " if code != 0 else "SURVIVED"
        if path.read_bytes() != original:
            status = "RESTORE-FAILED"
        print(f"{status} {label} (revert exit={code})")
        if status != "KILLED  ":
            problems.append(label)
    print("--- post-restore re-run of every pin")
    all_selectors = sorted({s for *_, sels in PROBES for s in sels})
    code = run(all_selectors)
    print(f"{'OK' if code == 0 else 'BROKEN'} post-restore (exit={code})")
    if code != 0:
        problems.append("post-restore")
    print("RESULT:", "ALL KILLED" if not problems else f"PROBLEMS: {problems}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
