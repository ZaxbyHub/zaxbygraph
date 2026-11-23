"""Extended frozen checks from the plan-critic round (collated #89 + #90).

These pin the defect-class siblings and the preserved contracts the critic
required before implementation:

* C12 (#90 AC12): the MCP bundle handler enforces the published numeric bounds
  for ``max_files`` and for per-item ``pool``/``exclude`` entries (the same
  schema-vs-handler class as ``count``).
* C13 (#90 AC13): the CLI's documented ``bundle --count 0`` path still answers
  success with no bundles (the MCP bound must not leak into shared derive code).
* C14 (#89 AC14): the canonical spaced list keeps reaching the seam unchanged,
  and an oversized digit run stays a coded refusal rather than a traceback.
* C15 (#89 AC15): the claim/release resolution error also names the ``--repo``
  remedy exactly once (the second wrapper the first check does not cover).
* C16 (#89 AC16): ``bundle --pool``/``--exclude`` refuse non-canonical tokens
  before any read (the same untrusted-token grammar as ``--numbers``).

Offline throughout: no gh process, no network, temp stores only.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fixtures import REPO, FakeGitHubSource, issue  # noqa: E402
from test_mcp import McpSession  # noqa: E402

from zaxbygraph import cli  # noqa: E402
from zaxbygraph.cli import main  # noqa: E402
from zaxbygraph.db import connect, init_schema  # noqa: E402
from zaxbygraph.sync import sync_repo  # noqa: E402


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            if isinstance(exc.code, int):
                code = exc.code
            elif exc.code is None:
                code = 0
            else:
                code = 2
    return code, out.getvalue(), err.getvalue()


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


# ==== #90 AC12: every published numeric bound in the bundle tool ===========

def test_bundle_numeric_bounds_refused() -> None:
    test_case = unittest.TestCase()
    calls = (
        {"count": 1, "max_files": 0},
        {"count": 1, "max_files": -1},
        {"count": 1, "pool": [0]},
        {"count": 1, "exclude": [-2]},
    )
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "history.db"
        _seed(db_path)
        for arguments in calls:
            session = McpSession(
                test_case, repo=REPO, db=str(db_path), stale_after_s=10**9
            )
            try:
                session.initialize()
                response = session.call_tool("bundle", dict(arguments))
            finally:
                session.stop()
            result = response.get("result") or {}
            assert result.get("isError") is True, (
                f"MCP bundle {arguments!r} must be refused (the published"
                f" inputSchema declares minimum 1); got {str(result)[:400]}"
            )
            text = "".join(
                part.get("text", "")
                for part in (result.get("content") or [])
                if isinstance(part, dict)
            )
            assert "bad_request" in text, (
                f"MCP bundle {arguments!r} must answer the coded bad_request;"
                f" got {text[:400]!r}"
            )


# ==== #90 AC13: the CLI's documented --count 0 path is unchanged ===========

def test_cli_bundle_count_zero_still_succeeds() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "history.db"
        _seed(db_path)
        code, out, err = _run_cli(
            ["bundle", "--count", "0", "--repo", REPO, "--db", str(db_path),
             "--format", "json"]
        )
    assert code == 0, (
        f"the CLI's documented `bundle --count 0` keeps exit 0; got {code}"
        f" ({err.strip()[:200]!r})"
    )
    payload = json.loads(out)
    assert payload.get("ok") is True, f"expected a success envelope; got {out[:300]!r}"
    assert payload["data"]["bundles"] == [], (
        f"`bundle --count 0` answers no bundles; got {payload['data']['bundles']!r}"
    )


# ==== #89 AC14: canonical lists keep working; oversized digits stay coded ===

def test_numbers_grammar_preserved_and_oversized_coded() -> None:
    seen: list[object] = []

    def _spy(*args, **kwargs):
        seen.append(kwargs.get("numbers"))
        return {"ingested": 0}

    oversized = "9" * 5000  # beyond CPython's int-string digit limit
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "history.db")
        with mock.patch.object(cli, "_sync_core", _spy):
            # A spaced, comma-separated list keeps reaching the seam unchanged.
            seen.clear()
            code, out, err = _run_cli(
                ["sync", "--numbers", "1, 2", "--repo", REPO, "--db", db_path,
                 "--format", "json"]
            )
            assert code == 0, f"a canonical spaced list must be accepted; got {code} ({err[:200]!r})"
            assert seen == [[1, 2]], (
                f"the canonical list must reach the sync seam as [1, 2]; got {seen!r}"
            )
            # Leading zeros normalize to their integer value.
            seen.clear()
            code, out, err = _run_cli(
                ["sync", "--numbers", "007", "--repo", REPO, "--db", db_path,
                 "--format", "json"]
            )
            assert code == 0, f"a leading-zero token must be accepted; got {code} ({err[:200]!r})"
            assert seen == [[7]], f"007 must reach the seam as [7]; got {seen!r}"
            # An oversized digit run is a coded refusal, never a traceback.
            seen.clear()
            code, out, err = _run_cli(
                ["sync", "--numbers", oversized, "--repo", REPO, "--db", db_path,
                 "--format", "json"]
            )
            assert code == 2, (
                f"an oversized --numbers token must be refused with exit 2; got {code}"
                f" ({err[:200]!r})"
            )
            assert json.loads(out)["error"]["code"] == "bad_request", (
                f"the oversized refusal carries bad_request; got {out[:300]!r}"
            )
            assert seen == [], f"no fetch may run for an oversized token; saw {seen!r}"


# ==== #89 AC15: the claim/release wrapper names the remedy once ============

def test_claim_resolution_error_names_remedy_once() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.run(
            [sys.executable, "-m", "zaxbygraph", "claim", "5", "--format", "json"],
            cwd=td,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )
    assert proc.returncode == 2, (
        f"an unresolvable repo keeps exit 2; got {proc.returncode}"
        f" ({(proc.stderr or proc.stdout).strip()[:300]!r})"
    )
    payload = json.loads(proc.stdout)
    message = str((payload.get("error") or {}).get("message") or "")
    count = message.count("pass --repo OWNER/REPO")
    assert count == 1, (
        "the claim/release resolution error must name the --repo remedy"
        f" exactly once; got {count} in {message!r}"
    )


# ==== #89 AC16: bundle pool/exclude use the canonical token grammar ========

def test_bundle_pool_rejects_noncanonical_tokens() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "history.db"
        _seed(db_path)
        for flag in ("--pool", "--exclude"):
            for token in ("1_0", "+5", "-5", "\u0663", "0"):
                code, out, err = _run_cli(
                    ["bundle", flag, token, "--repo", REPO, "--db",
                     str(db_path), "--format", "json"]
                )
                assert code == 2, (
                    f"`bundle {flag} {token!r}` must be refused with exit 2;"
                    f" got {code} ({err.strip()[:200]!r})"
                )
                assert json.loads(out)["error"]["code"] == "bad_request", (
                    f"`bundle {flag} {token!r}` must answer bad_request;"
                    f" got {out[:300]!r}"
                )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
