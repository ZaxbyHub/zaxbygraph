"""Frozen acceptance checks for the collated #89 + #90 seams trace.

These four CLI-facing checks and the two hermeticity checks live in their OWN
file rather than in `tests/test_cli.py`, because the #89 AC4 fix necessarily
edits `tests/test_cli.py` (it gives the existing `test_status_empty_json` an
explicit `--repo`). A checkpoint freezes the bytes of every file that hosts a
check, and a frozen blob must not change during implementation, so keeping
these checks in `test_cli.py` would have forced a post-implementation manifest
amendment and invalidated the pre-implementation anchor receipt. The deviation
from the issues' published argv paths is recorded in the trace and on the
issues.

Every check drives the real public entry point (`cli.main` or a subprocess of
it) and is offline: no gh process, no network, no live store.

RED at e6c8f41 for C4, C8, C9, C11; GREEN at both ends for C10.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from zaxbygraph import cli  # noqa: E402
from zaxbygraph.cli import main  # noqa: E402


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    """In-process `cli.main` under redirected stdio (the repo's run_cmd form)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            if isinstance(exc.code, int):
                code = exc.code
            elif exc.code is None:
                code = 0
            else:
                code = 2
    return code, out.getvalue(), err.getvalue()


def _error_of(out: str) -> dict:
    payload = json.loads(out)
    assert payload.get("ok") is False, f"expected a failure envelope; got {out[:300]!r}"
    return payload.get("error") or {}


# ==== #89 AC4: the nearest-flag hint never suggests the rejected flag ======
#
# The hint pool unions every subcommand's flags, so a token that is a real
# flag of a DIFFERENT subcommand (--max-age on `sql`) matched itself and was
# suggested back to the user. The same function serves "argument --flag:
# invalid value" errors, where naming the flag IS the useful hint, so the fix
# must target only the "unrecognized arguments" shape.
#
# RED at e6c8f41: `sql --max-age` answers "did you mean --max-age?".

def test_flag_hint_never_suggests_rejected_flag() -> None:
    code, out, err = _run_cli(["sql", "--max-age", "1s", "--format", "json"])
    assert code == 2, f"a usage error keeps exit 2; got {code} ({err[:200]!r})"
    hint = str(_error_of(out).get("hint") or "")
    assert "did you mean --max-age" not in hint, (
        "the hint must never suggest the very flag that was rejected;"
        f" got {hint!r}"
    )
    assert "help" in hint.lower(), (
        f"the fallback must still point at the command's help; got {hint!r}"
    )

    # The bad-value shape keeps naming its own (valid) flag -- a blanket
    # self-exclusion would regress this documented behaviour.
    code2, out2, _err2 = _run_cli(
        ["item", "--max-body-chars", "x", "1", "--format", "json"]
    )
    assert code2 == 2, f"a bad value keeps exit 2; got {code2}"
    hint2 = str(_error_of(out2).get("hint") or "")
    assert "--max-body-chars" in hint2, (
        "a bad-value error must keep naming the flag it is about;"
        f" got {hint2!r}"
    )


# ==== #89 AC3: --numbers accepts canonical issue numbers only ==============
#
# `cmd_sync` parsed --numbers with a bare `int()`, which accepts `1_0` as 10,
# `+5` as 5, `-5` as -5 and non-ASCII digits (`٣` as 3) -- the exact leniency
# the sibling `_parse_max_age` documents and avoids. Values below 1 are not
# issue numbers either.
#
# RED at e6c8f41: `--numbers 1_0` is accepted and the sync runs.
# The fetch seam (`cli._sync_core`) is spied so a non-refusal fails loudly
# instead of spawning gh or touching the network.

def test_numbers_rejects_noncanonical_tokens() -> None:
    seen: list[object] = []

    def _spy(*args, **kwargs):
        seen.append(kwargs.get("numbers"))
        return {"ingested": 0}

    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "history.db")
        with mock.patch.object(cli, "_sync_core", _spy):
            for token in ("1_0", "+5", "-5", "\u0663", "0"):
                seen.clear()
                code, out, err = _run_cli(
                    ["sync", "--numbers", token, "--repo", "acme/forgegate",
                     "--db", db, "--format", "json"]
                )
                assert code == 2, (
                    f"--numbers {token!r} must be refused with exit 2; got"
                    f" {code} ({err.strip()[:200]!r})"
                )
                assert _error_of(out).get("code") == "bad_request", (
                    f"--numbers {token!r} must answer the bad_request code;"
                    f" got {out[:300]!r}"
                )
                assert seen == [], (
                    f"no fetch may run for --numbers {token!r}; _sync_core"
                    f" was reached with numbers={seen!r}"
                )


# ==== #89 AC4/AC5: the no-corpus contract, hermetically ====================
#
# The existing `CliTests.test_status_empty_json` passes no `--repo`, so its
# verdict depended on the working directory resolving a git origin: from a
# non-git cwd repo resolution failed first and the test saw exit 2
# `bad_request` instead of exit 3 `no_corpus`. These two checks spawn that
# exact test as a subprocess, one from a temp non-git cwd (RED at base) and
# one from the repo root (GREEN at both ends: the exit-3 contract itself is
# unchanged).

_TEST = "tests.test_cli.CliTests.test_status_empty_json"


def _run_existing_test(cwd: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(ROOT / "src"), str(ROOT / "tests")]
    )
    return subprocess.run(
        [sys.executable, "-m", "unittest", _TEST, "-v"],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


def test_status_empty_json_is_hermetic() -> None:
    with tempfile.TemporaryDirectory() as td:
        proc = _run_existing_test(td)
    assert proc.returncode == 0, (
        "test_status_empty_json must pass from a working directory that is"
        " not a git repo; the child failed with:\n"
        + (proc.stderr or proc.stdout)[-1200:]
    )


def test_status_empty_json_from_repo_root() -> None:
    proc = _run_existing_test(str(ROOT))
    assert proc.returncode == 0, (
        "test_status_empty_json must keep passing from the repository root"
        " (the exit-3 no_corpus contract is unchanged):\n"
        + (proc.stderr or proc.stdout)[-1200:]
    )


# ==== #89 AC6: the repo-resolution error names its remedy exactly once =====
#
# `RepoError` messages already end with "; pass --repo OWNER/REPO" and the two
# CLI wrappers appended the same instruction again around `{exc}`, so the user
# saw "could not determine repo (no git origin remote; pass --repo OWNER/REPO);
# pass --repo OWNER/REPO".
#
# RED at e6c8f41: the message names the remedy twice.

def test_resolution_error_names_remedy_once() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    with tempfile.TemporaryDirectory() as td:
        fresh = str(Path(td) / "missing" / "history.db")
        proc = subprocess.run(
            [sys.executable, "-m", "zaxbygraph", "status", "--db", fresh,
             "--format", "json"],
            cwd=td,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )
    assert proc.returncode == 2, (
        f"an unresolvable repo keeps exit 2; got {proc.returncode}"
        f" ({(proc.stderr or proc.stdout).strip()[:300]!r})"
    )
    payload = json.loads(proc.stdout)
    message = str((payload.get("error") or {}).get("message") or "")
    count = message.count("pass --repo OWNER/REPO")
    assert count == 1, (
        "the repo-resolution error must name the --repo remedy exactly once;"
        f" got {count} in {message!r}"
    )
    assert "could not determine repo" in message, (
        f"the resolution error must stay recognisable; got {message!r}"
    )


if __name__ == "__main__":
    # Imported HERE, not at module scope: `python -m unittest discover` (the
    # documented stdlib-only command) imports every test module, and pytest is
    # not a runtime dependency (post-publication review).
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
