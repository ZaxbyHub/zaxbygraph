from __future__ import annotations

import sqlite3
import time

from fixtures import REPO, TempDBTest, issue, pr_file, pull
from zaxbygraph.db import connect_readonly_query
from zaxbygraph.query import (
    assert_read_sql,
    churn,
    open_items,
    path_between,
    related,
    run_sql,
    search,
)


class QueryTests(TempDBTest):
    def seed_two_prs(self) -> None:
        self.src.add_pr(
            issue(10, title="wal store", body="adds wal", kind="pr", state="closed"),
            pull(10, changed_files=2, merged=True),
            files=[pr_file("src/store.py"), pr_file("src/db.py")],
        )
        self.src.add_pr(
            issue(11, title="fts rebuild", body="fixes fts", kind="pr", state="closed"),
            pull(11, changed_files=1, merged=True),
            files=[pr_file("src/store.py")],
        )
        self.src.add_issue(issue(12, title="open watermark", state="open", body="need inclusive since"))
        self.src.add_issue(issue(5, title="mentions twelve", body="see #12"))
        self.sync()

    def test_related_one_hop(self) -> None:
        self.seed_two_prs()
        data = related(self.conn, 10, depth=1, repo=REPO)
        rels = {e["rel"] for e in data["edges"]}
        self.assertIn("touches", rels)
        self.assertIn("authored", rels)
        files = [e["dst_id"] for e in data["edges"] if e["rel"] == "touches"]
        self.assertIn("src/store.py", files)

    def test_churn_groups_by_path(self) -> None:
        self.seed_two_prs()
        rows = churn(self.conn, repo=REPO)
        by_path = {r["path"]: r["prs"] for r in rows}
        self.assertEqual(by_path["src/store.py"], 2)
        self.assertEqual(by_path["src/db.py"], 1)

    def test_path_between_two_prs_sharing_file(self) -> None:
        self.seed_two_prs()
        data = path_between(self.conn, "10", "11", repo=REPO)
        self.assertIsNotNone(data["path"])
        ids = [step["id"] for step in data["path"]]
        self.assertEqual(ids[0], "10")
        self.assertEqual(ids[-1], "11")
        self.assertIn("src/store.py", ids)

    def test_open_filter(self) -> None:
        self.seed_two_prs()
        rows = open_items(self.conn, repo=REPO)
        numbers = {r["number"] for r in rows}
        self.assertIn(12, numbers)
        self.assertNotIn(10, numbers)

    def test_search_fts(self) -> None:
        self.seed_two_prs()
        data = search(self.conn, "watermark", repo=REPO)
        titles = [i["title"] for i in data["items"]]
        self.assertTrue(any("watermark" in t for t in titles))

    def test_sql_prefix_rejects_insert(self) -> None:
        with self.assertRaises(ValueError):
            assert_read_sql("INSERT INTO items VALUES (1)")

    def test_sql_rejects_pragma(self) -> None:
        with self.assertRaises(ValueError):
            assert_read_sql("PRAGMA journal_mode=DELETE")

    def test_sql_rejects_stacked_statements(self) -> None:
        with self.assertRaises(ValueError):
            assert_read_sql("SELECT 1; DELETE FROM items")

    def test_sql_with_insert_fails_query_only(self) -> None:
        self.seed_two_prs()
        assert_read_sql("WITH x AS (SELECT 1) INSERT INTO items(id, repo, number, kind, title, state, raw_json) VALUES (1,'r',1,'issue','t','open','{}')")
        qconn = connect_readonly_query(self.db_path)
        try:
            with self.assertRaises(ValueError):
                run_sql(
                    qconn,
                    "WITH x AS (SELECT 1) INSERT INTO items(id, repo, number, kind, title, state, raw_json) "
                    "VALUES (1,'r',1,'issue','t','open','{}')",
                )
        finally:
            qconn.close()

    def test_sql_select_works(self) -> None:
        self.seed_two_prs()
        qconn = connect_readonly_query(self.db_path)
        try:
            data = run_sql(qconn, "SELECT number FROM items ORDER BY number")
        finally:
            qconn.close()
        self.assertIn("number", data["columns"])
        self.assertTrue(data["rows"])

    # -- ITEM 1: comment/string-aware SQL scanner -----------------------

    def test_sql_rejects_stacked_statement_hidden_in_string_literal(self) -> None:
        """A '--' inside a string literal must not be read as a comment
        that hides a real trailing statement -- the guard itself (not just
        pysqlite's one-statement-per-execute rule) must reject this."""
        with self.assertRaises(ValueError):
            assert_read_sql("SELECT '--'; DELETE FROM items")

    def test_sql_allows_semicolon_inside_string_literal(self) -> None:
        """A ';' inside a string literal is not a statement separator --
        this is legal read-only SQL and must be accepted."""
        assert_read_sql("SELECT ';' AS x")  # must not raise

    def test_sql_semicolon_in_string_literal_executes(self) -> None:
        self.seed_two_prs()
        qconn = connect_readonly_query(self.db_path)
        try:
            data = run_sql(qconn, "SELECT ';' AS x")
        finally:
            qconn.close()
        self.assertEqual(data["rows"], [[";"]])

    def test_sql_allows_trailing_semicolon_and_whitespace(self) -> None:
        assert_read_sql("SELECT 1;   ")  # must not raise

    def test_sql_rejects_second_statement_after_trailing_comment(self) -> None:
        with self.assertRaises(ValueError):
            assert_read_sql("SELECT 1; -- ok so far\nDELETE FROM items")

    # -- ITEM 2: connection authorizer defense in depth ------------------

    def test_readonly_conn_denies_pragma_query_only_off(self) -> None:
        self.seed_two_prs()
        qconn = connect_readonly_query(self.db_path)
        try:
            with self.assertRaises(sqlite3.Error):
                qconn.execute("PRAGMA query_only=OFF")
        finally:
            qconn.close()

    def test_readonly_conn_denies_attach(self) -> None:
        self.seed_two_prs()
        qconn = connect_readonly_query(self.db_path)
        try:
            with self.assertRaises(sqlite3.Error):
                qconn.execute("ATTACH DATABASE ':memory:' AS other")
        finally:
            qconn.close()

    def test_readonly_conn_denies_load_extension(self) -> None:
        self.seed_two_prs()
        qconn = connect_readonly_query(self.db_path)
        try:
            with self.assertRaises(sqlite3.Error):
                qconn.execute("SELECT load_extension('whatever')")
        finally:
            qconn.close()

    def test_readonly_conn_blocks_write_to_attached_database(self) -> None:
        """Full documented bypass chain: ATTACH a second, writable database
        file (mode=ro on the primary connection does not cover it) and try
        to write through it. The authorizer must stop this even though
        query_only/mode=ro do not."""
        self.seed_two_prs()
        other_path = self.db_path.parent / "other.db"
        other_conn = sqlite3.connect(str(other_path))
        other_conn.execute("CREATE TABLE w(x)")
        other_conn.commit()
        other_conn.close()

        qconn = connect_readonly_query(self.db_path)
        try:
            with self.assertRaises(sqlite3.Error):
                qconn.execute(f"ATTACH DATABASE '{other_path.as_posix()}' AS o")
            with self.assertRaises(sqlite3.Error):
                qconn.execute("INSERT INTO o.w VALUES ('z')")
        finally:
            qconn.close()

        check = sqlite3.connect(str(other_path))
        try:
            count = check.execute("SELECT COUNT(*) FROM w").fetchone()[0]
        finally:
            check.close()
        self.assertEqual(count, 0)

    def test_readonly_conn_still_allows_recursive_cte(self) -> None:
        self.seed_two_prs()
        qconn = connect_readonly_query(self.db_path)
        try:
            data = run_sql(
                qconn,
                "WITH RECURSIVE c(n) AS "
                "(SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 5) "
                "SELECT n FROM c",
            )
        finally:
            qconn.close()
        self.assertEqual([r[0] for r in data["rows"]], [1, 2, 3, 4, 5])

    def test_readonly_conn_still_allows_fts_match(self) -> None:
        self.seed_two_prs()
        qconn = connect_readonly_query(self.db_path)
        try:
            data = run_sql(
                qconn,
                "SELECT snippet(items_fts, 0, '[', ']', '...', 8) AS s "
                "FROM items_fts WHERE items_fts MATCH 'watermark'",
            )
        finally:
            qconn.close()
        self.assertTrue(data["rows"])

    # -- ITEM 3: incremental fetch / truncation signal -------------------

    def test_run_sql_never_materializes_full_result_set(self) -> None:
        """run_sql must page with fetchmany, not fetchall -- pulling a huge
        (or effectively unbounded, e.g. recursive-CTE) result to exhaustion
        just to keep `limit` rows is the bug. sqlite3.Cursor is a C type
        whose methods can't be monkeypatched, so this is exercised the way
        the adversarial review found it: a 2M-row recursive CTE must come
        back near-instantly when only 5 rows are requested, not after
        SQLite has stepped through all 2,000,000 rows (fetchall() on this
        query takes ~1.7s+ in this environment; fetchmany(6) takes ~0s
        because SQLite's VDBE is pull-based and a recursive CTE is only
        computed as far as it is actually stepped)."""
        self.seed_two_prs()
        qconn = connect_readonly_query(self.db_path)
        try:
            start = time.monotonic()
            data = run_sql(
                qconn,
                "WITH RECURSIVE c(n) AS "
                "(SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 2000000) "
                "SELECT n FROM c",
                limit=5,
            )
            elapsed = time.monotonic() - start
        finally:
            qconn.close()
        self.assertEqual(len(data["rows"]), 5)
        self.assertTrue(data["truncated"])
        self.assertLess(
            elapsed,
            1.0,
            f"run_sql took {elapsed:.2f}s for limit=5 on a 2M-row CTE -- "
            "looks like it materialized the full result set instead of "
            "paging with fetchmany",
        )

    def test_run_sql_truncated_false_when_result_fits(self) -> None:
        self.seed_two_prs()
        qconn = connect_readonly_query(self.db_path)
        try:
            data = run_sql(qconn, "SELECT number FROM items", limit=1000)
        finally:
            qconn.close()
        self.assertFalse(data["truncated"])

    # -- ITEM 4: non-positive limits are clamped -------------------------

    def seed_many_searchable(self, n: int, start: int = 100) -> None:
        for i in range(n):
            self.src.add_issue(issue(start + i, title=f"needle item {i}", body="needle"))
        self.sync()

    def seed_many_churn_paths(self, n: int, start: int = 200) -> None:
        for i in range(n):
            self.src.add_pr(
                issue(start + i, title=f"pr {i}", body="x", kind="pr", state="closed"),
                pull(start + i, changed_files=1, merged=True),
                files=[pr_file(f"src/file{i}.py")],
            )
        self.sync()

    def test_search_negative_limit_is_clamped_not_unlimited(self) -> None:
        self.seed_many_searchable(5)
        data = search(self.conn, "needle", limit=-1, repo=REPO)
        self.assertEqual(len(data["items"]), 1)

    def test_churn_negative_limit_is_clamped_not_unlimited(self) -> None:
        self.seed_many_churn_paths(5)
        rows = churn(self.conn, limit=-1, repo=REPO)
        self.assertEqual(len(rows), 1)

    def test_run_sql_negative_limit_is_clamped(self) -> None:
        self.seed_two_prs()
        qconn = connect_readonly_query(self.db_path)
        try:
            data = run_sql(qconn, "SELECT number FROM items ORDER BY number", limit=-1)
        finally:
            qconn.close()
        self.assertEqual(len(data["rows"]), 1)

    def test_bom_prefixed_select_is_accepted(self) -> None:
        """A UTF-8 BOM must not hide the leading keyword (Windows editors)."""
        assert_read_sql("﻿SELECT 1")  # must not raise
        assert_read_sql("﻿  WITH x AS (SELECT 1) SELECT * FROM x")

    def test_bom_does_not_smuggle_a_write(self) -> None:
        for bad in ("﻿INSERT INTO items VALUES (1)", "﻿DELETE FROM items"):
            with self.assertRaises(ValueError):
                assert_read_sql(bad)

    def test_sqlite_floor_guard_rejects_old_sqlite(self) -> None:
        """A too-old SQLite must fail early and legibly, not as a syntax error."""
        import unittest.mock as mock

        from zaxbygraph import db as db_mod

        with mock.patch.object(db_mod.sqlite3, "sqlite_version_info", (3, 31, 1)),              mock.patch.object(db_mod.sqlite3, "sqlite_version", "3.31.1"):
            with self.assertRaises(RuntimeError) as ctx:
                db_mod.assert_sqlite_supported()
        self.assertIn("3.35", str(ctx.exception))
        self.assertIn("3.31.1", str(ctx.exception))

    def test_sqlite_floor_guard_passes_on_current_sqlite(self) -> None:
        from zaxbygraph import db as db_mod

        db_mod.assert_sqlite_supported()  # must not raise
