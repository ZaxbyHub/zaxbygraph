"""Arm's-length acceptance checks for issue #30 (triage / bundle / brief)
plus the bundled #44 fixes, authored against the trace's acceptance table
(.agents/issue-traces/30-triage-bundle-brief/) BEFORE any implementation.

Frozen check names (02-reproduction.md acceptance table, notes column):

    C1  test_triage_matches_hand_computed_formula
    C2  test_triage_gates_named
    C3  test_bundles_disjoint_and_sized
    C4  test_bundle_payload_complete
    C5  test_duplicates_and_related_not_bundleable
    C6  test_brief_budget_and_trust_marking
    C7  test_open_and_churn_surface
    C8  test_skill_documents_bundle_workflow
    C10 test_eval_scopes_path_evidence_to_the_item_cutoff   (44-AC1)
    C11 test_hub_share_fires_on_majority_paths              (44-AC2)
    C12 test_spool_removed_when_manifest_tempfile_fails     (44-AC3)

NEW-SURFACE module: the triage/bundle/brief commands do not exist at base,
so every new-surface leg fails through the CLI (exit 2 / assertion mismatch)
while the module itself COLLECTS cleanly — the derive symbols that arrive
with the fix are reached only through the subprocess CLI or via keyword
arguments inside test bodies, never at import time.

Hermetic: FakeGitHubSource-derived bulk source only; no network, no gh
(the one --live leg points PATH at an empty directory on purpose).
Stdlib only; unittest (no pytest).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path

from fixtures import (
    REPO,
    FakeGitHubSource,
    comment,
    issue,
    pr_file,
    pull,
    scrubbed_env,
)
from fixtures_eval import _insert_edge, build_eval_store

from zaxbygraph.db import connect, init_schema, open_existing
from zaxbygraph.derive import build_context, explain, load_weights, similar
from zaxbygraph.evaluate import run_eval
from zaxbygraph.sync import sync_repo

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Deterministic "latest synced_at" anchor for every store whose stage-2
#: hotspot the hand computation depends on (04-root-cause.md section 2:
#: "recency = 1.0 if merged within 90 days of the LATEST synced_at (echoed
#: as hotspot_asof)"). Pinning both sync_state stamps removes the wall clock
#: from the arithmetic: PRs merged 2027-06-xx are inside the 90-day window
#: of 2027-07-01, PRs merged 2027-03-xx are outside it.
PINNED_SYNCED_AT = "2027-07-01T00:00:00Z"


# ==== shared plumbing ========================================================


class _BulkSource(FakeGitHubSource):
    """FakeGitHubSource + the GraphQL-only children sections (the
    tests/test_link_stream.py _BulkLinkSource pattern): list_issues yields
    one PAGE, fetch_children returns per-item children dicts carrying
    timeline / links sections, so blocked_by / linked_pr edges and reopened
    events reach the store through the real sync path."""

    def __init__(self) -> None:
        super().__init__()
        self.links: dict[int, dict] = {}
        self.links_incomplete: set[int] = set()
        self.timelines: dict[int, list[dict]] = {}

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
            if n in self.timelines:
                children["timeline"] = deepcopy(self.timelines[n])
            if n in self.links:
                children["links"] = deepcopy(self.links[n])
                if n in self.links_incomplete:
                    children["links_incomplete"] = True
            out[n] = children
        return out


def _node(number: int, repo: str = REPO) -> dict:
    """One link-connection endpoint in the house {"number", "repo"} shape."""
    return {"number": number, "repo": repo}


def _add_comment(
    src: _BulkSource,
    number: int,
    cid: int,
    body: str,
    author: str,
    created_at: str | None = None,
) -> dict:
    """Append one issue comment with an EXPLICIT globally-unique github id.

    FakeGitHubSource.comment_on derives ids from len(comments)+number, which
    collides across numbers (comments UNIQUE(repo, kind, github_id) is
    GLOBAL, so a collision silently folds rows — the trap fixtures_bulk's
    add_bulk_prs documents). Every id passed here must be unique across the
    whole store.
    """
    rec = comment(cid, body, author=author, created_at=created_at)
    src.issue_comments.setdefault(number, []).append(rec)
    src.issues[number]["comments"] = len(src.issue_comments[number])
    return rec


def _links(
    *,
    blocked_by: tuple[dict, ...] = (),
    parent: dict | None = None,
    duplicate_of: dict | None = None,
    open_prs: tuple[dict, ...] = (),
) -> dict:
    """A complete `links` children section (every connection reported)."""
    return {
        "blocked_by": [dict(n) for n in blocked_by],
        "parent": dict(parent) if parent else None,
        "duplicate_of": dict(duplicate_of) if duplicate_of else None,
        "open_prs": [dict(n) for n in open_prs],
    }


def _merged_pull(number: int, merged_at: str) -> dict:
    p = pull(number, merged=True)
    p["merged_at"] = merged_at
    return p


def _pin_synced_at(conn, repo: str = REPO) -> None:
    """Freeze the store's freshness stamps at PINNED_SYNCED_AT so the
    hotspot recency window is deterministic."""
    conn.execute(
        "UPDATE sync_state SET last_full_sync_at = ?, last_incr_sync_at = ?"
        " WHERE repo = ?",
        (PINNED_SYNCED_AT, PINNED_SYNCED_AT, repo),
    )
    conn.commit()


def _run_cli(*argv: str, path_override: str | None = None) -> tuple[int, bytes, bytes]:
    """One CLI leg: subprocess python -m zaxbygraph with the repo's src and
    tests on PYTHONPATH (Windows ';' handled by os.pathsep) and a scrubbed
    child environment (tests/test_similar.py CliContractTests pattern)."""
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


def _cli_json(*argv: str, path_override: str | None = None) -> dict:
    rc, out, err = _run_cli(*argv, path_override=path_override)
    if rc != 0:
        raise AssertionError(
            f"CLI {argv!r} exited {rc}; stderr:\n{err.decode('utf-8', 'replace')}"
        )
    return json.loads(out.decode("utf-8"))


def _bundle_member_numbers(bundle: dict) -> list[int]:
    """Members may be dicts ({number, title, ...}) or bare numbers."""
    out = []
    for m in bundle["members"]:
        out.append(int(m["number"]) if isinstance(m, dict) else int(m))
    return out


def _paths_from(entries) -> set[str]:
    """A files payload section (list of paths or of {path, ...} dicts)."""
    out: set[str] = set()
    if not isinstance(entries, list):
        return out
    for e in entries:
        if isinstance(e, str):
            out.add(e)
        elif isinstance(e, dict) and isinstance(e.get("path"), str):
            out.add(e["path"])
    return out


def _bundle_file_set(bundle: dict) -> set[str]:
    files = bundle.get("files") or {}
    direct = _paths_from(files.get("direct"))
    linked = _paths_from(files.get("linked"))
    analog = _paths_from(files.get("analog"))
    return direct | linked | analog


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
        raise NotImplementedError


# ==== C1: the documented triage formula =====================================


class TriageFormulaTests(_StoreTestCase):
    """Hand-computed store for 04-root-cause.md section 2 under the BLESSED
    defaults (issue 30 comment 6510715064): normalizations reactions
    min(1,x/3), participants min(1,x/3), comments min(1,x/5), mentions
    min(1,x/10) (blocking/gap stay /3, reopens/file_reverts min(1,x/1)),
    priority map values used raw clamped [0,1], stage2_weight default 0.25,
    and the missing-feature rule: a feature with NO source data (no labels
    rows; absent [triage.fields]/[triage.types] table) is MISSING (null +
    requires, excluded from the group mean; a group with no present
    features contributes 0); only a CONFIGURED table with no match is a
    present 0.0. Every number in the test body comments is derived here,
    not measured.

    Config (explicit --config, so config_source == "explicit"):
        [triage] demand/leverage/risk/priority = 1.0, top_k = 50 (no
        stage2_weight key — the blessed 0.25 DEFAULT must apply);
        [triage.labels] high = 2.0; [triage.types] Bug = 3.0 — both above
        the [0,1] clamp, so the priority features exercise the clamp — and
        NO [triage.fields] table, so field_weight is missing for every
        item.

    Merged PRs (state closed, merged within 90d of PINNED_SYNCED_AT):
        PR 20 merged 2027-06-10: [src/alpha.py]
        PR 21 merged 2027-06-20: [src/beta.py]
        PR 25 merged 2027-06-28: [src/unrelated.py]
    Plus the GitHub-reported-shaped reverts edge 25 -> 21 (PR 21 reverted).
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        # PR bodies carry their own path refs: item_path_refs derive from
        # text only, so a PR needs a body mention to be a similar() (and
        # therefore analog) candidate at all.
        src.add_pr(
            issue(20, title="Alpha plumbing", body="updates src/alpha.py:1:1",
                  kind="pr", state="closed", created_at="2027-06-01T00:00:00Z"),
            _merged_pull(20, "2027-06-10T00:00:00Z"),
            files=[pr_file("src/alpha.py")],
        )
        src.add_pr(
            issue(21, title="Beta plumbing", body="updates src/beta.py:1:1",
                  kind="pr", state="closed", created_at="2027-06-01T00:00:00Z"),
            _merged_pull(21, "2027-06-20T00:00:00Z"),
            files=[pr_file("src/beta.py")],
        )
        src.add_pr(
            issue(25, title="Revert Beta plumbing",
                  body="reverts the change in src/unrelated.py:1:1", kind="pr",
                  state="closed", created_at="2027-06-25T00:00:00Z"),
            _merged_pull(25, "2027-06-28T00:00:00Z"),
            files=[pr_file("src/unrelated.py")],
        )

        milestone = {"title": "m1", "due_on": "2030-01-01T00:00:00Z", "state": "open"}

        # 201: reactions +1=5, comments by bob+carol, duplicate_of from 204,
        # mention from 205, sub-issue gap 2, label high, type Bug,
        # body refs src/alpha.py.
        rec201 = issue(
            201,
            title="Crash on submit in alpha",
            body="crash at src/alpha.py:10:2 when submitting",
            created_at="2027-06-05T00:00:00Z",
        )
        rec201["reactions"] = {"+1": 5, "total_count": 5}
        rec201["milestone"] = milestone
        rec201["type"] = {"name": "Bug"}
        rec201["labels"] = [{"name": "high", "color": "ff0000"}]
        rec201["sub_issues_summary"] = {"total": 3, "completed": 1}
        src.add_issue(rec201)
        _add_comment(src, 201, 60201, "reproduced on main", author="bob")
        _add_comment(src, 201, 60202, "trace points at the submit path", author="carol")

        # 202: NO reactions key in raw_json (the null+requires case), one
        # comment by bob, one reopened event, body refs src/beta.py; its only
        # PR analog (PR 21) is reverted, and PR 21 touches its resolved path.
        rec202 = issue(
            202,
            title="Leak in beta handler",
            body="leak at src/beta.py:4:2 in handler",
            created_at="2027-06-05T00:00:00Z",
        )
        rec202["milestone"] = milestone
        src.add_issue(rec202)
        _add_comment(src, 202, 60210, "valgrind shows the leak", author="bob")
        src.timelines[202] = [
            {"type": "reopened", "created_at": "2027-06-15T00:00:00Z",
             "actor": {"login": "bob"}},
        ]

        # 203: no resolvable path refs (docs/guide.md is not in vocabulary).
        rec203 = issue(
            203,
            title="Docs typo in guide",
            body="typo in docs/guide.md paragraph 2",
            created_at="2027-06-05T00:00:00Z",
        )
        rec203["reactions"] = {"+1": 0, "total_count": 0}
        rec203["milestone"] = milestone
        src.add_issue(rec203)

        # 204: reactions +1=20, type Bug, no labels, no path refs; the
        # GitHub-reported duplicate_of edge 204 -> 201.
        rec204 = issue(
            204,
            title="Same crash as the alpha one",
            body="same crash, different reporter",
            author="dave",
            created_at="2027-06-05T00:00:00Z",
        )
        rec204["reactions"] = {"+1": 20, "total_count": 20}
        rec204["milestone"] = milestone
        rec204["type"] = {"name": "Bug"}
        src.add_issue(rec204)

        # 205: plain followup; its mentions edge 205 -> 201 is 201's one
        # inbound mention (GitHub-reported timeline shape).
        rec205 = issue(
            205,
            title="Alpha crash followup",
            body="tracking the alpha crash aftermath",
            created_at="2027-06-05T00:00:00Z",
        )
        rec205["reactions"] = {"+1": 0, "total_count": 0}
        rec205["milestone"] = milestone
        src.add_issue(rec205)

        sync_repo(conn, src, REPO)
        _insert_edge(conn, REPO, 204, "duplicate_of", 201)  # src=dup dst=target
        _insert_edge(conn, REPO, 205, "mentions", 201, source="timeline")
        _insert_edge(conn, REPO, 25, "reverts", 21, source="timeline")
        _pin_synced_at(conn)

    def test_triage_matches_hand_computed_formula(self) -> None:
        """RED at base, GREEN after the fix. The CLI rows equal the
        hand-computed documented formula exactly (scores ~1e-9, order
        total DESC number ASC) and the payload echoes weights +
        config_source + top_k/stage2_weight + triage_asof +
        formula_version."""
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "triage.toml"
            cfg.write_text(
                "[triage]\n"
                "demand = 1.0\nleverage = 1.0\nrisk = 1.0\npriority = 1.0\n"
                "top_k = 50\n"
                "[triage.labels]\nhigh = 2.0\n"
                "[triage.types]\nBug = 3.0\n",
                encoding="utf-8",
            )
            env = _cli_json(
                "triage", "--db", str(self.db_path), "--repo", REPO,
                "--config", str(cfg), "--format", "json",
            )
        self.assertTrue(env["ok"], env)
        data = env["data"]
        for key in ("rows", "weights", "config_source", "top_k", "stage2_weight"):
            self.assertIn(key, data)
        self.assertEqual(data["config_source"], "explicit")
        self.assertEqual(data["top_k"], 50)
        # stage2_weight is NOT in the config: the blessed DEFAULT 0.25 must
        # apply and be echoed.
        self.assertEqual(data["stage2_weight"], 0.25)
        # the payload names the formula and the deterministic as-of stamp
        # (the fixture pins both sync_state stamps to PINNED_SYNCED_AT).
        self.assertEqual(data.get("formula_version"), "triage.v1")
        self.assertEqual(data.get("triage_asof"), PINNED_SYNCED_AT)
        self.assertFalse(data.get("truncated"))
        w = data["weights"]
        self.assertEqual(w["demand"], 1.0)
        self.assertEqual(w["leverage"], 1.0)
        self.assertEqual(w["risk"], 1.0)
        self.assertEqual(w["priority"], 1.0)
        self.assertEqual(w["stage2_weight"], 0.25)
        self.assertEqual(w["top_k"], 50)
        self.assertEqual(w["labels"]["high"], 2.0)
        self.assertEqual(w["types"]["Bug"], 3.0)

        rows = {r["number"]: r for r in data["rows"]}
        # Documented ORDER: total_score DESC, number ASC. Hand totals:
        #   201: 1393/720 ~= 1.9347   202: 65/48 ~= 1.3542   204: 19/15
        #   ~= 1.2667   203: 1/15   205: 1/15  (203 and 205 tie -> number
        #   ASC puts 203 first).
        self.assertEqual(
            [r["number"] for r in data["rows"]], [201, 202, 204, 203, 205]
        )

        # ---- issue 201 -----------------------------------------------------
        # Raw features: reactions_plus1=5, participants=3 (alice+bob+carol),
        # comments=2, inbound_duplicates=1 (204 -> 201 duplicate_of),
        # inbound_mentions=1 (205 -> 201), blocking_open=0, sub_issue_gap=2
        # (3 total, 1 completed), milestone_due=2030-01-01 (far future -> 0),
        # reopens=0, file_reverts=0, issue_type="Bug", label_weight=2.0.
        #
        # Normalized (BLESSED defaults): min(1, 5/3)=1.0, min(1, 3/3)=1.0,
        # min(1, 2/5)=0.4, duplicates>=1 -> 1.0, min(1, 1/10)=0.1,
        # blocking 0, min(1, 2/3)=2/3, milestone 0, reopens 0, file_reverts
        # 0; priority used raw clamped [0,1]: type min(1,3.0)=1.0,
        # label min(1,2.0)=1.0.
        #
        # BLESSED missing-feature rule: a feature with NO source data is
        # MISSING (null + requires, excluded from the group mean); only a
        # CONFIGURED table with no match is a present 0.0. The config has
        # [triage.labels] and [triage.types] configured but NO
        # [triage.fields] table, so field_weight is MISSING for every item.
        # 201 has labels rows ([high], matching) and item_type "Bug"
        # (matching) -> TWO present priority features.
        #
        # demand   = (1.0 + 1.0 + 0.4 + 1.0 + 0.1) / 5 = 3.5/5 = 7/10
        # leverage = (0 + 2/3 + 0) / 3 = 2/9
        # risk     = (0 + 0) / 2 = 0
        # priority = (1.0 + 1.0) / 2 = 1.0        (field missing, excluded)
        # stage1   = 7/10 + 2/9 + 0 + 1 = (63 + 20 + 90)/90 = 173/90
        #          = 1.9222222222222222
        # hotspot  = PRs touching resolved src/alpha.py = {PR 20}, merged
        #            2027-06-10, within 90d of 2027-07-01 -> recency 1.0;
        #            min(1, 1.0/10) = 0.1
        # regression = 0 (analog PR 20 has no inbound reverts edge)
        # stage2_bonus = 0.25 * (0.1 + 0)/2 = 0.0125
        # total = 173/90 + 0.0125 = (1384 + 9)/720 = 1393/720
        #       = 1.9347222222222222
        r201 = rows[201]
        self.assertAlmostEqual(r201["total_score"], 1393 / 720, places=9)
        self.assertAlmostEqual(r201["scores"]["stage1"], 173 / 90, places=9)
        g = r201["scores"]["groups"]
        self.assertAlmostEqual(g["demand"], 0.7, places=9)
        self.assertAlmostEqual(g["leverage"], 2 / 9, places=9)
        self.assertAlmostEqual(g["risk"], 0.0, places=9)
        # priority mean over the TWO present features (type + label), the
        # missing field feature excluded.
        self.assertAlmostEqual(g["priority"], 1.0, places=9)
        fr = r201["scores"]["features_raw"]
        self.assertEqual(fr["reactions_plus1"], 5)
        self.assertEqual(fr["participants"], 3)
        self.assertEqual(fr["comments"], 2)
        self.assertEqual(fr["inbound_duplicates"], 1)
        self.assertEqual(fr["inbound_mentions"], 1)
        self.assertEqual(fr["sub_issue_gap"], 2)
        # present priority features echo their source data; field_weight:
        # NO [triage.fields] table -> null + requires note.
        self.assertEqual(fr["issue_type"], "Bug")
        self.assertEqual(fr["label_weight"], 2.0)
        self.assertIsNone(fr["field_weight"])
        self.assertIn(
            "requires", json.dumps(r201),
            "the missing field feature must carry a requires note",
        )
        fn = r201["scores"]["features_normalized"]
        self.assertAlmostEqual(fn["reactions_plus1"], 1.0, places=9)
        self.assertAlmostEqual(fn["participants"], 1.0, places=9)
        self.assertAlmostEqual(fn["comments"], 0.4, places=9)
        self.assertAlmostEqual(fn["inbound_duplicates"], 1.0, places=9)
        self.assertAlmostEqual(fn["inbound_mentions"], 0.1, places=9)
        self.assertAlmostEqual(fn["sub_issue_gap"], 2 / 3, places=9)
        self.assertAlmostEqual(fn["milestone_due"], 0.0, places=9)
        self.assertAlmostEqual(r201["hotspot"], 0.1, places=9)
        self.assertEqual(r201["regression"], 0)
        self.assertAlmostEqual(r201["stage2"], 0.0125, places=9)
        analog = r201.get("analog")
        self.assertTrue(
            isinstance(analog, dict) and analog.get("number") == 20,
            f"201's analog must echo PR 20, got {analog!r}",
        )

        # ---- issue 202 (the missing-feature null+requires case) ------------
        # reactions absent from raw_json -> feature null with a requires
        # note, EXCLUDED from the demand mean (4 present features).
        # demand = (min(1,2/3) + min(1,1/5) + 0 + 0) / 4
        #        = (2/3 + 1/5) / 4 = (13/15) / 4 = 13/60 = 0.2166666666666667
        # leverage = 0
        # risk = (min(1,1/1) + min(1,1/1)) / 2 = 1.0
        #   (reopens: one 'reopened' item_event; file_reverts: PR 21 touches
        #    the resolved path src/beta.py AND has an inbound reverts edge)
        # priority = 0: ALL THREE priority features are missing (no
        #   item_type value, no labels rows, no [triage.fields] table) and a
        #   group with no present features contributes 0.
        # stage1 = 13/60 + 0 + 1.0 + 0 = 73/60 = 1.2166666666666667
        # hotspot = 0.1 (PR 21 merged 2027-06-20, within 90d)
        # regression = 1 (analog PR 21 is reverted by PR 25)
        # stage2_bonus = 0.25 * (0.1 + 1)/2 = 0.1375
        # total = 73/60 + 0.1375 = (292 + 33)/240 = 325/240 = 65/48
        #       = 1.3541666666666667
        r202 = rows[202]
        self.assertAlmostEqual(r202["total_score"], 65 / 48, places=9)
        self.assertAlmostEqual(r202["scores"]["stage1"], 73 / 60, places=9)
        g2 = r202["scores"]["groups"]
        self.assertAlmostEqual(g2["demand"], 13 / 60, places=9)
        self.assertAlmostEqual(g2["risk"], 1.0, places=9)
        # an all-missing priority group contributes 0
        self.assertAlmostEqual(g2["priority"], 0.0, places=9)
        fr2 = r202["scores"]["features_raw"]
        self.assertIsNone(fr2["reactions_plus1"])
        self.assertIn("requires", json.dumps(r202), "null feature must carry a requires note")
        fn2 = r202["scores"]["features_normalized"]
        reactions_norm = fn2.get("reactions_plus1")
        self.assertTrue(
            reactions_norm is None
            or (isinstance(reactions_norm, dict) and reactions_norm.get("value") is None),
            f"absent reactions must be null, not a score: {reactions_norm!r}",
        )
        self.assertAlmostEqual(fn2["reopens"], 1.0, places=9)
        self.assertAlmostEqual(fn2["file_reverts"], 1.0, places=9)
        self.assertAlmostEqual(r202["hotspot"], 0.1, places=9)
        self.assertEqual(r202["regression"], 1)
        self.assertAlmostEqual(r202["stage2"], 0.1375, places=9)
        analog2 = r202.get("analog")
        self.assertTrue(
            isinstance(analog2, dict) and analog2.get("number") == 21,
            f"202's analog must echo PR 21, got {analog2!r}",
        )

        # ---- issue 204 -----------------------------------------------------
        # demand = (min(1,20/3)=1.0 + min(1,1/3)=1/3 + 0 + 0 + 0)/5
        #        = (4/3)/5 = 4/15 = 0.2666666666666667
        # leverage 0, risk 0.
        # BLESSED missing-feature rule: 204 has item_type "Bug" (the
        # CONFIGURED [triage.types] table matches -> present, clamped
        # min(1,3.0)=1.0) but NO labels rows (label feature MISSING) and no
        # [triage.fields] table (field feature MISSING) -> its only PRESENT
        # priority feature is type.
        # priority = 1.0 / 1 = 1.0        (mean over ONE present feature)
        # stage1 = 4/15 + 0 + 0 + 1.0 = 19/15 = 1.2666666666666667
        # no resolved paths -> hotspot 0, no PR analog -> regression 0;
        # total = 19/15.
        r204 = rows[204]
        self.assertAlmostEqual(r204["total_score"], 19 / 15, places=9)
        self.assertAlmostEqual(r204["scores"]["stage1"], 19 / 15, places=9)
        self.assertAlmostEqual(r204["scores"]["groups"]["demand"], 4 / 15, places=9)
        self.assertAlmostEqual(r204["scores"]["groups"]["priority"], 1.0, places=9)
        # the missing label/field features show as null with requires
        # notes, and the priority group mean is over 1 present feature
        # (asserted via the scores: the group equals the single present
        # type feature's clamped value 1.0).
        fr204 = r204["scores"]["features_raw"]
        self.assertIsNone(fr204["label_weight"], "no labels rows -> label feature missing")
        self.assertIsNone(fr204["field_weight"], "no [triage.fields] table -> field feature missing")
        self.assertIn(
            "requires", json.dumps(r204),
            "204's missing priority features must carry requires notes",
        )
        norm_label = r204["scores"]["features_normalized"].get("label_weight")
        self.assertTrue(
            norm_label is None
            or (isinstance(norm_label, dict) and norm_label.get("value") is None),
            f"missing label feature must be null, not a score: {norm_label!r}",
        )
        self.assertAlmostEqual(r204["hotspot"], 0.0, places=9)
        self.assertEqual(r204["regression"], 0)

        # ---- issues 203 / 205 (the total-score tie, broken by number) ------
        # demand = (0.0 + 1/3 + 0 + 0 + 0)/5 = 1/15 = 0.06666666666666667;
        # priority: all three features missing -> group 0; everything
        # else 0.
        for n in (203, 205):
            self.assertAlmostEqual(rows[n]["total_score"], 1 / 15, places=9)
            self.assertAlmostEqual(rows[n]["scores"]["groups"]["demand"], 1 / 15, places=9)
            self.assertAlmostEqual(rows[n]["scores"]["groups"]["priority"], 0.0, places=9)
            self.assertAlmostEqual(rows[n]["hotspot"], 0.0, places=9)

        # hotspot_asof is echoed and equals the pinned latest synced_at
        # (same stamp as triage_asof).
        asof = data.get("hotspot_asof") or rows[201].get("hotspot_asof")
        self.assertEqual(asof, PINNED_SYNCED_AT)


