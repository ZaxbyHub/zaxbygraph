"""Regression checks for PR #98 review P1 (slug
92-93-output-contract-honesty): the zero-row-hint pass shares the sql
statement's deadline. An expiry at the pass boundary — or a deadline
interrupt inside its DISTINCT probe — must convert to the coded
SqlTimeoutError exactly as `_vocabulary_warnings` does, never fabricate a
hint whose `stored_values` is `[]` (a lie: the table holds rows) and
never answer ok with hints computed on a dead deadline.

Deterministic by construction (no timing sweep): a controllable clock is
patched over ``time.monotonic`` and flipped at exact pass boundaries, so
the main statement and the vocabulary-warnings pass run on a healthy
clock and the hint pass runs on a dead one:

- leg 1 flips the clock expired right after the hint pass computes its
  scope (the 2nd ``_scope_tables`` call) — the DISTINCT probe is then
  interrupted mid-probe and the interrupt must surface as the timeout;
- leg 2 flips it the moment the vocabulary pass returns — the hint pass
  starts on a dead deadline and must refuse at its entry check;
- the control leg runs the same read on a healthy clock and pins the
  normal hint block (canonical valid_values in production order, the
  stored vocabulary, byte-identical shape).

Hermetic: FakeGitHubSource-seeded 2000-row temp store; patched seams
only; no network, no gh, no writes outside temp roots. Stdlib only;
unittest; Windows + Linux. Failure tags HD-*.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

import zaxbygraph

_WORKTREE = Path(__file__).resolve().parents[1]
if not Path(zaxbygraph.__file__).resolve().is_relative_to(_WORKTREE / "src"):
    raise AssertionError(
        f"HD-ENV: zaxbygraph resolved to {zaxbygraph.__file__}, not this"
        f" worktree's src ({_WORKTREE / 'src'}); set"
        f" PYTHONPATH=<worktree>/src{os.pathsep}<worktree>/tests"
    )

from fixtures import REPO, FakeGitHubSource, issue  # noqa: E402
from zaxbygraph import query as query_mod  # noqa: E402
from zaxbygraph.db import connect, init_schema  # noqa: E402
from zaxbygraph.sync import sync_repo  # noqa: E402

#: The statement every leg runs: zero rows, an out-of-vocabulary state
#: literal, so the hint pass is the only thing left to run.
_STATEMENT = "SELECT number FROM items WHERE state = 'nope'"


class TestHintDeadline(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        conn = connect(Path(cls._td.name) / "bulk.db")
        try:
            init_schema(conn)
            src = FakeGitHubSource()
            for n in range(1, 2001):
                src.add_issue(issue(n, title=f"bulk {n}", body="bulk row", state="open"))
            sync_repo(conn, src, REPO)
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._td.cleanup()

    def setUp(self) -> None:
        self.conn = connect(Path(self._td.name) / "bulk.db")
        self.addCleanup(self.conn.close)

    def _run_under_clock(self, flip_after_vocab: bool):
        """run_sql under a controllable clock.

        flip_after_vocab=False flips the clock expired right after the
        hint pass computes its scope (the DISTINCT probe is then
        interrupted mid-probe); True flips it the moment the
        vocabulary-warnings pass returns (the hint pass starts on a dead
        deadline)."""
        real_monotonic = time.monotonic
        state = {"expired": False}

        def fake_monotonic() -> float:
            return real_monotonic() + 3600.0 if state["expired"] else real_monotonic()

        saved_scope = query_mod._scope_tables
        saved_vocab = query_mod._vocabulary_warnings
        calls = {"n": 0}

        def counting_scope(sql, schema):
            calls["n"] += 1
            out = saved_scope(sql, schema)
            if not flip_after_vocab and calls["n"] >= 2:
                # 1st call is _vocabulary_warnings', 2nd is the hint pass's.
                state["expired"] = True
            return out

        def flipping_vocab(conn, sql, deadline_at, deadline):
            out = saved_vocab(conn, sql, deadline_at, deadline)
            if flip_after_vocab:
                state["expired"] = True
            return out

        query_mod._scope_tables = counting_scope
        query_mod._vocabulary_warnings = flipping_vocab
        time.monotonic = fake_monotonic
        try:
            return query_mod.run_sql(self.conn, _STATEMENT, deadline=60.0)
        finally:
            query_mod._scope_tables = saved_scope
            query_mod._vocabulary_warnings = saved_vocab
            time.monotonic = real_monotonic

    def test_interrupted_distinct_probe_raises_timeout(self) -> None:
        # Leg 1: expiry inside the probe — the interrupt must surface as
        # the coded timeout, never as a fabricated empty stored_values.
        with self.assertRaises(query_mod.SqlTimeoutError):
            self._run_under_clock(flip_after_vocab=False)

    def test_dead_deadline_at_hint_entry_raises_timeout(self) -> None:
        # Leg 2: expiry right after the hint pass's schema read — the
        # entry check must refuse before any hint is computed.
        with self.assertRaises(query_mod.SqlTimeoutError):
            self._run_under_clock(flip_after_vocab=True)

    def test_control_healthy_clock_keeps_normal_hints(self) -> None:
        # Control: the same read on a healthy clock answers the normal
        # annotate-only hint block, byte-identical to the pre-deadline
        # behavior (canonical valid_values in production order).
        result = query_mod.run_sql(self.conn, _STATEMENT, deadline=60.0)
        self.assertEqual(result["rows"], [], "HD: the control read answers zero rows")
        self.assertEqual(
            result.get("hints"),
            [
                {
                    "column": "items.state",
                    "literal": "nope",
                    "stored_values": ["open"],
                    "valid_values": ["open", "closed", "deleted", "transferred"],
                }
            ],
            f"HD: the control hint block must stay unchanged; got {result.get('hints')!r}",
        )

    def test_non_interrupt_probe_error_skips_the_hint(self) -> None:
        # A2 (closeout): a DISTINCT probe failing with a NON-interrupt
        # OperationalError must SKIP that column's hint — a hint whose
        # stored_values is [] would lie (the table holds rows). The seam
        # poisons only the probe identifier quoting for the state column;
        # _store_schema quotes table names, so discovery stays intact.
        saved_quote = query_mod._quote_ident

        def _poisoned(name):
            return "nope(" if name == "state" else saved_quote(name)

        query_mod._quote_ident = _poisoned
        try:
            result = query_mod.run_sql(self.conn, _STATEMENT, deadline=60.0)
        finally:
            query_mod._quote_ident = saved_quote
        self.assertEqual(result["rows"], [], "HD: fixture sanity — zero rows")
        self.assertNotIn(
            "hints",
            result,
            f"HD: a probe that cannot run must skip the hint, never emit"
            f" stored_values == []; got {result.get('hints')!r}",
        )

    def test_interrupt_with_live_deadline_skips_the_hint(self) -> None:
        # A2 (closeout): an interrupt-shaped OperationalError raised while
        # the deadline is STILL LIVE (someone else interrupted us) is a
        # probe failure, not a timeout: the hint is skipped and the read
        # answers normally — no raise, no fabricated empty stored_values.
        import sqlite3

        class _InterruptingConn:
            """Delegates everything; the DISTINCT probe reports an
            interrupt from elsewhere while our clock stays healthy."""

            def __init__(self, inner) -> None:
                self._inner = inner

            def execute(self, sql, *args):
                if " ".join(sql.split()).upper().startswith("SELECT DISTINCT"):
                    raise sqlite3.OperationalError("interrupted")
                return self._inner.execute(sql, *args)

            def set_progress_handler(self, *args):
                return self._inner.set_progress_handler(*args)

            def cursor(self):
                return self._inner.cursor()

        result = query_mod.run_sql(
            _InterruptingConn(self.conn), _STATEMENT, deadline=60.0
        )
        self.assertEqual(result["rows"], [], "HD: fixture sanity — zero rows")
        self.assertNotIn(
            "hints",
            result,
            f"HD: a live-deadline interrupt must skip the hint, never emit"
            f" stored_values == []; got {result.get('hints')!r}",
        )


if __name__ == "__main__":
    unittest.main()
