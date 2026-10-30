"""Supplementary arm's-length checks for issue #30 (plan critic round 1,
C13-C17) — NEW FILE alongside tests/test_triage_bundle.py, authored against
the REVISED design spec (.agents/issue-traces/30-triage-bundle-brief/
04-root-cause.md sections 2/3/8) before any implementation.

Frozen check names:

    C13 test_bundle_exclude_removes_pool_members
    C14 test_open_offset_and_enrichment
    C15 test_derived_commands_never_write
    C16 test_brief_live_gh_success_leg
    C17 test_cochange_cutoff_excludes_post_cutoff_support
    C18 test_candidate_side_comment_bait_scoped          (wave-3)
    C19 test_bundle_pool_exclude_invalid_values_are_coded (feedback round)
    C20 test_bundle_budget_caps_whole_payload            (feedback round)
    C21 test_brief_bare_issue_budget_bounded            (review round 2)

NEW-SURFACE module: collects cleanly at base (the derive symbols that arrive
with the fix are reached via getattr inside test bodies or through the
subprocess CLI); every check fails at the new-surface boundary at base and
must pass after the fix.

Hermetic: FakeGitHubSource-derived bulk source only; the one --live leg
points gh_path at a local stub script (no network, no real gh). Stdlib only;
unittest (no pytest).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from fixtures import (
    REPO,
    FakeGitHubSource,
    issue,
    pr_file,
    pull,
    scrubbed_env,
)
from fixtures_eval import _insert_edge

from zaxbygraph.db import connect, init_schema, open_existing
from zaxbygraph.derive import similar
from zaxbygraph.sync import sync_repo

REPO_ROOT = Path(__file__).resolve().parents[1]


# ==== shared plumbing (module-local; the supp file stays self-contained) ====


class _BulkSource(FakeGitHubSource):
    """FakeGitHubSource yielding one listing page and serving children (with
    the v10 `links` sections) from fetch_children — the same pattern as
    tests/test_triage_bundle.py, duplicated here so this module stands
    alone."""

    def __init__(self) -> None:
        super().__init__()
        self.links: dict[int, dict] = {}

    def list_issues(self, since: str | None):
        page = [
            deepcopy(rec)
            for rec in sorted(
                self.issues.values(), key=lambda r: (r["updated_at"], r["number"])
            )
            if since is None or rec["updated_at"] >= since
        ]
        if page:
            yield page

    def fetch_children(self, items) -> dict[int, dict]:
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


def _node(number: int, repo: str = REPO) -> dict:
    return {"number": number, "repo": repo}


def _links(
    *,
    blocked_by: tuple[dict, ...] = (),
    open_prs: tuple[dict, ...] = (),
) -> dict:
    return {
        "blocked_by": [dict(n) for n in blocked_by],
        "parent": None,
        "duplicate_of": None,
        "open_prs": [dict(n) for n in open_prs],
    }


def _merged_pull(number: int, merged_at: str) -> dict:
    p = pull(number, merged=True)
    p["merged_at"] = merged_at
    return p


def _pin_synced_at(conn, repo: str = REPO, stamp: str = "2027-07-01T00:00:00Z") -> None:
    conn.execute(
        "UPDATE sync_state SET last_full_sync_at = ?, last_incr_sync_at = ?"
        " WHERE repo = ?",
        (stamp, stamp, repo),
    )
    conn.commit()


def _run_cli(*argv: str, path_override: str | None = None) -> tuple[int, bytes, bytes]:
    env = scrubbed_env()
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(REPO_ROOT / "src"),
            str(REPO_ROOT / "tests"),
            env.get("PYTHONPATH", ""),
        ]
    )
    if path_override is not None:
        env["PATH"] = path_override
    proc = subprocess.run(
        [sys.executable, "-m", "zaxbygraph", *argv],
        capture_output=True,
        env=env,
        cwd=str(REPO_ROOT),
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


def _member_numbers(bundle: dict) -> list[int]:
    return [int(m["number"]) if isinstance(m, dict) else int(m) for m in bundle["members"]]


def _expected_bundle_id(members, weights: dict) -> str:
    """bundle_id = sha1("zaxbygraph.bundle.v1|" + ",".join(str(n) for n in
    sorted(members)) + "|" + config_hash), config_hash = sha1 of the
    canonical JSON of the effective [triage] dict (sort_keys + ensure_ascii
    + compact separators, the export.py house form)."""
    canonical = json.dumps(
        weights, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )
    config_hash = hashlib.sha1(canonical.encode("utf-8")).hexdigest()
    return hashlib.sha1(
        (
            "zaxbygraph.bundle.v1|"
            + ",".join(str(n) for n in sorted(members))
            + "|"
            + config_hash
        ).encode("utf-8")
    ).hexdigest()


class _StoreTestCase(unittest.TestCase):
    """One temp store per test class; subclasses override `_build`."""

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


# ==== C13: --exclude removes pool members ===================================


class BundleExcludeTests(_StoreTestCase):
    """Three disjoint families:

    - alpha: 431/432/433 over src/excl_a.py (PR 441 merged), identical
      triage features so totals tie and greedy is number-ASC deterministic.
    - beta:  434/435 over src/excl_b.py (PR 442 merged), same features.
    - blocked pair: 451/452 over src/excl_c.py (PR 453 merged); 452 is
      blocked by 451 (blocked_by link) and stays in the pool through the
      in-pool relaxation.
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        for n, path in ((441, "src/excl_a.py"), (442, "src/excl_b.py"),
                        (453, "src/excl_c.py")):
            src.add_pr(
                issue(n, title=f"history {n}", body="", kind="pr", state="closed",
                      created_at="2027-03-01T00:00:00Z"),
                _merged_pull(n, "2027-03-02T00:00:00Z"),
                files=[pr_file(path)],
            )
        spec = [
            (431, "src/excl_a.py:1:1"), (432, "src/excl_a.py:2:2"),
            (433, "src/excl_a.py:3:3"), (434, "src/excl_b.py:1:1"),
            (435, "src/excl_b.py:2:2"),
            (451, "src/excl_c.py:1:1"), (452, "src/excl_c.py:2:2"),
        ]
        for number, ref in spec:
            rec = issue(number, title="Crash in module", body=f"see {ref}",
                        created_at="2027-05-01T00:00:00Z")
            rec["reactions"] = {"+1": 10, "total_count": 10}
            src.add_issue(rec)
        src.links[452] = _links(blocked_by=(_node(451),))
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)

    def test_bundle_exclude_removes_pool_members(self) -> None:
        """RED at base, GREEN after the fix. --exclude drops the named
        members from the pool entirely (not bundled, not in
        merge_candidates, echoed as `excluded`), wins over --pool membership
        AND over in-pool relaxation, and the bundle_id reflects the
        remaining members."""
        # Baseline: the full pool bundles both families (count cap keeps
        # the blocked pair out of this leg's two bundles).
        env = _cli_json(
            "bundle", "--db", str(self.db_path), "--repo", REPO,
            "--count", "2", "--format", "json",
        )
        self.assertTrue(env["ok"], env)
        bundles = env["data"]["bundles"]
        self.assertEqual(len(bundles), 2)
        self.assertEqual(set(_member_numbers(bundles[0])), {431, 432, 433})
        self.assertEqual(set(_member_numbers(bundles[1])), {434, 435})

        # --exclude removes 433 from the pool: not bundled, not listed in
        # merge_candidates, echoed under `excluded`, and the shrunken
        # bundle's bundle_id reflects the remaining members.
        env_ex = _cli_json(
            "bundle", "--db", str(self.db_path), "--repo", REPO,
            "--count", "2", "--exclude", "433", "--format", "json",
        )
        self.assertTrue(env_ex["ok"], env_ex)
        data_ex = env_ex["data"]
        self.assertEqual(data_ex.get("excluded"), [433])
        members_ex = {n for b in data_ex["bundles"] for n in _member_numbers(b)}
        self.assertNotIn(433, members_ex)
        self.assertFalse(
            any("433" in json.dumps(e) for e in data_ex["merge_candidates"]),
            "an excluded number must not appear in merge_candidates",
        )
        alpha = next(
            b for b in data_ex["bundles"] if 431 in _member_numbers(b)
        )
        self.assertEqual(set(_member_numbers(alpha)), {431, 432})
        self.assertEqual(
            alpha["bundle_id"],
            _expected_bundle_id((431, 432), data_ex["weights"]),
            "bundle_id must reflect the remaining members after --exclude",
        )

        # --exclude wins over --pool membership: 433 named in --pool yet
        # excluded stays out; the remaining pair still bundles.
        env_pool = _cli_json(
            "bundle", "--db", str(self.db_path), "--repo", REPO,
            "--pool", "431,432,433", "--count", "5",
            "--exclude", "433", "--format", "json",
        )
        self.assertTrue(env_pool["ok"], env_pool)
        data_pool = env_pool["data"]
        self.assertEqual(data_pool.get("excluded"), [433])
        pool_members = {n for b in data_pool["bundles"] for n in _member_numbers(b)}
        self.assertNotIn(433, pool_members)
        self.assertEqual(pool_members, {431, 432})

        # In-pool relaxation baseline: the blocked pair bundles when both
        # endpoints are in the pool.
        env_relax = _cli_json(
            "bundle", "--db", str(self.db_path), "--repo", REPO,
            "--pool", "451,452", "--count", "5", "--format", "json",
        )
        self.assertTrue(env_relax["ok"], env_relax)
        relax_members = {
            n for b in env_relax["data"]["bundles"] for n in _member_numbers(b)
        }
        self.assertEqual(relax_members, {451, 452})

        # --exclude wins over in-pool relaxation EITHER way: excluding the
        # blocker (451) collapses 452's relaxation (blockers no longer all
        # in the pool), excluding the blocked item (452) removes it
        # outright — both leave a pool that cannot field a 2-member bundle.
        for excluded in ("451", "452"):
            env_no = _cli_json(
                "bundle", "--db", str(self.db_path), "--repo", REPO,
                "--pool", "451,452", "--count", "5",
                "--exclude", excluded, "--format", "json",
            )
            self.assertTrue(env_no["ok"], env_no)
            self.assertEqual(
                env_no["data"].get("excluded"), [int(excluded)],
                f"--exclude {excluded} must be echoed",
            )
            self.assertEqual(
                env_no["data"]["bundles"], [],
                f"--exclude {excluded} must defeat the in-pool relaxation",
            )


