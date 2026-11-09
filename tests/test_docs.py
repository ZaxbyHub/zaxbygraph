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
    1 with incomplete_store), and it read plausibly until cubic caught it.

    What is asserted here is the MAPPING, not the vocabulary. A token-presence
    pin ("the code names occur somewhere in the file") passes a document that
    says `dest_uninspectable` answers 2 -- five such mutations stayed green
    before this rewrite, including one that kept every code name and inverted
    the digit beside two of them. So each surface is decomposed into the
    smallest spans it uses to bind a code to an exit class, and a span counts
    as a binding only when it states exactly one exit digit: a span carrying
    both `1` and `2` is ambiguous and binds nothing, which is what makes the
    "which answers 2" mutation fail rather than pass."""

    #: Codes whose refusals answer 1, and codes whose refusals answer 2.
    EXIT_ONE = ("dest_uninspectable", "out_is_database")
    EXIT_TWO = ("git_timeout", "dest_open", "dest_sidecar_stale",
                "corpus_mismatch")
    #: Pre-existing refusals that answer 1 for reasons unrelated to the
    #: destination-inspection rule. Named, but not part of the mapping: they
    #: are carve-outs, and their absence is what made the README sentence read
    #: as false. `bad_as_of` is here because PRR-010's first fix hand-counted
    #: the set as four and missed it -- the count is gone from the docs, and
    #: this tuple is what keeps the enumeration honest.
    CARVED_OUT = ("incomplete_store", "bad_as_of", "store_unreadable",
                  "refresh_failed")

    _CODE_TOKEN = re.compile(r"`([a-z][a-z0-9_]{3,})`")
    _EXIT_DIGIT = re.compile(r"`([123])`|exit[ -]([123])")

    @staticmethod
    def _flat(text: str) -> str:
        return re.sub(r"\s+", " ", text)

    @classmethod
    def _bindings(cls, surface: str) -> list:
        """``(exit_class, span)`` pairs -- the spans that bind a code to an
        exit class. A markdown table contributes each meaning cell under its
        own row's code; prose contributes one sentence at a time. A span that
        states more than one distinct digit binds nothing: it is describing a
        code with a split exit class, not binding one."""
        out = []
        para = []

        def flush() -> None:
            if not para:
                return
            for sent in re.split(r"(?<=[.;:])\s+", " ".join(para)):
                sent = sent.strip()
                if sent:
                    out.append((cls._stated_digit(sent), sent))
            para.clear()

        for line in surface.splitlines():
            stripped = line.strip()
            if stripped.startswith("|"):
                flush()
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if len(cells) < 2 or cells[0] in ("---", "Code"):
                    continue
                row_digit = cells[0].strip("`")
                if row_digit in ("1", "2", "3"):
                    # The row's own code is the binding for its meaning cell.
                    out.append((row_digit, cells[1]))
                # The action cell is advice, not a binding -- unless it states
                # exactly one digit, in which case it is one.
                if len(cells) > 2 and cells[2]:
                    out.append((cls._stated_digit(cells[2]), cells[2]))
            elif stripped:
                para.append(stripped)
            else:
                flush()
        flush()
        return [(d, t) for d, t in out if d is not None]

    @classmethod
    def _stated_digit(cls, span: str) -> str | None:
        digits = {a or b for a, b in cls._EXIT_DIGIT.findall(span)}
        return digits.pop() if len(digits) == 1 else None

    @classmethod
    def _codes(cls, span: str) -> set:
        return set(cls._CODE_TOKEN.findall(span))

    def _assert_table(self, surface: str) -> None:
        bindings = self._bindings(surface)
        by_class = {"1": set(), "2": set()}
        for digit, span in bindings:
            by_class.setdefault(digit, set()).update(self._codes(span))

        # The two inspection refusals are bound to 1, and the four
        # retry-hinted refusals are bound to 2 -- in the doc, not merely
        # mentioned by it.
        for code in self.EXIT_ONE:
            self.assertIn(
                code, by_class["1"],
                f"{code} is not bound to exit 1 on this surface; it is either"
                " absent or stated beside an exit class the code does not"
                " answer")
        for code in self.EXIT_TWO:
            self.assertIn(
                code, by_class["2"],
                f"{code} is not bound to exit 2 on this surface")
        # And the two sets do not bleed into each other, which is what a
        # substitution mutation (one code re-pointed at the other digit)
        # would otherwise do silently.
        self.assertFalse(set(self.EXIT_TWO) & by_class["1"],
                         "an exit-2 refusal is stated as answering 1")
        self.assertFalse(set(self.EXIT_ONE) & by_class["2"],
                         "an exit-1 refusal is stated as answering 2")
        # A contradiction INSIDE a binding span is the one shape the mapping
        # assertions above cannot see, because the row's own code cell still
        # says 1. A meaning cell may legitimately disclose a second exit class
        # for a DIFFERENT leg of the same code -- `out_is_database` answers 2
        # on its alias legs -- and that disclosure says "legs"/"only" between
        # the code and the digit. The check is therefore per-CLAUSE, not
        # per-sentence: the legitimate disclosure and an injected
        # "`dest_uninspectable` answers `2`" land in the same sentence, and
        # only the intervening text tells them apart.
        for code in self.EXIT_ONE:
            for digit, span in bindings:
                if digit != "1":
                    continue
                for m in re.finditer(re.escape(code), span):
                    tail = span[m.end():]
                    # The opposite digit, in any of the shapes _EXIT_DIGIT
                    # parses -- a bare backticked `2` AND the prose "exit 2".
                    # Scanning only the backticked literal let "answers exit 2"
                    # (lowercase) and "answers Exit 2" survive.
                    hit = re.search(r"`2`|exit[ -]2", tail, re.I)
                    if not hit:
                        continue
                    between = tail[:hit.start()]
                    if re.search(r"\blegs?\b|\bonly\b", between, re.I):
                        continue          # a disclosed split, not a contradiction
                    self.fail(
                        f"{code} is bound to exit 1 but this span states it"
                        f" answers 2 with no leg qualifier:"
                        f" {between[-90:]!r}")
        # The narrowing survives: a retry hint does not by itself mean 1.
        # Compared case-insensitively because README opens the sentence with a
        # lowercase "narrow" and SKILL with an uppercase "Narrow".
        self.assertIn("not a general", self._flat(surface).lower())
        # The carve-outs are named, or "every snapshot guard rejection stays
        # 2" reads as false the moment someone checks --require-complete.
        for code in self.CARVED_OUT:
            self.assertIn(code, surface, f"{code} missing from the carve-out")

    def test_readme_carries_the_exit_code_split(self) -> None:
        self._assert_table(
            (REPO_ROOT / "README.md").read_text(encoding="utf-8"))

    def test_skill_carries_the_exit_code_split(self) -> None:
        self._assert_table(SKILL.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
