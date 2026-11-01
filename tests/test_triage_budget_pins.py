"""Budget-cap pins (issue #46 AC3): C20's vacuous pin replaced by
discriminating legs — a BINDING budget (truncated true + chars bound), the
below-floor FULL shed (every sheddable section empty, pool echo emptied),
and the above-floor bound arm reachable (no truncated key, chars bounded).

NEW file (new-checks-new-files convention; C20 stays the historical record).
Stdlib only; unittest; hermetic.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, issue, pull
from fixtures_eval import _insert_edge
from test_triage_bundle_supp import _BulkSource, _pin_synced_at
from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo

REPO_ROOT = Path(__file__).resolve().parents[1]

#: hermeticity (issue #46 AC2): bundle legs run in a dedicated temp cwd so
#: ambient operator configs cannot reweight the pins
_LEG_CWD = tempfile.mkdtemp(prefix="zaxbygraph-budget-legs-")


def _cli_json(argv: list[str]) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([
        str(REPO_ROOT / "src"), str(REPO_ROOT / "tests"),
        env.get("PYTHONPATH", "")])
    proc = subprocess.run([sys.executable, "-m", "zaxbygraph", *argv],
                          capture_output=True, env=env, cwd=_LEG_CWD,
                          timeout=120)
    return json.loads(proc.stdout.decode("utf-8"))
class BudgetCapPins(unittest.TestCase):
    """Three discriminating budget legs on a bundle-forming fixture."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.td = Path(tempfile.mkdtemp(prefix="budget_"))
        db = cls.td / "history.db"
        conn = connect(db)
        init_schema(conn)
        conn.row_factory = sqlite3.Row
        src = _BulkSource()
        for n in range(561, 585):
            src.add_issue(issue(n, title=f"Distinct report {n}",
                                body=f"see src/a.py:1:1",
                                created_at="2027-06-01T00:00:00Z"))
        src.add_pr(
            issue(600, kind="pr", title="history 600", body="",
                  state="closed", created_at="2027-03-01T00:00:00Z"),
            pull(600, merged=True),
            files=[{"filename": "src/a.py"}],
        )
        sync_repo(conn, src, REPO)
        for i in range(12):
            _insert_edge(conn, REPO, 561 + 2 * i, "duplicate_of", 562 + 2 * i)
        _pin_synced_at(conn)
        conn.close()
        cls.db = db

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.td, ignore_errors=True)

    @staticmethod
    def _run(budget: str) -> tuple[int, dict]:
        env = _cli_json([
            "bundle", "--db", str(BudgetCapPins.db), "--repo", REPO,
            "--count", "5", "--budget-tokens", budget,
            "--format", "json"])
        data = env["data"]
        chars = len(json.dumps(data, sort_keys=True, ensure_ascii=True,
                               separators=(",", ":")))
        return chars, data
    def test_binding_budget_caps_and_flags(self) -> None:
        chars, data = self._run("750")
        self.assertLessEqual(chars, 3000)
        self.assertIs(data.get("truncated"), True)

    def test_below_floor_full_shed(self) -> None:
        chars, data = self._run("100")
        self.assertIs(data.get("truncated"), True)
        self.assertEqual(data.get("bundles"), [])
        self.assertEqual(data.get("merge_candidates"), [])
        self.assertEqual(data.get("related_not_bundleable"), [])
        self.assertEqual(data.get("pool"), [])

    def test_above_floor_bound_reachable(self) -> None:
        chars, data = self._run("100000")
        self.assertNotIn("truncated", data)
        self.assertLessEqual(chars, 100000 * 4)
