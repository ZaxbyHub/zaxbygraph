"""Supporting checks for issue #26 (schema v9, text-derived references).

These complement the frozen acceptance checks (tests/test_text_refs.py):
the surfaces the plan promises that the frozen set does not pin — the v8->v9
backfill, frame-function extraction, title-location evidence, and backfill
idempotence. Authored by the implementer; the frozen checks remain the
acceptance contract.
"""

from __future__ import annotations

import json
import sqlite3
import unittest

from fixtures import REPO, TempDBTest, issue, ts
from zaxbygraph.db import CURRENT_USER_VERSION, connect, init_schema
from zaxbygraph.extract import parse_error_signatures


def _v8_store(path) -> sqlite3.Connection:
    """A store at exactly schema v8: run the forward chain minus the v9/v10
    migrations by building fresh and stamping back (the additive v9/v10
    tables are simply ignored by a v8 build — the reverse stamp proves the
    migration path, not the forward one)."""
    conn = connect(path)
    init_schema(conn)
    conn.execute(f"PRAGMA user_version = {CURRENT_USER_VERSION - 2}")
    conn.commit()
    return conn


class MigrationBackfillTests(TempDBTest):
    """migrate_v8_to_v9 backfills every derivable surface from stored
    raw_json; a second run is idempotent."""

    def _v8_with_content(self) -> None:
        self.conn.execute("BEGIN")
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " created_at, updated_at, head_ref, raw_json)"
            " VALUES (1, ?, 1, 'issue', 't', ?, 'open', ?, ?, NULL, ?)",
            (
                REPO,
                "Crash:\n```\nTypeError: boom at 0xdead\n```\nfixes #2 dup of #3",
                ts(0),
                ts(1),
                '{"number": 1, "title": "t", "body": "Crash:\\n```\\nTypeError:'
                ' boom at 0xdead\\n```\\nfixes #2 dup of #3", "user":'
                ' {"login": "a"}}',
            ),
        )
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " created_at, updated_at, raw_json) VALUES (2, ?, 2, 'issue',"
            " 'target', NULL, 'open', ?, ?, '{}')",
            (REPO, ts(0), ts(1)),
        )
        self.conn.commit()

    def test_v8_to_v9_backfills_derivable_surfaces(self) -> None:
        """Re-running the forward chain over a v8 store populates path refs,
        signatures, keyword edges with positions, and the forward branch_ref;
        running the chain AGAIN stores nothing new (idempotent backfill)."""
        body = (
            "Crash in `src/crash/boom.py`:\n"
            "```\n"
            "TypeError: boom at 0xdead\n"
            "```\n"
            "fixes #2 dup of #3"
        )
        self.conn.execute("BEGIN")
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " created_at, updated_at, head_ref, raw_json)"
            " VALUES (1, ?, 1, 'issue', 't', ?, 'open', ?, ?, NULL, ?)",
            (
                REPO,
                body,
                ts(0),
                ts(1),
                json.dumps({"number": 1, "title": "t", "body": body}),
            ),
        )
        self.conn.commit()
        from zaxbygraph.db import MIGRATIONS

        # A fresh store is born at the current version; the migration chain
        # must actually RUN here, so stamp the store back to v8 first (the
        # v9/v10 tables are additive -- a v8-stamped build ignores them, which
        # is exactly the migrated-store shape the backfill must serve).
        self.conn.execute(f"PRAGMA user_version = {CURRENT_USER_VERSION - 2}")
        self.conn.commit()
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        for target, migrate in MIGRATIONS:
            if version >= target:
                continue
            self.conn.execute("BEGIN IMMEDIATE")
            migrate(self.conn)
            self.conn.execute(f"PRAGMA user_version = {target}")
            self.conn.commit()
            version = target
        self.assertEqual(version, CURRENT_USER_VERSION)
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM item_path_refs WHERE number = 1"),
            1,
            "BACKFILL-PATH-MISSING: the backticked path was not backfilled",
        )
        sigs = self.conn.execute(
            "SELECT exception, error_line FROM item_signatures WHERE number = 1"
        ).fetchall()
        self.assertEqual(
            [(r["exception"], r["error_line"]) for r in sigs],
            [("TypeError", "TypeError: boom at <hex>")],
            "BACKFILL-SIGNATURE-MISMATCH: masked signature wrong",
        )
        evidence = {
            (r["rel"], r["dst_id"]): r["evidence"]
            for r in self.conn.execute(
                "SELECT rel, dst_id, evidence FROM edges WHERE src_type = 'item'"
                " AND src_id = '1' AND source = 'keyword'"
            ).fetchall()
        }
        # Offsets index the ORIGINAL body; '#' of '#2' and '#3' respectively.
        self.assertEqual(
            evidence[("closes_keyword", "2")],
            f"body@{body.find('#2')} closing keyword",
        )
        self.assertEqual(evidence[("duplicate_keyword", "3")], f"body@{body.find('#3')}")
        # Live-path parity (review round 1): the CLOSED #2 is not also a
        # mention; the duplicate-referenced #3 is (mentions and duplicates
        # are independent streams).
        self.assertNotIn(("mentions", "2"), evidence)
        self.assertEqual(evidence[("mentions", "3")], f"body@{body.find('#3')}")
        # Backfill idempotence: rerun the v9 migration; row counts must not
        # grow (the PKs dedup; keyword edges evidence-upsert to the same).
        from zaxbygraph.db import migrate_v8_to_v9

        before = self.count("SELECT COUNT(*) FROM item_path_refs")
        migrate_v8_to_v9(self.conn)
        self.assertEqual(self.count("SELECT COUNT(*) FROM item_path_refs"), before)

    def test_forward_branch_ref_backfilled_from_head(self) -> None:
        """A stored PR whose head_ref carries exactly one stored issue number
        gains its branch_ref during the v9 backfill (forward only)."""
        self.conn.execute("BEGIN")
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " created_at, updated_at, raw_json)"
            " VALUES (2, ?, 2, 'issue', 'target', NULL, 'open', ?, ?, '{}')",
            (REPO, ts(0), ts(1)),
        )
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " created_at, updated_at, head_ref, raw_json)"
            " VALUES (5, ?, 5, 'pr', 'pr', NULL, 'open', ?, ?, 'issue-2-fix', '{}')",
            (REPO, ts(0), ts(1)),
        )
        self.conn.commit()
        from zaxbygraph.db import migrate_v8_to_v9

        migrate_v8_to_v9(self.conn)
        rows = self.conn.execute(
            "SELECT src_id, dst_id FROM edges WHERE rel = 'branch_ref'"
        ).fetchall()
        self.assertEqual(
            [(r["src_id"], r["dst_id"]) for r in rows],
            [("5", "2")],
            "BRANCH-BACKFILL-MISSING: single-token head_ref did not backfill",
        )

    def test_branch_backfill_rejects_pr_target(self) -> None:
        """The backfill's target set is STORED ISSUES ONLY (review round 1):
        a head_ref naming a PR number emits nothing, matching the live
        kind='issue' lookup."""
        self.conn.execute("BEGIN")
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " created_at, updated_at, raw_json)"
            " VALUES (2, ?, 2, 'issue', 'target', NULL, 'open', ?, ?, '{}')",
            (REPO, ts(0), ts(1)),
        )
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " created_at, updated_at, raw_json)"
            " VALUES (3, ?, 3, 'pr', 'other pr', NULL, 'open', ?, ?, '{}')",
            (REPO, ts(0), ts(1)),
        )
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " created_at, updated_at, head_ref, raw_json)"
            " VALUES (7, ?, 7, 'pr', 'pr naming a PR', NULL, 'open', ?, ?,"
            " 'fix/3-feature', '{}')",
            (REPO, ts(0), ts(1)),
        )
        self.conn.commit()
        from zaxbygraph.db import migrate_v8_to_v9

        migrate_v8_to_v9(self.conn)
        rows = self.conn.execute(
            "SELECT src_id, dst_id FROM edges WHERE rel = 'branch_ref'"
        ).fetchall()
        self.assertEqual(
            [(r["src_id"], r["dst_id"]) for r in rows],
            [],
            "BRANCH-BACKFILL-PR-TARGET: a PR-numbered head_ref must not emit",
        )

    def test_comment_and_review_self_references_suppressed(self) -> None:
        """Comment/review self-references never become edges through the
        backfill (review round 2): live edges_from_comment/edges_from_review
        suppress n == number for mentions AND closes_keyword; the migration
        must match. Killed by removing either loop's `n != number` filter."""
        self.conn.execute("BEGIN")
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " created_at, updated_at, raw_json)"
            " VALUES (5, ?, 5, 'issue', 'self', NULL, 'open', ?, ?, '{}')",
            (REPO, ts(0), ts(1)),
        )
        self.conn.execute(
            "INSERT INTO comments(pk, github_id, repo, number, kind, body,"
            " created_at, updated_at, raw_json)"
            " VALUES (1, 501, ?, 5, 'issue_comment', 'see #5 for context',"
            " ?, ?, '{}')",
            (REPO, ts(0), ts(1)),
        )
        self.conn.execute(
            "INSERT INTO reviews(id, repo, number, author, state, body,"
            " submitted_at, raw_json)"
            " VALUES (601, ?, 5, 'bob', 'APPROVED', 'fixes #5 in this review',"
            " ?, '{}')",
            (REPO, ts(1)),
        )
        self.conn.commit()
        from zaxbygraph.db import migrate_v8_to_v9

        migrate_v8_to_v9(self.conn)
        rows = self.conn.execute(
            "SELECT rel, dst_id FROM edges WHERE src_id = '5' AND source = 'keyword'"
        ).fetchall()
        self.assertEqual(
            [(r["rel"], r["dst_id"]) for r in rows],
            [],
            "COMMENT-REVIEW-SELF-EDGE: a comment/review self-reference became"
            " a store edge",
        )

    def test_comment_review_evidence_prefix_parity(self) -> None:
        """Comment/review backfill evidence carries the LIVE prefixes
        (comment:<github_id>, review:<id>) — final critic finding 1. A
        body@<offset> label for a comment reference points at the item body,
        which contains no reference. Killed by dropping the prefix argument
        on either loop."""
        self.conn.execute("BEGIN")
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " created_at, updated_at, raw_json)"
            " VALUES (5, ?, 5, 'issue', 't', 'no refs here', 'open', ?, ?,"
            " '{}')",
            (REPO, ts(0), ts(1)),
        )
        self.conn.execute(
            "INSERT INTO comments(pk, github_id, repo, number, kind, body,"
            " created_at, updated_at, raw_json)"
            " VALUES (1, 501, ?, 5, 'issue_comment',"
            " 'see #8 for context, dup of #9', ?, ?, '{}')",
            (REPO, ts(0), ts(1)),
        )
        self.conn.execute(
            "INSERT INTO reviews(id, repo, number, author, state, body,"
            " submitted_at, raw_json)"
            " VALUES (601, ?, 5, 'bob', 'APPROVED', 'fixes #8 in this review',"
            " ?, '{}')",
            (REPO, ts(1)),
        )
        self.conn.commit()
        from zaxbygraph.db import migrate_v8_to_v9

        migrate_v8_to_v9(self.conn)
        evidence = {
            (r["rel"], r["dst_id"]): r["evidence"]
            for r in self.conn.execute(
                "SELECT rel, dst_id, evidence FROM edges WHERE src_id = '5'"
                " AND source = 'keyword'"
            ).fetchall()
        }
        live_comment = {
            (e[2], e[4]): e[5]
            for e in __import__("zaxbygraph.extract", fromlist=["edges_from_comment"])
            .edges_from_comment(REPO, 5, {"id": 501, "user": {"login": "b"}, "body": "see #8 for context, dup of #9"})
            if e[6] == "keyword"
        }
        live_review = {
            (e[2], e[4]): e[5]
            for e in __import__("zaxbygraph.extract", fromlist=["edges_from_review"])
            .edges_from_review(REPO, 5, {"id": 601, "user": {"login": "bob"}, "state": "APPROVED", "body": "fixes #8 in this review"})
            if e[6] == "keyword"
        }
        for key, expected in {**live_comment, **live_review}.items():
            self.assertEqual(
                evidence.get(key),
                expected,
                f"COMMENT-REVIEW-PREFIX: {key} migrated evidence"
                f" {evidence.get(key)!r} != live {expected!r}",
            )

    def test_multipayload_closes_collision_merges(self) -> None:
        """A stored item body AND comment both closing #N yield ONE
        closes_keyword edge whose evidence carries BOTH provenances through
        the backfill (final critic finding 2, the collapse_edges scoped-merge
        rule). Killed by reverting _upsert_keyword_edge to last-write-wins
        for closes_keyword."""
        body = "fixes #8 right here"
        self.conn.execute("BEGIN")
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " created_at, updated_at, raw_json)"
            " VALUES (5, ?, 5, 'issue', 't', ?, 'open', ?, ?, ?)",
            (
                REPO,
                body,
                ts(0),
                ts(1),
                json.dumps({"number": 5, "body": body}),
            ),
        )
        self.conn.execute(
            "INSERT INTO comments(pk, github_id, repo, number, kind, body,"
            " created_at, updated_at, raw_json)"
            " VALUES (1, 501, ?, 5, 'issue_comment', 'fixes #8 too',"
            " ?, ?, '{}')",
            (REPO, ts(0), ts(1)),
        )
        self.conn.commit()
        from zaxbygraph.db import migrate_v8_to_v9

        migrate_v8_to_v9(self.conn)
        rows = self.conn.execute(
            "SELECT evidence FROM edges WHERE src_id = '5' AND rel ="
            " 'closes_keyword' AND dst_id = '8' AND source = 'keyword'"
        ).fetchall()
        merged = [r["evidence"] for r in rows]
        self.assertEqual(len(merged), 1, "COLLISION-SPLIT: more than one row")
        self.assertIn("body@", merged[0], "body provenance dropped")
        self.assertIn("comment:501@", merged[0], "comment provenance dropped")

    def test_backfill_parity_with_live_extraction(self) -> None:
        """Fresh-vs-migrated parity (review round 1): for the same stored
        text the migration's keyword edges EQUAL what live ingest extracts —
        mutual exclusion, self-reference suppression included."""
        body = "Fixes #5 and also mentions #5 again, dup of #6"
        self.conn.execute("BEGIN")
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " created_at, updated_at, head_ref, raw_json)"
            " VALUES (5, ?, 5, 'issue', 'self', ?, 'open', ?, ?, 'issue-9-x', ?)",
            (
                REPO,
                body,
                ts(0),
                ts(1),
                json.dumps({"number": 5, "title": "self", "body": body}),
            ),
        )
        self.conn.commit()
        from zaxbygraph.db import migrate_v8_to_v9

        migrate_v8_to_v9(self.conn)
        migrated = {
            (r["rel"], r["dst_id"], r["evidence"])
            for r in self.conn.execute(
                "SELECT rel, dst_id, evidence FROM edges WHERE src_id = '5'"
                " AND source = 'keyword'"
            ).fetchall()
        }
        from zaxbygraph.extract import edges_from_item

        raw = {"number": 5, "title": "self", "body": body, "user": {"login": "a"}}
        live = {
            (e[2], e[4], e[5])
            for e in edges_from_item(REPO, raw)
            if e[6] == "keyword"
        }
        self.assertEqual(
            migrated,
            live,
            "BACKFILL-PARITY: migrated keyword edges (relation, destination"
            " AND evidence string) must equal live extraction for the same"
            " text",
        )

    def test_title_evidence_parity(self) -> None:
        """Title-sourced backfill evidence reads 'title' like the live
        extractor (review round 3): a body@<offset> label pointing into body
        text that contains no reference is exactly the divergence AC5 exists
        to prevent. Killed by dropping the prefix="title" argument on either
        backfill title loop, or by removing the title-beats-body precedence."""
        # The title ALSO carries a duplicate reference so the duplicate
        # title loop's prefix="title" is pinned (round-4 review: mutant B
        # survived a title with no dup token). The body mentions the SAME
        # number: the live extractor keeps the title's evidence (setdefault),
        # so precedence removal (MUTANT-C) flips the stored evidence to
        # body@<offset> and this test dies.
        title = "about #8 here, dup of #8"
        body = "see #8 too"
        self.conn.execute("BEGIN")
        self.conn.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " created_at, updated_at, raw_json)"
            " VALUES (9, ?, 9, 'issue', ?, ?, 'open', ?, ?, ?)",
            (
                REPO,
                title,
                body,
                ts(0),
                ts(1),
                json.dumps({"number": 9, "title": title, "body": body}),
            ),
        )
        self.conn.commit()
        from zaxbygraph.db import migrate_v8_to_v9

        migrate_v8_to_v9(self.conn)
        migrated = {
            (r["rel"], r["dst_id"]): r["evidence"]
            for r in self.conn.execute(
                "SELECT rel, dst_id, evidence FROM edges WHERE src_id = '9'"
                " AND source = 'keyword'"
            ).fetchall()
        }
        from zaxbygraph.extract import edges_from_item

        raw = {"number": 9, "title": title, "body": body, "user": {"login": "a"}}
        live = {
            (e[2], e[4]): e[5]
            for e in edges_from_item(REPO, raw)
            if e[6] == "keyword"
        }
        self.assertEqual(
            migrated, live, "TITLE-EDGE-PARITY: relations or destinations diverge"
        )
        for key, evidence in live.items():
            self.assertEqual(
                migrated[key],
                evidence,
                f"TITLE-EVIDENCE-PARITY: {key} migrated evidence"
                f" {migrated.get(key)!r} != live {evidence!r}",
            )
        self.assertEqual(
            migrated[("mentions", "8")],
            "title",
            "TITLE-LABEL-MISSING: the title-only mention must read 'title'",
        )


class FrameFunctionTests(unittest.TestCase):
    def test_frame_function_positive_and_negative(self) -> None:
        """The nearest preceding `File "...", line N, in <func>` names the
        frame_function; a block without frames stores NULL (not a fabricated
        name)."""
        with_frames = (
            "```\n"
            '  File "src/a.py", line 5, in handler\n'
            "ValueError: bad input 12\n"
            "```"
        )
        self.assertEqual(
            parse_error_signatures(with_frames),
            [("ValueError", "ValueError: bad input <n>", "handler")],
        )
        without = "```\nOSError: disk on fire 0xff\n```"
        self.assertEqual(
            parse_error_signatures(without),
            [("OSError", "OSError: disk on fire <hex>", None)],
        )


if __name__ == "__main__":
    unittest.main()
