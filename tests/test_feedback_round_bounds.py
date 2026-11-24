"""Regression pins for the post-publication feedback round on PR #96.

Each leg was RED at 12bc4dd before its fix (AGENTS.md: the regression test
comes first and is confirmed failing). The classes:

* the shared issue-number grammar and the MCP array sink bound values to the
  signed 64-bit range, so no issue number can reach a SQL bind and surface as
  an uncoded OverflowError/runtime failure (converged finding:
  correctness-state-C001, cubic mcp_server:1486 / cli.py:1870);
* the MCP sync ``numbers`` sink (the CLI ``--numbers`` twin) carries the same
  ceiling;
* the CLI ``bundle --max-files`` override honours the same positive-integer
  contract the ``[triage] max_files`` config key already enforces;
* the CLI ``bundle --count 0`` path stays PRESERVED (the MCP bound must not
  leak into shared derive code -- frozen C13);
* ``_status_from_stderr`` never reads an issue number as an HTTP status: an
  exact ``429`` inside a 404 message must not shadow the 404 verdict, while a
  bare ``429 Too Many Requests`` still classifies;
* ``CLOCK$`` is not a current Win32 reserved device name (CPython 3.13/3.14
  ``os.path.isreserved`` answers False and the name is creatable as a normal
  file/directory), so it must not be refused as one; the issue's literal
  device list stays refused;
* the two frozen pytest modules import without pytest installed, so the
  documented ``python -m unittest discover`` command does not error.

This file is deliberately NOT frozen: it is a ``unittest.TestCase`` module so
the documented discovery command collects it on every interpreter, with no
CI wiring of its own.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fixtures import REPO, FakeGitHubSource, issue  # noqa: E402
from test_mcp import McpSession  # noqa: E402

from zaxbygraph import cli  # noqa: E402
from zaxbygraph.db import connect, init_schema  # noqa: E402
from zaxbygraph.export import ExportError, _reject_database_alias  # noqa: E402
from zaxbygraph.github import _status_from_stderr  # noqa: E402
from zaxbygraph.sync import sync_repo  # noqa: E402

OVERSIZED = 2**63  # one past SQLite's signed-64-bit ceiling


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


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    """In-process ``cli.main`` under redirected stdio (the repo's offline form).

    Never a subprocess: a real ``sync`` would spawn ``gh``. Transport seams are
    patched by the caller where the command would otherwise reach one.
    """
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
    return code, out.getvalue(), err.getvalue()


def _mcp_call(db_path: Path, tool: str, arguments: dict) -> dict:
    case = unittest.TestCase()
    # A no-op sync runner: before the fix, the ``sync`` handler accepted an
    # out-of-range number and SPAWNED a real background sync, which raced the
    # TemporaryDirectory teardown and surfaced as a Windows ``PermissionError``
    # instead of the assertion under test (review round 1 finding). The pin
    # must fail on its own contract, not on cleanup.
    session = McpSession(
        case, repo=REPO, db=str(db_path), stale_after_s=10**9,
        sync_runner=lambda _db, _repo: None,
    )
    try:
        session.initialize()
        return session.call_tool(tool, arguments)
    finally:
        session.stop()


def _result_text(response: dict) -> str:
    result = response.get("result") or {}
    return "".join(
        part.get("text", "")
        for part in (result.get("content") or [])
        if isinstance(part, dict)
    )


def _assert_coded_bad_request(
    case: unittest.TestCase, response: dict, label: str
) -> None:
    result = response.get("result") or {}
    case.assertIs(result.get("isError"), True, f"{label} must be refused")
    text = _result_text(response)
    case.assertIn("bad_request", text, f"{label} must answer bad_request; got {text[:400]!r}")


class IssueNumberInt64Ceiling(unittest.TestCase):
    """Every number sink refuses values SQLite cannot bind."""

    def test_cli_sync_numbers_ceiling(self) -> None:
        seen: list[object] = []

        def _spy(*args, **kwargs):
            seen.append(kwargs.get("numbers"))
            return {"ingested": 0}

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "history.db"
            with mock.patch.object(cli, "_sync_core", _spy):
                code, out, err = _run_cli(
                    ["sync", "--numbers", str(OVERSIZED), "--repo", REPO,
                     "--db", str(db_path), "--format", "json"]
                )
        self.assertEqual(code, 2, f"out={out[:300]!r} err={err[:300]!r}")
        self.assertEqual(json.loads(out)["error"]["code"], "bad_request")
        self.assertEqual(seen, [], "no fetch may run for an out-of-range --numbers")

    def test_cli_bundle_pool_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "history.db"
            _seed(db_path)
            code, out, err = _run_cli(
                ["bundle", "--pool", str(OVERSIZED), "--db", str(db_path),
                 "--repo", REPO, "--format", "json"]
            )
        self.assertEqual(code, 2, f"out={out[:300]!r} err={err[:300]!r}")
        self.assertEqual(json.loads(out)["error"]["code"], "bad_request")

    def test_cli_bundle_exclude_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "history.db"
            _seed(db_path)
            code, out, err = _run_cli(
                ["bundle", "--exclude", str(OVERSIZED), "--db", str(db_path),
                 "--repo", REPO, "--format", "json"]
            )
        self.assertEqual(code, 2, f"out={out[:300]!r} err={err[:300]!r}")
        self.assertEqual(json.loads(out)["error"]["code"], "bad_request")

    def test_mcp_bundle_pool_and_exclude_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "history.db"
            _seed(db_path)
            pool = _mcp_call(db_path, "bundle", {"pool": [OVERSIZED]})
            exclude = _mcp_call(db_path, "bundle", {"exclude": [OVERSIZED]})
        _assert_coded_bad_request(self, pool, "MCP bundle pool=2**63")
        _assert_coded_bad_request(self, exclude, "MCP bundle exclude=2**63")

    def test_mcp_sync_numbers_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "history.db"
            _seed(db_path)
            response = _mcp_call(db_path, "sync", {"numbers": [OVERSIZED]})
        _assert_coded_bad_request(self, response, "MCP sync numbers=[2**63]")


class BundleFlagContract(unittest.TestCase):
    """The CLI override obeys the same bounds as the config key and the MCP
    handler, except the deliberately preserved ``--count 0`` path (frozen C13)."""

    def test_cli_bundle_max_files_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "history.db"
            _seed(db_path)
            for value in ("0", "-3"):
                code, out, err = _run_cli(
                    ["bundle", "--max-files", value, "--db", str(db_path),
                     "--repo", REPO, "--format", "json"]
                )
                self.assertEqual(
                    code, 2,
                    f"--max-files {value} must be refused; out={out[:300]!r}",
                )
                self.assertEqual(json.loads(out)["error"]["code"], "bad_request")

    def test_cli_bundle_count_zero_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "history.db"
            _seed(db_path)
            code, out, err = _run_cli(
                ["bundle", "--count", "0", "--db", str(db_path),
                 "--repo", REPO, "--format", "json"]
            )
        self.assertEqual(code, 0, f"out={out[:300]!r} err={err[:300]!r}")
        envelope = json.loads(out)
        self.assertIs(envelope["ok"], True)
        self.assertEqual(envelope["data"]["bundles"], [])


class StatusClassifier(unittest.TestCase):
    """A 429 inside an issue number or URL is never an HTTP status."""

    def test_exact_issue_number_429_does_not_shadow_404(self) -> None:
        shapes = (
            "gh: issue 429 not found (HTTP 404)",
            "gh: Could not resolve to an Issue or PullRequest with the number"
            " of 429. (HTTP 404)",
            "gh: HTTP 404: Not Found"
            " (https://api.github.com/repos/acme/forgegate/issues/429)",
        )
        for text in shapes:
            self.assertEqual(
                _status_from_stderr(text), 404,
                f"a 429 issue number must not shadow the 404 verdict: {text!r}",
            )

    def test_embedded_429_digits_still_do_not_classify(self) -> None:
        self.assertEqual(_status_from_stderr("gh: issue 1429 not found (HTTP 404)"), 404)
        self.assertEqual(
            _status_from_stderr("gh: HTTP 403: forbidden talking about issue 4291"), 403
        )

    def test_real_rate_limits_still_classify(self) -> None:
        for text in (
            "gh: HTTP 429: too many requests",
            "gh: API rate limit exceeded for 1.2.3.4",
            "gh: You have exceeded a secondary rate limit",
            "gh: 429 Too Many Requests",
            "gh: status code 429",
        ):
            self.assertEqual(
                _status_from_stderr(text), 429,
                f"a genuine rate-limit shape must classify 429: {text!r}",
            )


class DeviceNameSet(unittest.TestCase):
    """CLOCK$ is not a reserved Win32 device name; the issue's list still is."""

    def test_clock_dollar_is_an_ordinary_filename(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "history.db")
            Path(db_path).write_bytes(b"store bytes")
            # Build paths BEFORE patching os.name (pathlib reads it).
            with mock.patch.object(os, "name", "nt"), mock.patch.object(
                os.path, "realpath", lambda path: str(path)
            ):
                _reject_database_alias("CLOCK$", db_path)

    def test_issue_device_list_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "history.db")
            Path(db_path).write_bytes(b"store bytes")
            with mock.patch.object(os, "name", "nt"), mock.patch.object(
                os.path, "realpath", lambda path: str(path)
            ):
                for name in ("CON", "NUL", "CONIN$", "CONOUT$"):
                    with self.assertRaises(ExportError) as caught:
                        _reject_database_alias(name, db_path)
                    self.assertEqual(caught.exception.code, "out_is_database")


class PytestOptional(unittest.TestCase):
    """The documented stdlib-only discovery command imports every test module."""

    def test_frozen_modules_import_without_site_packages(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT / "tests")])
        proc = subprocess.run(
            [sys.executable, "-S", "-c",
             "import test_seams_extended, test_seam_honesty"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(
            proc.returncode, 0,
            "the documented stdlib-only suite imports every test module; a"
            " module-level `import pytest` breaks it:"
            f" {proc.stderr.strip()[:600]!r}",
        )


if __name__ == "__main__":
    unittest.main()
