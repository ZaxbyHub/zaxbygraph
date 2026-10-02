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


if __name__ == "__main__":
    unittest.main()
