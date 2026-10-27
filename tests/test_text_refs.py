"""Acceptance checks for issue-trace 26-27-text-refs-and-link-stream
(issue #26, schema v9): text-derived references become store facts.

Authored at arm's length against the trace's acceptance table. NEW-SURFACE
module (C1-C4): it is deliberately absent at base d33cfbc so the module's
absence is the frozen base signature. C5 (test_reference_position_in_evidence)
is the DISCRIMINATING check: it drives only base symbols (store.ingest_item)
and its single-line failure marker EVIDENCE-POSITION-MISSING is the frozen
base evidence, so its leg copies this file onto the base tree.

Seams: C1-C4 drive the production sync path -- sync_repo with a bulk source
built on the fixtures_bulk PageShapedBulkSource pattern (plain item pages from
list_issues, children dicts from fetch_children) -- so every extraction runs
inside the real per-item ingest transaction, and C4's head_ref facts ride the
provided pull payload exactly as the GraphQL bulk path delivers it. C5 uses
the store seam (ingest_item) the way tests/test_revisions.py does, because
its base-copy leg must run against base symbols alone.

Hermetic: FakeGitHubSource-derived fakes only; no network, no gh.
"""

from __future__ import annotations

import re
import sqlite3
import unittest
from copy import deepcopy

from fixtures import (
    REPO,
    FakeGitHubSource,
    TempDBTest,
    comment,
    issue,
    pull,
    review,
)
from zaxbygraph.store import ingest_item
from zaxbygraph.sync import sync_repo


class _BulkTextSource(FakeGitHubSource):
    """The fixtures_bulk PageShapedBulkSource pattern: list_issues yields one
    page of plain item dicts, the bulk children arrive through fetch_children.
    The inherited per-item REST methods remain as the (never-fired here)
    fallback."""

    def list_issues(self, since: str | None):
        page = [
            deepcopy(rec)
            for rec in sorted(
                self.issues.values(), key=lambda r: (r["updated_at"], r["number"])
            )
            if since is None or rec["updated_at"] >= since
        ]
        if page:
            yield page

    def fetch_children(self, items) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for raw in items:
            n = int(raw["number"])
            children: dict = {
                "issue_comments": deepcopy(self.issue_comments.get(n, [])),
                "review_comments": deepcopy(self.review_comments.get(n, [])),
                "reviews": deepcopy(self.reviews.get(n, [])),
                "files": deepcopy(self.files.get(n, [])),
            }
            if n in self.pulls:
                children["pull"] = deepcopy(self.pulls[n])
            out[n] = children
        return out


class TextRefCheckBase(TempDBTest):
    """Row fetcher that turns a missing v9 surface into the check's frozen
    failure line instead of a bare traceback."""

    def rows(
        self, sql: str, params: tuple = (), fail_id: str = "TEXT-REFS"
    ) -> list[tuple]:
        try:
            return [tuple(r) for r in self.conn.execute(sql, params)]
        except sqlite3.OperationalError as exc:
            self.fail(f"{fail_id}: v9 surface missing at ingest ({exc})")
            raise  # unreachable: self.fail raises


#: The body carries one instance of each AC1 family: a Python frame, a
#: path:L:C token, a same-repo blob URL and a backticked path segment.
CRASH_BODY = "\n".join(
    [
        "Crash reproduces:",
        'File "src/frame/one.py", line 42, in main',
        "then src/mod/parse.py:10:5 raises",
        "source lives at https://github.com/acme/forgegate/blob/main/src/web/route.py",
        "config lives at `src/backtick/body.py`",
    ]
)


