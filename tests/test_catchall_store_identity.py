"""Regression checks for PR #98 review P6 (slug
92-93-output-contract-honesty, the AC3 identity class): an unexpected
exception AFTER ``cmd_sync``/``cmd_init`` connected the store and ran
``init_schema`` must still answer the coded runtime envelope carrying the
opened store's db and repo identity (and its real freshness) — the
catch-all reads the per-invocation context, which those two commands never
recorded before this fix (their writable ``connect`` path is not
``_open_for_read``).

The forced seam: ``acquire_sync_lock`` patched to raise RuntimeError —
the lock arm catches only OSError, so the RuntimeError escapes the command
to main's catch-all exactly like any internal fault would, with the store
open and schema-checked.

Legs: sync and init (init's gh preflight faked offline); a control
proves the same seam still answers the coded envelope through the OSError
arm when the lock raises OSError (exit 1, runtime, real freshness — the
earlier preflight-freshness contract).

Hermetic: FakeGitHubSource-seeded previously-synced temp store, patched
seams only, no network, no gh, no writes outside temp roots. Stdlib only;
unittest; Windows + Linux. Failure tags CSI-*.
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
        f"CSI-ENV: zaxbygraph resolved to {zaxbygraph.__file__}, not this"
        f" worktree's src ({_WORKTREE / 'src'}); set"
        f" PYTHONPATH=<worktree>/src{os.pathsep}<worktree>/tests"
    )

from fixtures import REPO  # noqa: E402
from test_mcp import pin_env_for, run_cli  # noqa: E402
from test_output_contract_92_93_e import seed_corpus  # noqa: E402
from zaxbygraph import claims as claims_mod  # noqa: E402
from zaxbygraph import cli as cli_mod  # noqa: E402


class _AuthedGh:
    """init's preflight probe: gh present and authenticated (offline)."""

    def __call__(self, argv):
        if argv[:2] == ["gh", "--version"]:
            return (0, "gh version 2.92.0\n", "")
        return (0, " Logged in to github.com\n", "")


class TestCatchallStoreIdentity(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db = self.root / "history.db"
        seed_corpus(self.db)  # a previously-synced store: sync_state holds REPO
        pin_env_for(self, self.root, db=self.db)
        self.base = ["--repo", REPO, "--db", str(self.db), "--format", "json"]

    def _reference_synced_at(self) -> str:
        code, out, err = run_cli(["status", *self.base])
        self.assertEqual(code, 0, f"CSI: the reference status must succeed: {err[:160]}")
        fresh = (json.loads(out).get("freshness") or {})
        self.assertIsInstance(
            fresh.get("synced_at"), str, "CSI: the reference envelope must carry a real synced_at"
        )
        return fresh["synced_at"]

    def _run_with_lock_raising(self, command: str, exc: BaseException):
        """One command run with acquire_sync_lock patched to raise `exc`."""
        saved_lock = cli_mod.acquire_sync_lock
        saved_gh = claims_mod.run_gh

        def _boom(*a, **kw):
            raise exc

        cli_mod.acquire_sync_lock = _boom
        claims_mod.run_gh = _AuthedGh()
        try:
            return run_cli([command, *self.base])
        finally:
            cli_mod.acquire_sync_lock = saved_lock
            claims_mod.run_gh = saved_gh

    def test_catchall_after_connect_keeps_store_identity(self) -> None:
        synced_at = self._reference_synced_at()
        problems: list[str] = []
        for command in ("sync", "init"):
            code, out, err = self._run_with_lock_raising(
                command, RuntimeError("boom after connect")
            )
            if not out.strip().startswith("{"):
                problems.append(
                    f"{command}: the catch-all must answer one JSON envelope;"
                    f" got exit {code}, stdout {out[:120]!r}, stderr"
                    f" {err.strip()[:120]!r}"
                )
                continue
            env = json.loads(out)
            if code != 1 or env.get("ok") is not False:
                problems.append(
                    f"{command}: exit {code}, ok {env.get('ok')!r} (want the"
                    f" coded runtime envelope, exit 1)"
                )
            if (env.get("error") or {}).get("code") != "runtime":
                problems.append(
                    f"{command}: error.code {(env.get('error') or {}).get('code')!r}, want runtime"
                )
            if "RuntimeError" not in str((env.get("error") or {}).get("message", "")):
                problems.append(f"{command}: the message must name the exception type")
            if env.get("db") != str(self.db):
                problems.append(
                    f"{command}: the catch-all must carry the opened store's db"
                    f" identity {str(self.db)!r}; got {env.get('db')!r}"
                )
            if env.get("repo") != REPO:
                problems.append(
                    f"{command}: the catch-all must carry the resolved repo"
                    f" {REPO!r}; got {env.get('repo')!r}"
                )
            fresh = env.get("freshness") or {}
            if fresh.get("synced_at") != synced_at:
                problems.append(
                    f"{command}: freshness.synced_at {fresh.get('synced_at')!r},"
                    f" want the store's real {synced_at!r}"
                )
        self.assertFalse(
            problems,
            "CSI: an unexpected exception after sync/init connected the store"
            " must keep the catch-all's db/repo identity and real"
            " freshness:\n- " + "\n- ".join(problems),
        )

    def test_control_lock_oserror_stays_the_coded_arm(self) -> None:
        # Control: the SAME seam raising OSError answers through the lock
        # arm (exit 1, runtime, real freshness) — the earlier
        # preflight-freshness contract is unchanged beside the new one.
        synced_at = self._reference_synced_at()
        problems: list[str] = []
        for command in ("sync", "init"):
            code, out, err = self._run_with_lock_raising(
                command, OSError("probe: lock file unwritable")
            )
            env = json.loads(out) if out.strip().startswith("{") else {}
            if code != 1 or env.get("ok") is not False:
                problems.append(f"{command} control: exit {code}, ok {env.get('ok')!r}")
            if (env.get("error") or {}).get("code") != "runtime":
                problems.append(
                    f"{command} control: error.code"
                    f" {(env.get('error') or {}).get('code')!r}, want runtime"
                )
            if (env.get("freshness") or {}).get("synced_at") != synced_at:
                problems.append(
                    f"{command} control: freshness.synced_at"
                    f" {(env.get('freshness') or {}).get('synced_at')!r}, want"
                    f" {synced_at!r}"
                )
        self.assertFalse(
            problems,
            "CSI: the OSError lock arm keeps its coded envelope and real"
            " freshness (PRESERVING):\n- " + "\n- ".join(problems),
        )


if __name__ == "__main__":
    unittest.main()