# ==== C14: open --offset and enrichment =====================================


class OpenOffsetTests(_StoreTestCase):
    """Open items with fully controlled updated_at order (fixture
    ts(number*10)): 815 (open PR), 813, 812, 811 (issues). 811 carries an
    assignee row (listing-payload assignees) and an open linked PR (links
    open_prs -> 815)."""

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        rec811 = issue(811, title="enriched item", body="plain",
                       labels=[{"name": "high", "color": "ff0000"}])
        rec811["assignees"] = [{"login": "nina"}]
        src.add_issue(rec811)
        src.links[811] = _links(open_prs=(_node(815),))
        src.add_issue(issue(812, title="middle item", body="plain"))
        src.add_issue(issue(813, title="newest issue", body="plain"))
        src.add_pr(
            issue(815, title="open pull", body="", kind="pr"),
            pull(815, merged=False),
            files=[pr_file("src/enrich.py")],
        )
        sync_repo(conn, src, REPO)

    def test_open_offset_and_enrichment(self) -> None:
        """RED at base, GREEN after the fix. AMENDED AC7 SURFACE CONTRACT:
        `--offset` skips rows even when `--limit` is absent (offset applies
        when limit is None — the no-clause-preserving default applies to the
        LIMIT only, never to OFFSET). With --limit, --offset pages after the
        first row; rows that have assignees and open linked PRs carry
        non-empty `assignees` and `linked_open_prs` lists."""
        # --offset WITHOUT --limit FIRST (the amended contract): still skips
        # the first row and returns every remaining row in the same order.
        # This leg leads so the base-tree failure names --offset alone.
        env_off = _cli_json(
            "open", "--db", str(self.db_path), "--repo", REPO,
            "--offset", "1", "--format", "json",
        )
        self.assertTrue(env_off["ok"], env_off)
        self.assertEqual([r["number"] for r in env_off["data"]], [813, 812, 811])

        # ORDER (unchanged surface): updated_at DESC -> 815, 813, 812, 811.
        env_full = _cli_json(
            "open", "--db", str(self.db_path), "--repo", REPO, "--format", "json"
        )
        self.assertTrue(env_full["ok"], env_full)
        full = [r["number"] for r in env_full["data"]]
        self.assertEqual(full, [815, 813, 812, 811])

        # --offset with --limit: skips the first row.
        env = _cli_json(
            "open", "--db", str(self.db_path), "--repo", REPO,
            "--limit", "2", "--offset", "1", "--format", "json",
        )
        self.assertTrue(env["ok"], env)
        self.assertEqual([r["number"] for r in env["data"]], [813, 812])

        # Enrichment on the row that has an assignee and an open linked PR.
        env_tail = _cli_json(
            "open", "--db", str(self.db_path), "--repo", REPO,
            "--limit", "5", "--offset", "3", "--format", "json",
        )
        self.assertTrue(env_tail["ok"], env_tail)
        rows_tail = env_tail["data"]
        self.assertEqual([r["number"] for r in rows_tail], [811])
        row = rows_tail[0]
        self.assertEqual(row["assignees"], ["nina"])
        self.assertEqual(row["linked_open_prs"], [815])
        self.assertIn("high", row["labels"])


