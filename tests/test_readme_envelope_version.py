"""Docs pin for final-critic finding FC-2 (slug
92-93-output-contract-honesty): the README's envelope declaration must
name the SAME contract version the envelopes actually carry
(cli.ENVELOPE_VERSION, issues #92/#93 AC12) and list `version` among the
envelope keys — the sync example and the "One envelope" bullet had stayed
at v0.2 without the key after the code moved to v0.3.

Read-only: never edits the documents it pins. Stdlib only; unittest.
Failure tags REV-*.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

import zaxbygraph

_WORKTREE = Path(__file__).resolve().parents[1]
if not Path(zaxbygraph.__file__).resolve().is_relative_to(_WORKTREE / "src"):
    raise AssertionError(
        f"REV-ENV: zaxbygraph resolved to {zaxbygraph.__file__}, not this"
        f" worktree's src ({_WORKTREE / 'src'}); set"
        f" PYTHONPATH=<worktree>/src{os.pathsep}<worktree>/tests"
    )

from zaxbygraph import cli as cli_mod  # noqa: E402

README = _WORKTREE / "README.md"
SCHEMA_MD = _WORKTREE / "docs" / "schema.md"


class TestReadmeEnvelopeVersion(unittest.TestCase):
    def test_readme_declares_the_live_envelope_contract(self) -> None:
        version = cli_mod.ENVELOPE_VERSION
        text = README.read_text(encoding="utf-8")
        problems: list[str] = []
        # The "One envelope" bullet names the contract version...
        match = re.search(r"\*\*One envelope \(v(\d+\.\d+)", text)
        if match is None:
            problems.append(
                "REV: the README must carry the 'One envelope (vX.Y)' bullet"
                " naming the envelope contract version"
            )
        elif match.group(1) != version:
            problems.append(
                f"REV: the README declares envelope v{match.group(1)!r}, want"
                f" the live cli.ENVELOPE_VERSION {version!r}"
            )
        # ...and lists `version` among the envelope keys in that template.
        template = re.search(
            r'`\{"ok": true\|false.*?"truncated": bool[^\n]*\}`', text, re.S
        )
        if template is None:
            problems.append(
                "REV: the README's envelope key template must be present"
                " (`{\"ok\": true|false ... \"truncated\": bool ...}`)"
            )
        elif "version" not in template.group(0):
            problems.append(
                "REV: the README's envelope key template must list `version`"
                " among the keys"
            )
        # No stale version declaration survives anywhere in the README.
        for stale in re.findall(r"envelope \(v(\d+\.\d+)", text) + re.findall(
            r"\(v(\d+\.\d+), breaking\)", text
        ):
            if stale != version:
                problems.append(
                    f"REV: a stale envelope version declaration v{stale!r}"
                    f" survives; want {version!r}"
                )
        # The sync example envelope carries the version key too.
        example = re.search(
            r'"truncated": false,\s*\n\s*"version": "([^"]+)"', text
        )
        if example is None:
            problems.append(
                'REV: the sync example envelope must carry "version" after'
                ' "truncated"'
            )
        elif example.group(1) != version:
            problems.append(
                f"REV: the sync example carries version {example.group(1)!r},"
                f" want {version!r}"
            )
        # docs/schema.md points at this contract; its key list agrees.
        schema_text = SCHEMA_MD.read_text(encoding="utf-8")
        pointer = re.search(
            r"\{ok, db, repo, freshness, data, truncated([^}]*)\}", schema_text
        )
        if pointer is None:
            problems.append(
                "REV: docs/schema.md's envelope key list must be present"
                " ({ok, db, repo, freshness, data, truncated, ...})"
            )
        elif "version" not in pointer.group(1):
            problems.append(
                "REV: docs/schema.md's envelope key list must include"
                " `version` (it points at the README contract)"
            )
        self.assertFalse(
            problems,
            f"REV: the README/docs must declare the live envelope contract"
            f" ({version!r}) with the `version` key:\n- "
            + "\n- ".join(problems),
        )


if __name__ == "__main__":
    unittest.main()