# ==== C2: named gates =======================================================


class TriageGatesTests(_StoreTestCase):
    """Six open issues: one clean, five carrying exactly one gate each.

    - 302 blocked by OPEN issue 303 (blocked_by link, src=blocked dst=blocker;
      303 itself is locked so it is gated too and never ranks clean).
    - 304 has an assignee row (listing-payload assignees, the real sync path).
    - 305 has an open linked PR 306 (links.open_prs -> rel='linked_pr'
      source='link').
    - 307 locked.
    - 308 has ONLY a closes_keyword edge from OPEN PR 309 ("fixes #308" in
      the PR body) -> the append-only stale fallback label.
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        src.add_issue(issue(301, title="clean work item", body="plain"))
        src.add_issue(issue(302, title="blocked work item", body="plain"))
        rec303 = issue(303, title="the blocker", body="plain")
        rec303["locked"] = True
        src.add_issue(rec303)
        src.links[302] = _links(blocked_by=(_node(303),))
        rec304 = issue(304, title="assigned work item", body="plain")
        rec304["assignees"] = [{"login": "erin"}]
        src.add_issue(rec304)
        src.add_issue(issue(305, title="linked work item", body="plain"))
        src.add_pr(
            issue(306, title="open pull for 305", body="", kind="pr"),
            pull(306, merged=False),
            files=[pr_file("src/anything.py")],
        )
        src.links[305] = _links(open_prs=(_node(306),))
        rec307 = issue(307, title="locked work item", body="plain")
        rec307["locked"] = True
        src.add_issue(rec307)
        src.add_issue(issue(308, title="keyword-linked work item", body="plain"))
        src.add_pr(
            issue(309, title="fix in progress", body="This change fixes #308.",
                  kind="pr"),
            pull(309, merged=False),
            files=[pr_file("src/handlers.py")],
        )
        sync_repo(conn, src, REPO)

    def test_triage_gates_named(self) -> None:
        """RED at base, GREEN after the fix. The observable default-exclusion
        contract: the DEFAULT payload's ranked rows contain ONLY the ungated
        issue (301, ranked:true) — gated issues do not appear in the default
        rows at all — while --include-gated ADDS the six gated rows, each
        with ranked:false, named gates and raw features. The
        closes_keyword fallback gate carries the append-only stale label."""
        # DEFAULT: only the ungated issue is in the rows.
        env = _cli_json(
            "triage", "--db", str(self.db_path), "--repo", REPO, "--format", "json"
        )
        self.assertTrue(env["ok"], env)
        default_rows = env["data"]["rows"]
        self.assertEqual(
            [r["number"] for r in default_rows], [301],
            "the default payload's ranked rows must contain only the"
            " ungated issue; gated issues are excluded",
        )
        self.assertIs(default_rows[0].get("ranked"), True)

        # --include-gated: the six gated rows JOIN 301, each ranked:false
        # with its gate named and raw features kept.
        env_ig = _cli_json(
            "triage", "--db", str(self.db_path), "--repo", REPO,
            "--include-gated", "--format", "json",
        )
        self.assertTrue(env_ig["ok"], env_ig)
        rows = {r["number"]: r for r in env_ig["data"]["rows"]}
        for n in (301, 302, 303, 304, 305, 307, 308):
            self.assertIn(n, rows, f"issue {n} missing from --include-gated rows")
        # PRs 306/309 are never triage targets (kind='pr').
        self.assertNotIn(306, rows)
        self.assertNotIn(309, rows)

        self.assertIs(rows[301].get("ranked"), True, "the ungated row must carry ranked:true")
        ranked_true = [n for n, r in rows.items() if r.get("ranked") is True]
        self.assertEqual(ranked_true, [301], "the clean issue must be the only ranked row")
        for n in (302, 303, 304, 305, 307, 308):
            self.assertIs(rows[n].get("ranked"), False, f"gated row {n} must carry ranked:false")
            self.assertTrue(rows[n].get("gates"), f"gated row {n} keeps its gates")
            self.assertTrue(rows[n].get("scores"), f"gated row {n} keeps its raw features")

        self.assertIn("blocked", rows[302]["gates"])
        self.assertIn("locked", rows[303]["gates"])
        self.assertIn("assigned", rows[304]["gates"])
        # The link-stream linked_pr gate is the exact label, not the stale
        # fallback variant.
        self.assertIn("linked_pr", rows[305]["gates"])
        self.assertIn("locked", rows[307]["gates"])
        # The closes_keyword/closes fallback from an OPEN PR is labeled with
        # the append-only stale marker.
        self.assertTrue(
            any("append-only" in g for g in rows[308]["gates"]),
            f"308's gate must carry the append-only label, got {rows[308]['gates']!r}",
        )


# ==== C3: bundle sizing, disjointness, tie-break ============================


class BundleDisjointTests(_StoreTestCase):
    """Two disjoint path families plus one shared-file issue.

    - Family alpha: issues 401, 402, 403 (bodies ref src/alpha.py).
    - Family beta:  issues 404, 405 (bodies ref src/beta.py).
    - 406 refs BOTH src/alpha.py and src/beta.py (the shared-file candidate).
    - 401..405 carry identical triage features (reactions +1=10 -> demand
      0.24 each); 406 has no reactions (demand 0.05 with the null excluded)
      so its total stays BELOW the family members even though its hotspot
      spans both families. All seeds tie -> broken by number ASC.
    - PRs 420/421 are the only merged touchers (merged 2027-03-xx, OUTSIDE
      the 90-day window of the pinned synced_at -> recency 0.5).
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        src.add_pr(
            issue(420, title="alpha history", body="", kind="pr", state="closed",
                  created_at="2027-03-01T00:00:00Z"),
            _merged_pull(420, "2027-03-01T00:00:00Z"),
            files=[pr_file("src/alpha.py")],
        )
        src.add_pr(
            issue(421, title="beta history", body="", kind="pr", state="closed",
                  created_at="2027-03-01T00:00:00Z"),
            _merged_pull(421, "2027-03-02T00:00:00Z"),
            files=[pr_file("src/beta.py")],
        )
        for n, body in (
            (401, "see src/alpha.py:1:1"),
            (402, "see src/alpha.py:2:2"),
            (403, "see src/alpha.py:3:3"),
            (404, "see src/beta.py:1:1"),
            (405, "see src/beta.py:2:2"),
        ):
            rec = issue(n, title="Crash in module", body=body,
                        created_at="2027-05-01T00:00:00Z")
            rec["reactions"] = {"+1": 10, "total_count": 10}
            src.add_issue(rec)
        src.add_issue(
            issue(406, title="Crash in module",
                  body="see src/alpha.py:4:4 and src/beta.py:4:4",
                  created_at="2027-05-01T00:00:00Z")
        )
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)

    def test_bundles_disjoint_and_sized(self) -> None:
        """RED at base, GREEN after the fix. Greedy over the pool yields
        bundles of size 2-3 with no file shared across bundles by default;
        --allow-shared-files permits a bundle sharing a file; equal-score
        pool members are picked lower-number-first."""
        # Default leg: --count 2 caps the bundle count.
        env = _cli_json(
            "bundle", "--db", str(self.db_path), "--repo", REPO,
            "--count", "2", "--format", "json",
        )
        self.assertTrue(env["ok"], env)
        bundles = env["data"]["bundles"]
        self.assertLessEqual(len(bundles), 2)
        sizes = [len(b["members"]) for b in bundles]
        for s in sizes:
            self.assertIn(s, (2, 3), f"bundle sizes must be 2-3, got {sizes}")
        self.assertEqual(len(bundles), 2)
        self.assertIn(3, sizes, "greedy must grow a bundle to the size cap")
        members = [_bundle_member_numbers(b) for b in bundles]
        # Tie-break by number: 401 ties with 404/405 on total score, so the
        # lower number seeds first and its family is bundle[0].
        self.assertEqual(set(members[0]), {401, 402, 403})
        self.assertEqual(set(members[1]), {404, 405})
        # Cross-bundle file disjointness by default; 406 (shared files) is
        # skipped for bundle 2 (shared-file conflict) and bundle 1 is full.
        file_sets = [_bundle_file_set(b) for b in bundles]
        for i in range(len(file_sets)):
            for j in range(i + 1, len(file_sets)):
                self.assertEqual(
                    file_sets[i] & file_sets[j], set(),
                    "default bundles must not share files",
                )
        all_members = {n for m in members for n in m}
        self.assertNotIn(406, all_members)

        # Shared-files leg: with --allow-shared-files the same pool lets 406
        # join the second bundle, so two bundles share src/alpha.py.
        env_shared = _cli_json(
            "bundle", "--db", str(self.db_path), "--repo", REPO,
            "--count", "2", "--allow-shared-files", "--format", "json",
        )
        self.assertTrue(env_shared["ok"], env_shared)
        shared_bundles = env_shared["data"]["bundles"]
        shared_members = {n for b in shared_bundles for n in _bundle_member_numbers(b)}
        self.assertIn(406, shared_members)
        shared_sets = [_bundle_file_set(b) for b in shared_bundles]
        self.assertTrue(
            any(
                shared_sets[i] & shared_sets[j]
                for i in range(len(shared_sets))
                for j in range(i + 1, len(shared_sets))
            ),
            "--allow-shared-files must permit a bundle sharing a file",
        )


