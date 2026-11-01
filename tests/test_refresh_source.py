"""Fetch-source persistence acceptance checks (issue #48 AC1/AC2).

New file for the 40-48-36-followups trace (tests/test_read_truth_supp.py is
over the new-check file cap at 707 lines). The DISCRIMINATING checks
``test_refresh_honors_fetch_source`` (48-AC1: a stale store whose
sync_state.fetch_source records ``rest`` refreshes via the REST source and
never constructs a GraphQL source; a NULL fetch_source keeps the documented
GraphQL refresh) and ``test_refresh_config_reread_under_lock`` (48-AC2: the
refresh configuration - fetch source and include_patches - is re-read after
the sync lock is acquired and before the refresh runs) are authored by the
fix, not at freeze time: at the frozen base this module deliberately
carries no matching test names, so the absence legs observe ``NO TESTS
RAN`` (exit 5 on py -3.14).
"""

from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from zaxbygraph.db import connect, init_schema

REPO = "acme/forgegate"


class _CliStore(unittest.TestCase):
    """A temp store for in-process CLI runs (mirrors the frozen harness)."""

    def setUp(self) -> None:
        from zaxbygraph.store import ingest_item

        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db = str(Path(self._td.name) / "history.db")
        conn = connect(Path(self.db))
        init_schema(conn)
        self.addCleanup(conn.close)
        self.conn = conn
        rec = {
            "id": 10_001, "number": 1, "node_id": "I_1", "title": "one",
            "body": "hello", "state": "open",
            "user": {"login": "alice", "html_url": "u"}, "labels": [],
            "comments": 0,
            "created_at": "2026-03-01T12:00:00Z",
            "updated_at": "2026-03-01T12:00:30Z", "closed_at": None,
            "locked": False, "html_url": "h", "url": "u2",
        }
        conn.execute("BEGIN")
        ingest_item(
            self.conn, REPO, rec, pull_raw=None, issue_comments=[],
            review_comments=[], reviews=[], files=[], include_patches=False,
        )
        self.conn.commit()
        self.conn.execute(
            "INSERT INTO sync_state (repo, last_incr_sync_at, full_sync_pending,"
            " last_error) VALUES (?, ?, 0, NULL)"
            " ON CONFLICT (repo) DO NOTHING",
            (REPO, "2020-01-01T00:00:00Z"),
        )
        self.conn.commit()

    def _set_stamp(self, seconds_ago: int) -> None:
        stamp = (
            datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute(
            "UPDATE sync_state SET last_incr_sync_at = ? WHERE repo = ?",
            (stamp, REPO),
        )
        self.conn.commit()

    def run_cli(self, argv):
        from zaxbygraph.cli import main

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()


class FetchSourceRefreshTests(_CliStore):
    """48-AC1: the --max-age refresh constructs the store's recorded source."""

    def _set_source(self, value):
        if value is None:
            self.conn.execute(
                "UPDATE sync_state SET fetch_source = NULL WHERE repo = ?",
                (REPO,),
            )
        else:
            self.conn.execute(
                "UPDATE sync_state SET fetch_source = ? WHERE repo = ?",
                (value, REPO),
            )
        self.conn.commit()

    def _refresh(self, captured):
        from zaxbygraph import cli as cli_mod

        def fake_rest(a, b):
            captured["rest"] += 1
            raise AssertionError(
                "REST-SOURCE-BUILT: offline test never drives a real source"
            )

        def fake_graphql(a, b):
            captured["graphql"] += 1
            raise AssertionError(
                "GRAPHQL-SOURCE-BUILT: offline test never drives a real source"
            )

        with mock.patch.object(cli_mod, "GhRestSource", fake_rest), \
                mock.patch.object(cli_mod, "GhApiSource", fake_graphql):
            code, out, err = self.run_cli(
                ["item", "1", "--repo", REPO, "--db", self.db,
                 "--format", "json", "--max-age", "60"]
            )
        return code, out, err

    def test_refresh_honors_fetch_source(self):
        captured = {"rest": 0, "graphql": 0}
        self._set_source("rest")
        code, out, err = self._refresh(captured)
        self.assertEqual(
            captured["rest"], 1,
            f"the refresh must construct the REST source for a rest-stamped"
            f" store (code={code}, stderr={err[:300]!r})",
        )
        self.assertEqual(
            captured["graphql"], 0,
            "a rest-stamped store must never construct a GraphQL source",
        )
        self.assertIn("REST-SOURCE-BUILT", err)

    def test_null_fetch_source_keeps_graphql_refresh(self):
        captured = {"rest": 0, "graphql": 0}
        self._set_source(None)
        code, out, err = self._refresh(captured)
        self.assertEqual(
            captured["graphql"], 1,
            f"a NULL fetch_source keeps the documented GraphQL refresh"
            f" (code={code}, stderr={err[:300]!r})",
        )
        self.assertEqual(
            captured["rest"], 0,
            "a NULL fetch_source must never construct a REST source",
        )
        self.assertIn("GRAPHQL-SOURCE-BUILT", err)


class RefreshConfigRereadTests(_CliStore):
    """48-AC2: refresh configuration is re-read under the sync lock."""

    def test_refresh_config_reread_under_lock(self):
        """A concurrent writer flips BOTH config values while the read waits
        on the sync lock; the refresh must use the NEW values (captured at
        the real seam), not the pre-lock snapshot (include_patches=0,
        fetch_source unset). Mutants killed: no-reread (captures the stale
        pair) and one-value-reread (captures a half-stale pair)."""
        from zaxbygraph import cli as cli_mod

        self._set_stamp(10_000)
        self.conn.execute(
            "UPDATE sync_state SET fetch_source = 'graphql',"
            " include_patches = 0 WHERE repo = ?",
            (REPO,),
        )
        self.conn.commit()
        seen = {}

        class _FakeLock:
            def release_owned(self):
                pass

        def fake_lock(db_path, wait=False):
            self.assertTrue(wait)
            # The concurrent writer: flips both values while we wait.
            c = sqlite3.connect(self.db)
            try:
                c.execute(
                    "UPDATE sync_state SET fetch_source = 'rest',"
                    " include_patches = 1 WHERE repo = ?",
                    (REPO,),
                )
                c.commit()
            finally:
                c.close()
            return _FakeLock()

        def runner(conn, slug, *, include_patches=False, source_name=None, **kw):
            seen["include_patches"] = include_patches
            seen["source_name"] = source_name
            return 0

        with mock.patch.object(cli_mod, "acquire_sync_lock", fake_lock):
            with mock.patch.object(cli_mod, "_run_refresh_sync", runner):
                code, _out, err = self.run_cli(
                    ["item", "1", "--repo", REPO, "--db", self.db,
                     "--format", "json", "--max-age", "60"]
                )
        self.assertEqual(code, 0, err)
        self.assertIs(
            seen.get("include_patches"), True,
            f"the refresh must use the writer's include_patches=1, got"
            f" {seen!r} (no-reread or one-value mutant survived)",
        )
        self.assertEqual(
            seen.get("source_name"), "rest",
            f"the refresh must use the writer's fetch_source='rest', got"
            f" {seen!r}",
        )


class LegacyV10RefreshTests(_CliStore):
    """Plan-critic round 1, item 1: a pre-v11 store's --max-age refresh
    must work - the pre-lock staleness query never references the absent
    column, and the under-lock read happens after init_schema migrates."""

    def test_v10_stale_store_refreshes_without_fault(self):
        from zaxbygraph import cli as cli_mod

        self._set_stamp(10_000)
        # Demote to a real v10 shape: recreate sync_state without the v11
        # column (ALTER DROP chokes on the commented CREATE text) and
        # rewind the version stamp.
        self.conn.execute("DROP TABLE sync_state")
        self.conn.execute(
            "CREATE TABLE sync_state ("
            " repo TEXT PRIMARY KEY, issues_since TEXT,"
            " last_full_sync_at TEXT, last_incr_sync_at TEXT,"
            " last_error TEXT, item_count INTEGER NOT NULL DEFAULT 0,"
            " comment_count INTEGER NOT NULL DEFAULT 0,"
            " edge_count INTEGER NOT NULL DEFAULT 0,"
            " include_patches INTEGER NOT NULL DEFAULT 0,"
            " full_sync_pending INTEGER NOT NULL DEFAULT 0,"
            " rate_limit_remaining INTEGER, rate_limit_reset_at TEXT)"
        )
        self.conn.execute(
            "INSERT INTO sync_state(repo, last_incr_sync_at, include_patches,"
            " full_sync_pending) VALUES (?, ?, 0, 0)",
            (REPO, "2020-01-01T00:00:00Z"),
        )
        self.conn.execute("PRAGMA user_version = 10")
        self.conn.commit()

        calls = {}

        def runner(conn, slug, *, include_patches=False, source_name=None, **kw):
            calls["ran"] = True
            calls["include_patches"] = include_patches
            calls["source_name"] = source_name
            return 0

        with mock.patch.object(cli_mod, "_run_refresh_sync", runner):
            code, _out, err = self.run_cli(
                ["item", "1", "--repo", REPO, "--db", self.db,
                 "--format", "json", "--max-age", "60"]
            )
        self.assertEqual(code, 0, err)
        self.assertNotIn("no such column", err)
        self.assertTrue(calls.get("ran"), "the refresh must run")
        # Post-migration read: the migrated column is NULL. The raw NULL
        # reaches the seam; _run_refresh_sync maps it to the graphql default
        # for _sync_core.
        self.assertIsNone(calls.get("source_name"))
        # The store migrated to the current schema under the writable open.
        vc = sqlite3.connect(self.db)
        try:
            version = int(vc.execute("PRAGMA user_version").fetchone()[0])
        finally:
            vc.close()
        self.assertEqual(version, 11)


class SyncStampingTests(unittest.TestCase):
    """Reviewer round 1, Finding 1: the stamping WRITE path is executable.
    A listing sync stamps the requested fetch_source at start; a
    conflicting re-sync re-stamps via the ON CONFLICT arm; a failed sync
    leaves the start stamp in place. The finish-stamp and no-stamp mutants
    of _ensure_state_row both die here."""

    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        self.db = Path(td.name) / "history.db"

    def _sync(self, source, fetch_source):
        from zaxbygraph.sync import sync_repo

        conn = connect(self.db)
        init_schema(conn)
        try:
            return sync_repo(conn, source, REPO, fetch_source=fetch_source)
        finally:
            conn.close()

    def _stamped(self):
        conn = connect(self.db)
        try:
            row = conn.execute(
                "SELECT fetch_source FROM sync_state WHERE repo = ?", (REPO,)
            ).fetchone()
            return None if row is None else row[0]
        finally:
            conn.close()

    def _source(self):
        from fixtures import FakeGitHubSource, issue

        src = FakeGitHubSource()
        src.add_issue(
            issue(1, title="one", body="b", updated_at="2026-03-01T12:00:30Z")
        )
        return src

    def test_listing_sync_stamps_requested_source_at_start(self):
        result = self._sync(self._source(), "rest")
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(
            self._stamped(), "rest",
            "STAMP-MISSING: a listing sync requesting rest must stamp"
            " fetch_source='rest' at listing start",
        )

    def test_conflicting_resync_restamps(self):
        self._sync(self._source(), "rest")
        self.assertEqual(self._stamped(), "rest")
        self._sync(self._source(), "graphql")
        self.assertEqual(
            self._stamped(), "graphql",
            "the ON CONFLICT arm must re-stamp when the operator changes the"
            " requested source",
        )

    def test_failed_sync_keeps_start_stamp(self):
        from fixtures import FakeGitHubSource

        class FailingSource(FakeGitHubSource):
            def list_issues(self, since=None):
                raise RuntimeError("STAMP-FAIL-MARKER")

        with self.assertRaises(Exception):
            self._sync(FailingSource(), "rest")
        self.assertEqual(
            self._stamped(), "rest",
            "STAMP-AT-FINISH-MUTANT: a failed sync must leave the START"
            " stamp in place - the stamp is the requested source, written"
            " before listing",
        )


if __name__ == "__main__":
    unittest.main()
