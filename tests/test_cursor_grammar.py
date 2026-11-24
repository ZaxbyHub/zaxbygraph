"""Frozen acceptance check for #89 AC17: the MCP cursor parser is
producer-symmetric.

Every paging tool mints ``next_cursor`` as ``str(offset)`` — plain ASCII
decimal — and ``_int_cursor`` promises "validation symmetrically with the
producer", answering ``bad_request`` for malformed input. It nonetheless
accepted every ``int()`` leniency the sibling parsers reject: ``1_0`` -> 10,
``+5`` -> 5, ``٣`` -> 3, and a leading space. Those forms are never
producer-minted, so accepting them is a silent normalization rather than the
documented refusal.

RED at e6c8f41: ``cursor "1_0"`` is accepted (a page answers, isError false).
GREEN after the canonical ASCII-digit grammar lands.
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


def _result_text(result: dict) -> str:
    return "".join(
        part.get("text", "")
        for part in (result.get("content") or [])
        if isinstance(part, dict)
    )


def test_cursor_rejects_noncanonical_tokens() -> None:
    test_case = unittest.TestCase()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "history.db"
        _seed(db_path)
        session = McpSession(
            test_case, repo=REPO, db=str(db_path), stale_after_s=10**9
        )
        try:
            session.initialize()
            for bad in ("1_0", "+5", "\u0663", " 5", "-3"):
                response = session.call_tool(
                    "search", {"query": "issue", "cursor": bad}
                )
                result = response.get("result") or {}
                assert result.get("isError") is True, (
                    f"cursor {bad!r} must be refused as a malformed cursor"
                    f" (producer-minted cursors are plain ASCII digits);"
                    f" got {str(result)[:400]}"
                )
                assert "bad_request" in _result_text(result), (
                    f"cursor {bad!r} must answer the coded bad_request;"
                    f" got {_result_text(result)[:400]!r}"
                )
            # A producer-minted offset still pages normally.
            response = session.call_tool("search", {"query": "issue", "cursor": "0"})
            result = response.get("result") or {}
            assert result.get("isError", False) is False, (
                "a producer-minted cursor ('0') must keep working;"
                f" got {str(result)[:400]}"
            )
        finally:
            session.stop()


if __name__ == "__main__":
    raise SystemExit(unittest.main())
