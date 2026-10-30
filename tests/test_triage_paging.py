"""Feedback-round checks for PR #45 (post-publication review, 2026-10-30)
— NEW FILE per the new-checks-new-files rule: unlike the frozen
checkpoint files, these pin SHIPPED code (no getattr/lazy-import arms),
so a failure here is a regression, not a new-surface probe.

Pinned fixes:

    F7  budget-tokens <= 0 is a coded bad_request (brief + bundle), never
        a silently-disabled cap                        (review K09)
    F8  the triage cursor validates BOTH halves: a stale total half or an
        unknown number restarts from the top; a malformed cursor raises
        (review K10 / CUB#ebc5db39, CUB#b98d624a, CUB#5747ab19)
    F9  brief on a stored-but-not-open item says "is not an open issue"
        instead of claiming absence                     (review K11)
    F10 [triage] weights are magnitude-capped at 1e9     (review K13)
    F14 --pool "" is absent, not an explicit empty pool  (CUB#b08069ad)
    F6  a bundle that reached its target size reports
        "membership cap (reached target size)"           (review K06)

Hermetic: FakeGitHubSource only, no network, no credentials, no live gh.
Stdlib only; unittest (no pytest).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, TempDBTest, issue, pr_file, pull

from zaxbygraph.db import connect, init_schema, open_existing
from zaxbygraph.query import open_items
from zaxbygraph.derive import ConfigError, brief, bundle, load_triage_config, triage
from zaxbygraph.sync import sync_repo

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*argv: str) -> tuple[int, bytes, bytes]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO")
    }
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), str(REPO_ROOT / "tests"), env.get("PYTHONPATH", "")]
    )
    proc = subprocess.run(
        [sys.executable, "-m", "zaxbygraph", *argv],
        capture_output=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


class _PagingStore(unittest.TestCase):
    """One temp store per class; subclasses override ``_build``."""

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


class _RankingStore(_PagingStore):
    """Six open issues 801-806 whose only differing feature is the stored
    comment count (0..5 — ``comment_count`` is the stored-row count, so
    the comments are planted as real rows), so stage-1 totals are
    distinct, deterministic and directly mutable through the stored
    column; plus a closed issue (809) and an open PR (808) for the brief
    not-open message."""

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        for n in range(801, 807):
            src.add_issue(
                issue(
                    n,
                    title=f"paging fixture issue {n}",
                    body="crash on submit",
                    created_at=f"2027-06-0{n - 800:01d}T00:00:00Z",
                )
            )
            for _k in range(n - 801):
                src.comment_on(n, f"comment {n}", author="bob")
        src.add_issue(issue(809, title="closed issue", state="closed"))
        src.add_pr(
            issue(808, title="open pull request", body="", kind="pr"),
            pull(808, merged=False),
            files=[pr_file("src/paging.py")],
        )
        sync_repo(conn, src, REPO)


class CursorValidationTests(_RankingStore):
    """F8: the cursor's total half is validated against the CURRENT
    ranking — a stale cursor restarts from the top (the README's
    documented restart), a fresh one continues, an unknown number
    restarts, and garbage raises ValueError / a bad_request envelope."""

    def _triage(self, **kwargs) -> dict:
        conn = open_existing(self.db_path)
        try:
            return triage(conn, REPO, **kwargs)
        finally:
            conn.close()

    def test_fresh_cursor_continues_and_stale_cursor_restarts(self) -> None:
        # Pre-mutation order: comment_count 5,4,3,... -> 806,805,804,...
        page1 = self._triage(limit=2)
        self.assertEqual([r["number"] for r in page1["rows"]], [806, 805])
        self.assertTrue(page1["truncated"])
        stale_cursor = page1["cursor"]
        self.assertIsNotNone(stale_cursor)

        # Mutate one score through the stored column (a re-sync that
        # changed the item would move updated_at the same way): 805's
        # comments jump past the normalization cap, changing its total.
        conn = connect(self.db_path)
        try:
            conn.execute(
                "UPDATE items SET comment_count = 9 WHERE repo = ? AND number = ?",
                (REPO, 805),
            )
            conn.commit()
        finally:
            conn.close()

        fresh = self._triage(limit=2)
        # Post-mutation order: 805 ties 806 at the cap and wins on number
        # ASC, so the fresh ranking head is 805.
        self.assertEqual([r["number"] for r in fresh["rows"]], [805, 806])

        # The STALE cursor (805 at its old total) restarts from the top.
        restarted = self._triage(limit=2, cursor=stale_cursor)
        self.assertEqual(
            [r["number"] for r in restarted["rows"]],
            [r["number"] for r in fresh["rows"]],
            "a cursor whose total half no longer matches the row's current"
            " total must restart from the top, not resume mid-ranking",
        )

        # A FRESH cursor (minted from the post-mutation page 1) continues
        # at the next row.
        continued = self._triage(limit=2, cursor=fresh["cursor"])
        self.assertEqual(
            [r["number"] for r in continued["rows"]][0],
            804,
            "a fresh cursor must continue past the page-1 tail",
        )

    def test_unknown_cursor_number_restarts_from_top(self) -> None:
        full = self._triage(limit=2)
        head = full["rows"][0]["number"]
        bogus = f"{full['rows'][-1]['total_score']:.9f}:9999"
        restarted = self._triage(limit=2, cursor=bogus)
        self.assertEqual(
            restarted["rows"][0]["number"],
            head,
            "an unknown cursor number must restart from the top",
        )

    def test_malformed_cursor_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            self._triage(limit=2, cursor="not-a-cursor")
        with self.assertRaises(ValueError):
            self._triage(limit=2, cursor="1.0")  # no number half

    def test_malformed_cursor_is_coded_bad_request_at_the_cli(self) -> None:
        rc, out, _err = _run_cli(
            "triage", "--db", str(self.db_path), "--repo", REPO,
            "--cursor", "garbage", "--format", "json",
        )
        self.assertEqual(rc, 2)
        env = json.loads(out.decode("utf-8"))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "bad_request")


class BudgetValidationTests(_RankingStore):
    """F7: budget-tokens <= 0 answers the coded bad_request envelope (exit
    2) for brief AND bundle, and raises ValueError before any work on
    direct derive calls."""

    def test_brief_budget_zero_is_bad_request(self) -> None:
        rc, out, _err = _run_cli(
            "brief", "801", "--db", str(self.db_path), "--repo", REPO,
            "--budget-tokens", "0", "--format", "json",
        )
        self.assertEqual(rc, 2)
        env = json.loads(out.decode("utf-8"))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "bad_request")
        self.assertIn("positive integer", env["error"]["message"])

    def test_bundle_budget_negative_is_bad_request(self) -> None:
        rc, out, _err = _run_cli(
            "bundle", "--db", str(self.db_path), "--repo", REPO,
            "--budget-tokens", "-5", "--format", "json",
        )
        self.assertEqual(rc, 2)
        env = json.loads(out.decode("utf-8"))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "bad_request")
        self.assertIn("positive integer", env["error"]["message"])

    def test_direct_derive_calls_raise_before_work(self) -> None:
        conn = open_existing(self.db_path)
        try:
            with self.assertRaises(ValueError) as ctx:
                brief(conn, REPO, 801, budget_tokens=0)
            self.assertIn("positive integer", str(ctx.exception))
            with self.assertRaises(ValueError):
                bundle(conn, REPO, budget_tokens=-1)
        finally:
            conn.close()


class BriefNotOpenMessageTests(_RankingStore):
    """F9: brief distinguishes "stored but not an open issue" from
    "never stored" — a closed issue or a PR answers "is not an open
    issue", an unknown number keeps "item not found"."""

    def test_closed_issue_message(self) -> None:
        conn = open_existing(self.db_path)
        try:
            with self.assertRaises(LookupError) as ctx:
                brief(conn, REPO, 809)
            self.assertIn("is not an open issue", str(ctx.exception))
            self.assertNotIn("not found", str(ctx.exception))
        finally:
            conn.close()

    def test_pr_number_message(self) -> None:
        conn = open_existing(self.db_path)
        try:
            with self.assertRaises(LookupError) as ctx:
                brief(conn, REPO, 808)
            self.assertIn("is not an open issue", str(ctx.exception))
        finally:
            conn.close()

    def test_unknown_number_keeps_not_found(self) -> None:
        conn = open_existing(self.db_path)
        try:
            with self.assertRaises(LookupError) as ctx:
                brief(conn, REPO, 9999)
            self.assertIn("item not found", str(ctx.exception))
        finally:
            conn.close()

    def test_cli_envelope_is_not_found(self) -> None:
        rc, out, _err = _run_cli(
            "brief", "809", "--db", str(self.db_path), "--repo", REPO,
            "--format", "json",
        )
        self.assertEqual(rc, 1)
        env = json.loads(out.decode("utf-8"))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "not_found")
        self.assertIn("is not an open issue", env["error"]["message"])