# ==== C4: the full bundle payload ===========================================


class BundlePayloadTests(_StoreTestCase):
    """One bundle {501, 502} over src/core.py with every payload ingredient:

    - PR 510 merged touching [src/core.py, tests/test_core.py] (the coupling
      pair that makes tests/test_core.py a cochanged test).
    - PR 511 merged touching [src/linked.py] (vocabulary for the linked file).
    - PR 520 OPEN touching src/core.py (the open-PR collision).
    - 502 blocked by 501 via a blocked_by link; both are in the pool, so the
      in-pool relaxation keeps 502 (gate_relaxed) and the bundle order is
      the blocked_by topo order [501, 502].
    - 503 (gated out by an assignee, so never a pool member) mentions 501
      via a GitHub-reported timeline edge and refs src/linked.py -> the
      bundle's "linked" file.
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        src.add_pr(
            issue(510, title="core fix", body="fixes the fault at src/core.py:1:1",
                  kind="pr", state="closed", created_at="2027-06-01T00:00:00Z"),
            _merged_pull(510, "2027-06-10T00:00:00Z"),
            files=[pr_file("src/core.py"), pr_file("tests/test_core.py")],
        )
        src.add_pr(
            issue(511, title="linked area", body="", kind="pr", state="closed",
                  created_at="2027-06-01T00:00:00Z"),
            _merged_pull(511, "2027-06-11T00:00:00Z"),
            files=[pr_file("src/linked.py")],
        )
        src.add_pr(
            issue(520, title="open collision work", body="", kind="pr"),
            pull(520, merged=False),
            files=[pr_file("src/core.py")],
        )
        src.add_issue(
            issue(501, title="Fault in core path one",
                  body="fault at src/core.py:5:2",
                  created_at="2027-06-15T00:00:00Z")
        )
        src.add_issue(
            issue(502, title="Fault in core path two",
                  body="fault at src/core.py:6:2",
                  created_at="2027-06-15T00:00:00Z")
        )
        src.links[502] = _links(blocked_by=(_node(501),))
        rec503 = issue(503, title="adjacent report",
                       body="nearby area src/linked.py:1:1",
                       created_at="2027-06-15T00:00:00Z")
        rec503["assignees"] = [{"login": "zoe"}]
        src.add_issue(rec503)
        sync_repo(conn, src, REPO)
        # 503 -> 501 mention (GitHub-reported timeline shape): makes
        # src/linked.py a "linked" file of the bundle without putting 503
        # in the pool.
        _insert_edge(conn, REPO, 503, "mentions", 501, source="timeline")
        _pin_synced_at(conn)

    def _bundle_of(self, env: dict) -> dict:
        bundles = env["data"]["bundles"]
        self.assertTrue(bundles, "expected at least one bundle")
        for b in bundles:
            if set(_bundle_member_numbers(b)) == {501, 502}:
                return b
        self.fail(f"no bundle with members {{501, 502}}: {bundles!r}")

    def test_bundle_payload_complete(self) -> None:
        """RED at base, GREEN after the fix. One bundle's payload carries
        every AC4 field, the bundle_id equals the documented sha1 formula
        recomputed from the echoed config, and the id is stable across
        calls."""
        env = _cli_json(
            "bundle", "--db", str(self.db_path), "--repo", REPO, "--format", "json"
        )
        self.assertTrue(env["ok"], env)
        data = env["data"]
        for key in ("bundles", "merge_candidates", "related_not_bundleable",
                    "weights", "config_source"):
            self.assertIn(key, data)
        bundle = self._bundle_of(env)

        # members carry number + title.
        members = bundle["members"]
        self.assertEqual(len(members), 2)
        by_number = {m["number"]: m for m in members if isinstance(m, dict)}
        if len(by_number) == 2:  # dict-shaped members: titles present
            self.assertEqual(by_number[501]["title"], "Fault in core path one")

        # per-pair rationale: components + evidence from pair_components.
        rationale = None
        for key in ("pairs", "pair_rationale", "rationale"):
            v = bundle.get(key)
            if isinstance(v, list) and v:
                rationale = v
                break
            if isinstance(v, dict) and v:
                rationale = list(v.values())
                break
        self.assertIsNotNone(rationale, f"bundle carries no pair rationale: {sorted(bundle)}")
        blob = json.dumps(rationale)
        for needle in ("components", "evidence", "files_direct"):
            self.assertIn(needle, blob)

        # files split: direct (src/core.py), linked (src/linked.py via the
        # edge-linked 503's refs), analog (tagged "via #M").
        files = bundle["files"]
        self.assertIn("src/core.py", _paths_from(files.get("direct")))
        self.assertIn("src/linked.py", _paths_from(files.get("linked")))
        analog = files.get("analog")
        self.assertTrue(isinstance(analog, list) and analog,
                        "analog file list must be non-empty")
        for entry in analog:
            self.assertIsInstance(entry, dict)
            self.assertIn("path", entry)
            self.assertIn("via", entry)
        # 501's best PR analog is PR 510 (the only merged PR sharing
        # src/core.py), so at least one analog entry is tagged via #510.
        self.assertTrue(
            any("510" in str(e.get("via")) for e in analog),
            f"analog entries must tag the analog item, got {analog!r}",
        )

        # cochanged_tests: tests/ paths coupled to the bundle files via the
        # coupling index (PR 510 co-changes src/core.py and
        # tests/test_core.py); [triage] test_globs default ["tests/"].
        self.assertIn("tests/test_core.py", bundle.get("cochanged_tests", []))

        # open_pr_collisions: open PR 520 touches src/core.py.
        collisions = bundle.get("open_pr_collisions")
        self.assertTrue(
            collisions and "src/core.py" in json.dumps(collisions),
            f"collision with open PR 520 must list src/core.py, got {collisions!r}",
        )

        # order: in-pool blocked_by topo order (502 is blocked by 501).
        self.assertEqual(bundle["order"], [501, 502])

        # risk_summary / cohesion / why_not_bigger (shape pins).
        self.assertTrue(bundle.get("risk_summary"))
        self.assertIsInstance(bundle.get("cohesion"), (int, float))
        self.assertTrue(
            isinstance(bundle.get("why_not_bigger"), str) and bundle["why_not_bigger"],
            "why_not_bigger must be a non-empty reason string",
        )

        # the blocked-gate relaxation kept 502 in the pool, disclosed.
        self.assertTrue(
            any(
                isinstance(m, dict) and m.get("number") == 502 and m.get("gate_relaxed")
                for m in members
            ),
            "502 (blocked only by in-pool 501) must stay with gate_relaxed: true",
        )

        # bundle_id = sha1("zaxbygraph.bundle.v1|"
        #   + ",".join(str(n) for n in sorted(members)) + "|" + config_hash)
        # where config_hash = sha1 of the canonical JSON of the effective
        # [triage] dict (the payload's weights echo), canonical being
        # sort_keys + ensure_ascii + compact separators (the export.py house
        # form).
        weights = data["weights"]
        canonical = json.dumps(
            weights, sort_keys=True, ensure_ascii=True, separators=(",", ":")
        )
        config_hash = hashlib.sha1(canonical.encode("utf-8")).hexdigest()
        expected_id = hashlib.sha1(
            (
                "zaxbygraph.bundle.v1|"
                + ",".join(str(n) for n in sorted((501, 502)))
                + "|"
                + config_hash
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(bundle["bundle_id"], expected_id)

        # stable across two calls.
        env2 = _cli_json(
            "bundle", "--db", str(self.db_path), "--repo", REPO, "--format", "json"
        )
        bundle2 = self._bundle_of(env2)
        self.assertEqual(bundle2["bundle_id"], expected_id)


# ==== C5: duplicates and anchor-less text pairs =============================


class DuplicatesRelatedTests(_StoreTestCase):
    """- 601/602: identical title+body over src/thing.py with a
      GitHub-reported duplicate_of edge 602 -> 601 (merge candidate).
    - 631/632: plain issues joined ONLY by a text-stream duplicate_keyword
      edge 632 -> 631 (source='keyword', the GitHub shape extract emits) —
      also a merge candidate.
    - 641/642: near-identical titles (title component overlap 5/5 = 1.0 >=
      0.9) with NO anchor signals -> the possible_duplicate predicate's
      merge candidates, with evidence naming the title similarity.
    - 603/604: a second family over src/other.py (the bundleable pair).
    - 621/622: overlapping payments text + shared label (title component
      overlap 3/4 = 0.75 < 0.9), bodies reference docs/ paths that are NOT
      in the merged-PR vocabulary, no signatures, no edges, no cochange ->
      the anchor guard rejects the pair (related_not_bundleable with
      lexical/context evidence, NOT merge_candidates).
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        src.add_pr(
            issue(610, title="thing work", body="", kind="pr", state="closed",
                  created_at="2027-06-01T00:00:00Z"),
            _merged_pull(610, "2027-06-05T00:00:00Z"),
            files=[pr_file("src/thing.py"), pr_file("tests/test_thing.py")],
        )
        src.add_pr(
            issue(611, title="other work", body="", kind="pr", state="closed",
                  created_at="2027-06-01T00:00:00Z"),
            _merged_pull(611, "2027-06-06T00:00:00Z"),
            files=[pr_file("src/other.py")],
        )
        src.add_issue(
            issue(601, title="Crash in thing",
                  body="crash at src/thing.py:1:1",
                  created_at="2027-06-10T00:00:00Z")
        )
        src.add_issue(
            issue(602, title="Crash in thing",
                  body="crash at src/thing.py:1:1",
                  created_at="2027-06-10T00:00:00Z")
        )
        src.add_issue(
            issue(603, title="Fault in other",
                  body="fault at src/other.py:2:2",
                  created_at="2027-06-10T00:00:00Z")
        )
        src.add_issue(
            issue(604, title="Fault in other",
                  body="fault at src/other.py:3:3",
                  created_at="2027-06-10T00:00:00Z")
        )
        # Joined ONLY by the duplicate_keyword text-stream edge (no paths,
        # no other edges) — planted below via _insert_edge.
        src.add_issue(
            issue(631, title="Duplicate keyword report",
                  body="report body one",
                  created_at="2027-06-10T00:00:00Z")
        )
        src.add_issue(
            issue(632, title="Duplicate keyword report two",
                  body="report body two",
                  created_at="2027-06-10T00:00:00Z")
        )
        # possible_duplicate bait: near-identical titles, no anchors.
        src.add_issue(
            issue(641, title="Duplicate crash in payment retry loop",
                  body="first report of the duplicate",
                  created_at="2027-06-10T00:00:00Z")
        )
        src.add_issue(
            issue(642, title="Duplicate crash in payment retry loop again",
                  body="second report of the duplicate",
                  created_at="2027-06-10T00:00:00Z")
        )
        src.add_issue(
            issue(621, title="Payment flow fails on retry",
                  body="payment gateway timeout during retry; notes in docs/pay-a.md",
                  labels=[{"name": "payments", "color": "ff0000"}],
                  created_at="2027-06-10T00:00:00Z")
        )
        src.add_issue(
            issue(622, title="Payment flow fails on upload",
                  body="payment gateway timeout during upload; notes in docs/pay-b.md",
                  labels=[{"name": "payments", "color": "ff0000"}],
                  created_at="2027-06-10T00:00:00Z")
        )
        sync_repo(conn, src, REPO)
        _insert_edge(conn, REPO, 602, "duplicate_of", 601)
        _insert_edge(conn, REPO, 632, "duplicate_keyword", 631, source="keyword")
        _pin_synced_at(conn)

    def test_duplicates_and_related_not_bundleable(self) -> None:
        """RED at base, GREEN after the fix. A duplicate_of pair is offered
        as a merge candidate and never bundled together; a duplicate_keyword
        pair likewise produces merge_candidates; a possible_duplicate pair
        (near-identical titles, no anchors) lands in merge_candidates with
        title-similarity evidence; a text-related anchor-less pair with
        title similarity < 0.9 is listed in related_not_bundleable (NOT
        merge_candidates) with its lexical/context evidence, and neither
        member is ever bundled."""
        env = _cli_json(
            "bundle", "--db", str(self.db_path), "--repo", REPO, "--format", "json"
        )
        self.assertTrue(env["ok"], env)
        data = env["data"]
        merges = data["merge_candidates"]
        related = data["related_not_bundleable"]

        # duplicate_of pair -> merge_candidates ...
        self.assertTrue(
            any("601" in json.dumps(e) and "602" in json.dumps(e) for e in merges),
            f"merge_candidates must list the 601/602 duplicate_of pair, got {merges!r}",
        )
        # ... and the duplicate_keyword pair TOO (text-stream shape).
        self.assertTrue(
            any("631" in json.dumps(e) and "632" in json.dumps(e) for e in merges),
            f"merge_candidates must list the 631/632 duplicate_keyword pair, got {merges!r}",
        )
        # possible_duplicate: title overlap 5/5 = 1.0 >= 0.9 with no anchor
        # signals -> merge_candidates with evidence naming the title
        # similarity ...
        possible = next(
            (e for e in merges
             if "641" in json.dumps(e) and "642" in json.dumps(e)),
            None,
        )
        self.assertIsNotNone(
            possible,
            f"the 641/642 possible_duplicate pair (title overlap >= 0.9) must be"
            f" a merge candidate, got {merges!r}",
        )
        self.assertIn(
            "title", json.dumps(possible),
            "possible_duplicate evidence must name the title similarity",
        )
        # ... and it is NOT related_not_bundleable (that list requires title
        # similarity < 0.9).
        self.assertFalse(
            any("641" in json.dumps(e) and "642" in json.dumps(e) for e in related),
            "a possible_duplicate pair must not also be related_not_bundleable",
        )

        # text-related anchor-less pair with title overlap 3/4 = 0.75 < 0.9
        # -> related_not_bundleable with lexical/context evidence, NOT a
        # merge candidate.
        entry = next(
            (e for e in related
             if "621" in json.dumps(e) and "622" in json.dumps(e)),
            None,
        )
        self.assertIsNotNone(
            entry,
            f"the 621/622 text pair must be listed, got {related!r}",
        )
        blob = json.dumps(entry)
        self.assertIn("lexical", blob)
        self.assertIn("context", blob)
        self.assertFalse(
            any("621" in json.dumps(e) and "622" in json.dumps(e) for e in merges),
            "the sub-0.9 title pair must not be a merge candidate",
        )

        # none of the duplicate/related pairs are ever bundled together —
        # nor is either 621/622 member bundled at all (no anchor exists).
        for b in data["bundles"]:
            nums = set(_bundle_member_numbers(b))
            for pair in ((601, 602), (631, 632), (641, 642), (621, 622)):
                self.assertFalse(
                    set(pair) <= nums, f"pair {pair} bundled together: {nums}"
                )
            self.assertFalse(
                {621, 622} & nums,
                f"an anchor-less related item must never be bundled: {nums}",
            )

        # the second family still bundles (the surface works).
        all_members = {n for b in data["bundles"] for n in _bundle_member_numbers(b)}
        self.assertTrue({603, 604} <= all_members)


