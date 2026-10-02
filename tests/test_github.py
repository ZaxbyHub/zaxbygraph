from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import scrubbed_env

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Raw UTF-8 bytes from `gh api` carry the exact characters named in issue #1
#: AC1: "ā" (U+0101, invalid in cp1252 as \x81), "—" (U+2014), "🚀" (U+1F680).
PAYLOAD = [
    {
        "id": 90001,
        "number": 1,
        "title": "Crash \U0001f680 起動",
        "body": "macron \u0101 em dash \u2014 rocket \U0001f680",
        "state": "open",
        "user": {"login": "alice"},
        "labels": [],
        "comments": 0,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:01Z",
        "html_url": "https://github.com/acme/forgegate/issues/1",
        "url": "https://api.github.com/repos/acme/forgegate/issues/1",
    }
]

#: The fake `gh` is shell-free: gh_bin=sys.executable plus a script literally
#: named `api` in the probe's cwd, so `python api 'repos/...?a=1&b=2'` reaches
#: Python argv intact (a .cmd shim would let cmd.exe re-parse `&` as a command
#: separator; the real gh.exe is spawned by CreateProcess and never sees a
#: shell). The payload lives in a file so no escaping hazard exists.
FAKE_API_SCRIPT = (
    "import pathlib, sys\n"
    "sys.stdout.buffer.write("
    "pathlib.Path(__file__).with_name('payload.json').read_bytes())\n"
)

PROBE = """
import json, os, pathlib, sys
sys.path.insert(0, {src!r})
os.chdir({cwd!r})
# The probe only discriminates the pre-fix decode defect when the child's
# effective stdio/ANSI codec is NOT UTF-8-capable. On hosts where it is
# (e.g. Windows system-wide UTF-8 codepage), even text=True would decode
# cleanly, so report that instead of passing vacuously.
import locale
effective = (locale.getpreferredencoding(False) or "").lower()
if effective.replace("-", "") in ("utf8", "utf8mb4") or effective == "cp65001":
    print("SKIP_HOSTILE_LOCALE_ABSENT:" + effective)
    raise SystemExit(0)
from zaxbygraph.github import GhApiSource
src = GhApiSource("acme", "forgegate", gh_bin=sys.executable)
items = list(src.list_issues(None))
expected = json.loads(pathlib.Path("payload.json").read_text(encoding="utf-8"))
assert len(items) == 1, "expected 1 item, got %d" % len(items)
assert items[0]["title"] == expected[0]["title"], "title mismatch: %r" % items[0]["title"]
assert items[0]["body"] == expected[0]["body"], "body mismatch: %r" % items[0]["body"]
print("ROUNDTRIP_OK")
"""


class GhDecodeTests(unittest.TestCase):
    def test_utf8_payload_roundtrips_under_cp1252_locale(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            cwd = Path(td)
            (cwd / "payload.json").write_bytes(
                json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8")
            )
            (cwd / "api").write_text(FAKE_API_SCRIPT, encoding="utf-8")
            proc = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8=0",
                    "-c",
                    PROBE.format(src=str(REPO_ROOT / "src"), cwd=str(cwd)),
                ],
                capture_output=True,
                env=scrubbed_env(),
                cwd=str(cwd),
            )
        self.assertEqual(
            proc.returncode,
            0,
            "probe failed\nstdout=%r\nstderr=%r" % (proc.stdout, proc.stderr),
        )
        self.assertIn(b"ROUNDTRIP_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