# ==== C15: the derived commands never write ================================


class NeverWriteSuppTests(_StoreTestCase):
    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        src.add_pr(
            issue(861, title="vocab pr", body="touches src/never.py:1:1",
                  kind="pr", state="closed", created_at="2027-06-01T00:00:00Z"),
            _merged_pull(861, "2027-06-02T00:00:00Z"),
            files=[pr_file("src/never.py")],
        )
        for n in (871, 872):
            src.add_issue(
                issue(n, title=f"Fault in never path {n}",
                      body=f"fault at src/never.py:{n}:1",
                      created_at="2027-06-05T00:00:00Z")
            )
        src.comment_on(871, "reproduced quickly", author="bob")
        sync_repo(conn, src, REPO)

    def test_derived_commands_never_write(self) -> None:
        """RED at base, GREEN after the fix. The store file's bytes are
        identical before and after triage, bundle and brief runs (the
        NeverWriteTests pattern extended to the new commands), and all three
        payloads carry derived: true."""
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()

        env_triage = _cli_json(
            "triage", "--db", str(self.db_path), "--repo", REPO, "--format", "json"
        )
        self.assertTrue(env_triage["ok"], env_triage)
        self.assertIs(env_triage["data"].get("derived"), True)

        env_bundle = _cli_json(
            "bundle", "--db", str(self.db_path), "--repo", REPO, "--format", "json"
        )
        self.assertTrue(env_bundle["ok"], env_bundle)
        self.assertIs(env_bundle["data"].get("derived"), True)

        env_brief = _cli_json(
            "brief", "871", "--db", str(self.db_path), "--repo", REPO,
            "--format", "json",
        )
        self.assertTrue(env_brief["ok"], env_brief)
        self.assertIs(env_brief["data"].get("derived"), True)

        after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.assertEqual(
            after, before,
            "the derived tier must never write: store bytes changed",
        )


# ==== C16: brief --live with a WORKING gh ===================================


