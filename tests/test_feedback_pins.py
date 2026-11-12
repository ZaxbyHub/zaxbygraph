"""Swarm-pr-feedback pins for PR #76's review fixes (run pr76-20261111).

Each test discriminates ONE fix from the review round: reverting the fix
must make the pin fail. Kept in a NEW module so the frozen checkpoint
manifest (which anchors whole file blobs) needs no amendment for these
rows — only for edits to the frozen check files themselves.

Offline: no network, no live `gh`.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures import (
    REPO,
    DeletingSource,
    FakeGitHubSource,
    GitHubError,
    TempDBTest,
    issue,
    pull,
    ts,
)
from zaxbygraph import extract
from zaxbygraph.db import connect, init_schema
from zaxbygraph.doctor import doctor as doctor_mod_doctor
from zaxbygraph.github import GhApiSource, GitHubTransportError
from zaxbygraph.store import ingest_item, write_pending
from zaxbygraph.sync import SyncError, sync_repo

from test_doctor_legacy_adoption import _prep, _seed_store


class TornObligationReportingTests(TempDBTest):
    """PRR-003 + PRR-004: an open torn obligation keeps the store
    incomplete, and a failed torn retry is evidence."""

    def test_torn_obligation_and_failed_retry_are_reported(self) -> None:
        from test_sync_integrity import _TornPullSource, _dated_pull

        src = _TornPullSource()
        src.add_pr(
            issue(8, title="p8", kind="pr", state="open",
                  updated_at=ts(80), comments=0),
            _dated_pull(8, ts(120), ts(45)), files=[],
        )
        src.add_pr(
            issue(9, title="p9", kind="pr", state="closed",
                  updated_at=ts(130), comments=0),
            _dated_pull(9, ts(125), ts(125)), files=[],
        )
        # Two always-stale refreshes: run 1 cannot resolve the tear, so the
        # item lands in the durable pending set.
        src.refresh[8] = [
            issue(8, title="p8", kind="pr", state="open",
                  updated_at=ts(75), comments=0),
            issue(8, title="p8", kind="pr", state="open",
                  updated_at=ts(76), comments=0),
        ]
        self.src = src
        self.sync()

        state = self.conn.execute(
            "SELECT last_error, full_sync_pending, last_full_sync_at"
            " FROM sync_state WHERE repo = ?", (REPO,),
        ).fetchone()
        self.assertEqual(
            self.count(
                "SELECT COUNT(*) FROM sync_pending WHERE repo = ?"
                " AND number = 8 AND reason = 'torn'", (REPO,)),
            1, "the skipped item must be pending after run 1")
        self.assertEqual(
            state["full_sync_pending"], 1,
            "an open torn obligation keeps the incomplete marker after a "
            "clean finish — the finalizer used to count only "
            "reason='deletion', so a torn debt finished with "
            "full_sync_pending=0 and status.complete=True while a listed "
            "item was absent (PR #76 review PRR-003)")
        self.assertIsNone(state["last_error"],
                          "run 1 itself did not fail; only the retry does")
        self.assertIsNone(
            state["last_full_sync_at"],
            "a finish that still owes a torn obligation must not advance "
            "the completion stamp — the CASE used to key on the PRE-update "
            "flag, stamping while the same UPDATE withheld that flag "
            "(PR #76 review PRR-014)")

        # Run 2: the refresh now serves a row (so the retry reaches the
        # children fetch), but that FIRST fetch fails with a 403. Only the
        # first call fails — a real source's since-filter would never
        # re-serve the below-watermark item during the walk, and this fake
        # ignores `since`, so a persistent 403 would (correctly) fail the
        # walk too; the pin isolates the retry's swallow.
        src.refresh[8] = [
            issue(8, title="p8", kind="pr", state="open",
                  updated_at=ts(81), comments=0),
        ]
        real_get_pull = src.get_pull
        pulls = {"count": 0}

        def forbidden(number: int, *args, **kwargs):
            if number == 8:
                pulls["count"] += 1
                if pulls["count"] == 1:
                    raise GitHubError("gh: Forbidden (HTTP 403)",
                                      status=403)
            return real_get_pull(number, *args, **kwargs)

        src.get_pull = forbidden  # type: ignore[method-assign]
        result = self.sync()

        rows = self.count(
            "SELECT COUNT(*) FROM fetch_log WHERE repo = ?"
            " AND lower(note) LIKE '%torn retry failed%' AND http_status = 403",
            (REPO,),
        )
        self.assertGreaterEqual(
            rows, 1,
            "the swallowed children-fetch failure must be recorded with "
            "its real status — the old `except GitHubError: continue` "
            "wrote nothing (PR #76 review PRR-004)")
        state = self.conn.execute(
            "SELECT last_error, full_sync_pending FROM sync_state"
            " WHERE repo = ?", (REPO,),
        ).fetchone()
        self.assertIsNotNone(
            state["last_error"],
            "last_error must name the failed retry (PR #76 review PRR-004)")
        self.assertEqual(
            self.count(
                "SELECT COUNT(*) FROM sync_pending WHERE repo = ?"
                " AND number = 8 AND reason = 'torn'", (REPO,)),
            1, "the obligation stays pending for the next run")
        self.assertEqual(state["full_sync_pending"], 1)
        self.assertIsNotNone(result["last_error"],
                             "the run result must carry the failure too")


class FaultedReingestPreservesEdgesTests(TempDBTest):
    """PRR-005: a contained extraction fault on re-ingest must not delete
    the item's previously stored owned edges."""

    def test_faulted_reingest_keeps_prior_edges(self) -> None:
        self.src.add_issue(issue(1, title="one", body="see #5",
                                 updated_at=ts(10)))
        self.sync()
        before = self.count(
            "SELECT COUNT(*) FROM edges WHERE repo = ? AND src_type = 'item'"
            " AND src_id = '1' AND rel = 'mentions'", (REPO,))
        self.assertEqual(before, 1, "the first ingest must store the mention")

        def boom(repo, raw):
            raise ValueError("deterministic extraction fault marker")

        with patch("zaxbygraph.store.edges_from_item", side_effect=boom):
            result = self.sync(force=True)
        self.assertIsNone(result["last_error"],
                          "the fault is contained, never fatal")
        after = self.count(
            "SELECT COUNT(*) FROM edges WHERE repo = ? AND src_type = 'item'"
            " AND src_id = '1' AND rel = 'mentions'", (REPO,))
        self.assertEqual(
            after, 1,
            "a faulted re-ingest of an item that already holds owned edges "
            "must change nothing: the old delete-then-insert retracted the "
            "faulted stream's stored evidence under a clean verdict "
            "(PR #76 review PRR-005)")
        self.assertGreaterEqual(
            self.count(
                "SELECT COUNT(*) FROM fetch_log WHERE repo = ?"
                " AND lower(note) LIKE '%contained extraction fault%'",
                (REPO,)),
            1, "the fault must still be logged")


