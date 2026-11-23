"""Frozen acceptance check for #90 AC5: the MCP bundle tool agrees with its
published inputSchema.

`tools/list` publishes `{"count": {"type": "integer", "minimum": 1}}`, but
`_tool_bundle` read `count` through a type-only helper and applied no bound, so
`{"count": 0}` answered `isError: false` with `bundles: []` -- a schema lie to
every client that validates against the published schema. The sibling
`budget_tokens` already refuses an out-of-range value with `bad_request`.

RED at e6c8f41: `count: 0` answers success with an empty bundle list.
GREEN after the handler bounds `count`.

The check drives the real MCP server over its own stdio protocol with a
seeded temp store pinned to the repo, so no root discovery, no network and no
gh process are involved.
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


def test_bundle_count_zero_refused() -> None:
    test_case = unittest.TestCase()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "history.db"
        _seed(db_path)
        session = McpSession(
            test_case,
            repo=REPO,
            db=str(db_path),
            stale_after_s=10**9,
        )
        try:
            session.initialize()
            response = session.call_tool("bundle", {"count": 0})
        finally:
            session.stop()

    result = response.get("result") or {}
    assert result.get("isError") is True, (
        "MCP bundle with count=0 must be refused (the published inputSchema"
        f" declares minimum 1); got {str(result)[:500]}"
    )
    text = "".join(
        part.get("text", "")
        for part in (result.get("content") or [])
        if isinstance(part, dict)
    )
    assert "bad_request" in text, (
        "the refusal must carry the coded bad_request error;"
        f" got {text[:500]!r}"
    )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