class BriefLiveGhTests(_StoreTestCase):
    """Issue 961 with an open linked PR 962 (links open_prs), so brief's
    --live leg has a PR to ask gh about. The gh stub prints canned JSON
    carrying LIVEMARK strings and logs its argv; it is passed via the
    spec's gh_path seam as an ABSOLUTE path — a bare "gh" name on PATH
    cannot execute a script shim on Windows (CreateProcess appends .exe
    only), so the check drives the same argv-list subprocess path the
    CLI's --live flag uses, deterministically on every platform."""

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        rec = issue(961, title="Live leg item", body="body of the live item",
                    created_at="2027-06-01T00:00:00Z")
        src.add_issue(rec)
        src.comment_on(961, "a comment on the live item", author="bob")
        src.add_pr(
            issue(962, title="open pull for live", body="", kind="pr"),
            pull(962, merged=False),
            files=[pr_file("src/liveleg.py")],
        )
        src.links[961] = _links(open_prs=(_node(962),))
        sync_repo(conn, src, REPO)

    @staticmethod
    def _split_trust(obj, untrusted, others):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "untrusted_text" and isinstance(v, str):
                    untrusted.append(v)
                else:
                    others.append(str(k))
                    BriefLiveGhTests._split_trust(v, untrusted, others)
        elif isinstance(obj, list):
            for v in obj:
                BriefLiveGhTests._split_trust(v, untrusted, others)
        elif isinstance(obj, str):
            others.append(obj)

    def test_brief_live_gh_success_leg(self) -> None:
        """RED at base, GREEN after the fix. With a WORKING gh stub the
        --live leg returns the packet WITH a live block (not the
        degradation shape); BOTH live_ci AND live_review fields are present
        with parsed content tied to the canned response (both canned
        LIVEMARK values round-trip into the live fields); both canned
        strings occur inside untrusted_text and NOWHERE outside it. gh is
        invoked through an argv list (no shell), proven by the stub's argv
        log."""
        with tempfile.TemporaryDirectory() as td:
            shim = Path(td)
            canned = (
                '[{"name": "LIVEMARK-ci", "state": "SUCCESS", '
                '"conclusion": "success", "body": "LIVEMARK-review attached note"}]'
            )
            if os.name == "posix":
                stub = shim / "gh"
                stub.write_text(
                    "#!/bin/sh\n"
                    'printf \'"%s"\\n\' "$*" >> "$0.log"\n'
                    f"printf '%s\\n' '{canned}'\n",
                    encoding="utf-8", newline="\n",
                )
                stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            else:
                stub = shim / "gh.bat"
                stub.write_text(
                    "@echo off\r\n"
                    'echo %*>>"%~dp0gh.bat.log"\r\n'
                    f"echo {canned}\r\n",
                    encoding="ascii", newline="",
                )
            stub_path = stub

            import zaxbygraph.derive as derive_mod

            brief = getattr(derive_mod, "brief")
            conn = open_existing(self.db_path)
            try:
                out = brief(
                    conn, REPO, 961,
                    budget_tokens=4000, live=True, gh_path=str(stub_path),
                )
            finally:
                conn.close()

            # capture the argv log BEFORE the temp dir goes away
            log = stub_path.with_name(stub_path.name + ".log")
            log_text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""

        self.assertEqual(out["content_trust"], "untrusted: GitHub user content")
        # a live block is PRESENT (the success shape, not live:null).
        self.assertTrue(
            out.get("live"),
            f"--live with a working gh must produce a live block, got {out.get('live')!r}",
        )
        # BOTH labeled live fields exist in the packet.
        blob = json.dumps(out)
        self.assertIn(
            "live_ci", blob, "the packet must label its live CI field live_ci"
        )
        self.assertIn(
            "live_review", blob, "the packet must label its live review field live_review"
        )
        # the canned values ROUND-TRIP into the live fields: the live
        # subtree (the live block plus any top-level live_ci/live_review
        # fields) carries both canned strings.
        live_blob = json.dumps(
            {
                "live": out.get("live"),
                "live_ci": out.get("live_ci"),
                "live_review": out.get("live_review"),
            }
        )
        self.assertIn(
            "LIVEMARK-ci", live_blob,
            "the canned check name must round-trip into the live_ci content",
        )
        self.assertIn(
            "LIVEMARK-review", live_blob,
            "the canned review body must round-trip into the live_review content",
        )
        # BOTH canned strings occur inside untrusted_text and NOWHERE
        # outside it (the walkers classify every string in the packet).
        untrusted: list[str] = []
        others: list[str] = []
        self._split_trust(out, untrusted, others)
        for marker in ("LIVEMARK-ci", "LIVEMARK-review"):
            self.assertTrue(
                any(marker in u for u in untrusted),
                f"{marker} must occur inside an untrusted_text wrapper",
            )
            self.assertFalse(
                any(marker in o for o in others),
                f"{marker} leaked outside untrusted_text wrappers",
            )
        # the stub really ran, with an argv list (its log records the
        # arguments of every invocation).
        self.assertTrue(
            log_text.strip(),
            "the gh stub must have been invoked (argv log empty)",
        )


# ==== C17: the cochange population respects the cutoff ======================


