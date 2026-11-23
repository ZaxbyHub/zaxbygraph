"""Issue #88/#91 edge regressions for trace 88-91 — NON-FROZEN.

Deliberately outside the checkpoint manifest: the frozen test files keep
their anchored bytes. These cover the seams the frozen checks cannot:
pre-v12 crash safety, deletion-absence suppression, ownership scan faults and
NULL repo values, degenerate destinations, and the MCP sync runner's shared
path.
"""
from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fixtures import REPO, FakeGitHubSource, issue

from zaxbygraph import mcp_server
from zaxbygraph.db import connect, init_schema
from zaxbygraph.snapshot import (
    SNAPSHOT_REF,
    SnapshotError,
    _check_destination_shape,
    _verify_sample,
    publish,
    pull,
)
from zaxbygraph.sync import SyncError, sync_repo


class _PageSource(FakeGitHubSource):
    """Serves one fixed listing page (the trace's probe fixture pattern)."""

    def __init__(self, page: list) -> None:
        super().__init__()
        self.page = page

    def list_issues(self, since):
        if self.page:
            yield self.page


def _store(tdn: str) -> tuple[Path, sqlite3.Connection]:
    db = Path(tdn) / "history.db"
    conn = connect(db)
    init_schema(conn)
    conn.row_factory = sqlite3.Row
    return db, conn


def _items(conn: sqlite3.Connection) -> list[int]:
    return [
        r[0] for r in conn.execute(
            "SELECT number FROM items WHERE repo = ? ORDER BY number", (REPO,)
        )
    ]


