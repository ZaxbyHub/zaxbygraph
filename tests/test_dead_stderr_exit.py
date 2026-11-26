"""Regression checks for review finding F1 (slug
92-93-output-contract-honesty, the AC1 defect class): a FAILING command
with a DEAD stderr pipe and a LIVE stdout must return the failure's OWN
exit code and still render its ok:false envelope on stdout.

The hole (executed by the reviewer at HEAD 8151cb5): emit_result's
one-line stderr echo sat OUTSIDE the render-boundary try, so a dead
stderr raised BrokenPipeError out of the emitter and the dispatch-level
dead-pipe arm in main() answered a bare 0 with no envelope — an exit-3
leg answered 0. The same shape sat in _runtime_catchall's echo (its try
caught only BrokenPipeError, and catching it killed the stdout render
too; an OSError(EINVAL) echo escaped the catch-all entirely).

The fix these checks pin:

- every stderr echo rides the best-effort _echo_stderr boundary
  (BrokenPipeError and the Windows OSError(EINVAL)/POSIX EPIPE shape are
  absorbed AT the echo site), so the envelope still renders on a live
  stdout and the command returns ITS OWN code;
- the dispatch-level dead-pipe arm returns the dispatched code when one
  is known instead of unconditionally 0.

Legs: exit 3 (no corpus), exit 2 via the sql guard refusal (the
emit_failure echo path) and via an argparse usage error (the
usage-envelope path, which writes no stderr under a machine format — the
control for that arm), the catch-all runtime envelope (exit 1), the
OSError(EINVAL) stderr shape on the exit-3 leg, and successes (json and
text mode — the text one exercises the identity-line echo) staying 0.

Hermetic: FakeGitHubSource-seeded temp store, in-process redirected
stdio, patched seams only; no network, no gh, no writes outside temp
roots. Stdlib only; unittest; Windows + Linux. Failure tags DSE-*.
"""

from __future__ import annotations

import errno
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import zaxbygraph

_WORKTREE = Path(__file__).resolve().parents[1]
if not Path(zaxbygraph.__file__).resolve().is_relative_to(_WORKTREE / "src"):
    raise AssertionError(
        f"DSE-ENV: zaxbygraph resolved to {zaxbygraph.__file__}, not this"
        f" worktree's src ({_WORKTREE / 'src'}); set"
        f" PYTHONPATH=<worktree>/src;<worktree>/tests"
    )

from fixtures import REPO  # noqa: E402
from test_mcp import pin_env_for  # noqa: E402
from test_output_contract_92_93_e import seed_corpus  # noqa: E402
from zaxbygraph import cli as cli_mod  # noqa: E402