class CochangeCutoffTests(_StoreTestCase):
    """Two path families pin both cutoff shapes (issue 981's created_at is
    the cutoff, 2027-01-10):

    x/y family — the anchored-survivor (RAISE) shape:
    - PR 9811 merged 2027-01-05 (pre-cutoff): [src/px/x_widget.py, src/px/filler_a.py]
    - PR 9812 merged 2027-01-06 (pre-cutoff): [src/py/y_widget.py, src/py/filler_b.py]
    - PR 9813 merged 2027-01-20 (POST-cutoff): [src/px/x_widget.py, src/py/y_widget.py]
    - issue 981 created 2027-01-10 (the cutoff): body refs BOTH x and y
    - issue 982 created 2027-01-10: body refs BOTH x and y

    Direct read (no cutoffs): coupling population {9811..9816};
    support(x_widget, y_widget) = 1 (only PR 9813), support(x_widget) = 2,
    support(y_widget) = 2, so the symmetric confidence is 1/max(2,2) = 0.5
    -> the cochange component scores 0.5 (weight-free, so hub floors never
    touch it). Both items also resolve BOTH paths from pre-cutoff body
    refs, so files_direct fires as a second, scoping-proof anchor: with
    item_cutoff = comment_cutoff = 2027-01-10 the pair REMAINS (files_direct)
    while the scoped coupling population excludes PR 9813 (revised section
    8: the _coupling_index call inside similar gains the cutoff) and ZEROES
    the cochange component — cochange may raise an anchored pair, never
    create one.

    u/v family — the drop shape (the original no-other-anchor leg):
    - PR 9814 merged 2027-01-05 (pre-cutoff): [src/pu/u_gear.py, src/pu/filler_c.py]
    - PR 9815 merged 2027-01-06 (pre-cutoff): [src/pv/v_gear.py, src/pv/filler_d.py]
    - PR 9816 merged 2027-01-20 (POST-cutoff): [src/pu/u_gear.py, src/pv/v_gear.py]
    - issue 983 created 2027-01-10: body refs src/pu/u_gear.py only
    - issue 984 created 2027-01-10: body refs src/pv/v_gear.py only

    Direct read: support(u,v) = 1 (only PR 9816), support(u) = support(v)
    = 2 -> symmetric confidence 0.5 anchors the otherwise anchor-less pair.
    Under the cutoffs PR 9816 leaves the coupling population, the support
    vanishes, and no other anchor exists — 984 drops out entirely."""

    CUTOFF = "2027-01-10T00:00:00Z"

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        src.add_pr(
            issue(9811, title="px history", body="", kind="pr", state="closed",
                  created_at="2027-01-01T00:00:00Z"),
            _merged_pull(9811, "2027-01-05T00:00:00Z"),
            files=[pr_file("src/px/x_widget.py"), pr_file("src/px/filler_a.py")],
        )
        src.add_pr(
            issue(9812, title="py history", body="", kind="pr", state="closed",
                  created_at="2027-01-01T00:00:00Z"),
            _merged_pull(9812, "2027-01-06T00:00:00Z"),
            files=[pr_file("src/py/y_widget.py"), pr_file("src/py/filler_b.py")],
        )
        src.add_pr(
            issue(9813, title="cross family change", body="", kind="pr",
                  state="closed", created_at="2027-01-15T00:00:00Z"),
            _merged_pull(9813, "2027-01-20T00:00:00Z"),
            files=[pr_file("src/px/x_widget.py"), pr_file("src/py/y_widget.py")],
        )
        src.add_pr(
            issue(9814, title="pu history", body="", kind="pr", state="closed",
                  created_at="2027-01-01T00:00:00Z"),
            _merged_pull(9814, "2027-01-05T00:00:00Z"),
            files=[pr_file("src/pu/u_gear.py"), pr_file("src/pu/filler_c.py")],
        )
        src.add_pr(
            issue(9815, title="pv history", body="", kind="pr", state="closed",
                  created_at="2027-01-01T00:00:00Z"),
            _merged_pull(9815, "2027-01-06T00:00:00Z"),
            files=[pr_file("src/pv/v_gear.py"), pr_file("src/pv/filler_d.py")],
        )
        src.add_pr(
            issue(9816, title="cross gear change", body="", kind="pr",
                  state="closed", created_at="2027-01-15T00:00:00Z"),
            _merged_pull(9816, "2027-01-20T00:00:00Z"),
            files=[pr_file("src/pu/u_gear.py"), pr_file("src/pv/v_gear.py")],
        )
        # x/y family: BOTH items carry pre-cutoff body refs to BOTH paths,
        # so files_direct anchors the pair and survives scoping.
        src.add_issue(
            issue(981, title="Widget X fault",
                  body="see src/px/x_widget.py:1:1 and src/py/y_widget.py:1:1",
                  created_at="2027-01-10T00:00:00Z")
        )
        src.add_issue(
            issue(982, title="Widget Y fault",
                  body="see src/py/y_widget.py:1:1 and src/px/x_widget.py:1:1",
                  created_at="2027-01-10T00:00:00Z")
        )
        # u/v family: single-path bodies, no anchor besides cochange.
        src.add_issue(
            issue(983, title="Gear U fault",
                  body="see src/pu/u_gear.py:1:1",
                  created_at="2027-01-10T00:00:00Z")
        )
        src.add_issue(
            issue(984, title="Gear V fault",
                  body="see src/pv/v_gear.py:1:1",
                  created_at="2027-01-10T00:00:00Z")
        )
        sync_repo(conn, src, REPO)

    def test_cochange_cutoff_excludes_post_cutoff_support(self) -> None:
        """RED at base, GREEN after the fix. Wave-4 addition additionally
        fails on the no-index-cutoff mutant (item_cutoff dropped from the
        similar() coupling-index call) and passes on the shipped fix. The
        x/y pair keeps a second, scoping-proof anchor (both items carry
        pre-cutoff body refs to both paths), so the cutoffs must zero its
        cochange component while the pair survives on files_direct; the u/v
        pair's only anchor is post-cutoff cochange support, so under the
        cutoffs it drops out entirely."""
        conn = open_existing(self.db_path)
        self.addCleanup(conn.close)

        # (b) RAISE path — the anchored pair. Direct read (no cutoffs): 982
        # anchors to 981 through cochange (the sorted anchor order checks
        # cochange first), with the hand-derived symmetric confidence
        # 1/max(2,2) = 0.5, and the shared body-ref paths fire files_direct
        # as the second, scoping-proof anchor.
        direct = similar(conn, REPO, 981, limit=50)
        cand = next(
            (c for c in direct["candidates"] if c["number"] == 982), None
        )
        self.assertIsNotNone(
            cand, "the direct read must cochange-anchor 982 to 981"
        )
        self.assertAlmostEqual(
            cand["components"]["cochange"]["score"], 0.5, places=6
        )
        self.assertEqual(cand["anchor"], "cochange")
        self.assertTrue(
            cand["components"]["cochange"]["evidence"]["pairs"],
            "the direct cochange evidence must carry the post-cutoff pair",
        )
        self.assertGreater(
            cand["components"]["files_direct"]["score"], 0.0,
            "the x/y pair needs its second (files_direct) anchor",
        )

        # Scoped read: PR 9813 leaves the coupling population, so the
        # cochange component must ZERO while the pair REMAINS, anchored
        # through files_direct, with every non-cochange component unchanged
        # (an unscoped coupling index would still score 0.5 here — the
        # wave-4 mutant kill).
        scoped = similar(
            conn, REPO, 981, limit=50,
            item_cutoff=self.CUTOFF, comment_cutoff=self.CUTOFF,
        )
        scoped_cand = next(
            (c for c in scoped["candidates"] if c["number"] == 982), None
        )
        self.assertIsNotNone(
            scoped_cand,
            "the anchored pair must survive scoping via files_direct",
        )
        self.assertEqual(
            scoped_cand["components"]["cochange"]["score"], 0.0,
            "post-cutoff cochange support must not raise a scoped pair",
        )
        self.assertEqual(
            scoped_cand["components"]["cochange"]["evidence"]["pairs"], []
        )
        self.assertEqual(scoped_cand["anchor"], "files_direct")
        self.assertEqual(
            scoped_cand["components"]["files_direct"]["evidence"]["shared_paths"],
            ["src/px/x_widget.py", "src/py/y_widget.py"],
        )
        self.assertEqual(
            {k: v for k, v in scoped_cand["components"].items()
             if k != "cochange"},
            {k: v for k, v in cand["components"].items() if k != "cochange"},
            "scoping must change the cochange component and nothing else",
        )
        # the pre-cutoff paths themselves stay resolvable under the cutoff.
        self.assertGreaterEqual(scoped["resolved_path_count"], 2)

        # (a) DROP path — the pair whose ONLY anchor is post-cutoff
        # cochange support: the direct read admits it, the scoped read must
        # drop it entirely.
        direct_uv = similar(conn, REPO, 983, limit=50)
        cand_uv = next(
            (c for c in direct_uv["candidates"] if c["number"] == 984), None
        )
        self.assertIsNotNone(
            cand_uv, "the direct read must cochange-anchor 984 to 983"
        )
        self.assertAlmostEqual(
            cand_uv["components"]["cochange"]["score"], 0.5, places=6
        )
        self.assertEqual(cand_uv["anchor"], "cochange")
        scoped_uv = similar(
            conn, REPO, 983, limit=50,
            item_cutoff=self.CUTOFF, comment_cutoff=self.CUTOFF,
        )
        self.assertNotIn(
            984,
            [c["number"] for c in scoped_uv["candidates"]],
            "post-cutoff cochange support must not anchor the pair",
        )


