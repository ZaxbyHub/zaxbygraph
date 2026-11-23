"""Non-frozen pins for two canonicalization gaps the implementation review found.

Both properties were outside the frozen check set (adding cases would have
edited frozen blobs and invalidated the pre-implementation anchor), so they are
pinned here and wired into CI.

1. `--numbers`/`--pool`/`--exclude`: whitespace is allowed AROUND a list entry
   but never INSIDE one. A blanket space strip turned `1 0` into issue 10 -- a
   silent re-target -- while `1, 2` must keep working.
2. MCP cursor: the producer mints `str(offset)`, which never carries a leading
   zero beyond `"0"` itself, so `"00"`/`"01"` are aliases `int()` would
   silently normalize.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fixtures import REPO, FakeGitHubSource, issue  # noqa: E402
from test_mcp import McpSession  # noqa: E402

from zaxbygraph.cli import main  # noqa: E402
from zaxbygraph.db import connect, init_schema  # noqa: E402
from zaxbygraph.sync import sync_repo  # noqa: E402


def _run(argv: list[str]) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue() + err.getvalue()


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


def test_internal_whitespace_never_joins_digits() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "history.db"
        _seed(db_path)
        for flag in ("--pool", "--exclude"):
            for bad in ("1 0", "1 2,3", "0 7"):
                code, text = _run(
                    ["bundle", flag, bad, "--repo", REPO, "--db", str(db_path),
                     "--format", "json"]
                )
                assert code == 2, (
                    f"`{flag} {bad!r}` must be refused (whitespace inside a"
                    f" number is not a list separator); got exit {code}:"
                    f" {text.strip()[:200]!r}"
                )
                assert "bad_request" in text, (
                    f"`{flag} {bad!r}` must answer bad_request; got {text[:300]!r}"
                )
        # Spaces AROUND entries remain accepted.
        code, text = _run(
            ["bundle", "--pool", "1, 2, 3", "--repo", REPO, "--db",
             str(db_path), "--format", "json"]
        )
        assert code == 0, (
            "a list with spaces around its entries must still work; got"
            f" exit {code}: {text.strip()[:200]!r}"
        )


def test_cursor_rejects_leading_zero_aliases() -> None:
    test_case = unittest.TestCase()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "history.db"
        _seed(db_path)
        session = McpSession(test_case, repo=REPO, db=str(db_path), stale_after_s=10**9)
        try:
            session.initialize()
            for bad in ("00", "01", "0007"):
                result = (session.call_tool("search", {"query": "issue", "cursor": bad})
                          .get("result") or {})
                assert result.get("isError") is True, (
                    f"cursor {bad!r} is not producer-minted (leading zero) and"
                    f" must be refused; got {str(result)[:300]}"
                )
            for good in ("0", "10"):
                result = (session.call_tool("search", {"query": "issue", "cursor": good})
                          .get("result") or {})
                assert result.get("isError", False) is False, (
                    f"producer-minted cursor {good!r} must keep working; got"
                    f" {str(result)[:300]}"
                )
        finally:
            session.stop()
