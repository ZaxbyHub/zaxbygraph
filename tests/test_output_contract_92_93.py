"""Acceptance checks for issue-trace 92-93-output-contract-honesty (issues
#92+#93, decisions D1-D3: the CLI exit/envelope conformance contract),
amended through plan-critic rounds 1-3 and review wave 5. D4 lives in
_d.py; D5/D6/D7/D9 in _b.py; D8 in _c.py; shared infrastructure in
_e.py/_f.py (same stem, tests/).

D1 (AC1): a dead stdout or dead flush must NOT flatten the exit code. The
dead-pipe matrix covers every failure-emitter class (item ghost -> 1,
usage -> 2, sql bad_sql -> 2 / no_such_table -> 1, snapshot unreachable ->
1, no-corpus -> 3, dead-gh claim -> 6); each leg is proven on a LIVE
stdout first, under the dead-gh seam installed BEFORE it (round 3); flush
failures return the command's OWN code; a successful command whose WRITE
dies still exits 0. Plus the exit==0-iff-ok:true conformance sweep (claim
outcomes 4/5/6 keep their documented exits).

D2 (AC2): an unexpected exception inside a command must yield a nonzero
exit, a parseable ok:false envelope with error.code "runtime" naming the
exception type, and NO traceback — in a subprocess AND in-process.

D3 (AC3): a CLI failure after the store opened carries the store's real
freshness (synced_at as the success envelope's on the same store, age_s
int, complete true) — including the close-before-emit arms AND the
catch-all after an unexpected RuntimeError (db/repo identity too). A
no-filter post-open failure reports the SINGLE stored repo's freshness
(zero/multi-repo: null block; round-5 helpers in _f). Reopen rejection:
exactly one open, the ORIGINAL freshness (dedicated final assertions).
ROUND 5 (review F-3/F-4): mixed-case sync_state refusals (open/sql/status,
exit 2 bad_request) carry the store's real freshness, and the catch-all
after a --max-age refresh merges freshness.refreshed = true like
emit_result. Pre-open failures keep the null block (PRESERVING).

Hermetic: FakeGitHubSource, tempfile stores, patched seams; no network.
Failures carry OC-AC1..OC-AC3 tags.
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
        f" PYTHONPATH=<worktree>/src{os.pathsep}<worktree>/tests"
    )

from fixtures import REPO  # noqa: E402
from test_mcp import run_cli  # noqa: E402
from test_output_contract_92_93_e import (  # noqa: E402
    DEAD_PIPE_FAILURES, DeadGh, StoreCase, dead_flush_and_success_render_problems,
    parse_envelope, preopen_null_freshness_problems,
    repo_filter_freshness_problems, resolve_leg_arg, run_with_dead_stdout,
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
        self.assertIsNone(escaped, f"OC-AC2: the unexpected {type_name} escaped main() uncaught (must be contained as a coded envelope); escaped as {escaped}")
        self.assertIsInstance(code, int, "OC-AC2: main() must return an exit code")
        self.assertNotEqual(code, 0, f"OC-AC2: the {type_name} failure must exit nonzero")
        env = parse_envelope(self, out, "OC-AC2")
        self.assertIs(env.get("ok"), False, f"OC-AC2: the {type_name} envelope must answer ok:false")
        error = env.get("error")
        self.assertIsInstance(error, dict, f"OC-AC2: the {type_name} envelope must carry an error object")
        self.assertEqual(error.get("code"), "runtime", f"OC-AC2: the unexpected {type_name} must be coded 'runtime', got {error.get('code')!r}")
        message = str(error.get("message", ""))
        self.assertIn(type_name, message, f"OC-AC2: the runtime message must name the exception type {type_name}; got {message!r}")
        self.assertNotIn("Traceback", out, f"OC-AC2: no traceback text on stdout for the {type_name} envelope")
        self.assertNotIn("Traceback", err, f"OC-AC2: no traceback text on stderr for the {type_name} envelope")

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
        # ROUND 3 (finding 2a, the zero/multi sub-legs; round 5: displaced
        # into the _f helper at the line cap): with ZERO or SEVERAL stored
        # repos a no-filter post-open failure cannot pick one repo, so the
        # null block is the honest answer.
        problems = repo_filter_freshness_problems(self, str(self.root))
        self.assertFalse(
            problems,
            "OC-AC3: zero/multi-repo --repo '' refusals keep the null block:\n- "
            + "\n- ".join(problems),
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
        # PRESERVING: pre-open failures keep the null block (round 5:
        # displaced into the _f helper at the line cap; the executed codes
        # were verified at base: missing-db no-corpus -> 3, invalid slug
        # -> 2, mismatch -> 2).
        problems = preopen_null_freshness_problems(self)
        self.assertFalse(
            problems,
            "OC-AC3: pre-open failures keep the null freshness block:\n- "
            + "\n- ".join(problems),
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

    def test_ac3_mixed_case_and_refresh_catchall_freshness(self) -> None:
        synced_at = self._reference_synced_at()
        # ROUND 5 (review F-4, run FIRST so the store is not yet re-keyed):
        # a --max-age read whose refresh RAN (the _refresh_if_stale seam
        # answers True) and whose query then raises: the catch-all must
        # merge freshness.refreshed = true like emit_result.
        saved_refresh = cli_mod._refresh_if_stale
        saved_status = cli_mod.status
        cli_mod._refresh_if_stale = lambda args, db_path, repo: True
        cli_mod.status = lambda conn, *a, **kw: (_ for _ in ()).throw(RuntimeError("boom after refresh"))
        try:
            code, out, err = run_cli(["status", "--max-age", "0", *self.base])
        finally:
            cli_mod._refresh_if_stale = saved_refresh
            cli_mod.status = saved_status
        env = parse_envelope(self, out, "OC-AC3")
        self.assertIs(env.get("ok"), False, "OC-AC3: the refreshed catch-all must answer ok:false")
        self.assertEqual(
            (env.get("error") or {}).get("code"),
            "runtime",
            "OC-AC3: the refreshed catch-all codes runtime",
        )
        fresh = env.get("freshness") or {}
        self.assertEqual(fresh.get("synced_at"), synced_at, f"OC-AC3: the refreshed catch-all keeps the store's real synced_at {synced_at!r}; got {fresh.get('synced_at')!r}")
        self.assertIs(
            fresh.get("refreshed"), True,
            "OC-AC3: the catch-all after a --max-age refresh must carry"
            " freshness.refreshed = true exactly like emit_result does (F-4)",
        )

        # ROUND 5 (review F-3, run second — it re-keys the store): sync_state
        # re-keyed to mixed case (Acme/ForgeGate) read as acme/forgegate:
        # the open/sql/status refusals (exit 2 bad_request) fire AFTER the
        # open and must carry the store's REAL freshness.
        from zaxbygraph.db import connect as _connect

        conn = _connect(self.db)
        try:
            conn.execute("UPDATE sync_state SET repo = 'Acme/ForgeGate' WHERE repo = ?", (REPO,))
            conn.commit()
        finally:
            conn.close()
        problems: list[str] = []
        for command, tail in (
            ("open", []),
            ("sql", ["SELECT number FROM items"]),
            ("status", []),
        ):
            code, out, err = run_cli([command, *tail, *self.base])
            env = parse_envelope(self, out, "OC-AC3")
            error = (env.get("error") or {}).get("code")
            if code != 2 or env.get("ok") is not False or error != "bad_request":
                problems.append(f"{command}: the mixed-case refusal must exit 2 bad_request; got exit {code}, ok {env.get('ok')!r}, error {error!r}")
            self._check_real_freshness(env, f"{command} mixed-case refusal", synced_at, problems)
        self.assertFalse(
            problems,
            "OC-AC3: mixed-case sync_state refusals happen AFTER the open and"
            " must carry the store's real freshness:\n- " + "\n- ".join(problems),
        )


if __name__ == "__main__":
    unittest.main()