# ==== C18: CANDIDATE-side comment bait is scoped ============================


class CandidateSideBaitTests(_StoreTestCase):
    """Pinned stamps (all in comments of this docstring):

    - PR 5541 merged 2027-02-01T00:00:00Z (pre-cutoff): [src/cbait.py] —
      the bait path enters the vocabulary before the target exists.
    - target 551 created 2027-02-10T00:00:00Z (the cutoff anchor); body
      refs src/cbait.py (origin 'body').
    - candidate 552 created 2027-02-05T00:00:00Z (PRE item-cutoff, so the
      item stays visible); its ONLY src/cbait.py reference comes from its
      own comment created 2027-02-15T00:00:00Z (POST comment-cutoff).
    - candidate 553 created 2027-02-06T00:00:00Z; body refs src/cbait.py
      (origin 'body' — body refs are not cut by comment_cutoff).

    Direct read: both candidates share src/cbait.py with the target
    (552 via its comment) -> both anchor through files_direct. Eval-scoped
    read (item_cutoff = comment_cutoff = 2027-02-10T00:00:00Z): 552's
    post-cutoff comment ref is suppressed on the CANDIDATE side (revised
    section 8: references are filtered for EVERY scored item), so 552 has
    no anchor left; 553's body ref survives and still anchors."""

    CUTOFF = "2027-02-10T00:00:00Z"

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        src.add_pr(
            issue(5541, title="bait vocab pr", body="", kind="pr", state="closed",
                  created_at="2027-01-25T00:00:00Z"),
            _merged_pull(5541, "2027-02-01T00:00:00Z"),
            files=[pr_file("src/cbait.py")],
        )
        # target: body ref only (created 2027-02-10 — the cutoff anchor)
        src.add_issue(
            issue(551, title="Primary bait report",
                  body="fault at src/cbait.py:3:3 primary report",
                  created_at="2027-02-10T00:00:00Z")
        )
        # candidate whose ONLY shared ref rides a post-cutoff comment
        # (created 2027-02-05, comment 2027-02-15)
        src.add_issue(
            issue(552, title="Secondary comment-only report",
                  body="no paths in this body at all",
                  created_at="2027-02-05T00:00:00Z")
        )
        bait_comment = src.comment_on(
            552, "also see src/cbait.py:7:7 in the logs"
        )
        bait_comment["created_at"] = bait_comment["updated_at"] = (
            "2027-02-15T00:00:00Z"
        )
        # candidate with a pre-cutoff BODY ref (created 2027-02-06)
        src.add_issue(
            issue(553, title="Tertiary body report",
                  body="same fault at src/cbait.py:9:9 elsewhere",
                  created_at="2027-02-06T00:00:00Z")
        )
        sync_repo(conn, src, REPO)

    def test_candidate_side_comment_bait_scoped(self) -> None:
        """RED at base, GREEN after the fix. A CANDIDATE item whose only
        shared path reference comes from its own post-comment_cutoff
        COMMENT: the direct similar() read admits the pair (anchor via that
        path); the eval-scoped read rejects it; a pre-cutoff candidate BODY
        reference survives scoping and still anchors under the cutoffs."""
        conn = open_existing(self.db_path)
        self.addCleanup(conn.close)

        # Direct read (no cutoffs): both candidates anchor through the
        # shared bait path.
        direct = similar(conn, REPO, 551, limit=50)
        direct_numbers = [c["number"] for c in direct["candidates"]]
        self.assertIn(
            552, direct_numbers,
            "the direct read must admit the comment-bait candidate",
        )
        self.assertIn(553, direct_numbers)

        # Eval-scoped read: 552's post-cutoff comment ref is cut on the
        # CANDIDATE side, so the pair loses its only anchor and 552 drops;
        # 553's BODY ref survives and keeps the pair anchored with the
        # shared path as evidence.
        scoped = similar(
            conn, REPO, 551, limit=50,
            item_cutoff=self.CUTOFF, comment_cutoff=self.CUTOFF,
        )
        scoped_numbers = [c["number"] for c in scoped["candidates"]]
        self.assertNotIn(
            552, scoped_numbers,
            "a candidate's post-comment_cutoff comment ref must not anchor",
        )
        self.assertIn(
            553, scoped_numbers,
            "a candidate's pre-cutoff body ref must survive scoping",
        )
        cand553 = next(c for c in scoped["candidates"] if c["number"] == 553)
        self.assertIn(
            "src/cbait.py",
            cand553["components"]["files_direct"]["evidence"]["shared_paths"],
        )
        # ... and 552, present unfiltered, is gone ONLY because of the
        # comment cutoff: with just the item cutoff (both items pre-date it)
        # the comment ref is the discriminator — assert the item-cutoff-only
        # leg still admits 552 so the check pins the COMMENT side.
        item_only = similar(
            conn, REPO, 551, limit=50, item_cutoff=self.CUTOFF
        )
        self.assertIn(552, [c["number"] for c in item_only["candidates"]])


