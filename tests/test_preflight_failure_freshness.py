"""Regression checks for the Phase 4.2 sweep finding (slug
92-93-output-contract-honesty): the cmd_sync / cmd_init PRE-FLIGHT failure
arms closed the store's connection and emitted their failure envelope with
NO freshness, although the store was opened — the AC3 defect class the
frozen checks pin for the read commands but these two write-path commands
never routed through ``_open_for_read``/``_note_open``.

Built from the observational probe
``.agents/issue-traces/92-93-output-contract-honesty/repro/sync-preflight-probe.py``:
seed a PREVIOUSLY-SYNCED hermetic store, force each arm through its
production seam (monkeypatched at the cli module boundary — no repo file is
modified), and assert the failure envelope carries the store's REAL
freshness (synced_at equal to a success envelope's on the same store,
age_s int, complete true).

Legs (exit/error pinned so a leg that fails through some OTHER arm cannot
pose as the arm under test):

- cmd_sync RuntimeError arm (environment guard after connect succeeded,
  e.g. a newer-than-build schema): exit 2, bad_request, real freshness.
- cmd_init RuntimeError arm: exit 2, bad_request, real freshness.
- cmd_sync storage-fault arm (a non-RuntimeError fault from init_schema
  while the connection is open): exit 1, runtime, real freshness.
- cmd_init storage-fault arm: exit 1, runtime, real freshness.
- cmd_sync lock-OSError arm (the sync lock's file could not be created):
  exit 1, runtime, real freshness.
- cmd_init lock-OSError arm: exit 1, runtime, real freshness.
- CONTROL: the sibling lock-JOINED arm in the same function (the
  capture-before-close pattern this fix mirrors): exit 0, joined: true,
  real freshness.
- PRESERVING: a fault BEFORE the store opens (connect itself raises) keeps
  the null block on both commands — the fix must not fabricate freshness
  from a connection that never existed.

Hermetic: FakeGitHubSource-seeded temp store, patched seams only (gh is
faked for init's preflight), no network, no writes outside temp roots.
Stdlib only; unittest; Windows + Linux. Failure tags PFF-*.
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
        f"PFF-ENV: zaxbygraph resolved to {zaxbygraph.__file__}, not this"
        f" worktree's src ({_WORKTREE / 'src'}); set"
        f" PYTHONPATH=<worktree>/src{os.pathsep}<worktree>/tests"
    )

from fixtures import REPO  # noqa: E402
from test_mcp import pin_env_for, run_cli  # noqa: E402
from test_output_contract_92_93_e import seed_corpus  # noqa: E402
from zaxbygraph import claims as claims_mod  # noqa: E402
from zaxbygraph import cli as cli_mod  # noqa: E402

_NULL_BLOCK = {"synced_at": None, "age_s": None, "complete": False}


class _AuthedGh:
    """init's preflight probe: gh present and authenticated (offline)."""

    def __call__(self, argv):
        if argv[:2] == ["gh", "--version"]:
            return (0, "gh version 2.92.0\n", "")
        return (0, " Logged in to github.com\n", "")


