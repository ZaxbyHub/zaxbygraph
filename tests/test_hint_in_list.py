"""PR #98 closeout review: AC14 (issues #92/#93) is not limited to equality
filters. A zero-row read whose filter is an IN-list containing an
out-of-vocabulary literal must carry the same annotate-only hint as the
`=` form; an IN-list whose members are all valid, or a read that returns
rows, stays hint-free. Hermetic: FakeGitHubSource-seeded temp store, no
network. Failure tags HIN-*.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import zaxbygraph

_WORKTREE = Path(__file__).resolve().parents[1]
if not Path(zaxbygraph.__file__).resolve().is_relative_to(_WORKTREE / "src"):
    raise AssertionError(
        f"HIN-ENV: zaxbygraph resolved to {zaxbygraph.__file__}, not this"
        f" worktree's src; set PYTHONPATH=<worktree>/src{os.pathsep}<worktree>/tests"
    )

from fixtures import REPO, FakeGitHubSource, issue  # noqa: E402
from zaxbygraph import query as query_mod  # noqa: E402
from zaxbygraph.db import connect, init_schema  # noqa: E402
from zaxbygraph.sync import sync_repo  # noqa: E402


class TestHintInList(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        conn = connect(Path(cls._td.name) / "s.db")
        try:
            init_schema(conn)
            src = FakeGitHubSource()
            src.add_issue(issue(1, title="one", body="b", state="open"))
            sync_repo(conn, src, REPO)
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._td.cleanup()

    def _hints(self, sql: str):
        conn = connect(Path(self._td.name) / "s.db")
        try:
            out = query_mod.run_sql(conn, sql)
        finally:
            conn.close()
        return out, (out.get("hints") or [])

    def test_in_list_with_bad_literal_hints(self) -> None:
        out, hints = self._hints(
            "SELECT number FROM items WHERE kind IN ('pull_request')"
        )
        self.assertEqual(out["rows"], [], "HIN-PRECOND: statement must be zero-row")
        self.assertEqual([h["literal"] for h in hints], ["pull_request"], "HIN-1")
        self.assertEqual(hints[0]["column"], "items.kind", "HIN-1b")
        self.assertEqual(hints[0]["valid_values"], ["issue", "pr"], "HIN-1c")

    def test_in_list_mixed_valid_and_bad_hints_only_bad(self) -> None:
        # state 'open' exists but kind 'nope' filter keeps it zero-row.
        out, hints = self._hints(
            "SELECT number FROM items WHERE kind IN ( 'pr' , 'bogus' ) AND state = 'open'"
        )
        self.assertEqual(out["rows"], [], "HIN-PRECOND2")
        self.assertEqual([h["literal"] for h in hints], ["bogus"], "HIN-2")

    def test_in_list_all_valid_zero_row_has_no_hint(self) -> None:
        out, hints = self._hints(
            "SELECT number FROM items WHERE kind IN ('pr') AND state = 'closed'"
        )
        self.assertEqual(out["rows"], [], "HIN-PRECOND3")
        self.assertEqual(hints, [], "HIN-3")

    def test_in_list_non_literal_member_is_ignored(self) -> None:
        # A subquery / non-literal member must never crash or fabricate.
        out, hints = self._hints(
            "SELECT number FROM items WHERE kind IN (SELECT 'zzz')"
        )
        self.assertEqual(out["rows"], [], "HIN-PRECOND4")
        self.assertEqual(hints, [], "HIN-4")

    def test_reversed_equality_hints(self) -> None:
        out, hints = self._hints(
            "SELECT number FROM items WHERE 'pull_request' = kind"
        )
        self.assertEqual(out["rows"], [], "HIN-PRECOND6")
        self.assertEqual([h["literal"] for h in hints], ["pull_request"], "HIN-6")
        self.assertEqual(hints[0]["column"], "items.kind", "HIN-6b")

    def test_reversed_equality_valid_literal_no_hint(self) -> None:
        out, hints = self._hints(
            "SELECT number FROM items WHERE 'pr' = kind"
        )
        self.assertEqual(out["rows"], [], "HIN-PRECOND7")
        self.assertEqual(hints, [], "HIN-7")

    def test_double_equals_hints(self) -> None:
        out, hints = self._hints(
            "SELECT number FROM items WHERE kind == 'pull_request'"
        )
        self.assertEqual(out["rows"], [], "HIN-PRECOND8")
        self.assertEqual([h["literal"] for h in hints], ["pull_request"], "HIN-8")

    def test_text_inside_string_literals_is_never_scanned(self) -> None:
        # Critic finding (PR #98 final head): the text scan must read SQL
        # structure, not the contents of a string literal.
        for tag, sql in (
            ("HIN-9a", "SELECT number FROM items WHERE title LIKE '%kind = ''bug''%'"),
            ("HIN-9b", "SELECT number FROM items WHERE title = 'a.' OR title = ' = kind'"),
            ("HIN-9c", "SELECT number FROM items WHERE title = 'kind IN (''zzz'')'"),
            ("HIN-9d", "SELECT number FROM items WHERE title = 'state == ''nope'''"),
        ):
            out, hints = self._hints(sql)
            self.assertEqual(out["rows"], [], f"{tag}-PRECOND")
            self.assertEqual(hints, [], tag)

    def test_double_equals_reversed_and_aliased_hint(self) -> None:
        _, hints = self._hints("SELECT i.number FROM items i WHERE 'pull_request' == i.kind")
        self.assertEqual([h["literal"] for h in hints], ["pull_request"], "HIN-10")

    def test_equality_form_still_hints(self) -> None:
        _, hints = self._hints("SELECT number FROM items WHERE kind = 'pull_request'")
        self.assertEqual([h["literal"] for h in hints], ["pull_request"], "HIN-5")


if __name__ == "__main__":
    unittest.main()