class MigrationWrongTypedPayloadTests(unittest.TestCase):
    """PRR-006: a stored wrong-typed title must not abort the v12
    migration — one row used to block every open of the store."""

    def test_migration_survives_wrong_typed_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "history.db"
            conn = connect(db)
            try:
                init_schema(conn)
                ingest_item(
                    conn, REPO,
                    issue(1, title="real title", body="see #5"),
                    pull_raw=None, issue_comments=[], review_comments=[],
                    reviews=[], files=[], include_patches=False,
                )
                conn.commit()
                raw = json.loads(
                    conn.execute(
                        "SELECT raw_json FROM items WHERE repo = ?"
                        " AND number = 1",
                        (REPO,),
                    ).fetchone()[0]
                )
                raw["title"] = 5  # the #53 malformed-data class
                conn.execute(
                    "UPDATE items SET raw_json = ? WHERE repo = ?"
                    " AND number = 1",
                    (json.dumps(raw), REPO),
                )
                conn.execute("PRAGMA user_version = 11")
                conn.commit()
            finally:
                conn.close()

            migrated = connect(db)  # runs the v12 chain
            try:
                init_schema(migrated)
            except Exception as exc:  # pragma: no cover - the failure mode
                self.fail(
                    "a wrong-typed stored title must not abort the v12 "
                    f"migration (PR #76 review PRR-006): {exc!r}")
            try:
                kept = migrated.execute(
                    "SELECT COUNT(*) FROM edges WHERE repo = ?"
                    " AND src_type = 'item' AND src_id = '1'"
                    " AND rel = 'mentions' AND source = 'keyword'", (REPO,),
                ).fetchone()[0]
                self.assertEqual(
                    kept, 1,
                    "the body-derived keyword edge must survive the "
                    "migration (re-derived from the still-valid body text)")
            finally:
                migrated.close()