class PathRefTests(TextRefCheckBase):
    def test_path_refs_extracted(self) -> None:
        """C1 / 26-AC1: Python `File "<path>", line N` frames, `path:L:C`
        tokens, same-repo blob URLs and backticked path segments in item text
        become raw `item_path_refs` rows carrying their `origin` (`body`,
        `issue_comment:<id>`, `review_comment:<id>`, `review:<id>`) and a
        backticked flag. NEW-SURFACE: the module is absent at base d33cfbc;
        GREEN once schema v9 lands."""
        src = _BulkTextSource()
        src.add_pr(
            issue(5, title="crash report", kind="pr", body=CRASH_BODY),
            pull(5),
            files=[],
        )
        src.issue_comments[5].append(
            comment(5001, "patch `src/backtick/comment.py` please")
        )
        src.issues[5]["comments"] = 1
        src.review_comments[5].append(
            comment(
                6001,
                'File "src/frame/review_comment.py", line 7, in boom',
                author="bob",
            )
        )
        src.reviews[5] = [
            review(
                7001,
                "CHANGES_REQUESTED",
                body="fails at src/mod/review.py:3:1 after retry",
            )
        ]
        sync_repo(self.conn, src, REPO)

        rows = self.rows(
            "SELECT path, origin, backticked FROM item_path_refs"
            " WHERE repo = ? AND number = 5",
            (REPO,),
            "PATH-REFS-MISSING",
        )
        expected = [
            ("src/frame/one.py", "body", 0),
            ("src/mod/parse.py", "body", 0),
            ("src/web/route.py", "body", 0),
            ("src/backtick/body.py", "body", 1),
            ("src/backtick/comment.py", "issue_comment:5001", 1),
            ("src/frame/review_comment.py", "review_comment:6001", 0),
            ("src/mod/review.py", "review:7001", 0),
        ]
        for row in expected:
            self.assertIn(
                row,
                rows,
                "PATH-REFS-MISSING: expected a stored path ref %r, got rows %r"
                % (row, rows),
            )

    def test_foreign_blob_dropped(self) -> None:
        """C2 / 26-AC1's cross-repo rule: a foreign-repo blob URL produces NO
        path-ref row while a same-repo blob URL in the same text does.
        NEW-SURFACE: absent at base d33cfbc; GREEN once schema v9 lands."""
        src = _BulkTextSource()
        body = (
            "foreign https://github.com/other/repo/blob/main/src/foreign/other.py "
            "and local https://github.com/acme/forgegate/blob/main/src/ok.py end"
        )
        src.add_issue(issue(6, title="links", body=body))
        sync_repo(self.conn, src, REPO)

        rows = self.rows(
            "SELECT path, origin, backticked FROM item_path_refs"
            " WHERE repo = ? AND number = 6",
            (REPO,),
            "PATH-REFS-FOREIGN",
        )
        self.assertIn(
            ("src/ok.py", "body", 0),
            rows,
            "PATH-REFS-FOREIGN: the same-repo blob URL must produce a stored "
            "path ref, got rows %r" % (rows,),
        )
        self.assertNotIn(
            ("src/foreign/other.py", "body", 0),
            rows,
            "PATH-REFS-FOREIGN: a foreign-repo blob URL must produce NO path "
            "ref row",
        )


def _traceback_body(line_no: int, hex_addr: str) -> str:
    """One fenced traceback whose only run-varying constants are the frame
    line number and the hex pointer address."""
    return (
        "Crash:\n"
        "```\n"
        "Traceback (most recent call last):\n"
        f'  File "src/crash.py", line {line_no}, in main\n'
        f"TypeError: cannot cast pointer at {hex_addr}\n"
        "```\n"
    )


class SignatureTests(TextRefCheckBase):
    def test_error_signatures_masked(self) -> None:
        """C3 / 26-AC3: a fenced `TypeError: ...` block with hex addresses
        becomes an `item_signatures` row storing the exception name and a
        MASKED error line whose signature is identical across runs that
        differ in hex addresses and bare line numbers. NEW-SURFACE: absent at
        base d33cfbc; GREEN once schema v9 lands."""
        src = _BulkTextSource()
        src.add_issue(issue(3, title="crash a", body=_traceback_body(12, "0x7f3a1b2c")))
        src.add_issue(issue(4, title="crash b", body=_traceback_body(34, "0xdeadbeef99")))
        sync_repo(self.conn, src, REPO)

        rows3 = self.rows(
            "SELECT exception, error_line FROM item_signatures"
            " WHERE repo = ? AND number = 3",
            (REPO,),
            "SIGNATURES-MASKED",
        )
        rows4 = self.rows(
            "SELECT exception, error_line FROM item_signatures"
            " WHERE repo = ? AND number = 4",
            (REPO,),
            "SIGNATURES-MASKED",
        )
        self.assertEqual(
            len(rows3),
            1,
            "SIGNATURES-MASKED: expected one signature row for item 3, got %r"
            % (rows3,),
        )
        self.assertEqual(
            len(rows4),
            1,
            "SIGNATURES-MASKED: expected one signature row for item 4, got %r"
            % (rows4,),
        )
        exc3, line3 = rows3[0]
        exc4, line4 = rows4[0]
        self.assertEqual(
            exc3,
            "TypeError",
            "SIGNATURES-MASKED: the exception name must be stored verbatim, "
            "got %r" % (exc3,),
        )
        self.assertEqual(
            exc4,
            "TypeError",
            "SIGNATURES-MASKED: the exception name must be stored verbatim, "
            "got %r" % (exc4,),
        )
        self.assertTrue(
            line3,
            "SIGNATURES-MASKED: the masked error_line must be non-empty",
        )
        self.assertEqual(
            line3,
            line4,
            "SIGNATURES-MASKED: the masked error_line must be the same "
            "signature across differing hex/line constants: %r vs %r"
            % (line3, line4),
        )
        for raw in ("0x7f3a1b2c", "0xdeadbeef99", "line 12", "line 34"):
            self.assertNotIn(
                raw,
                line3,
                "SIGNATURES-MASKED: the masked error_line leaks the raw "
                "constant %r: %r" % (raw, line3),
            )