class TriageConfigMagnitudeCapTests(_RankingStore):
    """F10: [triage] weights share [derive]'s _MAX_WEIGHT magnitude cap —
    1e308 (or anything past 1e9) is a ConfigError naming the file, at the
    loader and through the CLI's config_error envelope."""

    def test_over_magnitude_weight_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "triage.toml"
            cfg.write_text("[triage]\ndemand = 1e308\n", encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_triage_config(explicit_config=str(cfg))
            self.assertIn("demand", str(ctx.exception))
            self.assertIn("1000000000", str(ctx.exception))
            self.assertEqual(ctx.exception.path, str(cfg))

    def test_map_weight_over_magnitude_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "triage.toml"
            cfg.write_text(
                "[triage.labels]\nhigh = 1e308\n", encoding="utf-8"
            )
            with self.assertRaises(ConfigError):
                load_triage_config(explicit_config=str(cfg))

    def test_cap_boundary_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "triage.toml"
            cfg.write_text("[triage]\ndemand = 1e9\n", encoding="utf-8")
            loaded = load_triage_config(explicit_config=str(cfg))
            self.assertEqual(loaded["demand"], 1e9)

    def test_cli_answers_config_error_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "triage.toml"
            cfg.write_text("[triage]\nstage2_weight = 1e308\n", encoding="utf-8")
            rc, out, _err = _run_cli(
                "triage", "--db", str(self.db_path), "--repo", REPO,
                "--config", str(cfg), "--format", "json",
            )
        self.assertEqual(rc, 2)
        env = json.loads(out.decode("utf-8"))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "config_error")


