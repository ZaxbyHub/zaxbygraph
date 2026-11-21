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


def _gh_process(stdout: bytes | str) -> object:
    proc = mock.Mock()
    proc.returncode = 0
    proc.stdout = stdout
    proc.stderr = b"" if isinstance(stdout, bytes) else ""
    return proc


def _run_probe(stdout: bytes | str) -> GitHubError:
    """Run a 429 through _with_rate_budget with the real _api parsing
    `stdout` (subprocess.run mocked — no network, no gh binary). The mock
    honours the encoding kwarg like the real transport: when _api passes
    encoding=, the stdout is decoded to str (errors="replace"); when it
    does not (the probe path), raw bytes are delivered. The returned mock
    records the kwargs so tests can assert the probe path ran in raw mode."""
    src = GhApiSource("acme", "forgegate", gh_bin="gh")

    def _fake_run(cmd, **kwargs):
        if kwargs.get("encoding") is not None:
            out = stdout
            if isinstance(out, bytes):
                out = out.decode(kwargs["encoding"], errors=kwargs.get("errors"))
            proc = _gh_process(out)
            proc.stderr = kwargs.get("input") and "" or ""
        else:
            proc = _gh_process(stdout)
        return proc

    with mock.patch("zaxbygraph.github.subprocess.run") as run:
        run.side_effect = _fake_run
        original = GitHubError("gh: API rate limit exceeded", status=429)
        result = src._with_rate_budget(original)
        run.result_kwargs = run.call_args.kwargs
        _run_probe.last_call_kwargs = run.call_args.kwargs
        return result


_run_probe.last_call_kwargs = {}


class _EnrichmentAssertions:
    def assert_unenriched(self, exc: GitHubError) -> None:
        self.assertFalse(hasattr(exc, "rate_limit_remaining"))
        self.assertFalse(hasattr(exc, "rate_limit_reset"))


class ProbeTransportDecodeTests(_EnrichmentAssertions, unittest.TestCase):
    """The probe's answer is decoded strictly, on the raw stdout."""

    def assert_probe_ran_raw_bytes(self) -> None:
        # The probe path must call subprocess.run WITHOUT encoding= so the
        # answer arrives as raw bytes (the strict decode then applies).
        # If _api regresses to text mode, this assertion fails even when
        # the payload happens to decode.
        kwargs = _run_probe.last_call_kwargs
        self.assertNotIn("encoding", kwargs)
        self.assertNotIn("errors", kwargs)

    def test_malformed_stdout_keeps_original(self) -> None:
        exc = _run_probe(b"not-json at all")
        self.assertEqual(exc.status, 429)
        self.assertEqual(str(exc), "gh: API rate limit exceeded")
        self.assert_unenriched(exc)
        self.assert_probe_ran_raw_bytes()

    def test_sibling_nan_keeps_original(self) -> None:
        # Valid core fields, bare NaN in a sibling the enrichment never
        # reads: the strict decode rejects the whole answer (RFC 8259 §6).
        exc = _run_probe(
            b'{"resources": {"core": {"remaining": 0, "reset": 1700000000},'
            b' "search": {"remaining": NaN}}}'
        )
        self.assertEqual(exc.status, 429)
        self.assert_unenriched(exc)
        self.assert_probe_ran_raw_bytes()

    def test_nonstandard_whitespace_keeps_original(self) -> None:
        # NBSP + vertical tab are not JSON whitespace; strip() would repair
        # this into decodable text, so the probe must decode unstripped.
        exc = _run_probe(
            "\xa0\x0b"
            '{"resources": {"core": {"remaining": 0, "reset": 1700000000}}}'
            "\xa0\x0b".encode()
        )
        self.assertEqual(exc.status, 429)
        self.assert_unenriched(exc)
        self.assert_probe_ran_raw_bytes()

    def test_deeply_nested_stdout_keeps_original(self) -> None:
        # json.loads raises RecursionError (not a ValueError) on a
        # pathologically nested body; the probe's fail-closed tuple must
        # catch it (issue #86 catch-breadth), so a hostile body cannot
        # escape as an unhandled exception.
        exc = _run_probe(b"[" * 100000)
        self.assertEqual(exc.status, 429)
        self.assert_unenriched(exc)
        self.assert_probe_ran_raw_bytes()

    def test_invalid_utf8_bytes_keep_original(self) -> None:
        # The probe transport reads RAW BYTES and decodes strict UTF-8
        # (issue #86): an invalid sequence inside a quoted value must not
        # be repaired into parseable text by errors="replace".
        exc = _run_probe(
            b'{"resources": {"core": {"remaining": 0, "reset": 1700000000},'
            b' "search": {"name": "caf\xe9"}}}'
        )
        self.assertEqual(exc.status, 429)
        self.assert_unenriched(exc)
        self.assert_probe_ran_raw_bytes()

    def test_wellformed_trailing_newline_still_enriches(self) -> None:
        # The production shape: gh's stdout ends with a newline, which is
        # legal JSON whitespace — the strict decode must accept it.
        exc = _run_probe(
            b'{"resources": {"core": {"remaining": 0, "reset": 1700000000}}}\n'
        )
        self.assertEqual(exc.rate_limit_remaining, 0)
        self.assertEqual(exc.rate_limit_reset, "2023-11-14T22:13:20Z")
        self.assert_probe_ran_raw_bytes()


if __name__ == "__main__":
    unittest.main()
