"""Additional regression pin for #90 AC4 (post-review strengthening).

`tests/test_seam_honesty.py::test_flag_hint_never_suggests_rejected_flag` is a
FROZEN check: it asserts the rejected flag is not suggested and that the hint
points at help. It deliberately does not forbid a DIFFERENT `did you mean`,
because that would over-constrain the documented cross-command hint. This file
adds the exact fallback property AC4 states: in the self-match case the answer
is the command help, not another flag.

Not frozen (added after the pre-implementation checkpoint at the
implementation reviewer's request); it lives in its own file so the frozen
check bytes are untouched.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zaxbygraph.cli import main  # noqa: E402


def _hint(argv: list[str]) -> str:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    assert code == 2, f"{argv!r} must be a usage error; got exit {code}"
    payload = json.loads(out.getvalue())
    return str((payload.get("error") or {}).get("hint") or "")


def test_self_match_falls_back_to_help_only() -> None:
    # The rejected token is a real flag of ANOTHER subcommand: the self-match
    # case. AC4 requires the help fallback, not a neighbour suggestion.
    for argv in (["sql", "--max-age", "1s"], ["sql", "--max-files", "3"]):
        hint = _hint([*argv, "--format", "json"])
        assert "did you mean" not in hint, (
            f"{argv!r} must fall back to the command help, not suggest a"
            f" different flag; got {hint!r}"
        )
        assert "help" in hint.lower(), f"{argv!r} must point at help; got {hint!r}"


def test_cross_command_and_bad_value_hints_unchanged() -> None:
    # A DIFFERENT, valid flag is still suggested across commands.
    limt = _hint(["search", "needle", "--limt", "5", "--format", "json"])
    assert "--limit" in limt, f"cross-command hint must survive; got {limt!r}"
    # A bad VALUE still names its own (valid) flag.
    bad = _hint(["item", "--max-body-chars", "x", "1", "--format", "json"])
    assert "--max-body-chars" in bad, f"bad-value hint must name its flag; got {bad!r}"
