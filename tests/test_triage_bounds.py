"""Frozen acceptance check for #90 AC18: the MCP triage tool's `top_k` bound.

`tools/list` publishes `top_k` with `minimum: 1`, but the handler only clamped
the UPPER end (`min(top_k, _MAX_TOP_K)`), so `top_k: 0` reached the ranking and
answered an empty row set -- the same schema/handler disagreement the issue
names for `bundle count`, found by the Phase-4.2 published-bound sweep.

RED at e6c8f41: `top_k: 0` answers `isError: false` with no rows.
GREEN after the handler bound.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fixtures import REPO, FakeGitHubSource, issue  # noqa: E402
from test_mcp import McpSession  # noqa: E402

from zaxbygraph.db import connect, init_schema  # noqa: E402
from zaxbygraph.sync import sync_repo  # noqa: E402


def _seed(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        init_schema(conn)
        src = FakeGitHubSource()
        for number in (1, 2, 3):
            src.add_issue(issue(number, title=f"issue {number}", state="open"))
        sync_repo(conn, src, REPO)
    finally:
        conn.close()


def test_triage_top_k_bound_refused() -> None:
    test_case = unittest.TestCase()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "history.db"
        _seed(db_path)
        for bad in (0, -1):
            session = McpSession(
                test_case, repo=REPO, db=str(db_path), stale_after_s=10**9
            )
            try:
                session.initialize()
                response = session.call_tool("triage", {"top_k": bad})
            finally:
                session.stop()
            result = response.get("result") or {}
            assert result.get("isError") is True, (
                f"triage top_k={bad} must be refused (the published"
                f" inputSchema declares minimum 1); got {str(result)[:400]}"
            )
            text = "".join(
                part.get("text", "")
                for part in (result.get("content") or [])
                if isinstance(part, dict)
            )
            assert "bad_request" in text, (
                f"triage top_k={bad} must answer the coded bad_request;"
                f" got {text[:400]!r}"
            )
        # A valid top_k still ranks.
        session = McpSession(test_case, repo=REPO, db=str(db_path), stale_after_s=10**9)
        try:
            session.initialize()
            response = session.call_tool("triage", {"top_k": 1})
        finally:
            session.stop()
        result = response.get("result") or {}
        assert result.get("isError", False) is False, (
            f"a valid top_k must still answer; got {str(result)[:400]}"
        )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
