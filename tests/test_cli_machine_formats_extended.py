"""Non-frozen extension of the issue #17 machine-mode silence contract:
the jsonl, compact, and resolved-default paths the frozen C1/C2 module does
not cover. The frozen module (tests/test_cli_json_machine_mode.py) pins the
json merged capture and the text-mode identity line; implementation review
found a partial fix that re-printed the identity line in jsonl mode survived
every check and test, so this module pins the remaining machine formats at
the same subprocess boundary (black-box, merged-stream where it matters).

- jsonl LIST payloads (`open --format jsonl`) print rows only — no envelope,
  so no identity fields exist on that surface; the contract there is "every
  line strict-parses and stderr is silent" (docs and README state this).
- jsonl single-payload commands (`status --format jsonl`) print the whole
  envelope: the merged capture must strict-parse as one JSON document.
- compact always carries the envelope: merged capture strict-parses.
- With no --format and a piped stdout the resolved default is json
  (`_format_of`): stderr must stay silent there too.

GREEN at base for the silence assertions is impossible (the identity line
printed for every format at base), so these tests are RED at base and GREEN
after the fix, exactly like the frozen C1.
"""

from __future__ import annotations

import json
import subprocess
import unittest

from test_cli_json_machine_mode import REPO, _SeededCliTestCase


class JsonlMergedCaptureTests(_SeededCliTestCase):
    def test_jsonl_list_payload_lines_parse_and_stderr_silent(self) -> None:
        """`open --format jsonl` renders list payloads rows-only; a merged
        capture must be line-parseable JSON with NO stderr bytes. Guards the
        partial-fix mutant where the identity line printed only in jsonl."""
        proc = self.run_cli(
            ["open", "--db", self.db, "--repo", REPO, "--format", "jsonl"],
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(
            proc.returncode, 0, "JSONL1: the open read must succeed"
        )
        text = proc.stdout.decode("utf-8", "replace")
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertTrue(lines, "JSONL1: jsonl list output must not be empty")
        for line in lines:
            row = json.loads(line)  # raises on the identity line interleaving
            # rows-only contract: no envelope keys on the jsonl list surface
            self.assertNotIn("ok", row)
            self.assertNotIn("freshness", row)
            self.assertNotIn("truncated", row)

    def test_jsonl_list_payload_keeps_stderr_clean(self) -> None:
        proc = self.run_cli(
            ["open", "--db", self.db, "--repo", REPO, "--format", "jsonl"],
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(
            [line for line in proc.stderr.decode("utf-8", "replace").splitlines()
             if line.strip()],
            [],
            "JSONL2: jsonl stderr must be silent, got: "
            + repr(proc.stderr.decode("utf-8", "replace")),
        )

    def test_jsonl_single_payload_merged_capture_strict_parses(self) -> None:
        """`status --format jsonl` prints the whole envelope: the merged
        capture is one JSON document."""
        proc = self.run_cli(
            ["status", "--db", self.db, "--repo", REPO, "--format", "jsonl"],
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(proc.returncode, 0, "JSONL3: status must succeed")
        payload = json.loads(proc.stdout.decode("utf-8", "replace"))
        self.assertIs(payload.get("ok"), True)


class CompactAndDefaultTests(_SeededCliTestCase):
    def test_compact_merged_capture_strict_parses(self) -> None:
        proc = self.run_cli(
            ["open", "--db", self.db, "--repo", REPO, "--format", "compact"],
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(proc.returncode, 0, "COMPACT1: the open read must succeed")
        payload = json.loads(proc.stdout.decode("utf-8", "replace"))
        self.assertIs(payload.get("ok"), True)

    def test_resolved_default_on_a_pipe_is_silent(self) -> None:
        """No --format with a piped (non-tty) stdout resolves to json
        (`_format_of`); the machine-mode silence rule must hold there too."""
        proc = self.run_cli(
            ["open", "--db", self.db, "--repo", REPO],
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, "DEFAULT1: the open read must succeed")
        self.assertEqual(
            [line for line in proc.stderr.decode("utf-8", "replace").splitlines()
             if line.strip()],
            [],
            "DEFAULT1: the resolved-default json path must keep stderr silent",
        )
        payload = json.loads(proc.stdout.decode("utf-8", "replace"))
        self.assertIs(payload.get("ok"), True)


if __name__ == "__main__":
    unittest.main()