# ==== C6: brief budget and untrusted-content marking ========================


class BriefTests(_StoreTestCase):
    """701: a ~9.6KB multi-paragraph body (40 paragraphs x ~240 chars, each
    ending with the token "paraend"; paragraph 0 carries TRUSTMARKBODY, the
    LAST paragraph carries TRUSTMARKLASTPARA) + 6 comments, every
    GitHub-authored string carrying a unique marker. Under
    --budget-tokens 2000 (8000 serialized chars) the body CANNOT fit whole,
    so it is truncated at a paragraph boundary: early paragraphs survive,
    the last one does not, and the final surviving paragraph is complete.
    702: a bare issue (zero-hit packet must still answer ok:true)."""

    MARKERS = ["TRUSTMARKTITLE", "TRUSTMARKBODY"] + [
        f"TRUSTMARKC{i}" for i in range(6)
    ]

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        paragraphs = []
        for i in range(40):
            head = (
                "TRUSTMARKBODY opening paragraph: " if i == 0
                else f"paragraph {i}: "
            )
            if i == 39:
                head = "TRUSTMARKLASTPARA final paragraph: "
            paragraphs.append(head + "filler " * 30 + "paraend")
        src.add_issue(
            issue(
                701,
                title="Flaky TRUSTMARKTITLE submit chain",
                body="\n\n".join(paragraphs),
                created_at="2027-06-01T00:00:00Z",
            )
        )
        for i in range(6):
            src.comment_on(
                701, f"TRUSTMARKC{i} comment number {i} with detail text", author="bob"
            )
        src.add_issue(issue(702, title="Bare issue", body="",
                            created_at="2027-06-01T00:00:00Z"))
        sync_repo(conn, src, REPO)

    @staticmethod
    def _split_trust(obj, untrusted, others):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "untrusted_text" and isinstance(v, str):
                    untrusted.append(v)
                else:
                    others.append(str(k))
                    BriefTests._split_trust(v, untrusted, others)
        elif isinstance(obj, list):
            for v in obj:
                BriefTests._split_trust(v, untrusted, others)
        elif isinstance(obj, str):
            others.append(obj)

    def test_brief_budget_and_trust_marking(self) -> None:
        """RED at base, GREEN after the fix. chars/4 of the serialized
        packet stays within --budget-tokens 2000; the body is truncated at a
        PARAGRAPH boundary (a trailing full paragraph present — the body
        string ends with the paragraph-terminal token "paraend" — and later
        content absent: the final paragraph's marker never appears); every
        EMITTED GitHub-authored string appears only inside untrusted_text
        wrappers while the OMITTED comment's marker is absent from the
        entire serialized packet (dropped comments are not aggregated into
        any other field); content_trust marks the packet; --live degrades
        (live_error / live null) and still returns the packet when gh
        cannot run.

        The store plants 6 comments; the top-comments cap keeps 5 ordered by
        author_association rank then recency DESC, so the OLDEST comment
        (TRUSTMARKC0, the first planted) is the omitted one and
        TRUSTMARKC1..C5 are emitted."""
        rc, out, err = _run_cli(
            "brief", "701", "--db", str(self.db_path), "--repo", REPO,
            "--budget-tokens", "2000", "--format", "json",
        )
        self.assertEqual(rc, 0, err.decode("utf-8", "replace"))
        env = json.loads(out.decode("utf-8"))
        self.assertTrue(env["ok"], env)
        data = env["data"]
        self.assertEqual(data["content_trust"], "untrusted: GitHub user content")
        for key in ("features", "body", "comments", "files", "analog", "linked_prs"):
            self.assertIn(key, data, f"brief packet key {key!r} missing")
        self.assertEqual(data["features"]["number"], 701)
        # exactly 5 of the 6 planted comments survive the top-comments cap.
        self.assertEqual(len(data["comments"]), 5)

        # Budget: chars/4 of the serialized packet (compact canonical form;
        # any looser serialization the CLI uses is strictly larger, so this
        # bound is the conservative one).
        packet_chars = len(
            json.dumps(data, sort_keys=True, ensure_ascii=True,
                       separators=(",", ":"))
        )
        self.assertLessEqual(
            packet_chars, 2000 * 4,
            f"packet chars/4 = {packet_chars / 4:.0f} exceeds --budget-tokens 2000",
        )

        # Trust marking: each EMITTED marker (title, body, comments 1-5)
        # appears ONLY inside untrusted wrappers.
        emitted_markers = ["TRUSTMARKTITLE", "TRUSTMARKBODY"] + [
            f"TRUSTMARKC{i}" for i in range(1, 6)
        ]
        untrusted: list[str] = []
        others: list[str] = []
        self._split_trust(data, untrusted, others)
        self.assertGreaterEqual(len(untrusted), 3, "no untrusted_text wrappers found")
        for marker in emitted_markers:
            self.assertTrue(
                any(marker in u for u in untrusted),
                f"{marker} must appear inside an untrusted_text wrapper",
            )
            self.assertFalse(
                any(marker in o for o in others),
                f"{marker} leaked outside untrusted_text wrappers",
            )

        # Paragraph-boundary truncation: the body cannot fit whole under the
        # budget, so its tail is dropped — the LAST paragraph's marker is
        # absent from the entire packet — and the surviving body string ends
        # with a COMPLETE paragraph (the paragraph-terminal "paraend" token),
        # never mid-paragraph.
        raw = out.decode("utf-8", "replace")
        self.assertNotIn(
            "TRUSTMARKLASTPARA", raw,
            "the body's final paragraph must be truncated away",
        )
        self.assertNotIn("TRUSTMARKLASTPARA", json.dumps(data))
        body_strings = [u for u in untrusted if "TRUSTMARKBODY" in u]
        self.assertTrue(
            body_strings, "the (truncated) body must be an untrusted_text value"
        )
        for body_text in body_strings:
            self.assertTrue(
                body_text.rstrip().endswith("paraend"),
                "body truncation must land on a paragraph boundary (the"
                " trailing paragraph is complete)",
            )

        # The OMITTED comment (the oldest, TRUSTMARKC0) is absent from the
        # ENTIRE serialized packet — raw stdout bytes and every re-serialization
        # — so dropped comments are not aggregated into any other field.
        self.assertNotIn(
            "TRUSTMARKC0", raw,
            "the omitted comment's marker must not appear anywhere in the packet",
        )
        self.assertNotIn("TRUSTMARKC0", json.dumps(data))
        self.assertNotIn("TRUSTMARKC0", json.dumps(env))

        # Zero-hit packet on the bare issue is still a packet (ok:true), not
        # an empty-corpus style error.
        env_bare = _cli_json(
            "brief", "702", "--db", str(self.db_path), "--repo", REPO,
            "--format", "json",
        )
        self.assertTrue(env_bare["ok"], env_bare)

        # --live with an unfindable gh: fail-open degradation — the packet
        # still returns, with live_error (or a null live block).
        with tempfile.TemporaryDirectory() as empty_dir:
            env_live = _cli_json(
                "brief", "701", "--db", str(self.db_path), "--repo", REPO,
                "--live", "--format", "json",
                path_override=empty_dir,
            )
        self.assertTrue(env_live["ok"], env_live)
        live_data = env_live["data"]
        self.assertEqual(
            live_data["content_trust"], "untrusted: GitHub user content"
        )
        self.assertTrue(
            live_data.get("live_error") is not None or live_data.get("live") is None,
            "gh failure must degrade to live_error/live null, not fail the packet",
        )


