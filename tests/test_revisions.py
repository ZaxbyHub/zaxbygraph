"""Issue #25 (schema v8): evidence history — revision pre-images, tombstones,
history/as-of reads, coverage, and replay fidelity.

Test order note: the A→B→A wedge regression (test_revert_a_b_a_does_not_wedge)
is authored FIRST in this file — the issue requires it to land first because
an earlier content-hash-UNIQUE design wedged sync permanently on revert.

Store rules, read surface, CLI errors, coverage, migration semantics, the
replay/resolver contracts, and the append-only guardrail all live here so the
feature's truth table cannot drift between modules. Pure migration mechanics
(fresh/upgrade/repeat-init) live in tests/test_migrations.py per house style.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from fixtures import REPO, TempDBTest, comment, issue, review
from zaxbygraph.db import connect, init_schema
from zaxbygraph.query import item_as_of, item_history, status
from zaxbygraph.store import ingest_item
from zaxbygraph.sync import sync_repo


# Built from chr() so the byte counts are provable regardless of how a
# console/transport renders literal non-ASCII (issue #25 status bytes).
UNICODE_SAMPLE = chr(0xE9) + chr(0x1F5A5) + "emoji"


def rev_rows(conn, number=1, order="id"):
    return [
        tuple(r)
        for r in conn.execute(
            f"SELECT entity, entity_id, kind, origin, content, valid_from,"
            f" valid_to FROM revisions WHERE number = ? ORDER BY {order}",
            (number,),
        )
    ]


class RevisionRuleTests(TempDBTest):
    """Write rules inside ingest_item (D2 of the approved plan)."""

    def ingest(self, list_raw, **kwargs):
        self.conn.execute("BEGIN")
        ingest_item(
            self.conn,
            REPO,
            list_raw,
            pull_raw=kwargs.get("pull_raw"),
            issue_comments=kwargs.get("issue_comments", []),
            review_comments=kwargs.get("review_comments", []),
            reviews=kwargs.get("reviews", []),
            files=kwargs.get("files", []),
            include_patches=kwargs.get("include_patches", False),
            children_complete=kwargs.get("children_complete"),
        )
        self.conn.commit()

    def test_revert_a_b_a_does_not_wedge(self) -> None:
        """A→B→A completes and leaves exactly the displaced versions (the
        regression for the reproduced content-hash-UNIQUE wedge; authored
        first per the issue)."""
        self.ingest(issue(1, title="t", body="A", updated_at="2026-01-01T00:00:00Z"))
        self.ingest(issue(1, title="t", body="B", updated_at="2026-01-02T00:00:00Z"))
        self.ingest(issue(1, title="t", body="A", updated_at="2026-01-03T00:00:00Z"))
        self.ingest(issue(1, title="t", body="A", updated_at="2026-01-03T00:00:00Z"))
        bodies = [
            row for row in rev_rows(self.conn) if row[0] == "item_body"
        ]
        self.assertEqual(
            [(r[4], r[5], r[6]) for r in bodies],
            [
                ("A", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
                ("B", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"),
            ],
        )
        self.assertEqual(self.count("SELECT COUNT(*) FROM items"), 1)

    def test_body_edit_writes_pre_image(self) -> None:
        """AC1: the replaced body becomes one revision with GitHub-time
        windows; the title twin shares the mechanism and is pinned too."""
        self.ingest(issue(1, title="one", body="A", updated_at="2026-01-01T00:00:00Z"))
        self.assertEqual(self.count("SELECT COUNT(*) FROM revisions"), 0)
        self.ingest(issue(1, title="two", body="B", updated_at="2026-01-02T00:00:00Z"))
        self.assertEqual(
            rev_rows(self.conn),
            [
                ("item_title", None, None, "edit", "one",
                 "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
                ("item_body", None, None, "edit", "A",
                 "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
            ],
        )

    def test_unchanged_reingest_is_noop(self) -> None:
        """AC3: identical re-ingest (idempotence) writes no revision."""
        rec = issue(1, title="t", body="same", updated_at="2026-01-01T00:00:00Z")
        self.ingest(rec)
        self.ingest(rec)
        self.assertEqual(self.count("SELECT COUNT(*) FROM revisions"), 0)

    def test_null_and_empty_transitions(self) -> None:
        """Single NULL rule: any changed stored value writes a pre-image with
        content exactly the displaced value; None→None writes nothing."""
        base = dict(issue(1, title="t", body="text", updated_at="2026-01-01T00:00:00Z"))
        self.ingest(base)
        # None -> text: displaced value is NULL
        self.ingest({**base, "body": None, "updated_at": "2026-01-02T00:00:00Z"})
        # text can't follow None without changing; None -> None is a no-op
        self.ingest({**base, "body": None, "updated_at": "2026-01-03T00:00:00Z"})
        # "" -> NULL and NULL -> "" are real changes
        self.ingest({**base, "body": "", "updated_at": "2026-01-04T00:00:00Z"})
        rows = [r for r in rev_rows(self.conn) if r[0] == "item_body"]
        self.assertEqual(
            [(r[4], r[6]) for r in rows],
            [
                ("text", "2026-01-02T00:00:00Z"),
                (None, "2026-01-04T00:00:00Z"),
            ],
        )

    def test_tombstones_only_on_complete_sections(self) -> None:
        """AC4 with the production section keys: exactly-True gates
        issue_comments / review_comments / reviews; False, missing key, None,
        1, and "true" never tombstone; edits of surviving children write
        pre-images regardless."""
        ts = "2026-01-04T00:00:00Z"
        base = dict(issue(2, title="t", body="b", updated_at=ts))
        ic = comment(501, "c1", "2026-01-04T01:00:00Z")
        rc = {**comment(501, "rc1", "2026-01-04T01:00:00Z"),
              "html_url": f"https://github.com/{REPO}/pull/2#issuecomment-501"}
        rv = review(7, state="APPROVED", body="rev1")
        self.ingest(base, issue_comments=[ic], review_comments=[rc], reviews=[rv],
                    children_complete={"issue_comments": True,
                                       "review_comments": True, "reviews": True})
        self.assertEqual(self.count("SELECT COUNT(*) FROM revisions"), 0)
        gone = dict(base, updated_at="2026-01-05T00:00:00Z")
        self.ingest(gone, issue_comments=[], review_comments=[], reviews=[],
                    children_complete={"issue_comments": True,
                                       "review_comments": True, "reviews": True})
        tomb = rev_rows(self.conn, number=2)
        self.assertEqual(
            [(r[0], r[1], r[2], r[3], r[4]) for r in tomb],
            [
                ("comment", 501, "issue_comment", "deleted", "c1"),
                ("comment", 501, "review_comment", "deleted", "rc1"),
                ("review", 7, None, "deleted", "rev1"),
            ],
        )
        # Gate shapes: only exactly-True tombstones; truthy impostors
        # (1, "true") and falsy shapes (False, None) never do.
        for flag, expected in ((False, 0), (None, 0), (1, 0), ("true", 0),
                               ({"issue_comments": True}, 1)):
            self.ingest(dict(issue(3, title="t", body="b", updated_at=ts)),
                        issue_comments=[comment(9, "x", "2026-01-04T01:00:00Z")],
                        children_complete={"issue_comments": True})
            self.ingest(dict(issue(3, title="t", body="b",
                                   updated_at="2026-01-06T00:00:00Z")),
                        issue_comments=[], children_complete=flag)
            self.assertEqual(
                self.count(
                    "SELECT COUNT(*) FROM revisions WHERE number = 3"
                    " AND origin = 'deleted'"
                ),
                expected,
                f"flag {flag!r} produced the wrong tombstone count",
            )

    def test_incomplete_then_complete_boundary(self) -> None:
        """Documented boundary (D2.14): a degraded absence replaces children
        without a tombstone, so the later complete fetch has nothing left to
        tombstone. Pinned so a silent change to replacement semantics is
        visible."""
        base = dict(issue(4, title="t", body="b", updated_at="2026-01-01T00:00:00Z"))
        self.ingest(base, issue_comments=[comment(11, "c", "2026-01-01T01:00:00Z")],
                    children_complete={"issue_comments": True})
        self.ingest(dict(base, updated_at="2026-01-02T00:00:00Z"),
                    issue_comments=[], children_complete=None)
        self.assertEqual(self.count("SELECT COUNT(*) FROM comments WHERE number = 4"), 0)
        self.ingest(dict(base, updated_at="2026-01-03T00:00:00Z"),
                    issue_comments=[], children_complete={"issue_comments": True})
        self.assertEqual(
            self.count(
                "SELECT COUNT(*) FROM revisions WHERE number = 4"
                " AND origin = 'deleted'"
            ),
            0,
            "degraded absence destroyed the only pre-image; complete fetch"
            " cannot tombstone what it never saw",
        )

    def test_mixed_kinds_share_github_id(self) -> None:
        """The two comment kinds have separate id spaces; identity is
        (kind, github_id)."""
        base = dict(issue(5, title="t", body="b", updated_at="2026-01-01T00:00:00Z"))
        ic = comment(77, "issue-side", "2026-01-01T01:00:00Z")
        rc = {**comment(77, "review-side", "2026-01-01T01:00:00Z"),
              "html_url": f"https://github.com/{REPO}/pull/5#issuecomment-77"}
        self.ingest(base, issue_comments=[ic], review_comments=[rc],
                    children_complete={"issue_comments": True,
                                       "review_comments": True})
        # Editing only the review-comment side writes one pre-image.
        rc2 = {**rc, "body": "review-side-v2", "updated_at": "2026-01-02T00:00:00Z"}
        self.ingest(dict(base, updated_at="2026-01-02T00:00:00Z"),
                    issue_comments=[ic], review_comments=[rc2],
                    children_complete={"issue_comments": True,
                                       "review_comments": True})
        edits = [r for r in rev_rows(self.conn, number=5) if r[3] == "edit"]
        self.assertEqual(
            [(r[0], r[1], r[2], r[4]) for r in edits],
            [("comment", 77, "review_comment", "review-side")],
        )

    def test_review_edit_with_unchanged_item_timestamp(self) -> None:
        """Repeated review edits under an unchanged item updated_at: two
        pre-images with equal valid_to; valid_from comes from submitted_at
        with the item-created fallback for NULL."""
        base = dict(issue(6, title="t", body="b", updated_at="2026-01-01T00:00:00Z"))

        def rv(body):
            return {**review(8, state="APPROVED", body=body),
                    "submitted_at": "2026-01-01T02:00:00Z"}

        self.ingest(base, reviews=[rv("r1")])
        self.ingest(dict(base, updated_at="2026-01-01T00:00:00Z"),
                    reviews=[rv("r2")])
        self.ingest(dict(base, updated_at="2026-01-01T00:00:00Z"),
                    reviews=[rv("r3")])
        edits = [r for r in rev_rows(self.conn, number=6) if r[0] == "review"]
        self.assertEqual(
            [(r[4], r[5], r[6]) for r in edits],
            [
                ("r1", "2026-01-01T02:00:00Z", "2026-01-01T00:00:00Z"),
                ("r2", "2026-01-01T02:00:00Z", "2026-01-01T00:00:00Z"),
            ],
        )

    def test_duplicate_payload_rows_collapse(self) -> None:
        """Duplicate pagination rows canonicalize last-writer-wins before the
        diff: one comment recorded twice with different bodies writes one
        pre-image of the ORIGINAL stored body, not two."""
        base = dict(issue(7, title="t", body="b", updated_at="2026-01-01T00:00:00Z"))
        self.ingest(base, issue_comments=[comment(21, "orig", "2026-01-01T01:00:00Z")],
                    children_complete={"issue_comments": True})
        dup = comment(21, "changed", "2026-01-02T00:00:00Z")
        self.ingest(dict(base, updated_at="2026-01-02T00:00:00Z"),
                    issue_comments=[dup, dup], children_complete={"issue_comments": True})
        edits = [r for r in rev_rows(self.conn, number=7) if r[3] == "edit"]
        self.assertEqual([(r[1], r[4]) for r in edits], [(21, "orig")])

    def test_gone_item_writes_no_tombstones(self) -> None:
        """The 404 path keeps stored children and the all-False completeness
        map (issue #23): no tombstones, children retained."""
        src_items = issue(8, title="t", body="b", updated_at="2026-01-01T00:00:00Z")
        self.ingest(src_items, issue_comments=[comment(31, "c", "2026-01-01T01:00:00Z")],
                    children_complete={"issue_comments": True})
        from zaxbygraph.sync import _ingest_and_mark_gone

        list_raw = issue(8, title="t", body="b",
                         updated_at="2026-01-01T00:00:00Z")
        ingested = _ingest_and_mark_gone(
            self.conn, REPO, list_raw, 8, 404,
            advance_watermark=True, include_patches=False, jsonl_handle=None,
        )
        self.assertEqual(ingested, 0)
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM comments WHERE number = 8"), 1
        )
        self.assertEqual(
            self.count(
                "SELECT COUNT(*) FROM revisions WHERE number = 8"
                " AND origin = 'deleted'"
            ),
            0,
        )


