from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = REPO_ROOT / "skills" / "zaxbygraph" / "SKILL.md"


def first_action_section() -> str:
    text = SKILL.read_text(encoding="utf-8")
    match = re.search(r"^## First action\s*$([\s\S]*?)(?=^## |\Z)", text, re.M)
    assert match, "skills/zaxbygraph/SKILL.md has no '## First action' section"
    return match.group(1)


class SkillDocTests(unittest.TestCase):
    def test_first_action_is_status_not_sync(self) -> None:
        section = first_action_section()
        commands = re.findall(r"zaxbygraph (\w+)", section)
        self.assertTrue(commands, "First action section names no zaxbygraph command")
        self.assertEqual(
            commands[0],
            "status",
            "first action must be status, got %r (section: %r)" % (commands[0], section),
        )
        # sync appears only as the fallback when no corpus exists
        self.assertIn(
            "no corpus",
            section,
            "section must state that sync runs only when no corpus is found",
        )
        # the resume rule this PR added: incomplete-but-present corpora re-sync
        sync_pos = section.find("zaxbygraph sync")
        status_pos = section.find("zaxbygraph status")
        self.assertIn(
            "complete: false",
            section,
            "section must route complete:false / last_error corpora back to sync",
        )
        self.assertIn(
            "last_error",
            section,
            "the resume rule must cover the last_error signal, not just complete",
        )
        self.assertGreater(
            section.find("complete: false"),
            status_pos,
            "the resume rule must appear after the status command",
        )
        self.assertGreater(
            sync_pos,
            status_pos,
            "sync must be mentioned after status, not before it",
        )


class MachineModeSilenceDocTests(unittest.TestCase):
    """Issue #17 AC4's cheap content pin: both doc surfaces must state the
    machine-mode silence rule and the capture hazard (the criterion itself is
    NON-EXECUTABLE in the frozen acceptance table; this pin keeps the two
    statements from drifting while the rendered diff remains the substitute
    evidence)."""

    @staticmethod
    def _flat(text: str) -> str:
        # markdown line wraps must not defeat multi-word pins
        return re.sub(r"\s+", " ", text)

    def test_readme_states_machine_mode_silence(self) -> None:
        readme = self._flat(
            (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        )
        self.assertIn("machine formats", readme)
        # the capture-hazard pin must be the actual hazard sentence, not the
        # word "merged" (which matches unrelated merged_commit/merged_by text)
        self.assertIn("a merged", readme)
        self.assertIn("parse line by line", readme)
        self.assertIn("the `error:` echo still", readme)
        # the jsonl bullet no longer points at the stderr line
        self.assertNotIn("identity is on the stderr line", readme)
        self.assertNotIn("identity is inside the envelope", readme)
        self.assertIn("no identity fields", readme)
        self.assertIn("`json` or `compact` when you need them", readme)

    def test_skill_states_machine_mode_silence(self) -> None:
        skill = self._flat(SKILL.read_text(encoding="utf-8"))
        self.assertIn("On a successful read", skill)
        self.assertIn("parse line by line", skill)
        self.assertIn("On a failure the `error:` echo still", skill)
        self.assertNotIn("identity on the stderr line", skill)
        self.assertNotIn("identity inside the envelope", skill)
        self.assertIn("no identity fields", skill)
        self.assertIn("`json`/`compact` when you need them", skill)


class ExitCodeTableDocTests(unittest.TestCase):
    """Issues #61/#62: the exit-code split is the one contract decision in that
    PR, and it is documented on two surfaces. The CLI tests pin the behaviour;
    this pins the DESCRIPTION of it, so the two doc surfaces cannot drift away
    from what the code answers -- the same shape as the machine-mode pin above.

    The failure this guards against is specific: the sentence "every other guard
    rejection stays 2" was itself false (export-corpus --require-complete answers
    1 with incomplete_store), and it read plausibly until cubic caught it."""

    @staticmethod
    def _flat(text: str) -> str:
        return re.sub(r"\s+", " ", text)

    def _assert_table(self, surface: str) -> None:
        text = self._flat(surface)
        low = text.lower()
        # The two exit-1 inspection refusals, named.
        self.assertIn("dest_uninspectable", text)
        self.assertIn("out_is_database", text)
        # The narrowing: a retry hint does NOT by itself mean exit 1. Compared
        # case-insensitively because README opens the sentence with a lowercase
        # "narrow" and SKILL with an uppercase "Narrow".
        self.assertIn("not a general", low)
        # The four retry-hinted refusals that still answer 2 -- without these
        # named, the "retry means 1" over-reading comes straight back.
        for code in ("git_timeout", "dest_open", "dest_sidecar_stale",
                     "corpus_mismatch"):
            self.assertIn(code, text, f"{code} missing from the exit-2 carve-out")
        # The unrelated pre-existing exit-1 refusal must be carved out too, or
        # the sentence "every destination guard rejection stays 2" reads as
        # false the moment someone checks --require-complete.
        self.assertIn("incomplete_store", text)

    def test_readme_carries_the_exit_code_split(self) -> None:
        self._assert_table(
            (REPO_ROOT / "README.md").read_text(encoding="utf-8"))

    def test_skill_carries_the_exit_code_split(self) -> None:
        self._assert_table(SKILL.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
