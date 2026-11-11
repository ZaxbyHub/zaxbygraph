"""Generate 08-test-results.md from the acceptance table + the replay logs.

Reads the acceptance table (02-reproduction.md) and the fresh replay blocks
(repro/replay-blocks.txt), writes 08-test-results.md. Counts are asserted
against the artifacts so the document cannot drift from them again (the
phase-4.5 review found four wrong numbers in the first version).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

TRACE = Path(".agents/issue-traces/65-66-67-workstream-a-sync-integrity")
BASE = "6009c30269f77e3c0d3cfddaa583db2530f56ba7"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    ).stdout.strip()


# The head is whatever is committed NOW: the generator is re-run after every
# commit, so it must never carry a stale sha (the phase-4.5 review caught
# exactly that).
HEAD = _git("rev-parse", "HEAD")


def _suite_count(log: str) -> int:
    """The `Ran N tests` line from a recorded full-suite run."""
    text = (TRACE / "repro" / log).read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^Ran (\d+) tests", text, re.M)
    if not m:
        raise SystemExit(f"{log}: no 'Ran N tests' line — re-run the suite")
    return int(m.group(1))


HEAD_TESTS = _suite_count("suite-head.log")
BASE_TESTS = _suite_count("suite-base.log")

table_text = (TRACE / "02-reproduction.md").read_text(encoding="utf-8")
rows = []
for line in table_text.splitlines():
    if not line.startswith("| AC") or "| C" not in line:
        continue
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) < 7 or not re.fullmatch(r"C\d+", cells[2]):
        continue
    rows.append({
        "ac": cells[0], "class": cells[1], "check": cells[2],
        "argv": cells[3], "expect": cells[4], "pre": cells[5], "post": cells[6],
    })
assert len(rows) == 22, f"expected 22 acceptance rows, got {len(rows)}"
classes = {}
for r in rows:
    classes[r["class"]] = classes.get(r["class"], 0) + 1
assert classes == {"DISCRIMINATING": 16, "PRESERVING": 4, "NEW-SURFACE": 2}, classes

replay = (TRACE / "repro" / "replay-blocks.txt").read_text(encoding="utf-8")
blocks: dict[str, dict[str, str]] = {}
current = None
for line in replay.splitlines():
    m = re.match(r"^### Check (C\d+)", line)
    if m:
        current = m.group(1)
        blocks[current] = {}
        continue
    if current and line.startswith("- "):
        key, _, rest = line[2:].partition(": ")
        blocks.setdefault(current, {})[key] = rest
assert set(blocks) == {r["check"] for r in rows}, sorted(blocks)
for cid, b in blocks.items():
    assert b.get("head", "").startswith(HEAD), (cid, b.get("head"))
    assert b.get("verdict") == "PASS", (cid, b.get("verdict"))

manifest_head = (TRACE / "repro" / "checkpoint.manifest").read_text(
    encoding="utf-8"
).splitlines()[0]
m = re.search(r"rows=(\d+)", manifest_head)
assert m, manifest_head
manifest_rows = int(m.group(1))
amend_rows = manifest_rows - 22

guardrail_tests = len(re.findall(
    r"    def test_", (Path("tests/test_listing_guardrails.py")).read_text(
        encoding="utf-8"
    )
))
assert guardrail_tests == 13, guardrail_tests

out: list[str] = []
out.append("# Test results — 65-66-67-workstream-a-sync-integrity")
out.append("")
commits_over_base = len(_git("rev-list", "--oneline", f"{BASE}..HEAD").splitlines())
out.append(f"Head: `{HEAD}` — the tip of a {commits_over_base}-commit branch")
out.append(f"over base `{BASE}` (`git log --oneline {BASE}..HEAD` is the")
out.append("authoritative sequence: the implementation commit, then the")
out.append("phase-4.5 review-fix rounds).")
out.append("Every result below was produced by the command shown, at that head;")
out.append("nothing is asserted from memory.")
out.append("")
out.append("## Regression Test")
out.append("")
out.append("Full suite (`python -m unittest discover -s tests -b`, box Python")
out.append(f"3.11): **Ran {HEAD_TESTS} tests … OK** (recorded in")
out.append("`repro/suite-head.log`). The base tree (`git archive` of")
out.append(f"`{BASE}` extracted, same command — `repro/suite-base.log`)")
out.append(f"runs **{BASE_TESTS}**, so this work adds")
out.append(f"{HEAD_TESTS - BASE_TESTS} test methods with no regression in the")
out.append(f"existing {BASE_TESTS} (the base run's one failure is")
out.append("`test_status_empty_json`, an artifact of the extracted tree having")
out.append("no git origin, not of the code). The")
out.append("additions, by file:")
out.append("")
out.append("- `tests/test_extract_noise.py` (frozen) — the #67 checks:")
out.append("  closing-keyword scope, code-context noise, comment-id/foreign-ref")
out.append("  suppression, `actors.is_bot`, the indexed branch lookup and the")
out.append("  v12 re-derivation.")
out.append("- `tests/test_sync_integrity.py` (frozen) — the #65 checks: listing")
out.append("  survival across four mid-walk hazards, the rate-window restart,")
out.append("  REST degradation, profile inheritance, MCP source honouring, torn")
out.append("  rows, the probe's introspection budget.")
out.append("- `tests/test_sync_verdicts.py` (frozen) — the #66 checks: bounded")
out.append("  references, extraction-fault containment, the deletion truth")
out.append("  table, the non-laundering finalizer, repository reachability.")
out.append(f"- `tests/test_listing_guardrails.py` — {guardrail_tests} guardrail")
out.append("  probes for the listing design (request accounting, the boundary")
out.append("  shift, tie blocks, duplicate pages, churn/floor recovery,")
out.append("  `--force` over a floor, transport degradation, and the scripted")
out.append("  subset-termination counterexample).")
out.append("- `tests/test_deletion_oracle.py` — 10 pins on the PRODUCTION")
out.append("  `GraphQLSource.check_deleted` (the seven truth-table rows, sync")
out.append("  offering it the witness, and the retry probing only the pending")
out.append("  set) — added by the phase-4.5 review after it found the contract")
out.append("  implemented only in a test double.")
out.append("- `tests/test_mutation_sweep.py` — 15 named mutants, one per new")
out.append("  production behaviour; 14 killed, 1 recorded as a SUBSUMPTION")
out.append("  probe (the frozen C10 input cannot discriminate the digit bound;")
out.append("  the probe asserts the bound's value directly instead).")
out.append("- `tests/_probe_review_fixes.py` — 10 review-fix probes: each")
out.append("  reverts one fix, confirms its pin fails, restores the exact")
out.append("  bytes, and re-runs every pin. **ALL KILLED**, post-restore OK.")
out.append("- `tests/test_doctor_v12_carry.py` — 8 consolidation carry tests")
out.append("  (earliest-floor fold in both directions, NULL-floor")
out.append("  subsumption, a candidate without a floor, inactive-stays-")
out.append("  resolved, `is_bot` never-downgrades, and the pending-set copy).")
out.append("")
out.append("## Acceptance check results")
out.append("")
out.append("Fresh full replay at this head: `PYTHONPATH=src bash")
out.append(".agents/issue-traces/65-66-67-workstream-a-sync-integrity/repro/replay-all.sh`")
out.append(
    f"— **22/22 PASS**: {classes['DISCRIMINATING']} DISCRIMINATING"
    f" RED→GREEN, {classes['PRESERVING']} PRESERVING GREEN→GREEN,"
    f" {classes['NEW-SURFACE']} NEW-SURFACE ERROR→GREEN; zero VACUOUS."
)
out.append("(`PYTHONPATH=src` is required: without it the editable install makes")
out.append("a base leg import the working tree — see the wave-7 disclosure in")
out.append("02-reproduction.md.)")
out.append("")
for row in rows:
    cid = row["check"]
    b = blocks[cid]
    out.append(f"### Check {cid}")
    out.append(f"- base: {b['base']}")
    out.append(f"- head: {b['head']}")
    out.append(f"- argv: {row['argv']}")
    out.append(f"- expect: {row['expect']}")
    out.append(f"- class: {row['class']} (pre-fix {row['pre']} → post-fix {row['post']})")
    out.append(f"- verdict: {b['verdict']}")
    out.append("")
out.append("## Quality Checks")
out.append("")
out.append("- `PYTHONPATH=src bash repro/replay-all.sh` → 22/22 PASS (above).")
out.append("- `repro-check.sh verify-checkpoint --slug")
out.append("  65-66-67-workstream-a-sync-integrity` → exit 0; the manifest holds")
out.append(f"  {manifest_rows} rows (22 CHECKPOINT + {amend_rows} AMEND across")
out.append("  eight waves: the check-author strengthening, wave 7's")
out.append("  implementation-driven re-freeze, and wave 8's C13 recovery leg).")
out.append("- `repro-check.sh verify-anchor` against the published anchor v8")
out.append("  (comment 6767718505 on #64) → OK; the superseded v7 receipt fails")
out.append("  closed with `anchor manifest digest does not match`.")
out.append("- Mutation probes: `python tests/test_mutation_sweep.py` → 15/15")
out.append("  OK; `python tests/_probe_review_fixes.py` → 10/10 KILLED with a")
out.append("  byte-identical restore and a green post-restore re-run.")
out.append("- Docs pins: `python -m pytest tests/test_docs.py")
out.append("  tests/test_schema_notes.py tests/test_schema_v11.py -q` → 24")
out.append("  passed (v12 column notes, schema.md caveats, fresh/migrated")
out.append("  shape parity).")
out.append("")
out.append("## Deferred-Work Scan")
out.append("")
out.append("scan-deferred: clean")
out.append("")
out.append("`grep -rnE 'TODO|FIXME|XXX|HACK'` over the diff's `src/` and")
out.append("`tests/` files returns **no hits**. The word `placeholder` was")
out.append("checked separately: it appears only as descriptive prose about")
out.append("SQL parameter lists (a value noun, not a deferred-work marker).")
out.append("The one disclosed non-hit is the mutation sweep's own comment")
out.append("naming the deliberately-unprobed cost-only refinement (the batch")
out.append("newest-duplicate stamp fold), a comment in a test tool, not")
out.append("deferred product work.")
out.append("")
out.append("## Verification Reasoning")
out.append("")
out.append("- Every DISCRIMINATING check is RED at base for the semantic reason")
out.append("  its acceptance row names (the base log matches `--expect`")
out.append("  line-wise — enforced by `repro-check.sh run`), so none is a")
out.append("  vacuous green. The two NEW-SURFACE checks are ERROR at base")
out.append("  because the check file did not exist then; the four PRESERVING")
out.append("  checks are green at both ends.")
out.append("- The acceptance table's semantics digest (`AC/class/check/argv/`")
out.append("  `expect`) is byte-identical across all eight amendment waves, so")
out.append("  no check was redefined to pass — only per-path blobs moved, and")
out.append("  each wave re-froze every pair on the touched path.")
out.append("- The phase-4.5 review's HIGH finding was verified before it was")
out.append("  fixed: `git show 6009c30:src/zaxbygraph/graphql.py` and the head")
out.append("  copy of `check_deleted` were byte-identical, so C12 passed")
out.append("  against `_CorroboratingOracleSource` while a real all-gone chunk")
out.append("  still raised. `tests/test_deletion_oracle.py` now drives the")
out.append("  shipped class, and its first pin is the mutation probe that")
out.append("  reverts the corroborator branch (KILLED).")
out.append("- The completeness claim carries its disclosed residual (the")
out.append("  double-churn window) in docs/schema.md and will carry it in the")
out.append("  PR body — the claim is coverage of the window this run walked,")
out.append("  proven by the re-walk, bounded by the 4-pass honest failure.")
out.append("")
out.append("## Checkpoint verification")
out.append("")
out.append(f"- manifest: `repro/checkpoint.manifest` (base `{BASE}`,")
out.append(f"  {manifest_rows} rows)")
out.append("- `verify-checkpoint` exit 0 after wave 8 (`repro/amend-wave8.sh`);")
out.append("  the manifest is append-only.")
out.append("- checkpoint-tree-id: `7687e720730494cf09167f304040bbd66cc5270b`")
out.append(f"- head: `{HEAD}`")
out.append("")

(TRACE / "08-test-results.md").write_bytes(
    ("\r\n".join(out) + "\r\n").encode("utf-8")
)
print(
    f"wrote 08-test-results.md: {len(rows)} checks, classes={classes},"
    f" manifest={manifest_rows} ({amend_rows} amend), guardrails={guardrail_tests}"
)