class _BundleableStore(_PagingStore):
    """Three issues sharing one resolved path (PR 810 merged over
    src/pg.py, bodies planting ``path:L:C`` refs the way the frozen
    fixtures do), so every pair carries a files_direct edge well past the
    default affinity floor."""

    @classmethod
    def _build(cls, conn) -> None:
        src = FakeGitHubSource()
        src.add_pr(
            issue(810, title="merge the paging path",
                  body="fixes the fault at src/pg.py:1:1", kind="pr",
                  state="closed"),
            pull(810, merged=True),
            files=[pr_file("src/pg.py")],
        )
        for n in (861, 862, 863):
            src.add_issue(
                issue(
                    n,
                    title=f"paging bundle fixture {n}",
                    body=f"fault at src/pg.py:{n}:1",
                    created_at="2027-06-01T00:00:00Z",
                )
            )
        sync_repo(conn, src, REPO)


class BundleFeedbackTests(_BundleableStore):
    """F6 (the normal size exit reports the membership cap) and F14
    (--pool "" is absent, not an explicit empty pool)."""

    def test_target_size_exit_reports_membership_cap(self) -> None:
        conn = open_existing(self.db_path)
        try:
            out = bundle(conn, REPO, size="2-2", count=1)
        finally:
            conn.close()
        self.assertEqual(len(out["bundles"]), 1)
        why = out["bundles"][0]["why_not_bigger"]
        self.assertEqual(
            why,
            "membership cap (reached target size)",
            f"a bundle that grew to size_max must report the membership cap,"
            f" not a stale default (got {why!r})",
        )

    def test_blank_pool_flag_means_absent(self) -> None:
        rc, out, _err = _run_cli(
            "bundle", "--db", str(self.db_path), "--repo", REPO,
            "--pool", "", "--format", "json",
        )
        self.assertEqual(rc, 0)
        env = json.loads(out.decode("utf-8"))
        self.assertTrue(env["ok"], env)
        pool = env["data"]["pool"]
        self.assertEqual(
            pool, [861, 862, 863],
            "--pool '' must be treated as absent (the default pool), never"
            " as an explicit empty pool",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

class PreV7OpenDegradesTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "pre_v7.db"
        self.conn = connect(self.db_path)
        self.addCleanup(self.conn.close)

    def test_open_items_on_pre_v7_store_keeps_base_listing(self):
        """RED at base, GREEN after the fix: a store without the v7
        enrichment tables keeps the base open listing (empty assignees)
        instead of raising."""
        self.conn.executescript(
            """
            CREATE TABLE items (
                id INTEGER PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL,
                kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT, state TEXT NOT NULL,
                author TEXT, created_at TEXT, updated_at TEXT, html_url TEXT,
                UNIQUE (repo, number)
            );
            CREATE TABLE labels (
                repo TEXT NOT NULL, number INTEGER NOT NULL, name TEXT NOT NULL,
                color TEXT, UNIQUE (repo, number, name)
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY, repo TEXT NOT NULL, src_type TEXT NOT NULL,
                src_id TEXT NOT NULL, rel TEXT NOT NULL, dst_type TEXT NOT NULL,
                dst_id TEXT NOT NULL, confidence TEXT NOT NULL DEFAULT 'EXTRACTED',
                evidence TEXT, source TEXT NOT NULL DEFAULT 'keyword',
                UNIQUE (repo, src_type, src_id, rel, dst_type, dst_id, source)
            );
            """
        )
        self.conn.execute(
            "INSERT INTO items (repo, number, kind, title, state)"
            " VALUES (?, 1, 'issue', 't', 'open')",
            (REPO,),
        )
        self.conn.commit()
        rows = open_items(self.conn, limit=10, repo=REPO)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["assignees"], [])
        self.assertEqual(rows[0]["linked_open_prs"], [])
