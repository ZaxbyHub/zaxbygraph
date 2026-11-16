"""The claimed gate's acceptance checks, CI-discovered (FB-003).

The frozen acceptance files deliberately do not duplicate these two checks --
they were pinned only by the issue-tracer trace legs, which CI never runs, so
the named-issue half of the gate could be removed with a green suite. This
module moves both into ``tests/`` where ``python -m unittest discover`` (the
CI command) finds them:

- ``test_own_thread_claim_gates_the_issue``  -- issue #74 AC1 (gate fires)
- ``test_claim_naming_other_issues_gates_each`` -- issue #74 AC2 (named issues)

Both drive the REAL ingest path (``sync_repo`` over ``FakeGitHubSource``) and
assert through the real ``triage`` CLI, so a mutation that drops either half
of the gate turns this file red.
"""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, comment, issue
from zaxbygraph.cli import main
from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo


def _ago(hours: float) -> str:
    """A WALL-CLOCK-relative stamp. The derived gate classifies the 72h
    window against the store's sync stamp -- the real clock at ingest -- so an
    absolute date is a time bomb: this file would pass today and fail on a
    fixed calendar day (probed: with the clock at 2026-11-20 both legs fail).
    Every date here is therefore relative to now."""
    moment = datetime.now(timezone.utc) - timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _claim(token: str, named: str = "") -> dict:
    body = (
        f"**Agent claim:** session={token}{named} Working this issue now."
    )
    rec = comment(50_000 + (len(token) % 1000), body, author="alice",
                  created_at=_ago(1))
    rec["author_association"] = "MEMBER"
    return rec


class ClaimedGateSuiteTests(unittest.TestCase):
    """AC1/AC2 of issue #74, discovered by CI's unittest run."""

    def setUp(self) -> None:
        self._td = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = self.root / "history.db"

    def _seed(self, comments_by_number: dict[int, list[dict]]) -> None:
        conn = connect(self.db_path)
        init_schema(conn)
        src = FakeGitHubSource()
        for number in (8, 9):
            src.issues[number] = issue(
                number,
                title=f"issue {number}",
                comments=len(comments_by_number.get(number, [])),
                created_at=_ago(24 * 30),
                updated_at=_ago(1),
            )
        for number, comments in comments_by_number.items():
            src.issue_comments[number] = comments
        sync_repo(conn, src, REPO)
        conn.commit()
        conn.close()

    def _triage_gates(self) -> dict[int, list[str]]:
        argv = [
            "triage", "--db", str(self.db_path), "--repo", REPO,
            "--include-gated", "--format", "json",
        ]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(argv)
        self.assertEqual(code, 0, "triage must answer")
        env = json.loads(buf.getvalue())
        return {
            row["number"]: list(row.get("gates") or [])
            for row in env["data"]["rows"]
        }

    def test_own_thread_claim_gates_the_issue(self) -> None:
        """AC1: a trusted in-window claim comment gates its own issue."""
        self._seed({8: [_claim("OWNTHREAD-TOKEN-1234567")]})
        gates = self._triage_gates()
        self.assertIn(
            "claimed",
            gates.get(8, []),
            f"issue 8 carries a trusted active claim and must be gated; gates={gates.get(8)}",
        )
        self.assertNotIn(
            "claimed",
            gates.get(9, []),
            f"issue 9 has no claim and must stay ungated; gates={gates.get(9)}",
        )

    def test_claim_naming_other_issues_gates_each(self) -> None:
        """AC2: a claim naming other same-repo issues gates each of them."""
        self._seed(
            {8: [_claim("NAMEDISSUE-TOKEN-1234567", named=" issues=#8,#9")]}
        )
        gates = self._triage_gates()
        self.assertIn(
            "claimed",
            gates.get(8, []),
            f"the claim's own issue must be gated; gates={gates.get(8)}",
        )
        self.assertIn(
            "claimed",
            gates.get(9, []),
            f"the NAMED issue must be gated too; gates={gates.get(9)}",
        )


if __name__ == "__main__":
    unittest.main()