class ListingReachabilityGateTests(TempDBTest):
    """PRR-010: a listing-walk 404/410 goes through the repository probe
    before anything is marked deleted."""

    def _prepare_pr_with_gone_child(self, probe_result: bool) -> None:
        self.src.add_issue(issue(1, title="one", updated_at=ts(10)))
        self.src.add_pr(
            issue(2, title="two", kind="pr", updated_at=ts(20)),
            pull(2, changed_files=1), files=[],
        )
        self.src.add_issue(issue(3, title="three", updated_at=ts(30)))

        def gone(number: int, *args, **kwargs):
            if number == 2:
                raise GitHubError("HTTP 404 Not Found", status=404)
            return real_list_pr_files(number, *args, **kwargs)

        real_list_pr_files = self.src.list_pr_files
        self.src.list_pr_files = gone  # type: ignore[method-assign]
        self.src.probe_repository = lambda: probe_result  # type: ignore

    def test_unreadable_repository_marks_nothing(self) -> None:
        self._prepare_pr_with_gone_child(probe_result=False)
        with self.assertRaises(SyncError) as ctx:
            self.sync()
        self.assertIn("not readable", str(ctx.exception))
        self.assertEqual(
            self.count(
                "SELECT COUNT(*) FROM items WHERE repo = ? AND number = 2"
                " AND state IN ('deleted', 'transferred')", (REPO,)),
            0,
            "an unreadable repository must never fabricate a deletion on "
            "the listing path (PR #76 review PRR-010)")

    def test_readable_repository_still_marks_the_gone_item(self) -> None:
        self._prepare_pr_with_gone_child(probe_result=True)
        self.sync()
        self.assertEqual(
            self.count(
                "SELECT COUNT(*) FROM items WHERE repo = ? AND number = 2"
                " AND state = 'deleted'", (REPO,)),
            1,
            "a genuine per-item 404 on a readable repository still marks "
            "the item deleted (the gate must not fail closed here)")


class TornSidecarBundleTests(TempDBTest):
    """PRR-011: a resolved torn item records the same replayable bundle
    every other ingested item writes."""

    def test_resolved_torn_item_reaches_the_sidecar(self) -> None:
        from test_sync_integrity import _TornPullSource, _dated_pull

        src = _TornPullSource()
        src.add_pr(
            issue(8, title="p8", kind="pr", state="open",
                  updated_at=ts(80), comments=0),
            _dated_pull(8, ts(120), ts(45)), files=[],
        )
        src.add_pr(
            issue(9, title="p9", kind="pr", state="closed",
                  updated_at=ts(130), comments=0),
            _dated_pull(9, ts(125), ts(125)), files=[],
        )
        self.src = src
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = Path(tmp) / "sidecar"
            sync_repo(self.conn, self.src, REPO, jsonl_path=sidecar)
            # The listing catches up for #8: the next run's pending retry
            # resolves it — that success must emit an item_bundle too.
            src.refresh[8] = [
                issue(8, title="p8", kind="pr", state="closed",
                      updated_at=ts(125), comments=0),
            ]
            sync_repo(self.conn, self.src, REPO, jsonl_path=sidecar)
            lines = (sidecar / "events.jsonl").read_text(
                encoding="utf-8").splitlines()
            bundled = [
                json.loads(ln)["payload"]["number"]
                for ln in lines
                if json.loads(ln)["resource"] == "item_bundle"
                and json.loads(ln)["payload"].get("number") == 8
            ]
            self.assertEqual(
                len(bundled), 1,
                "the resolved torn item must appear exactly once in "
                "events.jsonl — the old success path committed the row and "
                "cleared pending without writing a bundle (PR #76 review "
                "PRR-011)")


class ResumedFullRunsDeletionOracleTests(TempDBTest):
    """PRR-013: an unfinished full sync completes as a FULL sync on the
    next plain run — the deletion oracle sees the whole corpus."""

    def test_plain_run_after_interrupted_full_derives_deletions(self) -> None:
        src = DeletingSource()
        for n in (1, 2, 3):
            src.add_issue(issue(n, updated_at=ts(n * 10)))
        src.deletions[3] = "deleted"
        self.src = src
        self.sync()  # clean full: flag 0
        # Simulate a full sync interrupted before its deletion pass: the
        # marker stands, the watermark stands, and item 3 is gone upstream.
        self.conn.execute(
            "UPDATE sync_state SET full_sync_pending = 1 WHERE repo = ?",
            (REPO,),
        )
        self.conn.commit()
        self.src.issues = {
            k: v for k, v in self.src.issues.items()
            if int(v["number"]) != 3
        }
        result = self.sync()
        self.assertGreaterEqual(
            src.check_deleted_calls, 1,
            "the resumed full run must consult the deletion oracle — the "
            "old `started_full = since is None` gate skipped it on the "
            "watermark tail and then certified completion (PR #76 review "
            "PRR-013)")
        self.assertEqual(
            self.count(
                "SELECT COUNT(*) FROM items WHERE repo = ? AND number = 3"
                " AND state = 'deleted'", (REPO,)),
            1, "the upstream-deleted item must be marked on that run")
        state = self.conn.execute(
            "SELECT full_sync_pending, last_error FROM sync_state"
            " WHERE repo = ?", (REPO,),
        ).fetchone()
        self.assertEqual(state["full_sync_pending"], 0)
        self.assertIsNone(state["last_error"])
        self.assertIsNone(result["last_error"])