class BranchRefTests(TextRefCheckBase):
    def test_branch_ref_edge(self) -> None:
        """C4 / 26-AC4: a PR head_ref matching an issue-number pattern for
        exactly one stored issue yields one `branch_ref` edge (item->item,
        source='keyword'); zero-match and two-stored-match head_refs emit
        nothing. NEW-SURFACE: absent at base d33cfbc; GREEN once schema v9
        lands.

        The target issues sync FIRST so the branch resolution runs against
        stored items regardless of the implementer's lookup timing."""
        src = _BulkTextSource()
        src.add_issue(issue(26, title="add path refs"))
        src.add_issue(issue(27, title="add signatures"))
        sync_repo(self.conn, src, REPO)
        src.add_pr(
            issue(31, title="pr fix/26", kind="pr"),
            dict(pull(31), head={"ref": "fix/26-add-path-refs"}),
            files=[],
        )
        src.add_pr(
            issue(32, title="pr two numbers", kind="pr"),
            dict(pull(32), head={"ref": "fix/26-27"}),
            files=[],
        )
        src.add_pr(
            issue(33, title="pr ghost number", kind="pr"),
            dict(pull(33), head={"ref": "fix/999-ghost"}),
            files=[],
        )
        sync_repo(self.conn, src, REPO)

        rows = self.rows(
            "SELECT src_id, dst_id, source FROM edges"
            " WHERE repo = ? AND rel = 'branch_ref' ORDER BY src_id",
            (REPO,),
            "BRANCH-REF-EDGE-MISSING",
        )
        if rows != [("31", "26", "keyword")]:
            self.fail(
                "BRANCH-REF-EDGE-MISSING: exactly the single-match PR 31 "
                "must carry a branch_ref edge to issue 26 (item->item, "
                "source='keyword'); zero-match (fix/999-ghost) and "
                "two-match (fix/26-27) heads must emit nothing, got %r"
                % (rows,)
            )


class ReferencePositionTests(TempDBTest):
    """C5 / 26-AC5 (DISCRIMINATING): the mentions edge records the
    reference's location in evidence -- the EXACT body offset for a body
    reference ('body@6' below: '#2' starts at index 6 of 'alpha #2 omega')
    -- instead of the constant 'body #N', and the edge identity (the
    7-column key) is unchanged. The fixture is body-only, so this check
    accepts no 'title' alternative: a marker that labels every body
    reference 'title' must fail here (the title-location form is covered by
    a separate supporting test authored during implementation, not by this
    check). RED at base d33cfbc (the evidence constant carries no position),
    GREEN after the fix."""

    def test_reference_position_in_evidence(self) -> None:
        self.conn.execute("BEGIN")
        ingest_item(
            self.conn,
            REPO,
            issue(1, title="t", body="alpha #2 omega"),
            pull_raw=None,
            issue_comments=[],
            review_comments=[],
            reviews=[],
            files=[],
            include_patches=False,
        )
        self.conn.commit()
        # No source filter: exactly one mentions row for the pair across
        # every provenance stream IS the unchanged-edge-identity pin.
        rows = [
            tuple(r)
            for r in self.conn.execute(
                "SELECT evidence FROM edges WHERE repo = ?"
                " AND src_type = 'item' AND src_id = '1' AND rel = 'mentions'"
                " AND dst_type = 'item' AND dst_id = '2'",
                (REPO,),
            )
        ]
        self.assertEqual(
            len(rows),
            1,
            "EVIDENCE-POSITION-MISSING: expected exactly one mentions edge "
            "item:1 -> item:2, got %d rows" % len(rows),
        )
        evidence = rows[0][0] or ""
        # The '\b' rejects a longer run of digits: the offset must be exactly
        # 6, so 'body@60' fails. No 'title' alternative: the fixture is
        # body-only, so a marker that labels every body reference 'title'
        # must fail this check.
        if not re.search(r"body@6\b", evidence):
            self.fail(
                "EVIDENCE-POSITION-MISSING: mentions evidence is %r without "
                "the exact body reference location (expected 'body@6' for "
                "'#2' at body offset 6)" % (evidence,)
            )


if __name__ == "__main__":
    unittest.main()
