"""Regression checks for closeout finding NF-1 (slug
92-93-output-contract-honesty, the AC3 freshness class): a ``--max-age``
read whose refresh FAILS must answer its coded ``refresh_failed`` (or
``environment_guard``) envelope carrying the store's REAL freshness — the
store was opened by the staleness probe before the failure fired, and
none of ``_refresh_if_stale``'s _ReadFailure arms carried it before this
fix (``status --max-age 1`` answered the null block).

The fix under test: ``_refresh_if_stale`` records the invocation context
(``_note_open``) the moment its read-only probe opens the store, so every
later _ReadFailure falls back to the captured pre-refresh freshness
through build_envelope. A SUCCESSFUL refresh is still reported
post-refresh (``_open_for_read``'s own open re-records over it), which
the control leg keeps honest by asserting the reference block directly.

Legs (all hermetic — the refresh seam is patched, the sync stamp is
backdated so the budget is always exceeded):

- refresh fault: ``_run_refresh_sync`` patched to raise RuntimeError ->
  exit 1, ``refresh_failed``, real freshness.
- environment guard: ``_run_refresh_sync`` patched to raise
  ``_EnvironmentGuard`` -> exit 2, ``environment_guard``, real freshness.
- held-lock shape: ``acquire_sync_lock`` raising OSError -> exit 1,
  ``refresh_failed``, real freshness.
- control: the plain ``status`` reference carries the backdated
  synced_at with complete true (what every leg must match).

Hermetic: FakeGitHubSource-seeded temp store, patched seams only, no
network, no gh, no writes outside temp roots. Stdlib only; unittest;
Windows + Linux. Failure tags RFF-*.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import zaxbygraph

_WORKTREE = Path(__file__).resolve().parents[1]
if not Path(zaxbygraph.__file__).resolve().is_relative_to(_WORKTREE / "src"):
    raise AssertionError(
        f"RFF-ENV: zaxbygraph resolved to {zaxbygraph.__file__}, not this"
        f" worktree's src ({_WORKTREE / 'src'}); set"
        f" PYTHONPATH=<worktree>/src{os.pathsep}<worktree>/tests"
    )

from fixtures import REPO  # noqa: E402
from test_mcp import pin_env_for, run_cli  # noqa: E402
from test_output_contract_92_93_e import seed_corpus  # noqa: E402
from zaxbygraph import cli as cli_mod  # noqa: E402

#: A stamp far in the past: the budget is always exceeded, and the value
#: is unique enough that a leaked/null block cannot match it.
_BACKDATED = "2020-01-01T00:00:00Z"


def _backdate(db_path: Path) -> None:
    from zaxbygraph.db import connect

    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE sync_state SET last_full_sync_at = ?, last_incr_sync_at = ?,"
            " full_sync_pending = 0, last_error = NULL",
            (_BACKDATED, _BACKDATED),
        )
        conn.commit()
    finally:
        conn.close()


class TestRefreshFailureFreshness(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db = self.root / "history.db"
        seed_corpus(self.db)
        _backdate(self.db)
        pin_env_for(self, self.root, db=self.db)
        self.base = ["--repo", REPO, "--db", str(self.db), "--format", "json"]

    def _reference(self) -> dict:
        code, out, err = run_cli(["status", *self.base])
        self.assertEqual(code, 0, f"RFF: the reference status must succeed: {err[:160]}")
        env = json.loads(out)
        fresh = env.get("freshness") or {}
        self.assertEqual(
            fresh.get("synced_at"), _BACKDATED,
            f"RFF: the backdated reference stamp must be served; got {fresh!r}",
        )
        self.assertIs(fresh.get("complete"), True, "RFF: the reference store must be complete")
        self.assertIsInstance(fresh.get("age_s"), int)
        return fresh

    def _run_with_seams(self, argv, *, refresh_exc=None, lock_exc=None) -> tuple:
        saved_refresh = cli_mod._run_refresh_sync
        saved_lock = cli_mod.acquire_sync_lock

        def _boom_refresh(conn, slug, **kw):
            raise refresh_exc

        def _boom_lock(*a, **kw):
            raise lock_exc

        if refresh_exc is not None:
            cli_mod._run_refresh_sync = _boom_refresh
        if lock_exc is not None:
            cli_mod.acquire_sync_lock = _boom_lock
        try:
            return run_cli([*argv, "--max-age", "1", *self.base])
        finally:
            cli_mod._run_refresh_sync = saved_refresh
            cli_mod.acquire_sync_lock = saved_lock

    def test_refresh_failures_carry_real_freshness(self) -> None:
        reference = self._reference()
        problems: list[str] = []
        legs = [
            (
                ["status"],
                {"refresh_exc": RuntimeError("probe: sync blew up mid-refresh")},
                1, "refresh_failed", "refresh fault arm",
            ),
            (
                ["status"],
                {"refresh_exc": cli_mod._EnvironmentGuard(
                    "probe: database schema version 99 is newer than this build")},
                2, "environment_guard", "environment-guard arm",
            ),
            (
                ["status"],
                {"lock_exc": OSError("probe: lock file unwritable")},
                1, "refresh_failed", "held-lock (OSError) arm",
            ),
            (
                ["item", "1"],
                {"refresh_exc": RuntimeError("probe: sync blew up mid-refresh")},
                1, "refresh_failed", "item read, refresh fault arm",
            ),
        ]
        for argv, seams, want_code, want_error, note in legs:
            code, out, err = self._run_with_seams(argv, **seams)
            if not out.strip().startswith("{"):
                problems.append(
                    f"{note}: must answer one JSON envelope; got exit {code},"
                    f" stdout {out[:120]!r}, stderr {err.strip()[:120]!r}"
                )
                continue
            env = json.loads(out)
            if code != want_code or env.get("ok") is not False:
                problems.append(f"{note}: exit {code}, ok {env.get('ok')!r} (want {want_code})")
            if (env.get("error") or {}).get("code") != want_error:
                problems.append(
                    f"{note}: error.code {(env.get('error') or {}).get('code')!r}, want {want_error!r}"
                )
            fresh = env.get("freshness") or {}
            if fresh.get("synced_at") != reference["synced_at"]:
                problems.append(
                    f"{note}: freshness.synced_at {fresh.get('synced_at')!r}, want"
                    f" the store's real {reference['synced_at']!r}"
                )
            if fresh.get("complete") is not True:
                problems.append(f"{note}: freshness.complete must be true; got {fresh.get('complete')!r}")
            if not isinstance(fresh.get("age_s"), int):
                problems.append(f"{note}: freshness.age_s must be an int; got {fresh.get('age_s')!r}")
        self.assertFalse(
            problems,
            "RFF: a --max-age read whose refresh fails must carry the opened"
            " store's real freshness on its coded refusal:\n- "
            + "\n- ".join(problems),
        )

    def test_missing_store_keeps_the_null_block(self) -> None:
        # PRESERVING control: --max-age on a store that does not exist
        # never opens anything (the probe returns before the open), so the
        # no-corpus refusal keeps the null freshness block.
        code, out, _err = run_cli(
            ["status", "--repo", "acme/nosuch", "--db", str(self.root / "missing.db"),
             "--max-age", "1", "--format", "json"]
        )
        env = json.loads(out)
        self.assertEqual(code, 3, "RFF: the missing-store control stays exit 3")
        fresh = env.get("freshness") or {}
        for key, want in (("synced_at", None), ("age_s", None), ("complete", False)):
            self.assertEqual(
                fresh.get(key), want,
                f"RFF: the missing-store control keeps freshness.{key} = {want!r}",
            )


if __name__ == "__main__":
    unittest.main()