class NonDictOracleFailsLoudlyTests(TempDBTest):
    """PRR-015: a non-dict deletion-oracle answer is a failed probe, not a
    silent pass."""

    def test_non_dict_oracle_raises_and_keeps_candidates(self) -> None:
        src = DeletingSource()
        for n in (1, 2, 3):
            src.add_issue(issue(n, updated_at=ts(n * 10)))
        self.src = src
        self.sync()
        self.src.issues = {
            k: v for k, v in self.src.issues.items()
            if int(v["number"]) != 3
        }
        src.check_deleted = lambda numbers: None  # type: ignore
        before = self.conn.execute(
            "SELECT last_full_sync_at FROM sync_state WHERE repo = ?",
            (REPO,),
        ).fetchone()[0]
        with self.assertRaises(SyncError) as ctx:
            self.sync(force=True)
        self.assertIn("non-dict", str(ctx.exception))
        self.assertEqual(
            self.count(
                "SELECT COUNT(*) FROM sync_pending WHERE repo = ?"
                " AND reason = 'deletion'", (REPO,)),
            1, "every candidate stays pending for the next run")
        after = self.conn.execute(
            "SELECT last_full_sync_at, last_error FROM sync_state"
            " WHERE repo = ?", (REPO,),
        ).fetchone()
        self.assertEqual(
            after["last_full_sync_at"], before,
            "a failed probe must not advance the completion stamp")
        self.assertIsNotNone(after["last_error"])


class DoctorObligationUnionTests(unittest.TestCase):
    """PRR-019: consolidating a v12 candidate keeps the store's open
    retry obligations AND their markers."""

    def test_v12_candidate_union_keeps_store_obligations(self) -> None:
        store = _prep(Path(tempfile.mkdtemp()) / "store" / "history.db")
        _seed_store(store, (1, 2), "2027-05-01T00:00:00Z")
        cand = _prep(Path(tempfile.mkdtemp()) / "cand" / "history.db")
        _seed_store(cand, (1, 2), "2027-06-01T00:00:00Z")
        cwd = _prep(Path(tempfile.mkdtemp()) / "cwd")

        conn = connect(store)
        write_pending(conn, REPO, [2], "torn")
        conn.execute(
            "UPDATE sync_state SET last_error = 'deletion pass did not"
            " complete', full_sync_pending = 1 WHERE repo = ?",
            (REPO,),
        )
        conn.commit()
        conn.close()

        envelope = doctor_mod_doctor(cwd, REPO, store, extra_scans=[cand],
                                     consolidate_flag=True)
        self.assertTrue(envelope.get("ok"), envelope)

        check = connect(store)
        pending = check.execute(
            "SELECT COUNT(*) FROM sync_pending WHERE repo = ?"
            " AND number = 2 AND reason = 'torn'", (REPO,),
        ).fetchone()[0]
        state = check.execute(
            "SELECT last_error, full_sync_pending FROM sync_state"
            " WHERE repo = ?", (REPO,),
        ).fetchone()
        check.close()
        self.assertEqual(
            pending, 1,
            "the store's owed retry must survive a v12 candidate's "
            "delete-then-copy (PR #76 review PRR-019)")
        self.assertEqual(state["full_sync_pending"], 1,
                         "the incomplete marker must be re-imposed")
        self.assertEqual(
            state["last_error"], "deletion pass did not complete",
            "the store's error text must not be laundered by the "
            "candidate's clean sync_state")


class TransportOSErrorTypingTests(unittest.TestCase):
    """PRR-021: a non-FileNotFoundError launch failure is still typed."""

    def test_non_executable_gh_is_a_transport_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            garbage = Path(tmp) / "not-a-real-gh"
            garbage.write_bytes(b"this is not an executable\n")
            rest = GhApiSource("acme", "forgegate", gh_bin=str(garbage))
            with self.assertRaises(GitHubTransportError):
                rest._api("repos/acme/forgegate")
            from zaxbygraph.graphql import GraphQLSource

            gql = GraphQLSource("acme", "forgegate", gh_bin=str(garbage))
            with self.assertRaises(GitHubTransportError):
                gql._graphql("query { viewer { login } }")


