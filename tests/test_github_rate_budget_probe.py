"""Frozen checks: GhApiSource._with_rate_budget is an enrichment best-effort
on a 429 — a malformed, hostile, or unrepresentable rate_limit probe answer
MUST keep the original 429 unchanged: same object identity, same message,
same status, and NO enrichment attributes set (not even a partial
`rate_limit_remaining` left behind by a reset conversion that failed after
it). Only a well-formed exhausted budget enriches.

This file pins exactly that contract at the same seam as
tests/test_doctor.py's RateBudgetProbeGuardTests: a bare GhApiSource built
with __new__ whose _api stub returns (or raises) the canned probe answer.
No network, no subprocess.
"""
from __future__ import annotations

import json
import unittest

from zaxbygraph.github import GhApiSource, GitHubError


class RateBudgetProbeHostileTests(unittest.TestCase):
    """Hostile or unrepresentable probe answers keep the original 429."""

    def _source_with_probe(self, probe_body: object) -> GhApiSource:
        src = GhApiSource.__new__(GhApiSource)
        src._api = lambda path, paginate=False, _enrich_rate=True: probe_body  # noqa: E731
        return src

    def _source_with_raising_probe(
        self, probe_error: BaseException
    ) -> GhApiSource:
        src = GhApiSource.__new__(GhApiSource)

        def _raise(
            path: str, paginate: bool = False, _enrich_rate: bool = True
        ) -> object:
            raise probe_error

        src._api = _raise
        return src

    def _original(self) -> GitHubError:
        return GitHubError("gh: API rate limit exceeded", status=429)

    def test_malformed_probe_keeps_original(self) -> None:
        src = self._source_with_raising_probe(
            json.JSONDecodeError("Expecting value", "not-json", 0)
        )
        original = self._original()
        result = src._with_rate_budget(original)
        self.assertIs(result, original)
        self.assertEqual(str(result), "gh: API rate limit exceeded")
        self.assertEqual(result.status, 429)
        self.assertIsNone(getattr(result, "rate_limit_remaining", None))
        self.assertIsNone(getattr(result, "rate_limit_reset", None))

    def test_bool_remaining_keeps_original(self) -> None:
        src = self._source_with_probe(
            {"resources": {"core": {"remaining": False, "reset": 1700000000}}}
        )
        original = self._original()
        result = src._with_rate_budget(original)
        self.assertIs(result, original)
        self.assertEqual(str(result), "gh: API rate limit exceeded")
        self.assertEqual(result.status, 429)
        self.assertIsNone(getattr(result, "rate_limit_remaining", None))
        self.assertIsNone(getattr(result, "rate_limit_reset", None))

    def test_inf_reset_leaves_no_partial_state(self) -> None:
        src = self._source_with_probe(
            {"resources": {"core": {"remaining": 0, "reset": float("inf")}}}
        )
        original = self._original()
        result = src._with_rate_budget(original)
        self.assertIs(result, original)
        self.assertEqual(str(result), "gh: API rate limit exceeded")
        self.assertEqual(result.status, 429)
        self.assertIsNone(getattr(result, "rate_limit_remaining", None))
        self.assertIsNone(getattr(result, "rate_limit_reset", None))

    def test_wellformed_still_enriches(self) -> None:
        src = self._source_with_probe(
            {"resources": {"core": {"remaining": 0, "reset": 1700000000}}}
        )
        original = self._original()
        enriched = src._with_rate_budget(original)
        self.assertIs(enriched, original)
        self.assertEqual(enriched.rate_limit_remaining, 0)
        self.assertIsInstance(enriched.rate_limit_reset, str)
        self.assertTrue(enriched.rate_limit_reset.endswith("Z"))


if __name__ == "__main__":
    unittest.main()
