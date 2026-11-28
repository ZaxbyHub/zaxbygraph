from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from zaxbygraph.cli import main


REPO_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = r'''
import os
import runpy
import sys

mode = sys.argv.pop(1)
if mode in ("fail-enospc", "fail-eio"):
    import errno

    class FailingWriter:
        def __init__(self, number):
            self.number = number

        def write(self, text):
            raise OSError(self.number, "forced stream failure")

        def flush(self):
            raise OSError(self.number, "forced stream failure")

    sys.stdout = FailingWriter(
        errno.ENOSPC if mode == "fail-enospc" else errno.EIO
    )
elif mode == "none-stdout":
    sys.stdout = None
elif mode == "none-stderr":
    sys.stderr = None
elif mode == "closed-stdout-silence-write":
    from zaxbygraph.cli import main

    os.close(1)
    code = main(sys.argv[1:])
    try:
        sys.stdout.write("after-silence\\n")
        sys.stdout.flush()
    except Exception as exc:
        print(f"WRITE_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(6)
    print("WRITE_OK", file=sys.stderr)
    raise SystemExit(code)
elif mode == "closed-stdout":
    os.close(1)
elif mode == "closed-stderr":
    os.close(2)
elif mode == "dead-stdout-pipe":
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.dup2(write_fd, 1)
    os.close(write_fd)

sys.argv = ["zaxbygraph", *sys.argv[1:]]
runpy.run_module("zaxbygraph", run_name="__main__")
'''


class StreamBoundaryFileDescriptorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "missing.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, mode: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONPATH"] = os.pathsep.join(
            (str(REPO_ROOT / "src"), str(REPO_ROOT / "tests"))
        )
        return subprocess.run(
            [sys.executable, "-c", _LAUNCHER, mode, *args],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def missing_corpus_args(self) -> tuple[str, ...]:
        return (
            "status",
            "--db",
            self.db_path,
            "--repo",
            "acme/widgets",
            "--format",
            "json",
        )

    def assert_no_shutdown_noise(self, result: subprocess.CompletedProcess[str]) -> None:
        combined = result.stdout + result.stderr
        self.assertNotIn("Traceback", combined)
        self.assertNotIn("Exception ignored", combined)

    def test_closed_stdout_keeps_command_exit_without_shutdown_noise(self) -> None:
        result = self.run_cli("closed-stdout", *self.missing_corpus_args())

        self.assertEqual(result.returncode, 3, result.stderr)
        self.assert_no_shutdown_noise(result)

    def test_closed_stderr_keeps_json_envelope_and_command_exit(self) -> None:
        result = self.run_cli("closed-stderr", *self.missing_corpus_args())

        self.assertEqual(result.returncode, 3, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertIs(envelope["ok"], False)
        self.assertEqual(envelope["error"]["code"], "no_corpus")
        self.assertEqual(result.stderr, "")
        self.assert_no_shutdown_noise(result)

    def test_successful_argparse_exits_quietly_with_dead_stdout(self) -> None:
        for args in (("--help",), ("sql", "--help"), ("--version",)):
            with self.subTest(args=args):
                result = self.run_cli("dead-stdout-pipe", *args)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
                self.assert_no_shutdown_noise(result)

    def test_live_stream_controls_keep_success_failure_and_usage_contracts(self) -> None:
        success = self.run_cli("live", "--version")
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertTrue(success.stdout.strip())
        self.assertEqual(success.stderr, "")

        failure = self.run_cli("live", *self.missing_corpus_args())
        self.assertEqual(failure.returncode, 3, failure.stderr)
        self.assertIs(json.loads(failure.stdout)["ok"], False)
        self.assertIn("error:", failure.stderr)

        usage_error = self.run_cli("live", "sql", "--not-a-sql-option")
        self.assertEqual(usage_error.returncode, 2, usage_error.stderr)
        usage_envelope = json.loads(usage_error.stdout)
        self.assertIs(usage_envelope["ok"], False)
        self.assertEqual(usage_envelope["error"]["code"], "bad_request")
        self.assertEqual(usage_error.stderr, "")

    def test_other_stdout_os_errors_are_not_swallowed(self) -> None:
        for mode in ("fail-enospc", "fail-eio"):
            with self.subTest(mode=mode):
                result = self.run_cli(mode, *self.missing_corpus_args())

                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("forced stream failure", result.stderr)

    def test_stdout_remains_writable_after_silencing_closed_fd(self) -> None:
        result = self.run_cli(
            "closed-stdout-silence-write", *self.missing_corpus_args()
        )

        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertNotIn("WRITE_FAILED", result.stderr)
        self.assertIn("WRITE_OK", result.stderr)
        self.assert_no_shutdown_noise(result)

    def test_none_stdout_help_and_version_exit_quietly(self) -> None:
        for args in (("--help",), ("--version",)):
            with self.subTest(args=args):
                result = self.run_cli("none-stdout", *args)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)

    def test_none_stdout_keeps_command_exit_without_traceback(self) -> None:
        result = self.run_cli("none-stdout", *self.missing_corpus_args())

        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Exception ignored", result.stderr)

    def test_none_stderr_keeps_stdout_json_envelope(self) -> None:
        result = self.run_cli("none-stderr", *self.missing_corpus_args())

        self.assertEqual(result.returncode, 3, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertIs(envelope["ok"], False)
        self.assertEqual(envelope["error"]["code"], "no_corpus")
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertFalse(result.stdout.endswith("\n\n"))
        self.assertEqual(result.stderr, "")

    def test_in_process_none_stdout_version_still_raises_system_exit(self) -> None:
        saved_stdout = sys.stdout
        try:
            sys.stdout = None
            with self.assertRaises(SystemExit) as caught:
                main(["--version"])
        finally:
            sys.stdout = saved_stdout

        self.assertEqual(caught.exception.code, 0)

    def test_in_process_help_still_raises_successful_system_exit(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            main(["--help"])

        self.assertEqual(caught.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
