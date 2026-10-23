"""Acceptance checks for issue-trace 17-19-machine-output-closes-uniqueness
(issue #17, AC1/AC2): machine-format output must be merge-safe, text mode
keeps its stderr identity line. Authored at arm's length against the trace's
acceptance table; black-box at the subprocess boundary (the consumer idiom
`cmd 2>&1 | json.load`), never in-process, because the defect IS the stream
interleaving a merged pipe capture exposes.

- TestMergedCapture.test_json_merged_capture_strict_parses (AC1/C1) is
  RED at base, GREEN after the fix: at base the stderr identity line
  interleaves into the merged capture and strict json.loads raises
  json.decoder.JSONDecodeError; once machine formats emit no non-JSON bytes
  the merged stream is one JSON document.
- TextIdentityTests.test_text_mode_keeps_stderr_identity_line (AC2/C2) is
  GREEN at base and must stay green: text mode still writes exactly one
  stderr identity line.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import zaxbygraph
from fixtures import REPO, FakeGitHubSource, issue, pr_file, pull
from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo

_SRC_DIR = str(Path(zaxbygraph.__file__).resolve().parents[1])


def child_env() -> dict[str, str]:
    """Environment for the spawned CLI: inherits this process's environment
    (the wrapper exports PYTHONUTF8) but guarantees `python -m zaxbygraph`
    resolves to the SAME package this test process imported, so the probe
    can never accidentally exercise a different installation."""
    env = dict(os.environ)
    parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if _SRC_DIR not in parts:
        parts.insert(0, _SRC_DIR)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def seed_db(db_path: str) -> None:
    """Minimal offline fixture store: two open issues and one merged PR,
    seeded through the real sync path against FakeGitHubSource (no network,
    no gh), so `open` is a genuinely successful read with rows to render."""
    conn = connect(Path(db_path))
    init_schema(conn)
    src = FakeGitHubSource()
    src.add_issue(issue(1, title="first issue", body="seed one", state="open"))
    src.add_issue(issue(2, title="second issue", body="seed two", state="open"))
    src.add_pr(
        issue(10, title="seed pr", body="", kind="pr", state="closed"),
        pull(10, changed_files=1, merged=True),
        files=[pr_file("src/seed.py")],
    )
    sync_repo(conn, src, REPO)
    conn.close()


class _SeededCliTestCase(unittest.TestCase):
    """Shared harness: temp DB seeded offline, subprocess runner."""

    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        self.db = str(Path(td.name) / "history.db")
        seed_db(self.db)

    def run_cli(self, argv: list[str], *, stderr) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "zaxbygraph", *argv],
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=child_env(),
        )


class TestMergedCapture(_SeededCliTestCase):
    def test_json_merged_capture_strict_parses(self) -> None:
        """AC1 (#17). RED at base, GREEN after the fix.

        A successful machine-format read (`open --format json`) captured with
        stderr merged into stdout (the `2>&1` idiom, here stderr=STDOUT on a
        single pipe) must strict-parse as ONE JSON document. At base the
        unconditional stderr identity line interleaves into the merged pipe
        and json.loads raises JSONDecodeError; the assertEqual on the return
        code guards against a vacuous parse failure (a failing command must
        never look like the defect)."""
        proc = self.run_cli(
            ["open", "--db", self.db, "--repo", REPO, "--format", "json"],
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(
            proc.returncode,
            0,
            "MERGE1: the open read itself must succeed before the capture is judged",
        )
        payload = json.loads(proc.stdout.decode("utf-8", "replace"))
        self.assertIsInstance(
            payload, dict, "MERGE1: the merged capture must be one JSON object"
        )
        self.assertIs(
            payload.get("ok"), True, "MERGE1: the envelope must report ok true"
        )


class TextIdentityTests(_SeededCliTestCase):
    def test_text_mode_keeps_stderr_identity_line(self) -> None:
        """AC2 (#17). GREEN at base and must stay green.

        A successful text-format read writes EXACTLY ONE line to stderr and
        it is the identity line (`# db=... repo=... ...`). The machine-mode
        silence rule must never leak into text mode."""
        proc = self.run_cli(
            ["open", "--db", self.db, "--repo", REPO, "--format", "text"],
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            proc.returncode,
            0,
            "IDENT1: the text-mode open read must succeed",
        )
        err = proc.stderr.decode("utf-8", "replace")
        lines = err.splitlines()
        self.assertEqual(
            len(lines),
            1,
            "IDENT1: text-mode stderr must carry EXACTLY ONE line, got "
            + str(len(lines))
            + ": "
            + repr(err),
        )
        self.assertTrue(
            lines[0].startswith("# db="),
            "IDENT1: the single stderr line must be the identity line, got "
            + repr(lines[0]),
        )


if __name__ == "__main__":
    unittest.main()