class _DeadStderr(io.StringIO):
    """A stderr stream whose writes die (BrokenPipeError, or the Windows
    OSError(EINVAL) dead-pipe shape); a test seam, never a production edit."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    def write(self, text) -> int:
        raise self._exc

    def write_bytes(self, *_a) -> int:  # paranoia: no binary path may bypass
        raise self._exc

    def flush(self) -> None:
        pass


def _run_with_dead_stderr(argv: list[str], exc: Exception):
    """cli.main(argv) with a LIVE stdout buffer and the dead stderr seam.
    Returns (exit-or-None-if-escaped, stdout text, escaped-repr)."""
    out = io.StringIO()
    code: int | None = None
    escaped: str | None = None
    with redirect_stdout(out), redirect_stderr(_DeadStderr(exc)):
        try:
            code = cli_mod.main(list(argv))
        except SystemExit as exit_exc:
            code = exit_exc.code if isinstance(exit_exc.code, int) else 2
        except Exception as exc:  # the dead pipe must never escape main
            escaped = f"{type(exc).__name__}: {exc}"
    return code, out.getvalue(), escaped


class TestDeadStderrExit(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db = self.root / "history.db"
        seed_corpus(self.db)
        pin_env_for(self, self.root, db=self.db)
        self.base = ["--repo", REPO, "--db", str(self.db), "--format", "json"]
        self.missing_db = self.root / "missing.db"

    def _assert_failure_envelope(self, code, out, escaped, want_code, want_error, note, problems):
        if escaped:
            problems.append(f"{note}: the dead stderr escaped main ({escaped})")
            return
        if code != want_code:
            problems.append(
                f"{note}: a dead stderr must leave the failure's OWN exit code"
                f" {want_code}, got {code}"
            )
        if "Traceback" in out:
            problems.append(f"{note}: no traceback text on stdout")
        try:
            env = json.loads(out)
        except ValueError:
            problems.append(f"{note}: stdout must still carry one JSON envelope on the live pipe; got {out[:120]!r}")
            return
        if env.get("ok") is not False:
            problems.append(f"{note}: the envelope must answer ok:false; got ok {env.get('ok')!r}")
        if (env.get("error") or {}).get("code") != want_error:
            problems.append(
                f"{note}: error.code {(env.get('error') or {}).get('code')!r}, want {want_error!r}"
            )
        if env.get("version") != cli_mod.ENVELOPE_VERSION:
            problems.append(f"{note}: the envelope must carry the contract version")

    def test_failing_commands_keep_own_exit_and_envelope_on_dead_stderr(self) -> None:
        problems: list[str] = []
        broken = BrokenPipeError("stderr reader went away")
        einval = OSError(errno.EINVAL, "stderr reader went away")
        # (argv, expected exit, expected error code, dead-stderr shape, note)
        legs = [
            (
                ["item", "1", "--repo", "acme/nosuch", "--db", str(self.missing_db), "--format", "json"],
                3, "no_corpus", broken, "exit-3 no-corpus leg (BrokenPipeError)",
            ),
            (
                ["item", "1", "--repo", "acme/nosuch", "--db", str(self.missing_db), "--format", "json"],
                3, "no_corpus", einval, "exit-3 no-corpus leg (OSError EINVAL)",
            ),
            (
                ["sql", "DELETE FROM items", *self.base],
                2, "bad_sql", broken, "exit-2 guard refusal (emit_failure echo)",
            ),
            (
                ["item", "1", "--definitely-not-a-flag", *self.base],
                2, "bad_request", broken, "exit-2 usage error (usage envelope)",
            ),
        ]
        for argv, want_code, want_error, exc, note in legs:
            code, out, escaped = _run_with_dead_stderr(argv, exc)
            self._assert_failure_envelope(code, out, escaped, want_code, want_error, note, problems)

        # The catch-all runtime envelope (exit 1): the unexpected exception
        # is coded even when stderr is dead; the message still names the
        # type and stdout still carries the envelope.
        def _boom(args) -> None:
            raise RuntimeError("boom under dead stderr")

        saved = cli_mod.cmd_status
        cli_mod.cmd_status = _boom
        try:
            code, out, escaped = _run_with_dead_stderr(["status", *self.base], broken)
            self._assert_failure_envelope(code, out, escaped, 1, "runtime", "catch-all runtime leg (BrokenPipeError)", problems)
            if escaped is None and out.strip().startswith("{"):
                message = str((json.loads(out).get("error") or {}).get("message", ""))
                if "RuntimeError" not in message:
                    problems.append(f"catch-all runtime leg: the message must name the exception type; got {message!r}")
            code, out, escaped = _run_with_dead_stderr(["status", *self.base], einval)
            self._assert_failure_envelope(code, out, escaped, 1, "runtime", "catch-all runtime leg (OSError EINVAL)", problems)
        finally:
            cli_mod.cmd_status = saved

        self.assertFalse(
            problems,
            "DSE: a failing command with a dead stderr and a live stdout must"
            " return its OWN exit code with its ok:false envelope rendered:\n- "
            + "\n- ".join(problems),
        )

    def test_successes_stay_zero_on_dead_stderr(self) -> None:
        problems: list[str] = []
        broken = BrokenPipeError("stderr reader went away")
        # json mode: no stderr write happens; the envelope still answers.
        code, out, escaped = _run_with_dead_stderr(["status", *self.base], broken)
        if escaped or code != 0:
            problems.append(f"json success: exit {code}, escaped {escaped!r}")
        else:
            env = json.loads(out)
            if env.get("ok") is not True:
                problems.append(f"json success: ok {env.get('ok')!r}")
        # text mode: the identity line rides the (dead) stderr AFTER the
        # render — a dead stderr must not cost the caller its answer.
        code, out, escaped = _run_with_dead_stderr(
            ["status", "--repo", REPO, "--db", str(self.db), "--format", "text"], broken
        )
        if escaped or code != 0:
            problems.append(f"text success (identity line on dead stderr): exit {code}, escaped {escaped!r}")
        elif not out.strip():
            problems.append("text success: the text render must still land on the live stdout")
        self.assertFalse(
            problems,
            "DSE: successes with a dead stderr must still exit 0 and answer"
            " on the live stdout:\n- " + "\n- ".join(problems),
        )


if __name__ == "__main__":
    unittest.main()
