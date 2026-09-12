from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile

from zaxbygraph.cli import main
from zaxbygraph.db import connect, init_schema


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.db = str(Path(self._td.name) / "history.db")
        conn = connect(Path(self.db))
        init_schema(conn)
        conn.close()

    def tearDown(self) -> None:
        self._td.cleanup()

    def run_cmd(self, argv: list[str]) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_help(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            main(["--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_status_empty_json(self) -> None:
        code, out, err = self.run_cmd(["status", "--db", self.db, "--format", "json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIn("repos", data)
        self.assertIn("counts", data)
        self.assertEqual(data["repos"], [])

    def test_search_json_shape(self) -> None:
        code, out, err = self.run_cmd(
            ["search", "nothing", "--db", self.db, "--format", "json"]
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIn("items", data)
        self.assertIn("comments", data)
        self.assertEqual(data["items"], [])

    def test_sql_write_rejected(self) -> None:
        code, out, err = self.run_cmd(
            ["sql", "INSERT INTO items VALUES (1)", "--db", self.db, "--format", "json"]
        )
        self.assertNotEqual(code, 0)
        self.assertTrue(err)

    def test_item_missing(self) -> None:
        code, out, err = self.run_cmd(["item", "99", "--db", self.db, "--format", "json"])
        self.assertEqual(code, 1)
