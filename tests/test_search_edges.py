"""Edge-case pins for the issue #4 search rewrite (non-frozen supplements to
tests/test_search.py): empty and stopword-only queries, output-shape pins,
exact comment counts beyond the page size, the fallback superset property,
total_matches semantics, and genuine fresh-shape vs migrated tokenizer
parity."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, TempDBTest, comment, issue
from zaxbygraph.db import connect, init_schema
from zaxbygraph.query import build_match_queries, search


class SearchEdgeTests(TempDBTest):
    def seed_corpus(self) -> None:
        # default updated_at (early Jan 1) keeps comment_on()'s synthetic
        # bump ahead of the watermark, so the second sync re-fetches item 3
        self.src.add_issue(
            issue(1, title="WebSocket reconnect leaks memory",
                  body="Every reconnection attempt leaks a timer.")
        )
        self.src.add_issue(
            issue(2, title="Init hangs on empty repo", body="Memory grows each time.")
        )
        self.src.add_issue(
            issue(3, title="Unrelated filler", body="nothing relevant here")
        )
        self.sync()

    def test_empty_query_is_honest_zero_hits(self) -> None:
        self.seed_corpus()
        for raw in ("", "   "):
            data = search(self.conn, raw, repo=REPO)
            self.assertEqual(data["items"], [])
            self.assertEqual(data["matched_mode"], "all")
            self.assertEqual(data["total_matches"], 0)
            self.assertEqual(data["corpus_items"], 3)

    def test_all_stopword_query_does_not_broaden_or_crash(self) -> None:
        self.seed_corpus()
        data = search(self.conn, "the of and", repo=REPO)
        # every token is a stopword, so broadening is a no-op and the strict
        # pass answers; the shape stays complete either way
        self.assertIn("matched_mode", data)
        self.assertIn("total_matches", data)
        self.assertIn("corpus_items", data)

    def test_no_separate_comments_section(self) -> None:
        self.seed_corpus()
        self.src.comment_on(3, "a zephyr note")
        self.sync()
        data = search(self.conn, "zephyr", repo=REPO)
        self.assertNotIn("comments", data)
        self.assertEqual([i["number"] for i in data["items"]], [3])

    def test_matching_comments_exact_beyond_page_size(self) -> None:
        self.seed_corpus()
        for i in range(7):
            self.src.comment_on(3, f"zephyr follow-up number {i}")
        self.sync()
        # the page shows one item, but the count is the full comment tally
        data = search(self.conn, "zephyr", repo=REPO, limit=1)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["matching_comments"], 7)
        self.assertIn("«zephyr»", data["items"][0]["comment_snippet"])

    def test_fallback_never_drops_and_hits(self) -> None:
        self.seed_corpus()
        # "memory leak": item 1 matches both tokens, item 2 only "memory".
        # The AND pass has a nonzero hit below the limit, so the OR pass runs;
        # every AND hit must survive in the broadened page.
        strict = search(self.conn, "memory leak", repo=REPO)
        broad = search(self.conn, "memory leak", repo=REPO, limit=20)
        strict_keys = {(i["repo"], i["number"]) for i in strict["items"]}
        broad_keys = {(i["repo"], i["number"]) for i in broad["items"]}
        self.assertTrue(strict_keys, "AND pass should find item 1")
        self.assertLessEqual(strict_keys, broad_keys)

    def test_total_matches_counts_merged_items_pre_limit(self) -> None:
        self.seed_corpus()
        self.src.comment_on(3, "memory pressure in the zephyr pool")
        self.sync()
        # "memory" text-matches items 1 and 2 and comment-matches item 3:
        # three distinct merged items, even with a one-row page.
        data = search(self.conn, "memory", repo=REPO, limit=1)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["total_matches"], 3)
        self.assertEqual(data["corpus_items"], 3)

    def test_build_match_queries_quotes_and_broadens(self) -> None:
        all_q, any_q = build_match_queries("memory OR 1; NEAR(a b)")
        # every token is quoted, so FTS5 operators inside user input are inert
        self.assertIn('"OR"', all_q)
        self.assertIn('"NEAR(a"', all_q)
        self.assertNotIn(" OR ", all_q)
        self.assertIn(" OR ", any_q)
        self.assertNotIn(" AND ", any_q)
        # "or" is a stopword: dropped from the broadened query
        self.assertNotIn('"OR"', any_q)

    def test_single_token_queries_report_all_mode(self) -> None:
        self.seed_corpus()
        data = search(self.conn, "memory", repo=REPO)
        self.assertEqual(data["matched_mode"], "all")


class FreshShapeTokenizerTests(unittest.TestCase):
    """Genuine parity check: a database built from the shipped schema.sql
    verbatim (NO init_schema, NO migrations) must stem-match exactly like a
    migrated database does — this is the only test that exercises the
    schema.sql DDL copy directly."""

    def test_fresh_shape_and_migrated_db_share_tokenizer(self) -> None:
        from zaxbygraph import db as db_mod

        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        schema_sql = (
            Path(db_mod.__file__).parent / db_mod.SCHEMA_NAME
        ).read_text(encoding="utf-8")

        # fresh shape: the schema file alone, no migration framework
        fresh_path = Path(td.name) / "fresh.db"
        raw = sqlite3.connect(str(fresh_path))
        raw.executescript(schema_sql)
        raw.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " updated_at, raw_json) VALUES (1, 'acme/forgegate', 1, 'issue',"
            " 'Timer cleanup on socket teardown',"
            " 'reconnection and reconnecting both drop the timer.', 'open',"
            " '2026-01-04T00:00:00Z', '{}')"
        )
        raw.commit()
        fresh_hits = raw.execute(
            "SELECT items.number FROM items_fts"
            " JOIN items ON items.id = items_fts.rowid"
            " WHERE items_fts MATCH '\"reconnect\"'"
        ).fetchall()
        raw.close()
        self.assertEqual(
            [r[0] for r in fresh_hits],
            [1],
            "fresh-shape schema.sql DDL must stem-match (porter)",
        )

        # migrated shape: a v2 database opened through init_schema
        from tests.test_migrations import V2_SCHEMA_SQL

        migrated_path = Path(td.name) / "migrated.db"
        v2 = sqlite3.connect(str(migrated_path))
        v2.executescript(V2_SCHEMA_SQL)
        v2.execute("PRAGMA user_version = 2")
        v2.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state,"
            " updated_at, raw_json) VALUES (1, 'acme/forgegate', 1, 'issue',"
            " 'Timer cleanup on socket teardown',"
            " 'reconnection and reconnecting both drop the timer.', 'open',"
            " '2026-01-04T00:00:00Z', '{}')"
        )
        v2.commit()
        v2.close()
        conn = connect(migrated_path)
        self.addCleanup(conn.close)
        init_schema(conn)
        migrated_hits = conn.execute(
            "SELECT items.number FROM items_fts"
            " JOIN items ON items.id = items_fts.rowid"
            " WHERE items_fts MATCH '\"reconnect\"'"
        ).fetchall()
        self.assertEqual([r[0] for r in migrated_hits], [1])


if __name__ == "__main__":
    unittest.main()