class RevisionTransactionTests(TempDBTest):
    def test_revision_failure_aborts_item_and_watermark(self) -> None:
        """A failing revision write rolls the whole item ingest back — the
        item row and the watermark move together or not at all."""
        self.conn.execute("BEGIN")
        ingest_item(self.conn, REPO,
                    issue(1, title="t", body="A", updated_at="2026-01-01T00:00:00Z"),
                    pull_raw=None, issue_comments=[], review_comments=[],
                    reviews=[], files=[], include_patches=False)
        self.conn.commit()
        before = self.conn.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()
        # Sabotage the append-only target: CHECK violation on origin.
        self.conn.execute(
            "CREATE TRIGGER revisions_block BEFORE INSERT ON revisions"
            " BEGIN SELECT RAISE(ABORT, 'sabotaged'); END"
        )
        with self.assertRaises(Exception):
            self.conn.execute("BEGIN")
            try:
                ingest_item(self.conn, REPO,
                            issue(1, title="t2", body="B",
                                  updated_at="2026-01-02T00:00:00Z"),
                            pull_raw=None, issue_comments=[], review_comments=[],
                            reviews=[], files=[], include_patches=False)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        self.conn.execute("DROP TRIGGER revisions_block")
        self.assertEqual(
            tuple(self.conn.execute(
                "SELECT title, body FROM items WHERE number = 1"
            ).fetchone()),
            ("t", "A"),
        )
        after = self.conn.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()
        self.assertEqual(tuple(before), tuple(after))