class TestPreflightFailureFreshness(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db = self.root / "history.db"
        seed_corpus(self.db)  # a previously-synced store: sync_state holds REPO
        pin_env_for(self, self.root, db=self.db)
        self.base = ["--repo", REPO, "--db", str(self.db), "--format", "json"]

    def _patched(self, obj, name, value, body):
        """Run `body()` with one seam replaced, restoring it IMMEDIATELY
        after (legs in one test must not see each other's seams)."""
        saved = getattr(obj, name)
        setattr(obj, name, value)
        try:
            return body()
        finally:
            setattr(obj, name, saved)

    def _reference_synced_at(self) -> str:
        code, out, err = run_cli(["status", *self.base])
        self.assertEqual(code, 0, f"PFF: the reference status must succeed: {err[:160]}")
        fresh = (json.loads(out).get("freshness") or {})
        self.assertIsInstance(
            fresh.get("synced_at"), str, "PFF: the reference envelope must carry a real synced_at"
        )
        self.assertIs(fresh.get("complete"), True, "PFF: the reference store must be complete")
        return fresh["synced_at"]

    def _assert_real_freshness(self, env: dict, note: str, synced_at: str, problems: list) -> None:
        fresh = env.get("freshness") or {}
        if fresh.get("synced_at") != synced_at:
            problems.append(
                f"{note}: freshness.synced_at {fresh.get('synced_at')!r}, want the"
                f" store's real {synced_at!r}"
            )
        if not isinstance(fresh.get("age_s"), int):
            problems.append(f"{note}: freshness.age_s must be an int, got {fresh.get('age_s')!r}")
        if fresh.get("complete") is not True:
            problems.append(f"{note}: freshness.complete must be true, got {fresh.get('complete')!r}")

    def _run(self, argv):
        code, out, err = run_cli(argv)
        self.assertTrue(
            out.strip().startswith("{"),
            f"PFF: the leg must answer one JSON envelope; got exit {code},"
            f" stdout {out[:120]!r}, stderr {err.strip()[:120]!r}",
        )
        return code, json.loads(out)

    def test_preflight_failure_arms_carry_real_freshness(self) -> None:
        synced_at = self._reference_synced_at()
        problems: list[str] = []

        def leg(note, command, seam, boom, want_code, want_error, gh=False):
            """One forced arm through its production seam."""

            def body():
                if gh:
                    saved_gh = claims_mod.run_gh
                    claims_mod.run_gh = _AuthedGh()
                try:
                    return self._run([command, *self.base])
                finally:
                    if gh:
                        claims_mod.run_gh = saved_gh

            code, env = self._patched(cli_mod, seam, boom, body)
            if code != want_code or env.get("ok") is not False:
                problems.append(
                    f"{note}: exit {code}, ok {env.get('ok')!r} (want exit"
                    f" {want_code} beside ok:false — a different arm answered)"
                )
                return
            if (env.get("error") or {}).get("code") != want_error:
                problems.append(
                    f"{note}: error.code {(env.get('error') or {}).get('code')!r},"
                    f" want {want_error!r}"
                )
            self._assert_real_freshness(env, note, synced_at, problems)

        # The environment-guard arm: connect succeeded, init_schema refused
        # the store (the db.py newer-than-build guard shape).
        def _schema_guard(conn):
            raise RuntimeError(
                "database schema version 99 is newer than this build supports (probe)"
            )

        leg(
            "cmd_sync RuntimeError arm", "sync", "init_schema", _schema_guard,
            2, "bad_request",
        )
        leg(
            "cmd_init RuntimeError arm", "init", "init_schema", _schema_guard,
            2, "bad_request", gh=True,
        )

        # The storage-fault arm: a non-RuntimeError fault while the
        # connection is open.
        def _storage_fault(conn):
            raise OSError("probe: disk I/O error mid-schema")

        leg(
            "cmd_sync storage-fault arm", "sync", "init_schema", _storage_fault,
            1, "runtime",
        )
        leg(
            "cmd_init storage-fault arm", "init", "init_schema", _storage_fault,
            1, "runtime", gh=True,
        )

        # The lock-OSError arm: the sync lock's file could not be created.
        def _lock_fault(*a, **kw):
            raise OSError("probe: lock file unwritable")

        leg(
            "cmd_sync lock-OSError arm", "sync", "acquire_sync_lock", _lock_fault,
            1, "runtime",
        )
        leg(
            "cmd_init lock-OSError arm", "init", "acquire_sync_lock", _lock_fault,
            1, "runtime", gh=True,
        )

        # CONTROL: the sibling lock-JOINED arm — the capture-before-close
        # pattern this fix mirrors (offline: joining reports and returns).
        code, env = self._patched(
            cli_mod, "acquire_sync_lock", lambda *a, **kw: None,
            lambda: self._run(["sync", *self.base]),
        )
        if code != 0 or (env.get("data") or {}).get("joined") is not True:
            problems.append(
                f"control lock-joined arm: exit {code}, joined"
                f" {(env.get('data') or {}).get('joined')!r} (want exit 0, joined true)"
            )
        else:
            self._assert_real_freshness(env, "control lock-joined arm", synced_at, problems)

        self.assertFalse(
            problems,
            "PFF: every cmd_sync/cmd_init pre-flight failure arm emitted"
            " after the store was opened must carry the store's real"
            " freshness (the control lock-joined arm shows the pattern):\n- "
            + "\n- ".join(problems),
        )

    def test_fault_before_open_keeps_null_freshness(self) -> None:
        # PRESERVING: a fault BEFORE the store opens (connect itself
        # raises) keeps the null block on both commands — the capture must
        # not fabricate freshness from a connection that never existed.
        def _connect_fault(db_path):
            raise OSError("probe: cannot open database file")

        for command in ("sync", "init"):
            def body(command=command):
                # init's preflight must pass before connect is reached.
                saved_gh = claims_mod.run_gh
                claims_mod.run_gh = _AuthedGh()
                try:
                    return self._run([command, *self.base])
                finally:
                    claims_mod.run_gh = saved_gh

            code, env = self._patched(cli_mod, "connect", _connect_fault, body)
            self.assertEqual(code, 1, f"PFF: the {command} pre-open fault must exit 1")
            self.assertIs(env.get("ok"), False, f"PFF: the {command} pre-open fault answers ok:false")
            self.assertEqual(
                (env.get("error") or {}).get("code"),
                "runtime",
                f"PFF: the {command} pre-open fault codes runtime",
            )
            fresh = env.get("freshness") or {}
            for key, want in (("synced_at", None), ("age_s", None), ("complete", False)):
                self.assertEqual(
                    fresh.get(key),
                    want,
                    f"PFF: the {command} pre-open fault keeps freshness.{key}"
                    f" = {want!r} (PRESERVING)",
                )


if __name__ == "__main__":
    unittest.main()
