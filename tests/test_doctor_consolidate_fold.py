"""Issue #52: doctor --consolidate must adopt case-split legacy candidates
instead of aborting on a raw UNIQUE error, and the v1->v2 collision cleanup
must cover every repo-keyed item-scoped table.

Base behavior pinned here (RED at f28e4b4):
- a candidate carrying one slug under two casings aborts the whole adoption
  with `UNIQUE constraint failed: ...` (doctor.py:333-343 plain-INSERTs
  lower(repo)-selected rows with no folded-key dedupe);
- `_drop_collision_losers` (db.py:264-274) deletes the loser's children from
  only labels/comments/reviews/pr_files/edges, so the post-v2 item-scoped
  tables keep loser-casing rows;
- the generic edges fold hits the `closes`-pair partial unique index on a
  dual-cased two-provenance closure, and drops same-source evidence before
  any collapse can merge it.
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from zaxbygraph.db import (  # noqa: E402
    CURRENT_USER_VERSION,
    _column_exists,
    connect,
    init_schema,
    migrate_v1_to_v2,
)
from zaxbygraph.extract import CLOSES_REF_MARKER, EVIDENCE_CAP  # noqa: E402

REPO = "probe/clinic"
MIXED = "Probe/Clinic"

_COPY_TABLES = (
    "items", "labels", "assignees", "issue_field_values", "comments",
    "reviews", "pr_files", "releases", "edges", "fetch_log", "revisions",
    "item_path_refs", "item_signatures", "item_events", "pr_commits",
)


def _rmtree(path: Path) -> None:
    for _ in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        gc.collect()
        time.sleep(0.2)


def _child_env() -> dict:
    env = dict(os.environ)
    parts = [str(ROOT / "src")]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _parse_envelope(stdout: bytes) -> dict | None:
    text = stdout.decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    try:
        return json.loads("\n".join(lines))
    except ValueError:
        return None


def _table_rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]


def _seed_item(conn, gid, repo, number, title, updated):
    conn.execute(
        "INSERT INTO items(id, repo, number, kind, title, state, updated_at, raw_json)"
        " VALUES (?, ?, ?, 'issue', ?, 'open', ?, '{}')",
        (gid, repo, number, title, updated),
    )


class _ConsolidateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="zgt5253-fold-"))
        self.addCleanup(_rmtree, self.scratch)
        self.stamp = "2026-02-01T00:00:00Z"

    def _store(self) -> Path:
        store = self.scratch / "store" / "history.db"
        store.parent.mkdir(parents=True)
        conn = connect(store)
        init_schema(conn)
        conn.close()
        return store

    def _scan(self) -> Path:
        scan_dir = self.scratch / "scan"
        scan_dir.mkdir()
        return scan_dir

    def _run_doctor(self, store: Path, scan_dir: Path) -> tuple[int, dict, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "zaxbygraph", "doctor", "--repo", REPO,
             "--db", str(store), "--scan", str(scan_dir), "--consolidate",
             "--format", "json"],
            capture_output=True, env=_child_env(), cwd=str(ROOT), timeout=180,
        )
        stderr = proc.stderr.decode("utf-8", "replace")
        return proc.returncode, _parse_envelope(proc.stdout) or {}, stderr


class DualCasingCandidateAdoptsTests(_ConsolidateCase):
    def test_dual_casing_candidate_adopts(self) -> None:
        scan_dir = self._scan()
        candidate = scan_dir / "history.db"
        conn = connect(candidate)
        init_schema(conn)
        # delete-and-recreate shape: distinct ids, the folded casing newer
        _seed_item(conn, 101, MIXED, 1, "one", "2026-01-01T00:00:00Z")
        _seed_item(conn, 102, REPO, 1, "one recreated", "2026-01-02T00:00:00Z")
        for repo in (MIXED, REPO):
            conn.execute(
                "INSERT INTO labels(repo, number, name, color) VALUES (?, 1, 'bug', 'ff0000')",
                (repo,),
            )
            conn.execute(
                "INSERT INTO assignees(repo, number, login) VALUES (?, 1, 'bob')",
                (repo,),
            )
            conn.execute(
                "INSERT INTO issue_field_values(repo, number, field_name, value)"
                " VALUES (?, 1, 'Team', 'core')",
                (repo,),
            )
            conn.execute(
                "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
                " last_incr_sync_at, last_error, include_patches, full_sync_pending)"
                " VALUES (?, ?, ?, ?, NULL, 0, 0)",
                (repo, self.stamp, self.stamp, self.stamp),
            )
        conn.commit()
        conn.close()
        self.assertEqual(
            sqlite3.connect(str(candidate)).execute(
                "PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )

        store = self._store()
        rc, envelope, stderr = self._run_doctor(store, scan_dir)
        self.assertEqual(rc, 0, stderr)
        self.assertTrue((envelope.get("data") or {}).get("adopted"), envelope)
        conn = sqlite3.connect(str(store))
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM items WHERE repo = ?", (REPO,)).fetchone()[0], 1
            )
            for table, key in (
                ("labels", ("repo", "number", "name")),
                ("assignees", ("repo", "number", "login")),
                ("issue_field_values", ("repo", "number", "field_name")),
            ):
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                self.assertEqual(len(rows), 1, f"{table}: {rows}")
        finally:
            conn.close()

    def test_inspect_sync_state_reports_the_freshest_row(self) -> None:
        scan_dir = self._scan()
        candidate = scan_dir / "history.db"
        conn = connect(candidate)
        init_schema(conn)
        _seed_item(conn, 101, REPO, 1, "one", "2026-01-01T00:00:00Z")
        # the OLDER row first (mixed casing, no full-sync stamp), the FRESHER
        # second (folded casing, complete)
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_error, include_patches, full_sync_pending)"
            " VALUES (?, '2026-01-01T00:00:00Z', NULL, 'interrupted', 0, 1)",
            (MIXED,),
        )
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_error, include_patches, full_sync_pending)"
            " VALUES (?, ?, ?, NULL, 0, 0)",
            (REPO, self.stamp, self.stamp),
        )
        conn.commit()
        conn.close()
        store = self.scratch / "store" / "history.db"
        store.parent.mkdir(parents=True)
        answers = set()
        for _ in range(5):
            proc = subprocess.run(
                [sys.executable, "-m", "zaxbygraph", "doctor", "--repo", REPO,
                 "--db", str(store), "--scan", str(scan_dir), "--format", "json"],
                capture_output=True, env=_child_env(), cwd=str(ROOT), timeout=180,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            envelope = _parse_envelope(proc.stdout)
            entry = [
                row for row in (envelope.get("data") or {}).get("scanned") or []
                if os.path.normcase(str(row.get("path", "")))
                == os.path.normcase(str(candidate))
            ]
            self.assertEqual(len(entry), 1, envelope)
            answers.add((entry[0]["issues_since"], bool(entry[0]["complete"])))
        self.assertEqual(len(answers), 1, f"non-deterministic inspection: {answers}")
        self.assertEqual(
            answers.pop(), (self.stamp, True),
            "inspection must report the freshest sync_state row",
        )


class ClosesPairFoldTests(_ConsolidateCase):
    """The closes-specific fold steps, on a v11 candidate whose dual casing
    lives in the EDGES only (no distinct-id item collision, so the collision
    cleanup cannot explain the outcome)."""

    def _closes_candidate(self, rows: list[tuple]) -> Path:
        scan_dir = self._scan()
        candidate = scan_dir / "history.db"
        conn = connect(candidate)
        init_schema(conn)
        _seed_item(conn, 7, REPO, 7, "closer", "2026-01-02T00:00:00Z")
        _seed_item(conn, 8, REPO, 8, "the fix", "2026-01-03T00:00:00Z")
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_incr_sync_at, last_error, include_patches, full_sync_pending)"
            " VALUES (?, ?, ?, ?, NULL, 0, 0)",
            (REPO, self.stamp, self.stamp, self.stamp),
        )
        for repo, source, evidence in rows:
            conn.execute(
                "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
                " confidence, evidence, source) VALUES (?, 'item', '8', 'closes',"
                " 'item', '7', 'EXTRACTED', ?, ?)",
                (repo, evidence, source),
            )
        conn.commit()
        conn.close()
        return candidate

    def test_two_provenance_pair_folds_without_abort(self) -> None:
        candidate = self._closes_candidate([
            (MIXED, "timeline", "timeline closed event 2026-01-03T00:00:00Z"),
            (REPO, "closing_ref", "closingIssuesReferences 2026-01-03T00:00:00Z"),
        ])
        store = self._store()
        rc, envelope, stderr = self._run_doctor(store, self._scan_dir(candidate))
        self.assertEqual(rc, 0, stderr)
        self.assertTrue((envelope.get("data") or {}).get("adopted"), envelope)
        conn = sqlite3.connect(str(store))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT repo, source, evidence FROM edges WHERE rel = 'closes'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["repo"], REPO)
        self.assertEqual(row["source"], "closing_ref")
        self.assertIn("timeline closed event", row["evidence"])
        self.assertIn("closingIssuesReferences", row["evidence"])

    def test_same_source_distinct_evidence_unions(self) -> None:
        candidate = self._closes_candidate([
            (MIXED, "timeline", "timeline observation from mixed casing"),
            (REPO, "timeline", "timeline observation from folded casing"),
        ])
        store = self._store()
        rc, envelope, stderr = self._run_doctor(store, self._scan_dir(candidate))
        self.assertEqual(rc, 0, stderr)
        conn = sqlite3.connect(str(store))
        try:
            rows = conn.execute(
                "SELECT evidence FROM edges WHERE rel = 'closes'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1, rows)
        evidence = rows[0][0]
        self.assertIn("mixed casing", evidence)
        self.assertIn("folded casing", evidence)

    def test_same_source_over_cap_retains_the_marker(self) -> None:
        """The same-source union reserves the closing_ref marker.

        Two dual-casing `closing_ref` rows for one pair, each carrying the
        marker plus oversized filler, merge over the 512-byte evidence cap.
        Without `reserve=(CLOSES_REF_MARKER,)` the merge_evidence truncation
        drops the marker and the surviving row stops saying WHY the closure
        fact is trusted (issue #52, plan critic Round 4; probe leg 8).
        """
        filler = "x" * 300
        candidate = self._closes_candidate([
            (MIXED, "closing_ref", f"{CLOSES_REF_MARKER} {MIXED} {filler}"),
            (REPO, "closing_ref", f"{CLOSES_REF_MARKER} {REPO} {filler}"),
        ])
        store = self._store()
        rc, envelope, stderr = self._run_doctor(store, self._scan_dir(candidate))
        self.assertEqual(rc, 0, stderr)
        conn = sqlite3.connect(str(store))
        try:
            rows = conn.execute(
                "SELECT evidence FROM edges WHERE rel = 'closes'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1, rows)
        evidence = rows[0][0]
        self.assertIn(CLOSES_REF_MARKER, evidence)
        self.assertLessEqual(len(evidence), EVIDENCE_CAP)
        self.assertIn(MIXED, evidence)
        self.assertIn(REPO, evidence)

    def _scan_dir(self, candidate: Path) -> Path:
        return candidate.parent


class FoldSemanticsTests(_ConsolidateCase):
    def _v11_candidate(self) -> Path:
        scan_dir = self._scan()
        candidate = scan_dir / "history.db"
        conn = connect(candidate)
        init_schema(conn)
        return candidate, conn

    def test_duplicate_revision_payloads_collapse_to_one_row(self) -> None:
        """`revisions` declares no unique key beyond `id`, so the generic
        fold's `ON CONFLICT(id)` for it is inert: a case-split candidate's
        byte-identical duplicate pre-images (same folded key, same payload,
        distinct ids) would BOTH survive and be adopted twice (issue #53,
        review PRR-002). The observation's identity is its payload tuple;
        the lowest id survives. A DISTINCT observation never collapses."""
        candidate, conn = self._v11_candidate()
        _seed_item(conn, 101, MIXED, 1, "one", "2026-01-01T00:00:00Z")
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_incr_sync_at, last_error, include_patches, full_sync_pending)"
            " VALUES (?, ?, ?, ?, NULL, 0, 0)",
            (REPO, self.stamp, self.stamp, self.stamp),
        )
        payload = (
            1, "item_title", None, "title", "edit",
            "the old title", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z",
        )
        # two byte-identical observations under the two casings, distinct ids
        conn.execute(
            "INSERT INTO revisions(id, repo, number, entity, entity_id, kind,"
            " origin, content, valid_from, valid_to)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (9100, MIXED, *payload),
        )
        conn.execute(
            "INSERT INTO revisions(id, repo, number, entity, entity_id, kind,"
            " origin, content, valid_from, valid_to)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (9101, REPO, *payload),
        )
        # a genuinely different observation (a later edit window) survives too
        conn.execute(
            "INSERT INTO revisions(id, repo, number, entity, entity_id, kind,"
            " origin, content, valid_from, valid_to)"
            " VALUES (9102, ?, 1, 'item_title', NULL, 'title', 'edit',"
            " 'an even older title', '2025-12-01T00:00:00Z',"
            " '2026-01-01T00:00:00Z')",
            (MIXED,),
        )
        conn.commit()
        conn.close()
        store = self._store()
        rc, envelope, stderr = self._run_doctor(store, candidate.parent)
        self.assertEqual(rc, 0, stderr)
        conn = sqlite3.connect(str(store))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, repo, content FROM revisions ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [(r["repo"], r["content"]) for r in rows],
            [
                (REPO, "the old title"),
                (REPO, "an even older title"),
            ],
            "the duplicate observation must collapse and the distinct"
            " observation must survive, both under the folded casing",
        )
        # ids are not pinned: consolidation adopts revisions with FRESH ids
        # per item (issue #40, _COPY_TABLES ("revisions", "id")), so the
        # survivor's identity is its payload tuple, not its rowid.
        self.assertEqual(len({r["id"] for r in rows}), 2)

    def test_folded_sync_state_keeps_a_recorded_fault(self) -> None:
        """PRR-007: an adopted corpus must never report fault-free over a
        failed run.

        The freshest casing (the winner) carries `last_error = NULL` while
        the loser casing recorded `boom: disk I/O error`. A winner-only
        merge drops the fault and `status` then reports a clean sync for a
        corpus whose last run actually failed (AGENTS.md: a failing sync
        records `last_error`), so the merged row keeps the winner's error
        when it has one and any casing's otherwise."""
        candidate, conn = self._v11_candidate()
        _seed_item(conn, 101, REPO, 1, "one", "2026-01-01T00:00:00Z")
        _seed_item(conn, 102, MIXED, 1, "one again", "2026-01-02T00:00:00Z")
        # the freshest row (the fold's winner) is the clean one
        # the winner is the greatest issues_since (ties by rowid), so the
        # clean REPO row wins outright and the fault lives only on the loser
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_incr_sync_at, last_error, include_patches, full_sync_pending)"
            " VALUES (?, ?, ?, ?, NULL, 0, 0)",
            (REPO, "2026-03-01T00:00:00Z", self.stamp, self.stamp),
        )
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_incr_sync_at, last_error, include_patches, full_sync_pending)"
            " VALUES (?, ?, ?, ?, 'boom: disk I/O error', 0, 0)",
            (MIXED, self.stamp, self.stamp, self.stamp),
        )
        conn.commit()
        conn.close()
        store = self._store()
        rc, envelope, stderr = self._run_doctor(store, candidate.parent)
        self.assertEqual(rc, 0, stderr)
        conn = sqlite3.connect(str(store))
        try:
            rows = conn.execute(
                "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0][0], "boom: disk I/O error")

    def test_folded_sync_state_preserves_nullness_of_last_error(self) -> None:
        """A non-NULL but EMPTY `last_error` is still a recorded state
        (cubic PR #54 round 1): the coalesce tests `is not None`, not
        truthiness, so a corpus that stopped early cannot report complete
        through an empty-string fault being merged away."""
        candidate, conn = self._v11_candidate()
        _seed_item(conn, 101, REPO, 1, "one", "2026-01-01T00:00:00Z")
        _seed_item(conn, 102, MIXED, 1, "one again", "2026-01-02T00:00:00Z")
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_incr_sync_at, last_error, include_patches, full_sync_pending)"
            " VALUES (?, ?, ?, ?, '', 0, 0)",
            (REPO, "2026-03-01T00:00:00Z", self.stamp, self.stamp),
        )
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_incr_sync_at, last_error, include_patches, full_sync_pending)"
            " VALUES (?, ?, ?, ?, NULL, 0, 0)",
            (MIXED, self.stamp, self.stamp, self.stamp),
        )
        conn.commit()
        conn.close()
        store = self._store()
        rc, envelope, stderr = self._run_doctor(store, candidate.parent)
        self.assertEqual(rc, 0, stderr)
        conn = sqlite3.connect(str(store))
        try:
            rows = conn.execute(
                "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([r[0] for r in rows], [""])

    def test_folded_sync_state_keeps_the_winner_s_own_fault(self) -> None:
        """The other precedence: when the WINNER carries the fault, it is
        the one reported — the coalesce is winner-first, not loser-first."""
        candidate, conn = self._v11_candidate()
        _seed_item(conn, 101, MIXED, 1, "one", "2026-01-01T00:00:00Z")
        _seed_item(conn, 102, REPO, 1, "one again", "2026-01-02T00:00:00Z")
        # the winner is the greatest issues_since (ties by rowid), so the
        # REPO row wins outright here rather than on an insertion-order tie
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_incr_sync_at, last_error, include_patches, full_sync_pending)"
            " VALUES (?, ?, ?, ?, 'winner failed', 0, 0)",
            (REPO, "2026-03-01T00:00:00Z", self.stamp, self.stamp),
        )
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_incr_sync_at, last_error, include_patches, full_sync_pending)"
            " VALUES (?, ?, ?, ?, 'loser failed', 0, 0)",
            (MIXED, self.stamp, self.stamp, self.stamp),
        )
        conn.commit()
        conn.close()
        store = self._store()
        rc, envelope, stderr = self._run_doctor(store, candidate.parent)
        self.assertEqual(rc, 0, stderr)
        conn = sqlite3.connect(str(store))
        try:
            rows = conn.execute(
                "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([r[0] for r in rows], ["winner failed"])

    def test_sync_state_columns_survive_the_fold(self) -> None:
        """A folded v11 candidate keeps its newer sync_state columns: the
        generalized merge must carry fetch_source (v11) and the rate-limit
        columns (v4), not the pre-v4 snapshot list."""
        candidate, conn = self._v11_candidate()
        _seed_item(conn, 101, MIXED, 1, "one", "2026-01-01T00:00:00Z")
        _seed_item(conn, 102, REPO, 1, "one recreated", "2026-01-02T00:00:00Z")
        for repo, source in ((MIXED, "graphql"), (REPO, "rest")):
            conn.execute(
                "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
                " last_incr_sync_at, last_error, include_patches, full_sync_pending,"
                " fetch_source, rate_limit_remaining, rate_limit_reset_at)"
                " VALUES (?, ?, ?, ?, NULL, 0, 0, ?, 4321, '2026-02-01T00:05:00Z')",
                (repo, self.stamp, self.stamp, self.stamp, source),
            )
        conn.commit()
        conn.close()
        store = self._store()
        rc, envelope, stderr = self._run_doctor(store, candidate.parent)
        self.assertEqual(rc, 0, stderr)
        conn = sqlite3.connect(str(store))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT fetch_source, rate_limit_remaining, rate_limit_reset_at"
                " FROM sync_state WHERE repo = ?", (REPO,)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["fetch_source"], "rest")
        self.assertEqual(row["rate_limit_remaining"], 4321)
        self.assertEqual(row["rate_limit_reset_at"], "2026-02-01T00:05:00Z")

    def test_mixed_case_winner_folds_to_the_slug(self) -> None:
        """The freshest item may live under the mixed casing; the fold keeps
        it and folds its casing (and its children's)."""
        candidate, conn = self._v11_candidate()
        _seed_item(conn, 101, REPO, 1, "older", "2026-01-01T00:00:00Z")
        _seed_item(conn, 102, MIXED, 1, "newer", "2026-01-02T00:00:00Z")
        conn.execute(
            "INSERT INTO labels(repo, number, name, color)"
            " VALUES (?, 1, 'bug', 'ff0000')",
            (MIXED,),
        )
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_incr_sync_at, last_error, include_patches, full_sync_pending)"
            " VALUES (?, ?, ?, ?, NULL, 0, 0)",
            (REPO, self.stamp, self.stamp, self.stamp),
        )
        conn.commit()
        conn.close()
        store = self._store()
        rc, envelope, stderr = self._run_doctor(store, candidate.parent)
        self.assertEqual(rc, 0, stderr)
        conn = sqlite3.connect(str(store))
        try:
            rows = conn.execute(
                "SELECT repo, title FROM items WHERE number = 1"
            ).fetchall()
            labels = conn.execute("SELECT repo FROM labels").fetchall()
        finally:
            conn.close()
        self.assertEqual([tuple(r) for r in rows], [(REPO, "newer")])
        self.assertEqual([tuple(r) for r in labels], [(REPO,)])

    def test_tool_produced_candidate_fold_is_a_noop(self) -> None:
        """A single-cased, constraint-valid candidate is logically unchanged
        by the fold — every _COPY_TABLES row survives, including a valid
        closes edge (the store holds at most one per pair by invariant)."""
        candidate, conn = self._v11_candidate()
        _seed_item(conn, 101, REPO, 1, "closer", "2026-01-02T00:00:00Z")
        _seed_item(conn, 102, REPO, 2, "the fix", "2026-01-03T00:00:00Z")
        conn.execute(
            "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
            " confidence, evidence, source) VALUES (?, 'item', '2', 'closes',"
            " 'item', '1', 'EXTRACTED', ?, ?)",
            (REPO, "closingIssuesReferences 2026-01-03T00:00:00Z", "closing_ref"),
        )
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_incr_sync_at, last_error, include_patches, full_sync_pending)"
            " VALUES (?, ?, ?, ?, NULL, 0, 0)",
            (REPO, self.stamp, self.stamp, self.stamp),
        )
        before = {
            table: _table_rows(conn, table) for table in _COPY_TABLES
        }
        conn.commit()
        conn.close()
        store = self._store()
        rc, envelope, stderr = self._run_doctor(store, candidate.parent)
        self.assertEqual(rc, 0, stderr)
        conn = sqlite3.connect(str(store))
        try:
            for table, rows in before.items():
                after = _table_rows(conn, table)
                self.assertEqual(
                    sorted(after), sorted(rows), f"{table} changed by the fold"
                )
            closes = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE rel = 'closes'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(closes, 1, "the valid closes edge must survive the fold")

    def test_second_fold_is_a_noop(self) -> None:
        """fold_repo_casings is idempotent: folding an already-folded copy
        leaves every table byte-identical (the doctor path folds a pre-v2
        candidate a second time after _MigratedCopy's migration)."""
        from zaxbygraph.db import fold_repo_casings

        candidate, conn = self._v11_candidate()
        _seed_item(conn, 101, MIXED, 1, "one", "2026-01-01T00:00:00Z")
        _seed_item(conn, 102, REPO, 1, "one recreated", "2026-01-02T00:00:00Z")
        conn.execute(
            "INSERT INTO labels(repo, number, name, color) VALUES (?, 1, 'bug', 'ff0000')",
            (REPO,),
        )
        conn.commit()
        conn.close()
        conn = sqlite3.connect(str(candidate))
        try:
            fold_repo_casings(conn)
            conn.commit()
            first = {table: _table_rows(conn, table) for table in _COPY_TABLES}
            fold_repo_casings(conn)
            conn.commit()
            second = {table: _table_rows(conn, table) for table in _COPY_TABLES}
        finally:
            conn.close()
        for table, rows in first.items():
            self.assertEqual(
                sorted(second[table]), sorted(rows), f"{table} changed on the second fold"
            )