class DegradationRowStatusTests(TempDBTest):
    """PRR-030: the GraphQL-degradation fetch_log row carries the failure's
    real status, not the log_fetch default of 200."""

    def test_degradation_row_records_the_failure_status(self) -> None:
        from zaxbygraph.graphql import GitHubError as GQLError

        class _ForbiddenBulk(FakeGitHubSource):
            def fetch_children(self, items):
                raise GQLError("gh: Forbidden (HTTP 403)", status=403)

        for n in (1, 2, 3):
            self.src.add_issue(issue(n, updated_at=ts(n * 10)))
        self.src.fetch_children = _ForbiddenBulk().fetch_children  # type: ignore
        result = self.sync()
        self.assertIsNone(result["last_error"], "the run degrades, not dies")
        row = self.conn.execute(
            "SELECT http_status FROM fetch_log WHERE repo = ?"
            " AND resource = 'graphql' AND note LIKE 'graphql unavailable%'"
            " ORDER BY id DESC LIMIT 1", (REPO,),
        ).fetchone()
        self.assertIsNotNone(row, "the degradation must be logged")
        self.assertEqual(
            row[0], 403,
            "a failed fetch must be filed with its HTTP status — the "
            "log_fetch default of 200 filed it as a success (PR #76 review "
            "PRR-030)")


class ExtractNoiseBoundaryTests(unittest.TestCase):
    """PRR-008 + PRR-009: the quoting-noise shapes cubic C8/C9 named."""

    def test_four_backtick_fence_does_not_close_on_inner_three(self) -> None:
        body = "````\n```\nfixes #5\n````\n"
        got = list(extract.iter_closing_locations(body, REPO))
        self.assertEqual(
            got, [],
            "a four-backtick block's inner three-backtick line is content, "
            "not a closing fence — its code must not emit references "
            "(PR #76 review PRR-008)")

    def test_double_backtick_span_masks_its_reference(self) -> None:
        body = "see ``fixes #5`` for the details"
        got = list(extract.iter_closing_locations(body, REPO))
        self.assertEqual(
            got, [],
            "a double-backtick code span is quoting noise regardless of "
            "delimiter length (PR #76 review PRR-009)")


class ProbeFailureCounterTests(TempDBTest):
    """PRR-033: a successful probe clears EVERY probe-failure marker, so a
    superseded gh-version key cannot keep status.coverage counting a
    recovered probe as failed."""

    def test_success_clears_stale_version_keys(self) -> None:
        from zaxbygraph import sync as sync_mod
        from zaxbygraph.db import set_meta

        # A failure recorded under an OLDER host/version key (the shape an
        # upgrade leaves behind).
        set_meta(self.conn,
                 "capability_probe_failed:v3:somehost:gh-0.0-old",
                 "boom: probe failed once")
        self.conn.commit()

        class _ProbeSource:
            gh_bin = "gh"
            attached: dict = {}

            def probe_capabilities(self):
                return {"Issue": ["number"]}

            def attach_capabilities(self, caps):
                type(self).attached = caps

        with patch.dict(sync_mod._GH_VERSION_CACHE, {"gh": "offline"}):
            failed = sync_mod._attach_capabilities(
                self.conn, _ProbeSource(), REPO)
        self.assertFalse(failed, "the fresh probe succeeded")
        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM meta"
            " WHERE key LIKE 'capability_probe_failed:%'"
            " AND value IS NOT NULL AND value != ''",
        ).fetchone()[0]
        self.assertEqual(
            remaining, 0,
            "one success must clear every probe-failure key: the old"
            " version-scoped key used to survive and status.coverage kept"
            " counting it (PR #76 review PRR-033)")

    def test_probe_failure_marker_is_bounded(self) -> None:
        """The failure marker stores _error_text (bounded), not raw gh
        stderr (PR #76 review PRR-046)."""
        from zaxbygraph import sync as sync_mod
        from zaxbygraph.db import get_meta

        class _FailingProbeSource:
            gh_bin = "gh"

            def probe_capabilities(self):
                raise GitHubError("boom: " + "x" * 5000, status=500)

            def attach_capabilities(self, caps):  # pragma: no cover
                raise AssertionError("the probe must fail first")

        src = _FailingProbeSource()
        with patch.dict(sync_mod._GH_VERSION_CACHE, {"gh": "offline"}):
            failed = sync_mod._attach_capabilities(self.conn, src, REPO)
        self.assertTrue(failed, "the probe failure must be reported")
        value = get_meta(self.conn, sync_mod._probe_failed_key(src)) or ""
        self.assertLessEqual(
            len(value), 2000,
            "the persisted marker must be bounded by _error_text, not raw "
            "exception text (PR #76 review PRR-046)")


if __name__ == "__main__":
    unittest.main()
