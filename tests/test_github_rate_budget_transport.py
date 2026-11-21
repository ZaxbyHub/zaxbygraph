"""Transport-level regression tests for the rate-budget probe decode
(issue #86, plan change 0): the probe path of GhApiSource._api decodes the
UNSTRIPPED, transport-decoded stdout with the non-standard JSON constants
(NaN/Infinity, RFC 8259 section 6) rejected, while every other path keeps
the lenient strip-then-parse decode.

These tests exercise the REAL `_api` through a mocked subprocess.run — no
network, no gh binary. They are the change-0 guardrail the frozen seam-level
checks (tests/test_github_rate_budget_probe.py) cannot provide: their stubs
replace `_api` wholesale, so a regression that reverts `_api` to permissive
decoding stays invisible to them, while these tests go red.
"""
from __future__ import annotations

import unittest
from unittest import mock

from zaxbygraph.github import GhApiSource, GitHubError


def _gh_process(stdout: str) -> object:
    proc = mock.Mock()
    proc.returncode = 0
    proc.stdout = stdout
    proc.stderr = ""
    return proc


def _run_probe(stdout: str) -> GitHubError:
    """Run a 429 through _with_rate_budget with the real _api parsing
    `stdout` (subprocess.run mocked — no network, no gh binary)."""
    src = GhApiSource("acme", "forgegate", gh_bin="gh")
    with mock.patch("zaxbygraph.github.subprocess.run") as run:
        run.return_value = _gh_process(stdout)
        original = GitHubError("gh: API rate limit exceeded", status=429)
        return src._with_rate_budget(original)


class _EnrichmentAssertions:
    def assert_unenriched(self, exc: GitHubError) -> None:
        self.assertIsNone(getattr(exc, "rate_limit_remaining", None))
        self.assertIsNone(getattr(exc, "rate_limit_reset", None))


class ProbeTransportDecodeTests(_EnrichmentAssertions, unittest.TestCase):
    """The probe's answer is decoded strictly, on the raw stdout."""

    def test_malformed_stdout_keeps_original(self) -> None:
        exc = _run_probe("not-json at all")
        self.assertEqual(exc.status, 429)
        self.assertEqual(str(exc), "gh: API rate limit exceeded")
        self.assert_unenriched(exc)

    def test_sibling_nan_keeps_original(self) -> None:
        # Valid core fields, bare NaN in a sibling the enrichment never
        # reads: the strict decode rejects the whole answer (RFC 8259 §6).
        exc = _run_probe(
            '{"resources": {"core": {"remaining": 0, "reset": 1700000000},'
            ' "search": {"remaining": NaN}}}'
        )
        self.assertEqual(exc.status, 429)
        self.assert_unenriched(exc)

    def test_nonstandard_whitespace_keeps_original(self) -> None:
        # NBSP + vertical tab are not JSON whitespace; strip() would repair
        # this into decodable text, so the probe must decode unstripped.
        exc = _run_probe(
            "\xa0\x0b"
            '{"resources": {"core": {"remaining": 0, "reset": 1700000000}}}'
            "\xa0\x0b"
        )
        self.assertEqual(exc.status, 429)
        self.assert_unenriched(exc)

    def test_wellformed_trailing_newline_still_enriches(self) -> None:
        # The production shape: gh's stdout ends with a newline, which is
        # legal JSON whitespace — the strict decode must accept it.
        exc = _run_probe(
            '{"resources": {"core": {"remaining": 0, "reset": 1700000000}}}'
            "\n"
        )
        self.assertEqual(exc.rate_limit_remaining, 0)
        self.assertTrue(str(getattr(exc, "rate_limit_reset", "")).endswith("Z"))


if __name__ == "__main__":
    unittest.main()