# ==== C7: open/churn surface fixes ==========================================


class OpenChurnSurfaceTests(_StoreTestCase):
    """- PR 801: CLOSED but NEVER MERGED, touching src/unmerged_only.py (the
      churn discriminator: counted by default, excluded by --merged-only).
    - PR 802: merged, touching src/merged_only.py.
    - 803: open issue labelled high; 804: open issue labelled low; 805:
      open PR (kind filter discriminator)."""

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        src.add_pr(
            issue(801, title="closed without merging", body="", kind="pr",
                  state="closed", created_at="2027-06-01T00:00:00Z"),
            pull(801, merged=False),
            files=[pr_file("src/unmerged_only.py")],
        )
        src.add_pr(
            issue(802, title="merged work", body="", kind="pr", state="closed",
                  created_at="2027-06-01T00:00:00Z"),
            _merged_pull(802, "2027-06-05T00:00:00Z"),
            files=[pr_file("src/merged_only.py")],
        )
        src.add_issue(
            issue(803, title="high priority item", body="urgent",
                  labels=[{"name": "high", "color": "ff0000"}],
                  created_at="2027-06-02T00:00:00Z")
        )
        src.add_issue(
            issue(804, title="low priority item", body="later",
                  labels=[{"name": "low", "color": "0000ff"}],
                  created_at="2027-06-02T00:00:00Z")
        )
        src.add_pr(
            issue(805, title="open pull request", body="", kind="pr"),
            pull(805, merged=False),
            files=[pr_file("src/wip.py")],
        )
        sync_repo(conn, src, REPO)

    def test_open_and_churn_surface(self) -> None:
        """RED at base, GREEN after the fix. `open --limit/--kind/--label`
        is honoured and rows carry labels/assignees/linked_open_prs; the
        closed-UNMERGED PR's file appears in default churn and is absent
        under --merged-only (both orders in this one test)."""
        # --kind issue --label high: only 803 matches (804 is label low,
        # 805/801/802 are kind pr and not open issues).
        env = _cli_json(
            "open", "--db", str(self.db_path), "--repo", REPO,
            "--limit", "2", "--kind", "issue", "--label", "high",
            "--format", "json",
        )
        self.assertTrue(env["ok"], env)
        rows = env["data"]
        self.assertEqual([r["number"] for r in rows], [803])
        row = rows[0]
        for key in ("labels", "assignees", "linked_open_prs"):
            self.assertIn(key, row, f"open rows must carry {key!r}")
        self.assertIn("high", row["labels"])

        # limit honoured on the unfiltered surface.
        env_lim = _cli_json(
            "open", "--db", str(self.db_path), "--repo", REPO,
            "--limit", "1", "--format", "json",
        )
        self.assertTrue(env_lim["ok"], env_lim)
        self.assertEqual(len(env_lim["data"]), 1)

        # churn, default FIRST: the closed-unmerged PR's file counts.
        env_default = _cli_json(
            "churn", "--db", str(self.db_path), "--repo", REPO, "--format", "json"
        )
        self.assertTrue(env_default["ok"], env_default)
        default_paths = {r["path"] for r in env_default["data"]}
        self.assertIn(
            "src/unmerged_only.py", default_paths,
            "default churn must still count the closed-unmerged PR's files",
        )
        # then --merged-only: the same file is gone, merged work stays.
        env_merged = _cli_json(
            "churn", "--db", str(self.db_path), "--repo", REPO,
            "--merged-only", "--format", "json",
        )
        self.assertTrue(env_merged["ok"], env_merged)
        merged_paths = {r["path"] for r in env_merged["data"]}
        self.assertNotIn(
            "src/unmerged_only.py", merged_paths,
            "--merged-only must exclude the closed-unmerged PR's files",
        )
        self.assertIn("src/merged_only.py", merged_paths)


