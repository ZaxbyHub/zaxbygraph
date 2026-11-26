"""Acceptance checks for issue-trace 92-93-output-contract-honesty (issues
#92+#93, decisions D1-D3: the CLI exit/envelope conformance contract),
amended after plan-critic rounds 1-3. D4 lives in _d.py; D5/D6/D7/D9 in
_b.py; D8 in _c.py; shared infrastructure in _e.py/_f.py (same stem,
tests/).

D1 (AC1): a dead stdout or dead flush must NOT flatten the exit code. A
matrix over EVERY failure-emitter class (cli._write_stdout raising
BrokenPipeError; the usage envelope writes through the same boundary):
item ghost -> 1; usage error -> 2; sql bad_sql -> 2 and no_such_table -> 1;
a snapshot pull with an unreachable source (a nonexistent local path) -> 1
(source_unreachable); a no-corpus read -> 3; a dead-gh claim failure -> 6.
Each leg first proves its argv on a LIVE stdout (own exit + error code),
then under the dead pipe; ROUND 3: the dead-gh seam is installed BEFORE
the live precondition and kept through the dead-pipe leg, so no leg ever
invokes a real gh. FLUSH legs: a failing command whose render succeeds but
whose sys.stdout.flush() raises BrokenPipeError / OSError(EINVAL) -> main
returns 1; a successful command (where) -> 0 (wrapper stream, never
production edits). ROUND 3: a SUCCESSFUL command whose stdout WRITE dies
(the _write_stdout seam) still returns 0 — an exit-7 mutant must fail.
Plus the conformance sweep: exit==0 iff ok:true (claim outcomes 4/5/6 keep
their documented exits beside ok:false).

D2 (AC2): an unexpected exception inside a command (cmd_status patched to
raise RuntimeError; also a plain ValueError reaching main) must yield a
nonzero exit, a parseable ok:false envelope on stdout with error.code
"runtime" naming the exception type, NO 'Traceback' on stdout or stderr --
in a subprocess AND in-process.

D3 (AC3): a CLI failure after the store opened carries the store's real
freshness (synced_at equal to a success envelope's on the same store,
age_s int, complete true) -- including the close-before-emit arms (item
ghost, invalid --as-of, export-corpus refusals that close the connection
first, the schema no-such-table arm) AND an unexpected RuntimeError raised
AFTER the store opened: main's catch-all must report the store's real
freshness AND db/repo identity. ROUND 3: a post-open failure that resolves
NO repo filter (export-corpus --repo '') reports the SINGLE stored repo's
real freshness; zero- or multi-repo stores keep the null block. Reopen
rejection: with any SECOND open patched to raise, the failing read opens
the store exactly once and still reports the ORIGINAL freshness -- ROUND 3
adds the dedicated final assertion so a null-freshness-on-reopen mutant
FAILS. Pre-open failures (no corpus with a missing db -> 3, invalid slug
-> 2, repo/db mismatch -> 2; overrides ride AFTER the common flags) keep
the null block (PRESERVING).

Hermetic: FakeGitHubSource, tempfile stores, patched seams; no network, no
gh, no writes outside temp roots. Stdlib only; unittest. Every failure
message carries a distinctive single-line tag (OC-AC1..OC-AC3).
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import zaxbygraph

_WORKTREE = Path(__file__).resolve().parents[1]
if not Path(zaxbygraph.__file__).resolve().is_relative_to(_WORKTREE / "src"):
    raise AssertionError(
        f"OC-ENV: zaxbygraph resolved to {zaxbygraph.__file__}, not this"
        f" worktree's src ({_WORKTREE / 'src'}); set"
        f" PYTHONPATH=<worktree>/src;<worktree>/tests"
    )

from fixtures import REPO  # noqa: E402
from test_mcp import run_cli  # noqa: E402
from test_output_contract_92_93_e import (  # noqa: E402
    DEAD_PIPE_FAILURES, DeadGh, StoreCase, dead_flush_and_success_render_problems,
    parse_envelope, resolve_leg_arg, run_with_dead_stdout, seed_multi_repo_store,
    seed_zero_repo_store,
)
from zaxbygraph import cli as cli_mod  # noqa: E402


class TestAc1ExitContract(StoreCase):
    """D1 (AC1): exits agree with the envelope's ok, even on a dead stdout,
    for every failure-emitter class."""

    def _resolve_leg_argv(self, command: str, pre: list[str], post: list[str]) -> list[str]:
        sub = {"__SNAP__": str(self.root / "snapshot-src-not-a-repo"), "__MISSING__": str(self.root / "missing.db")}
        return [command, *[resolve_leg_arg(p, sub) for p in pre], *self.base,
                *[resolve_leg_arg(p, sub) for p in post]]

    def test_ac1_failure_dead_pipe_exits_nonzero(self) -> None:
        problems: list[str] = []
        for label, command, pre, post, want_code, want_error, needs_dead_gh in DEAD_PIPE_FAILURES:
            argv = self._resolve_leg_argv(command, pre, post)
            # ROUND 3 (finding 4a): the dead-gh seam is installed BEFORE the
            # LIVE-stdout precondition and kept through the dead-pipe leg, so
            # a leg that needs the seam never invokes a real gh on either
            # leg. The live precondition itself still proves the argv really
            # answers the documented failure (own exit + error code) before
            # the dead-pipe injection is trusted (critic round 2, finding 1).
            saved_gh = None
            if needs_dead_gh:
                from zaxbygraph import claims as claims_mod

                saved_gh = claims_mod.run_gh
                claims_mod.run_gh = DeadGh()
            try:
                live_code, live_out, _live_err = run_cli(argv)
                live_env = parse_envelope(self, live_out, "OC-AC1") if live_out.strip() else {}
                if live_code != want_code or live_env.get("ok") is not False or (live_env.get("error") or {}).get("code") != want_error:
                    problems.append(
                        f"{label}: the leg's own argv must answer exit {want_code} error"
                        f" {want_error} on a LIVE stdout first (with the dead-gh"
                        f" seam installed); got exit {live_code},"
                        f" error {(live_env.get('error') or {}).get('code')!r}"
                    )
                    continue
                code, err, escaped = run_with_dead_stdout(argv)
            finally:
                if saved_gh is not None:
                    from zaxbygraph import claims as claims_mod

                    claims_mod.run_gh = saved_gh
            if escaped:
                problems.append(f"{label}: BrokenPipeError escaped main ({escaped})")
            elif code != want_code:
                problems.append(f"{label}: a dead stdout must leave the failure's OWN exit code {want_code}, got {code}")
            if "Traceback" in err:
                problems.append(f"{label}: the dead-pipe exit must stay quiet (no traceback)")
        # FLUSH legs plus the ROUND-3 successful dead-RENDER leg (finding
        # 4b): the render SUCCEEDS into a stream whose flush() raises --
        # main returns the command's OWN code for a failure (1) and 0 for a
        # success; and a successful command whose stdout WRITE dies still
        # returns 0 (never a synthetic nonzero code).
        problems += dead_flush_and_success_render_problems(self.base)
        self.assertFalse(
            problems,
            "OC-AC1: every failure-emitter class must return its OWN exit"
            " code on a dead stdout or dead flush (successes still 0, all"
            " quiet), each leg's argv must answer its documented failure"
            " on a live stdout first, and no leg may invoke a real gh:\n- "
            + "\n- ".join(problems),
        )

    def test_ac1_exit_agrees_with_ok_sweep(self) -> None:
        # Conformance sweep: for a representative matrix of success and
        # failure invocations, exit==0 iff ok:true. Claim outcomes 4/5/6
        # are the documented exception: a nonzero outcome keeps its OWN
        # exit code (6 for github_unreachable) beside ok:false.
        matrix: list[tuple[str, list[str], str]] = [
            ("success", ["status"], "plain read"),
            ("success", ["item", "1"], "stored number"),
            ("success", ["search", "needle"], "fts hit"),
            ("success", ["sql", "SELECT number FROM items ORDER BY number"], "read sql"),
            ("success", ["open"], "bare list"),
            ("success", ["churn"], "derived read"),
            ("failure", ["item", "99999"], "ghost number -> not_found"),
            ("failure", ["sql", "SELECT pr_number FROM pr_files"], "no_such_column"),
            ("failure", ["sql", "DELETE FROM items"], "guard refusal -> bad_sql"),
            ("failure", ["file-history", "src/nothing-here.py"], "unknown_path"),
            ("failure", ["schema", "--output", "no-such-command"], "bad_request"),
            ("failure", ["release", "1"], "release without --token -> bad_request"),
            ("failure", ["item", "1", "--max-body-chars", "-1"], "usage -> bad_request"),
        ]
        problems: list[str] = []
        for kind, argv, note in matrix:
            code, out, err = run_cli([*argv, *self.base])
            env = parse_envelope(self, out, "OC-AC1") if out.strip() else {}
            ok = env.get("ok")
            if kind == "success":
                if code != 0 or ok is not True:
                    problems.append(f"{argv[0]} ({note}): exit {code}, ok {ok!r}")
            else:
                if code == 0 or ok is not False:
                    problems.append(f"{argv[0]} ({note}): exit {code}, ok {ok!r}")
        # The claim outcome exception: the dead-gh failure keeps exit 6
        # beside ok:false (documented outcomes 4/5/6).
        from zaxbygraph import claims as claims_mod

        saved_gh = claims_mod.run_gh
        claims_mod.run_gh = DeadGh()
        try:
            code6, out6, _err6 = run_cli(["claim", "1", "--settle-seconds", "0.05", *self.base])
        finally:
            claims_mod.run_gh = saved_gh
        env6 = parse_envelope(self, out6, "OC-AC1")
        if not (code6 == 6 and env6.get("ok") is False):
            problems.append(
                f"claim dead-gh: exit {code6}, ok {env6.get('ok')!r} (want the"
                " documented claim outcome exit 6 beside ok:false)"
            )
        self.assertFalse(
            problems,
            "OC-AC1: exit must agree with the envelope's ok across the"
            " representative matrix (claim outcomes 4/5/6 keep their own"
            " documented exits):\n- " + "\n- ".join(problems),
        )


class TestAc2RuntimeEnvelope(StoreCase):
    """D2 (AC2): an unexpected command exception is a coded envelope, never
    a traceback."""

    def _run_raising(self, exc: BaseException) -> tuple[int | None, str, str, str | None]:
        """main() with cmd_status patched to raise `exc`. Returns
        (exit-or-None-if-escaped, stdout, stderr, escaped-repr)."""
        saved = cli_mod.cmd_status

        def _boom(args) -> None:
            raise exc

        cli_mod.cmd_status = _boom
        self.addCleanup(setattr, cli_mod, "cmd_status", saved)
        out, err = io.StringIO(), io.StringIO()
        escaped: str | None = None
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli_mod.main(["status", *self.base])
        except SystemExit as exc_exit:
            code = exc_exit.code if isinstance(exc_exit.code, int) else 2
        except Exception as escaped_exc:
            code = None
            escaped = f"{type(escaped_exc).__name__}: {escaped_exc}"
        return code, out.getvalue(), err.getvalue(), escaped

    def _assert_runtime_envelope(
        self, code: int | None, out: str, err: str, escaped: str | None, type_name: str
    ) -> None:
        self.assertIsNone(
            escaped,
            f"OC-AC2: the unexpected {type_name} escaped main() uncaught"
            f" (must be contained as a coded envelope); escaped as {escaped}",
        )
        self.assertIsInstance(code, int, "OC-AC2: main() must return an exit code")
        self.assertNotEqual(code, 0, f"OC-AC2: the {type_name} failure must exit nonzero")
        env = parse_envelope(self, out, "OC-AC2")
        self.assertIs(env.get("ok"), False, f"OC-AC2: the {type_name} envelope must answer ok:false")
        error = env.get("error")
        self.assertIsInstance(
            error, dict, f"OC-AC2: the {type_name} envelope must carry an error object"
        )
        self.assertEqual(
            error.get("code"),
            "runtime",
            f"OC-AC2: the unexpected {type_name} must be coded 'runtime', got {error.get('code')!r}",
        )
        message = str(error.get("message", ""))
        self.assertIn(
            type_name,
            message,
            f"OC-AC2: the runtime message must name the exception type"
            f" {type_name}; got {message!r}",
        )
        self.assertNotIn(
            "Traceback", out, f"OC-AC2: no traceback text on stdout for the {type_name} envelope"
        )
        self.assertNotIn(
            "Traceback", err, f"OC-AC2: no traceback text on stderr for the {type_name} envelope"
        )

    def test_ac2_unexpected_exception_is_coded_envelope(self) -> None:
        # In-process: RuntimeError inside a command.
        self._assert_runtime_envelope(
            *self._run_raising(RuntimeError("boom")), type_name="RuntimeError"
        )
        # In-process: a plain ValueError reaching main() gets the same
        # containment (message names ValueError).
        self._assert_runtime_envelope(
            *self._run_raising(ValueError("bad value")), type_name="ValueError"
        )
        # Subprocess: the same RuntimeError escapes no longer; the child
        # answers the coded envelope on stdout with no traceback anywhere.
        src_dir = str(_WORKTREE / "src")
        tests_dir = str(_WORKTREE / "tests")
        child = (
            "import sys;"
            "sys.path.insert(0, r'" + src_dir + "');"
            "sys.path.insert(0, r'" + tests_dir + "');"
            "from zaxbygraph import cli;"
            "cli.cmd_status = lambda args: (_ for _ in ()).throw(RuntimeError('boom child'));"
            "raise SystemExit(cli.main(['status', '--repo', %r, '--db', %r,"
            " '--format', 'json']))"
        ) % (REPO, str(self.db))
        env_child = dict(os.environ)
        env_child["ZAXBYGRAPH_HOME"] = str(self.root / "zhome")
        env_child["ZAXBYGRAPH_DB"] = str(self.db)
        proc = subprocess.run(
            [sys.executable, "-c", child],
            capture_output=True,
            text=True,
            cwd=str(_WORKTREE),
            env=env_child,
            timeout=120,
            check=False,
        )
        self.assertNotEqual(
            proc.returncode, 0, "OC-AC2: the subprocess fault must exit nonzero"
        )
        env = parse_envelope(self, proc.stdout, "OC-AC2")
        self.assertIs(env.get("ok"), False, "OC-AC2: the subprocess envelope must answer ok:false")
        error = env.get("error") or {}
        self.assertEqual(
            error.get("code"),
            "runtime",
            f"OC-AC2: the subprocess fault must be coded 'runtime', got {error.get('code')!r}",
        )
        self.assertIn(
            "RuntimeError",
            str(error.get("message", "")),
            "OC-AC2: the subprocess runtime message must name the exception type",
        )
        self.assertNotIn("Traceback", proc.stdout, "OC-AC2: no traceback text on subprocess stdout")
        self.assertNotIn("Traceback", proc.stderr, "OC-AC2: no traceback text on subprocess stderr")


class TestAc3CliFailureFreshness(StoreCase):
    """D3 (AC3): a CLI failure emitted after the store opened carries the
    store's real freshness -- including the close-before-emit arms and the
    catch-all; pre-open failures keep the null block."""

    def _reference_synced_at(self) -> str:
        code, out, err = run_cli(["status", *self.base])
        self.assertEqual(code, 0, f"OC-AC3: the reference status must succeed: {err[:160]}")
        env = parse_envelope(self, out, "OC-AC3")
        fresh = env.get("freshness") or {}
        synced_at = fresh.get("synced_at")
        self.assertIsInstance(
            synced_at, str, "OC-AC3: the reference envelope must carry a real synced_at"
        )
        self.assertIs(fresh.get("complete"), True, "OC-AC3: the reference store must be complete")
        return synced_at

    def _check_real_freshness(self, env: dict, note: str, synced_at: str, problems: list) -> None:
        fresh = env.get("freshness") or {}
        if fresh.get("synced_at") != synced_at:
            problems.append(
                f"{note}: freshness.synced_at {fresh.get('synced_at')!r}, want the store's real {synced_at!r}"
            )
        if not isinstance(fresh.get("age_s"), int):
            problems.append(f"{note}: freshness.age_s must be an int, got {fresh.get('age_s')!r}")
        if fresh.get("complete") is not True:
            problems.append(f"{note}: freshness.complete must be true, got {fresh.get('complete')!r}")

    def test_ac3_cli_failure_freshness(self) -> None:
        synced_at = self._reference_synced_at()
        # (pre-common args, post-common args, expected error code, note) --
        # every one fails AFTER the store was opened; the export-corpus arms
        # close the connection BEFORE emitting (the empty-repo refusal's
        # --repo override rides AFTER the common flags, argparse last-wins),
        # the schema arm emits from the no-such-table arm. ROUND 3 (finding
        # 2a): the --repo '' leg resolves no repo filter, so its freshness
        # comes from the store's SINGLE stored repo (this store holds
        # exactly one) -- the expectation stays REAL freshness.
        legs = [
            (["item", "99999"], [], "not_found", "ghost number (close-before-emit)"),
            (["item", "1", "--as-of", "not-a-timestamp"], [], "bad_as_of", "early close-before-emit arm"),
            (["export-corpus"], ["--repo", ""], "repo_required", "export-corpus closes conn first (no repo filter -> single stored repo)"),
            (["export-corpus", "--include", "bogus"], [], "bad_include", "export-corpus closes conn first"),
            (["schema", "nope_table"], [], "not_found", "schema no-such-table arm"),
            (["file-history", "src/nothing-here.py"], [], "unknown_path", "unknown path"),
            (["overlap", "99999", "3"], [], "not_found", "ghost overlap number"),
            (["file-history", "src/store.py", "--limit", "99999999999"], [], "bad_request", "bad limit"),
        ]
        problems: list[str] = []
        for pre, post, want_code, note in legs:
            code, out, err = run_cli([*pre, *self.base, *post])
            env = parse_envelope(self, out, "OC-AC3")
            error = env.get("error") or {}
            if code == 0 or env.get("ok") is not False:
                problems.append(f"{note}: exit {code}, ok {env.get('ok')!r}")
                continue
            if error.get("code") != want_code:
                problems.append(f"{note}: error.code {error.get('code')!r}, want {want_code}")
            self._check_real_freshness(env, note, synced_at, problems)
        # ROUND 3 (finding 2a, the zero/multi sub-legs): with ZERO or
        # SEVERAL stored repos a no-filter post-open failure cannot pick
        # one repo, so the null block is the honest answer.
        zero_db = self.root / "zero-repo.db"
        seed_zero_repo_store(zero_db)
        multi_db = self.root / "multi-repo.db"
        seed_multi_repo_store(multi_db)
        for db_path, note in ((zero_db, "zero-repo store"), (multi_db, "multi-repo store")):
            code, out, err = run_cli(
                ["export-corpus", "--db", str(db_path), "--format", "json", "--repo", ""]
            )
            env2 = parse_envelope(self, out, "OC-AC3")
            error2 = (env2.get("error") or {}).get("code")
            self.assertEqual(code, 2, f"OC-AC3: the {note} --repo '' refusal must exit 2; got {code}")
            self.assertEqual(error2, "repo_required", f"OC-AC3: the {note} --repo '' refusal codes repo_required; got {error2!r}")
            fresh2 = env2.get("freshness") or {}
            for key, want in (("synced_at", None), ("age_s", None), ("complete", False)):
                self.assertEqual(
                    fresh2.get(key), want,
                    f"OC-AC3: the {note} --repo '' refusal keeps freshness.{key}"
                    f" = {want!r} (no single stored repo to report)",
                )
        # An unexpected RuntimeError AFTER the store opened: the catch-all
        # must report the store's real freshness AND db/repo identity.
        def _boom(conn, *a, **kw):
            raise RuntimeError("query exploded after open")

        saved_status, cli_mod.status = cli_mod.status, _boom
        escaped = None
        try:
            code, out, err = run_cli(["status", *self.base])
        except Exception as exc:  # base: the exception escapes main uncaught
            escaped = f"{type(exc).__name__}: {exc}"
            code, out, err = None, "", ""
        finally:
            cli_mod.status = saved_status
        env = parse_envelope(self, out, "OC-AC3") if out.strip() else {}
        if escaped or not out.strip() or env.get("ok") is not False or code == 0:
            problems.append(
                "runtime after open: the unexpected exception must answer a"
                f" coded ok:false envelope carrying the store's identity"
                f" (escaped: {escaped!r}, exit {code}, stdout {out[:80]!r})"
            )
        else:
            error = env.get("error") or {}
            if error.get("code") != "runtime":
                problems.append(f"runtime after open: error.code {error.get('code')!r}, want runtime")
            if "RuntimeError" not in str(error.get("message", "")):
                problems.append("runtime after open: the message must name the exception type")
            if env.get("db") != str(self.db):
                problems.append(f"runtime after open: db identity lost ({env.get('db')!r})")
            if env.get("repo") != REPO:
                problems.append(f"runtime after open: repo identity lost ({env.get('repo')!r})")
            self._check_real_freshness(env, "runtime after open", synced_at, problems)
        self.assertFalse(
            problems,
            "OC-AC3: post-open CLI failures (and the catch-all) must carry"
            " the store's real freshness and identity:\n- " + "\n- ".join(problems),
        )
        # PRESERVING: pre-open failures keep the null block. Each override
        # rides AFTER the common flags (argparse last-wins) so the refusal
        # is actually reached; the executed codes were verified at base
        # (missing-db no-corpus -> 3, invalid slug -> 2, mismatch -> 2).
        for post, want_code, note in (
            (["--repo", "acme/nosuch", "--db", str(self.root / "missing.db")], 3, "no corpus (missing db)"),
            (["--repo", "NOT A SLUG"], 2, "bad repo slug"),
            (["--repo", "acme/nosuch"], 2, "repo/--db mismatch"),
        ):
            code, out, err = run_cli(["item", "1", *self.base, *post])
            env = parse_envelope(self, out, "OC-AC3")
            self.assertEqual(
                code, want_code,
                f"OC-AC3: the {note} failure must exit {want_code} (executed at"
                f" base); got {code}: {(env.get('error') or {}).get('message', err)[:120]!r}",
            )
            self.assertIs(env.get("ok"), False, f"OC-AC3: the {note} failure must answer ok:false")
            fresh = env.get("freshness") or {}
            for key, want in (("synced_at", None), ("age_s", None), ("complete", False)):
                self.assertEqual(
                    fresh.get(key), want,
                    f"OC-AC3: the {note} failure keeps freshness.{key} = {want!r} (PRESERVING)",
                )

        # Reopen rejection: after the command's FIRST successful open, any
        # SECOND open of the store raises -- the failing read must open the
        # store exactly once and still report the ORIGINAL freshness values
        # captured at that open (critic round 2, finding 2).
        real_open = cli_mod.open_existing
        calls = {"n": 0}

        def _counting_open(path):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("second open refused")
            return real_open(path)

        cli_mod.open_existing = _counting_open
        try:
            code, out, err = run_cli(["item", "99999", *self.base])
        finally:
            cli_mod.open_existing = real_open
        env = parse_envelope(self, out, "OC-AC3") if out.strip() else {}
        self.assertEqual(
            calls["n"], 1,
            f"OC-AC3: a failing read must open the store exactly once (no reopen"
            f" to stamp freshness); got {calls['n']} opens",
        )
        self.assertIs(env.get("ok"), False, "OC-AC3: the reopen-rejection leg must still fail")
        # ROUND 3 (finding 2b): the reopen arm's freshness is DEDICATEDLY
        # asserted (the wave-2 problems list was already emptied above), so
        # a null-freshness-on-reopen mutant FAILS here.
        fresh = env.get("freshness") or {}
        self.assertEqual(
            fresh.get("synced_at"), synced_at,
            f"OC-AC3: reopen rejection: freshness.synced_at must stay the value"
            f" captured at the ORIGINAL open {synced_at!r}; got"
            f" {fresh.get('synced_at')!r} (a null-freshness-on-reopen mutant"
            " must fail here)",
        )
        self.assertIs(fresh.get("complete"), True, "OC-AC3: reopen rejection: freshness.complete must stay true")
        self.assertIsInstance(fresh.get("age_s"), int, "OC-AC3: reopen rejection: freshness.age_s must stay an int")


if __name__ == "__main__":
    unittest.main()