class RevisionReadTests(TempDBTest):
    def _seed(self) -> None:
        self.conn.execute("BEGIN")
        ingest_item(self.conn, REPO,
                    issue(1, title="v1", body="B1", updated_at="2026-01-01T00:00:00Z"),
                    pull_raw=None,
                    issue_comments=[comment(501, "c1", created_at="2026-01-01T01:00:00Z")],
                    review_comments=[], reviews=[], files=[], include_patches=False,
                    children_complete={"issue_comments": True})
        self.conn.commit()
        self.conn.execute("BEGIN")
        ingest_item(self.conn, REPO,
                    issue(1, title="v2", body="B2", updated_at="2026-01-02T00:00:00Z"),
                    pull_raw=None, issue_comments=[], review_comments=[],
                    reviews=[], files=[], include_patches=False,
                    children_complete={"issue_comments": True})
        self.conn.commit()

    def test_history_lists_versions_ordered(self) -> None:
        self._seed()
        data = item_history(self.conn, 1, repo=REPO)
        self.assertEqual(
            [(r["entity"], r["origin"], r["content"]) for r in data["revisions"]],
            [
                ("item_title", "edit", "v1"),
                ("item_body", "edit", "B1"),
                ("comment", "deleted", "c1"),
            ],
        )
        empty = item_history(self.conn, 2, repo=REPO)
        self.assertIsNone(empty)

    def test_item_as_of(self) -> None:
        self._seed()
        from zaxbygraph.query import _parse_instant

        mid = item_as_of(self.conn, 1, _parse_instant("2026-01-01T12:00:00Z"), repo=REPO)
        self.assertEqual(mid["title"], "v1")
        self.assertEqual(mid["body"], "B1")
        self.assertEqual(mid["title_version"]["version_source"], "revision")
        # T at the exact valid_to belongs to the next version.
        edge = item_as_of(self.conn, 1, _parse_instant("2026-01-02T00:00:00Z"), repo=REPO)
        self.assertEqual(edge["body"], "B2")
        self.assertEqual(edge["body_version"]["version_source"], "live")
        # The deleted comment exists at T inside its tombstone window...
        early = item_as_of(self.conn, 1, _parse_instant("2026-01-01T06:00:00Z"), repo=REPO)
        self.assertEqual([c["github_id"] for c in early["comments"]], [501])
        # The live row is gone (deleted upstream); the version at T is the
        # tombstone's recorded pre-image.
        self.assertEqual(early["comments"][0]["version_source"], "revision")
        self.assertEqual(early["comments"][0]["body"], "c1")
        # ...and is absent after the deleting fetch, reported honestly.
        late = item_as_of(self.conn, 1, _parse_instant("2026-01-02T06:00:00Z"), repo=REPO)
        self.assertEqual(late["comments"], [])
        self.assertIn(
            ("comment", 501),
            [(a["entity"], a["entity_id"]) for a in late["absent_entities"]],
        )
        self.assertEqual(late["absent_entities"][0]["origin"], "deleted")
        # Basis discloses every rule.
        self.assertIn("windows", late["as_of_basis"])
        self.assertIn("GitHub times", late["as_of_basis"]["windows"])

    def test_as_of_window_gap_and_first_sighted_edit(self) -> None:
        """Pre-first-revision gaps and first-sighted edited entities are
        absent before their current-version start, never back-dated."""
        self.conn.execute("BEGIN")
        ingest_item(self.conn, REPO,
                    issue(2, title="t", body="b",
                          updated_at="2026-01-05T00:00:00Z",
                          created_at="2026-01-01T00:00:00Z"),
                    pull_raw=None,
                    issue_comments=[{**comment(9, "edited-later",
                                              created_at="2026-01-01T01:00:00Z"),
                                     "updated_at": "2026-01-04T00:00:00Z"}],
                    review_comments=[], reviews=[], files=[], include_patches=False,
                    children_complete={"issue_comments": True})
        self.conn.commit()
        from zaxbygraph.query import _parse_instant

        # created 01-01, first sighted with updated_at 01-04: the current
        # text has no evidence before 01-04.
        at = item_as_of(self.conn, 2, _parse_instant("2026-01-02T00:00:00Z"), repo=REPO)
        self.assertEqual(at["comments"], [])
        self.assertEqual(at["absent_entities"][0]["origin"], "no_version")
        ok = item_as_of(self.conn, 2, _parse_instant("2026-01-04T00:00:00Z"), repo=REPO)
        self.assertEqual([c["body"] for c in ok["comments"]], ["edited-later"])
        self.assertEqual(ok["comments"][0]["version_source"], "live")

    def test_as_of_mixed_offset_boundary(self) -> None:
        """A non-UTC --as-of that lands exactly on a window boundary compares
        as UTC instants."""
        self._seed()
        from zaxbygraph.query import _parse_instant

        # 2026-01-02T00:00:00+02:00 == 2026-01-01T22:00:00Z: inside rev 1.
        at = item_as_of(self.conn, 1, _parse_instant("2026-01-02T00:00:00+02:00"), repo=REPO)
        self.assertEqual(at["body"], "B1")

    def test_plain_item_unchanged(self) -> None:
        """AC6: the no-flag item output stays byte-identical to the base
        shape. The golden was captured at base 548c07a (trace
        repro/item_golden_base.json); the per-run temp db value is the only
        normalization, and the fixture is never regenerated here."""
        self.conn.execute("BEGIN")
        ingest_item(self.conn, REPO,
                    issue(1, title="golden one", body="GOLDEN-BODY-ONE",
                          updated_at="2026-01-01T00:30:00Z",
                          created_at="2026-01-01T00:00:00Z",
                          labels=[{"name": "bug", "color": "ff0000"}]),
                    pull_raw=None,
                    issue_comments=[comment(501, "GOLDEN-COMMENT",
                                            created_at="2026-01-01T01:00:00Z")],
                    review_comments=[], reviews=[], files=[], include_patches=False,
                    children_complete={"comments": True})
        self.conn.commit()
        from zaxbygraph.cli import build_parser, cmd_item

        args = build_parser().parse_args(
            ["item", "1", "--format", "json", "--db", str(self.db_path),
             "--repo", REPO]
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cmd_item(args), 0)
        payload = json.loads(buf.getvalue())
        payload["db"] = "<DB>"
        actual = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        with open(Path(__file__).parent / "revisions_item_golden.expected.json") as fh:
            golden = fh.read()
        self.assertEqual(actual, golden)

    def test_history_cli_flags_and_truncation(self) -> None:
        self._seed()
        from zaxbygraph.cli import build_parser, cmd_item

        def run(*extra):
            args = build_parser().parse_args(
                ["item", "1", "--db", str(self.db_path), "--repo", REPO,
                 "--format", "json", *extra]
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cmd_item(args)
            return code, json.loads(buf.getvalue())

        code, out = run("--history", "--max-body-chars", "1")
        self.assertEqual(code, 0)
        self.assertEqual(
            [r["content"] for r in out["data"]["revisions"]], ["v", "B", "c"]
        )
        self.assertTrue(out["data"]["revisions"][0]["truncated"])

        code, out = run("--history", "--as-of", "2026-01-01T00:00:00Z")
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "bad_flags")

        code, out = run("--as-of", "not-a-time")
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "bad_as_of")

        args = build_parser().parse_args(
            ["item", "404", "--db", str(self.db_path), "--repo", REPO,
             "--format", "json", "--history"]
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cmd_item(args), 1)


