"""Regression checks for final-critic finding FC-1 (slug
92-93-output-contract-honesty, the AC1 exit class): with a DEAD STDOUT,
the early-return arms in main() — the usage-error arm and the
invalid-repo-slug arm — used to skip the dispatch block's final-flush
guard, so the interpreter's own exit flush hit the dead pipe as an
unraisable OSError ('Exception ignored ... [Errno 22]') and the process
exited 120 instead of the arm's code 2 (observed at base AND head before
this fix).

These legs run REAL SUBPROCESSES (the unraisable-exit-flush 120 is an
interpreter-shutdown behavior no in-process redirect can reproduce): the
child runs zaxbygraph.cli.main(argv) under `python -c`, the parent closes
the pipe's read end immediately, and the child's answer must still be the
arm's OWN code with a quiet stderr.

Legs:
- usage error (unknown flag beside the subcommand, --format json): exit 2.
- invalid repo slug: exit 2.
- FC-1 sweep extension — the catch-all runtime arm (a command patched to
  raise), which also returned before the flush guard: exit 1.
- controls under the same dead stdout: a no-corpus read keeps exit 3 and
  a successful read keeps exit 0 (the dispatch path's own flush guard).

Hermetic: temp ZAXBYGRAPH_HOME/ZAXBYGRAPH_DB pins, a missing temp db for
the no-corpus leg, no network, no gh, no writes outside temp roots.
Stdlib only; unittest; Windows + Linux. Failure tags DSEA-*.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import zaxbygraph

_WORKTREE = Path(__file__).resolve().parents[1]
if not Path(zaxbygraph.__file__).resolve().is_relative_to(_WORKTREE / "src"):
    raise AssertionError(
        f"DSEA-ENV: zaxbygraph resolved to {zaxbygraph.__file__}, not this"
        f" worktree's src ({_WORKTREE / 'src'}); set"
        f" PYTHONPATH=<worktree>/src{os.pathsep}<worktree>/tests"
    )

_CHILD_TEMPLATE = (
    "import sys;\n"
    "sys.path.insert(0, {src!r});\n"
    "from zaxbygraph import cli;\n"
    "{prelude}"
    "raise SystemExit(cli.main({argv!r}))"
)


class TestDeadStdoutEarlyArms(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.zhome = str(self.root / "zhome")
        self.db = str(self.root / "history.db")

    def _child_dead_stdout(self, argv: list[str], prelude: str = "") -> tuple[int, str]:
        """One subprocess with a dead stdout pipe: (returncode, stderr)."""
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["ZAXBYGRAPH_HOME"] = self.zhome
        env["ZAXBYGRAPH_DB"] = self.db
        pythonpath = str(_WORKTREE / "src")
        env["PYTHONPATH"] = (
            pythonpath + os.pathsep + env["PYTHONPATH"]
            if env.get("PYTHONPATH")
            else pythonpath
        )
        child = _CHILD_TEMPLATE.format(
            src=str(_WORKTREE / "src"), prelude=prelude, argv=list(argv)
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(_WORKTREE),
            env=env,
        )
        return _finish(proc)


    def _assert_quiet_own_code(
        self, code: int, err: str, want: int, note: str, problems: list
    ) -> None:
        if code != want:
            problems.append(
                f"{note}: a dead stdout must leave the arm's OWN exit code"
                f" {want}, got {code} (stderr: {err.strip()[:160]!r})"
            )
        if "Exception ignored" in err:
            problems.append(
                f"{note}: the dead-pipe exit flush must stay quiet (no"
                f" 'Exception ignored' noise); stderr: {err.strip()[-200:]!r}"
            )
        if "Traceback" in err:
            problems.append(
                f"{note}: no traceback on stderr; stderr: {err.strip()[:200]!r}"
            )

    def test_early_arms_keep_own_code_on_dead_stdout(self) -> None:
        problems: list[str] = []
        legs = [
            (
                ["--format", "json", "status", "--no-such-flag"],
                "",
                2,
                "usage-error arm (unknown flag)",
            ),
            (
                ["--repo", "not a slug!!", "status", "--format", "json"],
                "",
                2,
                "invalid-repo-slug arm",
            ),
            (
                ["status", "--format", "json"],
                "cli.cmd_status = lambda a: (_ for _ in ()).throw(RuntimeError('boom dead stdout'));\n",
                1,
                "catch-all runtime arm (FC-1 sweep)",
            ),
            (
                ["item", "1", "--repo", "acme/nosuch", "--db", self.db, "--format", "json"],
                "",
                3,
                "control: no-corpus read (dispatch flush guard)",
            ),
        ]
        for argv, prelude, want, note in legs:
            code, err = self._child_dead_stdout(argv, prelude)
            self._assert_quiet_own_code(code, err, want, note, problems)
        self.assertFalse(
            problems,
            "DSEA: main()'s early-return arms must survive a dead stdout with"
            " their OWN exit code and a quiet shutdown:\n- "
            + "\n- ".join(problems),
        )

    def test_success_keeps_zero_on_dead_stdout(self) -> None:
        # Control: a successful read (the where command resolves a store
        # path but never needs one) stays 0 under the same dead pipe.
        code, err = self._child_dead_stdout(
            ["where", "--repo", "acme/nosuch", "--format", "json"]
        )
        problems: list[str] = []
        self._assert_quiet_own_code(code, err, 0, "control: success (where)", problems)
        self.assertFalse(
            problems,
            "DSEA: a successful command must stay exit 0 on a dead stdout:\n- "
            + "\n- ".join(problems),
        )

    def test_finish_helper_times_out_a_wedged_child(self) -> None:
        # FB-10 (Stage-B probe): a child that hangs must be KILLED and
        # reported within the helper's timeout — never a silent stall and
        # never an orphan. The child writes nothing and sleeps 30 s, so
        # the AssertionError can only come from the bounded wait; a
        # drain-BEFORE-wait ordering would block on the open stderr pipe
        # for the full sleep instead (the mutant demonstration).
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(proc.kill)  # no-op once _finish reaped it
        started = time.monotonic()
        with self.assertRaises(
            AssertionError,
            msg="DSEA: the wedged child must be killed and reported",
        ):
            _finish(proc, timeout=3)
        elapsed = time.monotonic() - started
        self.assertLess(
            elapsed,
            15.0,
            f"DSEA: the wedged-child report must fire fast; took {elapsed:.1f}s",
        )
        self.assertIsNotNone(
            proc.poll(),
            "DSEA: the wedged child must be dead after the report (no orphans)",
        )



def _finish(proc: subprocess.Popen, timeout: float = 120) -> tuple[int, str]:
    """Kill the pipe, WAIT (bounded), then drain: stdout's read end closes
    FIRST (the dead-pipe shape under test), the wait is bounded (a wedged
    child is killed and reported — never a silent 30s stall and never an
    orphan), and stderr is drained only AFTER the wait returns (the
    child's few lines fit the pipe buffer; draining first would block on
    a child that keeps its stderr open, which is exactly the ordering the
    FB-10 probe discriminates)."""
    proc.stdout.close()  # the reader goes away; the child's flush must stay quiet
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=30)
        raise AssertionError("DSEA: the child did not exit (deadlock?)")
    stderr = proc.stderr.read() if proc.stderr else b""
    proc.stderr.close()
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    return proc.returncode, text

if __name__ == "__main__":
    unittest.main()
