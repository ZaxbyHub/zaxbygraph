"""Non-frozen extension of the #90 AC2 device-name pin (final-critic revision).

The frozen C2 check covers the issue's literal list (CON/PRN/AUX/NUL/COM1-9/
LPT1-9, case variants, extension forms, absolute paths). The final critic drove
the real CLI and found four further Win32 device spellings still reaching
`os.replace`: `CON:`, `NUL:`, `CONIN$`, `CONOUT$`. `ntpath.isreserved` agrees
they are devices. This pin covers them without touching the frozen bytes (the
v4 anchor binds C2's file exactly).

Added at the final critic's request; wired into the CI pytest step.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zaxbygraph.export import ExportError, _reject_database_alias  # noqa: E402


def test_colon_and_console_device_spellings_refused() -> None:
    names = ("CON:", "NUL:", "conin$", "CONOUT$", "CON:txt", "NUL:stream.txt")
    with tempfile.TemporaryDirectory() as td:
        # Build every path BEFORE any os.name patch (pathlib reads os.name).
        db_path = str(Path(td) / "history.db")
        Path(db_path).write_bytes(b"store bytes")
        with mock.patch.object(os, "name", "nt"):
            for name in names:
                try:
                    _reject_database_alias(name, db_path)
                except ExportError as exc:
                    assert exc.code == "out_is_database", (
                        f"--out {name!r} must be refused with out_is_database;"
                        f" got {exc.code!r}"
                    )
                else:
                    raise AssertionError(
                        f"--out {name!r} names a reserved DOS device and must be"
                        " refused before the corpus is spooled"
                    )
        # POSIX keeps permitting these as ordinary filenames.
        with mock.patch.object(os, "name", "posix"):
            _reject_database_alias("CON:", db_path)
            _reject_database_alias("CONOUT$", db_path)