def _state(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute(
        "SELECT last_error, issues_since, listing_floor, listing_floor_active"
        " FROM sync_state WHERE repo = ?", (REPO,)
    ).fetchone()


def _seed(conn: sqlite3.Connection, numbers: tuple[int, ...] = (1, 2, 3)) -> None:
    src = FakeGitHubSource()
    for n in numbers:
        src.add_issue(issue(n))
    sync_repo(conn, src, REPO)


class PreV12MalformedRowTests(unittest.TestCase):
    def test_abort_is_loud_and_crash_safe(self) -> None:
        """On a pre-v12 store the malformed row records + errors and the run
        ABORTS before a later row's watermark bump can seal the skip; the
        corrected row is ingested by the next ordinary run (issue #88)."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            _db, conn = _store(tdn)
            # The v12 floor columns cannot be dropped in place (the CREATE
            # text carries comments), so the pre-v12 shape is a rebuilt
            # sync_state without them.
            conn.execute("DROP TABLE sync_state")
            conn.execute(
                """
                CREATE TABLE sync_state (
                    repo TEXT PRIMARY KEY,
                    issues_since TEXT,
                    last_error TEXT,
                    full_sync_pending INTEGER NOT NULL DEFAULT 0,
                    last_full_sync_at TEXT,
                    last_incr_sync_at TEXT,
                    rate_limit_remaining INTEGER,
                    rate_limit_reset_at TEXT,
                    include_patches INTEGER NOT NULL DEFAULT 0,
                    item_count INTEGER NOT NULL DEFAULT 0,
                    comment_count INTEGER NOT NULL DEFAULT 0,
                    edge_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "INSERT INTO sync_state(repo, issues_since) VALUES (?, NULL)",
                (REPO,),
            )
            conn.commit()
            bad = dict(issue(2))
            bad["number"] = "nope"
            with self.assertRaises(SyncError):
                sync_repo(conn, _PageSource([issue(1), bad, issue(3)]), REPO)
            logged = conn.execute(
                "SELECT COUNT(*) FROM fetch_log"
                " WHERE note LIKE 'skipped malformed listing row:%'"
            ).fetchone()[0]
            self.assertGreaterEqual(logged, 1, "the skip is recorded")
            state = conn.execute(
                "SELECT last_error, issues_since FROM sync_state WHERE repo = ?",
                (REPO,),
            ).fetchone()
            self.assertIsNotNone(state["last_error"])
            self.assertEqual(_items(conn), [1], "the later healthy row was never reached")
            self.assertEqual(
                state["issues_since"], issue(1)["updated_at"],
                "the watermark did not advance past the malformed row",
            )
            # Recovery: the corrected row (unchanged stamp) re-drains in.
            sync_repo(conn, _PageSource([issue(1), issue(2), issue(3)]), REPO)
            self.assertEqual(_items(conn), [1, 2, 3])
            self.assertIsNone(
                conn.execute(
                    "SELECT last_error FROM sync_state WHERE repo = ?", (REPO,)
                ).fetchone()[0]
            )

    def test_v12_store_keeps_the_floor_instead_of_aborting(self) -> None:
        """The v12 sibling of the case above: record + pin, no abort, and the
        run answers incomplete (the frozen checks' contract)."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            _db, conn = _store(tdn)
            bad = dict(issue(2))
            bad["number"] = "nope"
            result = sync_repo(conn, _PageSource([issue(1), bad, issue(3)]), REPO)
            state = _state(conn)
            self.assertEqual(state["listing_floor_active"], 1)
            self.assertIsNotNone(result["last_error"])
            self.assertEqual(_items(conn), [1, 3])
            self.assertEqual(result["ingested"], 2)


class DeletionAbsenceTests(unittest.TestCase):
    def test_entirely_malformed_full_run_marks_nothing(self) -> None:
        """A full/forced run whose listing evidence is malformed derives NO
        new deletion candidates; a complete-evidence run still does (the
        positive control) — issue #88."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            _db, conn = _store(tdn)
            _seed(conn)

            class OracleSource(_PageSource):
                def __init__(self, page: list) -> None:
                    super().__init__(page)
                    self.calls: list[list[int]] = []

                def check_deleted(self, numbers, corroborator=None):
                    self.calls.append(sorted(numbers))
                    return {}

            malformed = OracleSource(["junk", {"title": "no number"}])
            sync_repo(conn, malformed, REPO, force=True)
            self.assertEqual(malformed.calls, [], "no absence derivation on malformed evidence")
            states = dict(conn.execute(
                "SELECT number, state FROM items WHERE repo = ?", (REPO,)
            ))
            self.assertEqual(states, {1: "open", 2: "open", 3: "open"})
            state = _state(conn)
            self.assertIsNotNone(state["last_error"])
            self.assertEqual(state["listing_floor_active"], 1)

            # Positive control: complete evidence DOES derive absence (the
            # gate suppresses only the malformed run).
            complete = OracleSource([issue(1)])
            sync_repo(conn, complete, REPO, force=True)
            self.assertTrue(complete.calls, "complete evidence still derives absence")


class OwnershipScanFaultTests(unittest.TestCase):
    def test_scan_fault_on_a_store_refuses(self) -> None:
        """A USABLE store (its sync_state reads) whose SIBLING ownership scan
        faults must refuse, not abort into an allowed replacement (#91)."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            db = Path(tdn) / "dest.db"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE sync_state (repo TEXT)")
            conn.execute("CREATE TABLE labels (x INTEGER)")  # present, no repo
            conn.commit()
            conn.close()
            with self.assertRaises(SnapshotError) as ctx:
                _check_destination_shape(db, {"repo": REPO}, False)
            self.assertEqual(ctx.exception.code, "dest_uninspectable")

    def test_null_repo_value_refuses(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            db = Path(tdn) / "dest.db"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE labels (repo TEXT)")
            conn.execute("INSERT INTO labels(repo) VALUES (NULL)")
            conn.commit()
            conn.close()
            with self.assertRaises(SnapshotError) as ctx:
                _check_destination_shape(db, {"repo": REPO}, False)
            self.assertEqual(ctx.exception.code, "dest_uninspectable")

    def test_schema_less_destination_stays_replaceable(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            db = Path(tdn) / "dest.db"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE unrelated (x INTEGER)")
            conn.commit()
            conn.close()
            _check_destination_shape(db, {"repo": REPO}, False)  # no refusal

    def test_empty_and_same_repo_child_only_destinations(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            empty = Path(tdn) / "empty.db"
            conn = connect(empty)
            init_schema(conn)
            conn.close()
            _check_destination_shape(empty, {"repo": REPO}, False)

            child = Path(tdn) / "child.db"
            conn = connect(child)
            init_schema(conn)
            conn.execute(
                "INSERT INTO labels(repo, number, name) VALUES (?, 1, 'x')",
                (REPO,),
            )
            conn.commit()
            conn.close()
            _check_destination_shape(child, {"repo": REPO}, False)  # same repo


class PublishScanFaultTests(unittest.TestCase):
    def _bare(self, tdn: str) -> Path:
        bare = Path(tdn) / "snapshot.git"
        bare.mkdir()
        env = {
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid",
            "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
        }
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(bare)],
            cwd=tdn, capture_output=True, timeout=120,
            env={**__import__("os").environ, **env}, check=False,
        )
        return bare

    def _assert_refused(self, db: Path, bare: Path) -> str:
        with self.assertRaises(SnapshotError) as ctx:
            publish(db, str(bare), publisher="t")
        return ctx.exception.code

    def test_present_table_fault_refuses_publish(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            db, conn = _store(tdn)
            _seed(conn)
            conn.execute("DROP TABLE issue_field_values")
            conn.execute("CREATE TABLE issue_field_values (x INTEGER)")
            conn.commit()
            conn.close()
            bare = self._bare(tdn)
            self.assertEqual(self._assert_refused(db, bare), "store_uninspectable")
            ls = subprocess.run(
                ["git", "ls-remote", str(bare), SNAPSHOT_REF],
                cwd=tdn, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(ls.stdout.strip(), "", "nothing was pushed")

    def test_null_repo_refuses_publish(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            db, conn = _store(tdn)
            _seed(conn)
            conn.execute("DROP TABLE issue_field_values")
            conn.execute("CREATE TABLE issue_field_values (repo TEXT)")
            conn.execute("INSERT INTO issue_field_values(repo) VALUES (NULL)")
            conn.commit()
            conn.close()
            bare = self._bare(tdn)
            self.assertEqual(self._assert_refused(db, bare), "store_uninspectable")


class CaseVariantTransportTests(unittest.TestCase):
    """Issue #91 review fixes: publish and the post-install check must hash
    the SAME canonical projection, and sample verification must see a
    case-variant corpus (the review found the publish digest still used the
    single lowercase manifest spelling)."""

    @staticmethod
    def _uppercase_every_repo(conn: sqlite3.Connection) -> None:
        from zaxbygraph.snapshot import _REPO_BEARING_TABLES

        for table in _REPO_BEARING_TABLES:
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if present is None:
                continue
            conn.execute(
                f"UPDATE {table} SET repo = upper(repo) WHERE repo = ?", (REPO,)
            )
        conn.commit()

    def test_publish_and_pull_agree_on_a_case_variant_store(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            db, conn = _store(tdn)
            _seed(conn)
            self._uppercase_every_repo(conn)
            conn.close()
            bare = PublishScanFaultTests()._bare(tdn)
            result = publish(db, str(bare), publisher="t")
            self.assertEqual(result["manifest"]["repo"], REPO.lower())
            dest = Path(tdn) / "pulled.db"
            pulled = pull(dest, str(bare))
            self.assertTrue(pulled.get("installed", True))
            check = connect(dest)
            try:
                count = check.execute(
                    "SELECT COUNT(*) FROM items WHERE lower(repo) = ?",
                    (REPO.lower(),),
                ).fetchone()[0]
            finally:
                check.close()
            self.assertGreater(count, 0, "the case-variant corpus traveled whole")

    def test_verify_sample_sees_case_variant_items(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            db, conn = _store(tdn)
            _seed(conn)
            self._uppercase_every_repo(conn)
            conn.close()

            class SampleSource(FakeGitHubSource):
                def get_issues(self, numbers):
                    return {
                        n: (self.issues[n], 200)
                        for n in numbers
                        if n in self.issues
                    }

            source = SampleSource()
            for n in (1, 2, 3):
                source.add_issue(issue(n))
            out = _verify_sample(db, REPO, 5, source_factory=lambda: source)
            self.assertEqual(out["checked"], 3)
            self.assertEqual(out["changed"], 0)
            self.assertEqual(out["matched"], 3)


class TargetedMalformedFloorTests(unittest.TestCase):
    """Final-critic probe A fix: a targeted run that meets a malformed row in
    the MAIN loop pins the floor from the beginning (its true stamp is
    unknown), not at the run-start watermark, so the next ordinary run can
    re-serve the row instead of clearing the floor without it."""

    def test_targeted_main_loop_malformed_pins_from_the_beginning(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            _db, conn = _store(tdn)
            _seed(conn)

            class ExactSource(FakeGitHubSource):
                def get_issues(self, numbers):
                    row = dict(issue(2))
                    row["id"] = "bad"
                    row.pop("updated_at", None)
                    return {n: (row, 200) for n in numbers}

            result = sync_repo(conn, ExactSource(), REPO, numbers=[2])
            state = _state(conn)
            self.assertEqual(state["listing_floor_active"], 1)
            self.assertIsNone(
                state["listing_floor"],
                "a targeted main-loop malformed row pins from the beginning",
            )
            self.assertIsNotNone(result["last_error"])

            class SinceAware(FakeGitHubSource):
                def list_issues(self, since):
                    row = issue(2)  # true stamp ts(20), older than the watermark
                    if since is None or row["updated_at"] >= since:
                        yield [row]

            second = sync_repo(conn, SinceAware(), REPO)
            self.assertEqual(second["ingested"], 1, "the row was re-served")
            self.assertEqual(_state(conn)["listing_floor_active"], 0)
            self.assertIsNone(second["last_error"])


class McpSyncRunnerTests(unittest.TestCase):
    def test_default_runner_records_malformed_row(self) -> None:
        """The MCP background sync reaches the same shared sync_repo
        behavior (issue #88) — no separate validator exists there."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
            db = Path(tdn) / "history.db"

            class StubSource(FakeGitHubSource):
                def list_issues(self, since):
                    bad = dict(issue(2))
                    bad["number"] = "nope"
                    yield [issue(1), bad, issue(3)]

            with mock.patch.object(
                mcp_server, "GhApiSource", lambda *a, **k: StubSource()
            ):
                mcp_server._default_sync_runner(db, REPO)
            conn = connect(db)
            conn.row_factory = sqlite3.Row
            state = conn.execute(
                "SELECT last_error, listing_floor_active FROM sync_state"
                " WHERE repo = ?", (REPO,)
            ).fetchone()
            self.assertIsNotNone(state["last_error"])
            self.assertEqual(state["listing_floor_active"], 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