class RevisionCoverageTests(TempDBTest):
    def test_status_counts_bytes_and_scoping(self) -> None:
        self.conn.execute("BEGIN")
        ingest_item(self.conn, REPO,
                    issue(1, title="t", body="A", updated_at="2026-01-01T00:00:00Z"),
                    pull_raw=None, issue_comments=[], review_comments=[],
                    reviews=[], files=[], include_patches=False)
        self.conn.commit()
        self.conn.execute("BEGIN")
        ingest_item(self.conn, REPO,
                    issue(1, title="t", body=UNICODE_SAMPLE,
                          updated_at="2026-01-02T00:00:00Z"),
                    pull_raw=None, issue_comments=[], review_comments=[],
                    reviews=[], files=[], include_patches=False)
        self.conn.commit()
        cov = status(self.conn, repo=REPO)["coverage"]
        self.assertEqual(cov["revision_rows"], 1)
        # Bytes count the DISPLACED content (the pre-image "A"), not the
        # incoming multi-byte body.
        self.assertEqual(cov["revision_bytes"], 1)
        # NULL content counts as zero bytes, not NULL.
        self.conn.execute(
            "INSERT INTO revisions(repo, number, entity, origin, content,"
            " valid_from, valid_to) VALUES (?, 1, 'item_body', 'edit', NULL,"
            " '2026-01-02T00:00:00Z', '2026-01-03T00:00:00Z')",
            (REPO,),
        )
        cov = status(self.conn, repo=REPO)["coverage"]
        self.assertEqual(cov["revision_rows"], 2)
        self.assertEqual(cov["revision_bytes"], 1, "NULL content adds 0 bytes")

    def test_pre_v8_reads_degrade_with_notes(self) -> None:
        """A v7 store keeps v7 features, reports index_stale honestly, and
        the revision surface degrades with an explicit note instead of
        fabricating history."""
        self.conn.execute("BEGIN")
        ingest_item(self.conn, REPO,
                    issue(1, title="t", body="b", updated_at="2026-01-01T00:00:00Z"),
                    pull_raw=None, issue_comments=[], review_comments=[],
                    reviews=[], files=[], include_patches=False)
        self.conn.commit()
        self.conn.execute("PRAGMA user_version = 7")
        try:
            hist = item_history(self.conn, 1, repo=REPO)
            self.assertEqual(hist["revisions"], [])
            self.assertIn("history_note", hist)
            asof = item_as_of(
                self.conn, 1,
                __import__("datetime").datetime(
                    2026, 1, 1, 12, 0, tzinfo=__import__("datetime").timezone.utc
                ),
                repo=REPO,
            )
            self.assertIn("history_note", asof)
            self.assertIn("GitHub times", asof["as_of_basis"]["windows"])
            cov = status(self.conn, repo=REPO)["coverage"]
            self.assertIsNone(cov["revision_rows"])
            self.assertIsNone(cov["revision_bytes"])
            self.assertIsNotNone(cov["null_v7_rows"])
        finally:
            self.conn.execute("PRAGMA user_version = 8")