# ==== C10 (44-AC1): eval cutoffs scope path evidence ========================


class EvalCutoffTests(_StoreTestCase):
    """build_eval_store geometry EXTENDED with the bait-isolation pair:

    - issue 107 (created T0+3d, PRE-cutoff for target 101): body refs
      docs/leak.md — resolvable ONLY through PR 130 (merged T0+9d,
      post-cutoff). With no cutoffs, 101's post-fix comment (T0+8d) also
      refs docs/leak.md, so 101 and 107 anchor through the bait.
    - issue 109 (created T0+2d): body refs src/hub.py (in vocabulary via
      the pre-cutoff PRs 10/12). A comment added to 102 at T0+6d
      (POST 102.created_at T0+4d+1h) refs src/hub.py, so 102 and 109
      anchor through it absent the comment cutoff.
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = build_eval_store(conn)
        # Bait pair ---------------------------------------------------------
        rec107 = issue(
            107,
            title="Leak page mention",
            body="see docs/leak.md:1:1 for the note",
            created_at="2026-06-04T00:00:00Z",  # T0+3d: visible to 101
            updated_at="2026-06-20T00:00:00Z",  # past the stored watermark
        )
        src.add_issue(rec107)
        rec109 = issue(
            109,
            title="Hub area note",
            body="see src/hub.py:1:1 as well",
            created_at="2026-06-03T00:00:00Z",  # T0+2d
            updated_at="2026-06-20T00:00:00Z",
        )
        src.add_issue(rec109)
        # A post-created_at comment ref on 102 (no closing PR -> the eval's
        # documented fallback comment_cutoff = target.created_at must cut
        # it; 102's body ref src/feature.py must stay). The explicit id
        # avoids comment_on's len-based scheme, which would collide with
        # build_eval_store's second 101 comment (github_id 50102).
        _add_comment(
            src, 102, 60102, "also src/hub.py:9:9 appears here", author="bob",
            created_at="2026-06-07T00:00:00Z",
        )
        src.issues[102]["updated_at"] = "2026-06-20T00:00:00Z"
        sync_repo(conn, src, REPO)

    def test_eval_scopes_path_evidence_to_the_item_cutoff(self) -> None:
        """RED at base, GREEN after the fix. A bait path resolvable only
        through a post-cutoff PR never enters the target's scored path set
        in eval similar/bundle (pinned by the unchanged synthetic floors),
        while direct CLI similar (no cutoff) still resolves it; and an eval
        target with NO closing PR uses comment_cutoff = target.created_at
        (a post-created_at comment ref is excluded, its body ref stays)."""
        conn = open_existing(self.db_path)
        self.addCleanup(conn.close)

        # Vocabulary oracle, computed from the store (not hardcoded): the
        # cutoff-restricted merged-PR path set for target 101 (created
        # 2026-06-05T00:00:00Z) is PRs 10/11/12/141/142 — 55 distinct paths.
        cutoff = "2026-06-05T00:00:00Z"
        expected_cut = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT pf.path FROM pr_files pf"
                " JOIN items i ON i.repo = pf.repo AND i.number = pf.number"
                " WHERE pf.repo = ? AND i.merged_at IS NOT NULL"
                " AND i.merged_at <= ?"
                " AND (pf.status IS NULL OR pf.status != 'removed')",
                (REPO, cutoff),
            )
        }
        expected_all = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT pf.path FROM pr_files pf"
                " JOIN items i ON i.repo = pf.repo AND i.number = pf.number"
                " WHERE pf.repo = ? AND i.merged_at IS NOT NULL"
                " AND (pf.status IS NULL OR pf.status != 'removed')",
                (REPO,),
            )
        }
        self.assertIn("docs/leak.md", expected_all)
        self.assertNotIn("docs/leak.md", expected_cut)

        # Direct read (no cutoffs): the bait anchors 107 to 101 through
        # docs/leak.md, and the vocabulary carries the bait.
        direct = similar(conn, REPO, 101, limit=50)
        self.assertIn("docs/leak.md", expected_all)  # oracle sanity
        self.assertIn(107, [c["number"] for c in direct["candidates"]])
        self.assertEqual(direct["resolved_path_count"], len(expected_all))
        # ... and through the CLI too (the direct surface keeps resolving).
        env = _cli_json(
            "similar", "101", "--db", str(self.db_path), "--repo", REPO,
            "--format", "json",
        )
        self.assertTrue(env["ok"], env)
        self.assertIn(107, [c["number"] for c in env["data"]["candidates"]])

        # Eval-shaped read (the 44-AC1 knobs): with item_cutoff =
        # target.created_at and comment_cutoff = the closing PR's creation
        # (PR 120 created T0+6d), the bait is doubly unreachable — the
        # vocabulary excludes PR 130 and 101's post-fix comment ref is cut.
        scoped = similar(
            conn, REPO, 101, limit=50,
            item_cutoff=cutoff, comment_cutoff="2026-06-07T00:00:00Z",
        )
        self.assertEqual(scoped["resolved_path_count"], len(expected_cut))
        self.assertNotIn(
            107,
            [c["number"] for c in scoped["candidates"]],
            "the post-cutoff bait must not anchor 107 to 101",
        )
        for cand in scoped["candidates"]:
            shared = cand["components"]["files_direct"]["evidence"]["shared_paths"]
            self.assertNotIn("docs/leak.md", shared)

        # Documented fallback: 102 has NO closing PR, so the eval's
        # comment_cutoff = 102.created_at (2026-06-05T01:00:00Z). Without
        # the comment cutoff, 109 anchors through src/hub.py; with it, the
        # post-created_at comment ref is excluded (109 drops) while 102's
        # body ref src/feature.py keeps the 101 pair anchored.
        uncut = similar(conn, REPO, 102, limit=50)
        self.assertIn(109, [c["number"] for c in uncut["candidates"]])
        fallback = similar(
            conn, REPO, 102, limit=50,
            comment_cutoff="2026-06-05T01:00:00Z",
        )
        self.assertNotIn(
            109, [c["number"] for c in fallback["candidates"]],
            "a comment ref post-created_at must not anchor 109 to 102",
        )
        partner = next(
            c for c in fallback["candidates"] if c["number"] == 101
        )
        self.assertIn(
            "src/feature.py",
            partner["components"]["files_direct"]["evidence"]["shared_paths"],
            "102's body ref must survive the comment cutoff",
        )

        # The eval modes themselves: the synthetic floors from
        # tests/test_similar.py's derivation must hold UNCHANGED on this
        # extended geometry — pre-fix, the leaked baits drag bundle
        # pair_precision to 1/3 (107 proposed for 101; 109 for 102).
        # A spy wraps evaluate.derive_similar through BOTH run_eval modes
        # (44-AC1's call-site contract): every call for a target must pass
        # item_cutoff == target.created_at and comment_cutoff == min(closing
        # PR created_at) for closer-backed targets, comment_cutoff ==
        # target.created_at for the no-closer target.
        import zaxbygraph.evaluate as evaluate_module
        from unittest import mock

        # Pinned fixture instants (build_eval_store geometry):
        #   101 created t(4)   = 2026-06-05T00:00:00Z (closer PR 120)
        #   102 created t(4,1) = 2026-06-05T01:00:00Z (NO closer -> the
        #                        documented created_at fallback)
        #   105 created t(4,4) = 2026-06-05T04:00:00Z (closer PR 120)
        #   120 created t(6)   = 2026-06-07T00:00:00Z (the min closer stamp)
        expected_cutoffs = {
            101: ("2026-06-05T00:00:00Z", "2026-06-07T00:00:00Z"),
            102: ("2026-06-05T01:00:00Z", "2026-06-05T01:00:00Z"),
            105: ("2026-06-05T04:00:00Z", "2026-06-07T00:00:00Z"),
        }
        # oracle self-check: the pins equal the store's actual stamps
        for n, (item_cut, _comment_cut) in expected_cutoffs.items():
            stored = conn.execute(
                "SELECT created_at FROM items WHERE repo = ? AND number = ?",
                (REPO, n),
            ).fetchone()[0]
            self.assertEqual(stored, item_cut, f"pin drifted for {n}")
        stored_closer = conn.execute(
            "SELECT created_at FROM items WHERE repo = ? AND number = 120",
            (REPO,),
        ).fetchone()[0]
        self.assertEqual(stored_closer, "2026-06-07T00:00:00Z")

        spy_calls: list[tuple[tuple, dict]] = []
        original_similar = evaluate_module.derive_similar

        def _recording_similar(*args, **kwargs):
            spy_calls.append((args, kwargs))
            return original_similar(*args, **kwargs)

        with mock.patch.object(
            evaluate_module, "derive_similar", side_effect=_recording_similar
        ):
            similar_eval = run_eval(conn, REPO, "similar")
            bundle_eval = run_eval(conn, REPO, "bundle")
        self.assertEqual(similar_eval["metrics"]["recall_at_5"], 1.0)
        self.assertEqual(similar_eval["metrics"]["mrr"], 1.0)
        self.assertEqual(similar_eval["metrics"]["eligible_targets"], 1)
        self.assertEqual(bundle_eval["metrics"]["pair_recall"], 1.0)
        self.assertEqual(
            bundle_eval["metrics"]["pair_precision"], 0.5,
            "post-cutoff baits (107 via docs/leak.md, 109 via the post-"
            "created_at comment) must not enter eval proposals",
        )
        # the spy: both modes went through derive_similar, every recorded
        # call carried the target's creation-time scoping.
        self.assertTrue(spy_calls, "run_eval never called derive_similar")
        seen_targets: set[int] = set()
        for args, kwargs in spy_calls:
            target = args[2] if len(args) > 2 else kwargs.get("target")
            self.assertIn(target, expected_cutoffs, f"unexpected target {target}")
            seen_targets.add(target)
            item_cut, comment_cut = expected_cutoffs[target]
            self.assertEqual(
                kwargs.get("item_cutoff"), item_cut,
                f"target {target}: item_cutoff must be its created_at",
            )
            self.assertEqual(
                kwargs.get("comment_cutoff"), comment_cut,
                f"target {target}: comment_cutoff must be min(closing PR"
                f" created_at) with the created_at fallback",
            )
        # similar mode targets 101/102; bundle mode targets 101/105 — the
        # union must cover all three pinned targets across the two modes.
        self.assertTrue({101, 102} <= seen_targets)
        self.assertTrue({101, 105} <= seen_targets)


# ==== C11 (44-AC2): hub share over the merged-PR population ================


class HubShareTests(_StoreTestCase):
    """src/hub_file.py is touched by 3 of 4 merged PRs — PR 904 is merged
    with NO pr_files rows, and the 44-AC2 denominator counts ALL merged
    PRs in scope including zero-file ones. Post-fix the share is 3/4 =
    0.75 >= hub_share (0.5 default), so _file_weight returns hub_floor.
    At base the denominator counts ALL items (11 rows -> 3/11 < 0.5), the
    knob never fires, and the key is still hub_share_of_items.

    Hand-derived files_direct for (910, 911): 910 resolves {hub}, 911
    resolves {hub, src/uniq1.py}; uniq1 is touched by 1/4 of merged PRs
    (weight 1.0). With the hub fired: wa = 0.25, wb = 0.25 + 1.0 = 1.25,
    wi = 0.25, so score = 0.25 / (0.25 + 1.25 - 0.25) = 0.2. At base every
    weight is 1.0 and the score is 1 / (1 + 2 - 1) = 0.5.
    """

    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        for i in (1, 2, 3):
            src.add_pr(
                issue(900 + i, title=f"pr {900 + i}", body="", kind="pr",
                      state="closed", created_at="2027-05-01T00:00:00Z"),
                _merged_pull(900 + i, "2027-05-0%dT00:00:00Z" % i),
                files=[pr_file("src/hub_file.py"), pr_file(f"src/uniq{i}.py")],
            )
        # a MERGED PR with NO files: the hub denominator must still count it
        src.add_pr(
            issue(904, title="empty merge", body="", kind="pr", state="closed",
                  created_at="2027-05-01T00:00:00Z"),
            _merged_pull(904, "2027-05-04T00:00:00Z"),
            files=[],
        )
        src.add_issue(
            issue(910, title="hub issue",
                  body="see src/hub_file.py:1:1",
                  created_at="2027-06-01T00:00:00Z")
        )
        src.add_issue(
            issue(911, title="hub and unique",
                  body="see src/hub_file.py:2:2 and src/uniq1.py:3:3",
                  created_at="2027-06-01T00:00:00Z")
        )
        for n in range(912, 917):
            src.add_issue(
                issue(n, title=f"plain issue {n}", body="no paths",
                      created_at="2027-06-01T00:00:00Z")
            )
        sync_repo(conn, src, REPO)

    def test_hub_share_fires_on_majority_paths(self) -> None:
        """RED at base, GREEN after the fix. With hub_share 0.5 and one
        path in >50% of merged PRs (denominator = ALL merged PRs, including
        the zero-file merge), _file_weight returns hub_floor for that
        path's evidence in a similar call; the payload key is
        hub_share_of_population and hub_share_of_items is gone."""
        import zaxbygraph.derive as derive_mod

        conn = open_existing(self.db_path)
        self.addCleanup(conn.close)

        weights = load_weights()
        self.assertEqual(weights["hub_share"], 0.5)
        self.assertEqual(weights["hub_floor"], 0.25)

        ctx = build_context(conn, REPO)
        # the renamed key exists, the old key is gone
        self.assertIn("hub_share_of_population", ctx)
        self.assertNotIn("hub_share_of_items", ctx)
        hub = ctx["hub_share_of_population"]
        # 3 touching PRs of 4 merged PRs in scope (PR 904 has no files but
        # still counts in the denominator).
        self.assertEqual(hub["src/hub_file.py"], 0.75)

        # the knob is reachable: _file_weight consumes the renamed key and
        # returns the floor for the majority path (1.0 for the rare path).
        file_weight = getattr(derive_mod, "_file_weight")
        self.assertEqual(file_weight("src/hub_file.py", ctx, weights), 0.25)
        self.assertEqual(file_weight("src/uniq1.py", ctx, weights), 1.0)

        # ... and the floor shows up in a real scoring call: files_direct
        # for (910, 911) = 0.25 / 1.25 = 0.2 (0.5 at base, where the hub
        # never fires).
        out = explain(conn, REPO, 910, 911)
        self.assertAlmostEqual(
            out["components"]["files_direct"]["score"], 0.2, places=6
        )

        # the similar payload no longer names the old key anywhere.
        sim = similar(conn, REPO, 910, limit=5)
        self.assertNotIn("hub_share_of_items", sim)


# ==== C12 (44-AC3): spool cleanup when the manifest tempfile fails =========


class SpoolCleanupTests(_StoreTestCase):
    @classmethod
    def _build(cls, conn) -> None:
        src = _BulkSource()
        src.add_issue(issue(1, title="one item", body="enough"))
        sync_repo(conn, src, REPO)

    def test_spool_removed_when_manifest_tempfile_fails(self) -> None:
        """RED at base, GREEN after the fix. When the SECOND tempfile
        (the manifest spool) raises, no zaxbygraph-export-* file survives
        in the temp directory and the destination is untouched."""
        import tempfile as tempfile_mod
        from unittest import mock

        from zaxbygraph.cli import main

        out = Path(self._td.name) / "corpus.jsonl"
        calls = {"export_stemp": 0}
        real_mkstemp = tempfile_mod.mkstemp

        def failing_second_export_mkstemp(*args, **kwargs):
            prefix = kwargs.get("prefix", args[0] if args else "")
            if isinstance(prefix, str) and prefix.startswith("zaxbygraph-export"):
                calls["export_stemp"] += 1
                if calls["export_stemp"] == 2:
                    raise OSError("simulated manifest tempfile failure (ENOSPC)")
                kwargs["dir"] = self._td.name
            return real_mkstemp(*args, **kwargs)

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
            tempfile_mod, "mkstemp", side_effect=failing_second_export_mkstemp
        ):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "export-corpus",
                        "--db", str(self.db_path),
                        "--repo", REPO,
                        "--out", str(out),
                        "--format", "json",
                    ]
                )
        self.assertEqual(code, 1, "the failed export must answer a failure envelope")
        self.assertNotIn("Traceback", stderr.getvalue())
        leftovers = sorted(p.name for p in Path(self._td.name).glob("zaxbygraph-export-*"))
        self.assertEqual(
            leftovers, [],
            "the spool must be cleaned up when the manifest tempfile fails",
        )
        self.assertFalse(out.exists(), "the destination must stay untouched")


# ==== C8: the skill documents the workflow ==================================


class SkillDocTests(unittest.TestCase):
    def test_skill_documents_bundle_workflow(self) -> None:
        """RED at base, GREEN after the fix. skills/zaxbygraph/SKILL.md
        documents the status -> triage -> bundle -> brief/verify workflow,
        carries triage/bundle/brief rows in the command table, and keeps
        the 'candidates, not facts' wording."""
        skill = REPO_ROOT / "skills" / "zaxbygraph" / "SKILL.md"
        self.assertTrue(skill.exists(), "skills/zaxbygraph/SKILL.md missing")
        text = skill.read_text(encoding="utf-8")
        # the candidates-not-facts wording (exact phrase)
        self.assertIn(
            "candidates, not facts", text,
            "SKILL.md must state that scores are candidates, not facts",
        )
        # a workflow section exists
        self.assertIsNotNone(
            re.search(r"(?im)^#{2,3}\s.*workflow", text),
            "SKILL.md needs a workflow section for triage -> bundle -> brief",
        )
        # command-table rows for the three new commands
        table_lines = [ln for ln in text.splitlines() if ln.lstrip().startswith("|")]
        for word in ("triage", "bundle", "brief"):
            self.assertTrue(
                any(re.search(rf"\b{word}\b", ln) for ln in table_lines),
                f"SKILL.md command table needs a row mentioning {word!r}",
            )
        # the workflow chain names its stations
        lowered = text.lower()
        for word in ("status", "triage", "bundle", "brief", "verify"):
            self.assertIn(word, lowered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