# ==== C19 (feedback round): --pool/--exclude invalid values are coded ======


class BundleBadValuesTests(_StoreTestCase):
    """One synced open issue: enough store for the bundle command to
    resolve the repo and reach its argument validation."""

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        src.add_issue(issue(471, title="single item", body="plain"))
        sync_repo(conn, src, REPO)

    def test_bundle_pool_exclude_invalid_values_are_coded(self) -> None:
        """RED at base, GREEN after the fix. Review-fix regression pin
        (feedback round): an invalid --pool or --exclude value returns the
        coded bad_request envelope at exit 2 — never a traceback — exactly
        like an invalid --size. At base the bundle command is absent (RED =
        argparse invalid choice: exit 2 with usage on stderr and no JSON
        envelope); the RED-on-revert proof is disabling the validation
        (evaluating _csv_numbers outside _run_derived_read's protected try
        again), which turns the envelope back into an exit-1 traceback."""
        for flag, value in (("--pool", "abc"), ("--exclude", "1,x")):
            rc, out, err = _run_cli(
                "bundle", "--db", str(self.db_path), "--repo", REPO,
                flag, value, "--format", "json",
            )
            self.assertEqual(
                rc, 2,
                f"{flag} {value!r} must exit 2 (bad_request), got {rc};"
                f" stderr:\n{err.decode('utf-8', 'replace')}",
            )
            self.assertNotIn(
                b"Traceback", err,
                f"{flag} {value!r} must not print a traceback",
            )
            env = json.loads(out.decode("utf-8"))
            self.assertIs(env["ok"], False, env)
            self.assertIsInstance(env.get("error"), dict, env)
            self.assertEqual(
                env["error"].get("code"), "bad_request",
                f"{flag} {value!r} must carry error.code bad_request",
            )


# ==== C20 (feedback round): --budget-tokens caps the whole payload =========


class BundleBudgetCapTests(_StoreTestCase):
    """Twelve duplicate_of pairs (561/562 .. 583/584) and nothing
    bundleable: no merged PRs, so no resolved paths, no coupling, and every
    pair's text affinity sits far below the floor — every pair lands in
    merge_candidates and the bundles list stays EMPTY. A tiny
    --budget-tokens can therefore only be honored by shrinking the
    payload's non-bundle sections."""

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        for n in range(561, 585):
            src.add_issue(
                issue(n, title=f"Distinct report {n}", body=f"body text {n}",
                      created_at="2027-06-01T00:00:00Z")
            )
        sync_repo(conn, src, REPO)
        for i in range(12):
            _insert_edge(conn, REPO, 561 + 2 * i, "duplicate_of", 562 + 2 * i)
        _pin_synced_at(conn)

    def test_bundle_budget_caps_whole_payload(self) -> None:
        """RED at base, GREEN after the fix. Review-fix regression pin
        (feedback round): --budget-tokens caps the WHOLE serialized
        payload, not just the bundles list. With zero bundles and twelve
        merge candidates, a 100-token budget must shrink merge_candidates
        (they drop from the tail) and leave a payload that either fits
        the budget (chars/4 <= 100) or carries truncated: true. At base
        the bundle command is absent (RED = invalid choice); the
        RED-on-revert proof is bypassing the whole-payload shrink (popping
        only bundles_payload again), which leaves the merge-candidate
        payload over budget and unflagged."""
        # Unbounded leg: all twelve merge candidates survive, bundles stay
        # empty, and no truncated key is set.
        env_full = _cli_json(
            "bundle", "--db", str(self.db_path), "--repo", REPO,
            "--count", "5", "--format", "json",
        )
        self.assertTrue(env_full["ok"], env_full)
        full = env_full["data"]
        self.assertEqual(full["bundles"], [])
        self.assertEqual(len(full["merge_candidates"]), 12)
        self.assertNotIn("truncated", full)

        # Tiny budget: the whole payload is capped or flagged.
        rc, out, err = _run_cli(
            "bundle", "--db", str(self.db_path), "--repo", REPO,
            "--count", "5", "--budget-tokens", "100", "--format", "json",
        )
        self.assertEqual(rc, 0, err.decode("utf-8", "replace"))
        env = json.loads(out.decode("utf-8"))
        self.assertTrue(env["ok"], env)
        data = env["data"]
        payload_chars = len(
            json.dumps(data, sort_keys=True, ensure_ascii=True,
                       separators=(",", ":"))
        )
        self.assertTrue(
            payload_chars <= 100 * 4 or data.get("truncated") is True,
            f"--budget-tokens 100 must cap the whole payload"
            f" (chars/4 = {payload_chars / 4:.0f}) or flag truncated: true,"
            f" got truncated={data.get('truncated')!r}",
        )
        self.assertIs(
            data.get("truncated"), True,
            "a payload that needed shrinking must carry truncated: true",
        )
        self.assertLess(
            len(data["merge_candidates"]), 12,
            "the shrink must reach merge_candidates once bundles are"
            " exhausted (zero bundles in this store)",
        )