class AppendOnlyGuardTests(TempDBTest):
    def test_no_update_or_delete_targets_revisions(self) -> None:
        """Guardrail: the ingest path never rewrites history. Only doctor's
        consolidation (a whole-snapshot replacement) may touch revisions."""
        src = Path(__file__).parent.parent / "src" / "zaxbygraph"
        offenders = []
        for path in src.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                up = line.upper()
                if ("UPDATE REVISIONS" in up or "DELETE FROM REVISIONS" in up):
                    if path.name == "doctor.py" and "DELETE FROM {table}" in line:
                        continue  # the sanctioned copy-loop delete
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [])


def _bundle(number, item_payload, **extra):
    payload = {
        "number": number,
        "item": item_payload,
        "pull": None,
        "issue_comments": [],
        "review_comments": [],
        "reviews": [],
        "files": [],
        "timeline": [],
        "children_complete": {name: True for name in (
            "issue_comments", "review_comments", "reviews", "files",
            "pull", "timeline")},
        "gone": False,
        "gone_status": None,
    }
    payload.update(extra)
    return {"resource": "item_bundle", "payload": payload}


def _write_sidecar(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return path


class ReplayFidelityTests(TempDBTest):
    """Replay/resolver contracts (D5): per-version children, adjacency-aware
    dedup, gone-metadata equality, legacy fail-closed, targeted cursor."""

    def _replay(self, records, *, numbers=None):
        from replay import JsonlReplaySource

        sidecar = _write_sidecar(
            Path(self._td.name) / "sidecar" / "events.jsonl", records
        )
        replay_conn = connect(Path(self._td.name) / "replay.db")
        init_schema(replay_conn)
        try:
            src = JsonlReplaySource(sidecar)
            result = sync_repo(replay_conn, src, REPO, numbers=numbers)
            return replay_conn, result
        except Exception:
            replay_conn.close()
            raise

    def test_two_versions_keep_their_own_children(self) -> None:
        """One number, two versions with DIFFERENT child sets, one recording:
        replay ingests both versions each with its own children — the
        number-keyed provided_map must not collapse them."""
        v1 = dict(issue(1, title="v1", body="b", updated_at="2026-01-01T00:00:00Z"))
        v2 = dict(issue(1, title="v2", body="b", updated_at="2026-01-02T00:00:00Z"))
        records = [
            _bundle(1, v1, issue_comments=[comment(1, "old", "2026-01-01T01:00:00Z")]),
            _bundle(1, v2, issue_comments=[comment(2, "new", "2026-01-02T01:00:00Z")]),
        ]
        conn, _ = self._replay(records)
        try:
            rows = [
                tuple(r)
                for r in conn.execute(
                    "SELECT entity, entity_id, origin, content FROM revisions"
                    " ORDER BY id"
                )
            ]
            self.assertIn(("comment", 1, "deleted", "old"), rows)
            self.assertNotIn(("comment", 2, "deleted", "new"), rows)
        finally:
            conn.close()

    def test_byte_identical_items_different_children(self) -> None:
        """Same item bytes and timestamp, different children, different
        pages: page-scoped identity serves each page its own children."""
        same = dict(issue(1, title="t", body="b", updated_at="2026-01-01T00:00:00Z"))
        records = [
            _bundle(1, same,
                    issue_comments=[comment(1, "first", created_at="2026-01-01T01:00:00Z")]),
            _bundle(1, same,
                    issue_comments=[comment(2, "second", created_at="2026-01-01T01:00:00Z")]),
            _bundle(1, same, issue_comments=[]),
        ]
        conn, _ = self._replay(records)
        try:
            tomb = [
                tuple(r)
                for r in conn.execute(
                    "SELECT entity_id, origin FROM revisions WHERE number = 1"
                )
            ]
            self.assertEqual(
                sorted(tomb), [(1, "deleted"), (2, "deleted")],
                "each version must ingest with its OWN children",
            )
        finally:
            conn.close()

    def test_nonconsecutive_revert_survives_adjacent_dedup(self) -> None:
        """A byte-identical A→B→A keeps all three observations (adjacent-only
        collapse); an adjacent exact duplicate collapses to one."""
        a = dict(issue(1, title="t", body="A", updated_at="2026-01-01T00:00:00Z"))
        b = dict(issue(1, title="t", body="B", updated_at="2026-01-02T00:00:00Z"))
        a_back = dict(issue(1, title="t", body="A", updated_at="2026-01-03T00:00:00Z"))
        a2 = dict(issue(2, title="t", body="A", updated_at="2026-01-01T00:00:00Z"))
        records = [
            _bundle(1, a), _bundle(1, b), _bundle(1, a_back),
            _bundle(2, a2), _bundle(2, dict(a2)),
        ]
        conn, _ = self._replay(records)
        try:
            self.assertEqual(
                tuple(conn.execute(
                    "SELECT body FROM items WHERE number = 1"
                ).fetchone()),
                ("A",),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM revisions WHERE number = 1"
                ).fetchone()[0],
                2,
                "nonconsecutive A→B→A must keep both pre-images",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM revisions WHERE number = 2"
                ).fetchone()[0],
                0,
                "adjacent exact duplicates must collapse to one observation",
            )
        finally:
            conn.close()

    def test_gone_metadata_differs_not_collapsed(self) -> None:
        """Two gone bundles differing only in note/verdict stay distinct."""
        g1 = dict(issue(1, title="t", body="b", updated_at="2026-01-01T00:00:00Z"))
        records = [
            _bundle(1, g1),
            _bundle(1, g1, gone=True, gone_status=404,
                    gone_note="HTTP 404 while fetching", gone_verdict="deleted"),
            _bundle(1, g1, gone=True, gone_status=410,
                    gone_note="HTTP 410 while fetching", gone_verdict="transferred"),
        ]
        conn, result = self._replay(records)
        try:
            # The first ingest stores the item; the latest observation is the
            # 410/transferred gone bundle, so the final state is transferred.
            self.assertEqual(
                tuple(conn.execute(
                    "SELECT state FROM items WHERE number = 1"
                ).fetchone()),
                ("transferred",),
            )
        finally:
            conn.close()

    def test_legacy_bundle_fails_closed_through_resolver(self) -> None:
        """Old bundles (no children_complete) get an injected all-False map
        from the READER, and the real _resolve_children preserves it — replay
        never fabricates tombstones."""
        from replay import JsonlReplaySource
        from zaxbygraph.sync import _resolve_children

        legacy = {
            "number": 1,
            "item": dict(issue(1, title="t", body="b",
                               updated_at="2026-01-01T00:00:00Z")),
            "pull": None,
            "issue_comments": [comment(1, "c", "2026-01-01T01:00:00Z")],
            "review_comments": [],
            "reviews": [],
            "files": [],
            "timeline": [],
            "gone": False,
            "gone_status": None,
        }
        sidecar = _write_sidecar(
            Path(self._td.name) / "legacy" / "events.jsonl",
            [{"resource": "item_bundle", "payload": legacy}],
        )
        src = JsonlReplaySource(sidecar)
        page = list(src.list_issues(None))
        list_raw = page[0][0]
        provided = src.fetch_children(page[0])[1]
        self.assertIn("children_complete", provided)
        self.assertFalse(any(provided["children_complete"].values()))
        resolved = _resolve_children(src, list_raw, "issue", False,
                                     provided=provided)
        complete = resolved[-1]
        self.assertFalse(complete["issue_comments"])
        # And through a full sync: no tombstone can exist for the section.
        conn, _ = self._replay([{"resource": "item_bundle", "payload": legacy}])
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM revisions WHERE origin = 'deleted'"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_recorded_map_applied_verbatim(self) -> None:
        """A recorded False on a section the payload answered is honored (the
        resolver neither upgrades nor re-fetches it)."""
        from replay import JsonlReplaySource
        from zaxbygraph.sync import _resolve_children

        rec = {
            "number": 1,
            "item": dict(issue(1, title="t", body="b",
                               updated_at="2026-01-01T00:00:00Z")),
            "pull": None,
            "issue_comments": [comment(1, "c", "2026-01-01T01:00:00Z")],
            "review_comments": [],
            "reviews": [],
            "files": [],
            "timeline": [],
            "children_complete": {"issue_comments": True, "review_comments": True,
                                  "reviews": True, "files": True,
                                  "pull": True, "timeline": True},
            "gone": False,
            "gone_status": None,
        }
        sidecar = _write_sidecar(
            Path(self._td.name) / "rec" / "events.jsonl",
            [{"resource": "item_bundle", "payload": rec}],
        )
        src = JsonlReplaySource(sidecar)
        page = list(src.list_issues(None))
        provided = src.fetch_children(page[0])[1]
        resolved = _resolve_children(src, page[0][0], "issue", False,
                                     provided=provided)
        self.assertTrue(resolved[-1]["issue_comments"])
        # Degraded recording: same payload, recorded False — no fallback
        # fires (the REST stubs would raise a clean GitHubError).
        rec["children_complete"] = {name: False for name in rec["children_complete"]}
        sidecar2 = _write_sidecar(
            Path(self._td.name) / "rec2" / "events.jsonl",
            [{"resource": "item_bundle", "payload": rec}],
        )
        src2 = JsonlReplaySource(sidecar2)
        page2 = list(src2.list_issues(None))
        provided2 = src2.fetch_children(page2[0])[1]
        resolved2 = _resolve_children(src2, page2[0][0], "issue", False,
                                      provided=provided2)
        self.assertFalse(resolved2[-1]["issue_comments"])

    def test_targeted_replay_uses_latest_children(self) -> None:
        """`sync --numbers` over a replay source: latest shell, latest
        children, watermark untouched, gone metadata preserved."""
        v1 = dict(issue(1, title="v1", body="b", updated_at="2026-01-01T00:00:00Z"))
        v2 = dict(issue(1, title="v2", body="b", updated_at="2026-01-02T00:00:00Z"))
        records = [
            _bundle(1, v1, issue_comments=[comment(1, "old", "2026-01-01T01:00:00Z")]),
            _bundle(1, v2, issue_comments=[comment(2, "new", "2026-01-02T01:00:00Z")]),
        ]
        # Seed the store with the FIRST version only, then targeted-refresh.
        conn = connect(Path(self._td.name) / "targeted.db")
        init_schema(conn)
        try:
            conn.execute("BEGIN")
            ingest_item(conn, REPO, v1, pull_raw=None,
                        issue_comments=[comment(1, "old",
                                                created_at="2026-01-01T01:00:00Z")],
                        review_comments=[], reviews=[], files=[],
                        include_patches=False,
                        children_complete={"issue_comments": True})
            conn.commit()
            from replay import JsonlReplaySource

            sidecar = _write_sidecar(
                Path(self._td.name) / "targeted" / "events.jsonl", records
            )
            src = JsonlReplaySource(sidecar)
            result = sync_repo(conn, src, REPO, numbers=[1])
            self.assertGreaterEqual(result["ingested"], 1)
            self.assertEqual(
                tuple(conn.execute(
                    "SELECT title FROM items WHERE number = 1"
                ).fetchone()),
                ("v2",),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM comments WHERE number = 1"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM revisions WHERE number = 1"
                    " AND origin = 'deleted'"
                ).fetchone()[0],
                1,
                "the old comment's tombstone is recorded history",
            )
        finally:
            conn.close()

    def test_numbers_refresh_captures_revisions_watermark_untouched(self) -> None:
        """A targeted refresh captures revisions through a normal source
        while the watermark stays exactly as found (existing --numbers
        contract, now with history)."""
        self.conn.execute("BEGIN")
        ingest_item(self.conn, REPO,
                    issue(1, title="v1", body="A", updated_at="2026-01-01T00:00:00Z"),
                    pull_raw=None, issue_comments=[], review_comments=[],
                    reviews=[], files=[], include_patches=False)
        self.conn.commit()
        watermark = self.conn.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)
        ).fetchone()[0]

        class _OneShot:
            def list_issues(self, since):
                yield [dict(issue(1, title="v2", body="B",
                                  updated_at="2026-01-02T00:00:00Z"))]

            def fetch_children(self, items):
                return {}

        sync_repo(self.conn, _OneShot(), REPO, numbers=[1])
        self.assertEqual(
            [
                tuple(r)
                for r in self.conn.execute(
                    "SELECT entity, origin FROM revisions WHERE number = 1"
                )
            ],
            [("item_title", "edit"), ("item_body", "edit")],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)
            ).fetchone()[0],
            watermark,
        )


if __name__ == "__main__":
    unittest.main()