class GenuineV1ClosesTests(unittest.TestCase):
    """The Round-6 discriminating regression: a genuine v1 DB whose dual
    casing lives in the EDGES only. `migrate_v1_to_v2` alone must not touch
    `source` (the v1 window has no such column), must fold the two legacy
    closes edges with the six-column clause, and must not create the
    `closes`-pair index before its v5->v6 slot."""

    def test_v1_window_has_no_source_and_folds_six_column(self) -> None:
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        db = Path(td.name) / "history.db"
        sys.path.insert(0, str(ROOT / "tests"))
        from test_migrations import V1_SCHEMA_SQL

        v1 = sqlite3.connect(str(db))
        v1.executescript(V1_SCHEMA_SQL)
        v1.execute(
            "INSERT INTO items(id, repo, number, kind, title, state, updated_at, raw_json)"
            " VALUES (7, 'probe/clinic', 7, 'issue', 'closer', 'closed',"
            " '2026-01-02T00:00:00Z', '{}')"
        )
        for repo, ev in (
            ("Probe/Clinic", "older mixed-casing body closes #7"),
            ("probe/clinic", "newer folded-casing body closes #7"),
        ):
            v1.execute(
                "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
                " confidence, evidence) VALUES (?, 'item', '7', 'closes', 'item',"
                " '7', 'EXTRACTED', ?)",
                (repo, ev),
            )
        v1.commit()
        v1.close()

        conn = connect(db)
        try:
            self.assertFalse(_column_exists(conn, "edges", "source"))
            conn.execute("BEGIN")
            migrate_v1_to_v2(conn)
            conn.commit()
            rows = conn.execute(
                "SELECT repo, rel, evidence FROM edges ORDER BY id"
            ).fetchall()
            self.assertEqual(len(rows), 1, rows)
            self.assertEqual(rows[0]["repo"], "probe/clinic")
            self.assertEqual(rows[0]["rel"], "closes")  # rename happens at v4->v5
            self.assertEqual(rows[0]["evidence"], "newer folded-casing body closes #7")
            idx = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index'"
                " AND name = 'ux_edges_closes_pair'"
            ).fetchone()
            self.assertIsNone(idx, "v1->v2 must not create the closes-pair index")
        finally:
            conn.close()

        # the full chain then completes the documented rename
        conn = connect(db)
        try:
            init_schema(conn)
            rows = conn.execute(
                "SELECT rel, source FROM edges"
            ).fetchall()
            self.assertEqual([tuple(r) for r in rows], [("closes_keyword", "keyword")])
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