# ==== C21 (review round 2): bare-issue brief packet honors the budget =======


class BriefBareIssueBudgetTests(_StoreTestCase):
    """621: a BARE issue — zero comments, zero path refs — whose body is one
    ~30k-char ASCII paragraph, so under --budget-tokens 4000 no whole
    paragraph fits and the character-level hard cap is the only body trim;
    with zero comments the comment-pop absorber is a no-op, which is exactly
    the shape where the post-fit `truncated`/`derived` echoes used to land
    outside the allowance. 622: the same shape with a CJK body ("文" * 7250)
    where every character serializes to six chars under ensure_ascii and the
    old char-arithmetic cut went negative, emptying the body."""

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        src.add_issue(
            issue(621, title="Single paragraph wall of text",
                  body="crash on submit filler text " * 1100,
                  created_at="2027-06-01T00:00:00Z")
        )
        src.add_issue(
            issue(622, title="CJK wall of text", body="文" * 7250,
                  created_at="2027-06-01T00:00:00Z")
        )
        sync_repo(conn, src, REPO)

    @staticmethod
    def _packet_chars(pkt: dict) -> int:
        return len(
            json.dumps(pkt, sort_keys=True, ensure_ascii=True,
                       separators=(",", ":"))
        )

    def test_brief_bare_issue_budget_bounded(self) -> None:
        """RED at base, GREEN after the fix. Review-round-2 regression pin:
        on a bare issue whose body must be hard-capped, the packet's
        mandatory echoes (`truncated: true` whenever a trim fired,
        `derived: true` always) ride INSIDE the budget envelope — the FINAL
        serialized packet satisfies chars/4 <= --budget-tokens with nothing
        left to absorb the flags. The CJK leg pins the serialized-units cut:
        a body whose characters each serialize to six chars must come back
        NON-EMPTY and ellipsis-terminated, never emptied. RED at base (the
        round-1 code): the ASCII packet lands 16011 chars against the 16000
        bound and the CJK body comes back empty. RED-on-revert proofs:
        recomputing the body allowance without the flag reservation (FIX A)
        fails the ASCII bound below; restoring the char-arithmetic cut in
        _hard_cap (FIX B) fails the CJK non-empty/marker assertions."""
        # ASCII leg: single-paragraph ~30k body, budget 4000 tokens (16000
        # serialized chars), zero comments to pop.
        rc, out, err = _run_cli(
            "brief", "621", "--db", str(self.db_path), "--repo", REPO,
            "--budget-tokens", "4000", "--format", "json",
        )
        self.assertEqual(rc, 0, err.decode("utf-8", "replace"))
        env = json.loads(out.decode("utf-8"))
        self.assertTrue(env["ok"], env)
        data = env["data"]
        packet_chars = self._packet_chars(data)
        self.assertLessEqual(
            packet_chars, 4000 * 4,
            f"ASCII leg: packet chars/4 = {packet_chars / 4:.2f} exceeds"
            " --budget-tokens 4000 — the truncated/derived echoes must ride"
            " inside the envelope",
        )
        self.assertIs(
            data.get("truncated"), True,
            "a hard-capped body must set truncated: true",
        )
        body = data["body"]["untrusted_text"]
        self.assertTrue(body, "the hard-capped ASCII body must be non-empty")
        self.assertTrue(
            body.endswith("…"),
            "a single-paragraph body is cut at the character level and"
            " ends with the … marker",
        )

        # CJK leg: every 文 serializes to six chars (\u6587), so the cut
        # must be computed in serialized units for the body to survive.
        rc_c, out_c, err_c = _run_cli(
            "brief", "622", "--db", str(self.db_path), "--repo", REPO,
            "--budget-tokens", "4000", "--format", "json",
        )
        self.assertEqual(rc_c, 0, err_c.decode("utf-8", "replace"))
        env_c = json.loads(out_c.decode("utf-8"))
        self.assertTrue(env_c["ok"], env_c)
        data_c = env_c["data"]
        packet_chars_c = self._packet_chars(data_c)
        self.assertLessEqual(
            packet_chars_c, 4000 * 4,
            f"CJK leg: packet chars/4 = {packet_chars_c / 4:.2f} exceeds"
            " --budget-tokens 4000",
        )
        self.assertIs(
            data_c.get("truncated"), True,
            "CJK leg: a hard-capped body must set truncated: true",
        )
        body_c = data_c["body"]["untrusted_text"]
        self.assertTrue(
            body_c, "CJK leg: the hard cap must not empty the body"
        )
        self.assertTrue(
            body_c.endswith("…"),
            "CJK leg: the hard-capped body ends with the … marker",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
