"""Issue #88/#91 edge regressions for trace 88-91 — NON-FROZEN.

Deliberately outside the checkpoint manifest: the frozen test files keep
their anchored bytes. These cover the seams the frozen checks cannot:
pre-v12 crash safety, deletion-absence suppression, ownership scan faults and
NULL repo values, degenerate destinations, and the MCP sync runner's shared
path.
"""
from __future__ import annotations

import contextlib
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fixtures import REPO, FakeGitHubSource, issue, pull as fixture_pull

from zaxbygraph import mcp_server
from zaxbygraph.db import connect, init_schema
from zaxbygraph.github import ISSUES_PAGE_SIZE, GhApiSource
from zaxbygraph.snapshot import (
    SNAPSHOT_REF,
    SnapshotError,
    _check_destination_shape,
    _corpus_digest_conn,
    _verify_sample,
    publish,
    pull,
)
from zaxbygraph.store import write_pending
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


@contextlib.contextmanager
def _temp_store():
    """A temp directory plus an initialized store whose connection closes
    BEFORE the directory is removed. `addCleanup(conn.close)` is not enough:
    unittest runs cleanups after the `with TemporaryDirectory()` block, so
    the directory would still be removed with the handle open (Stage B
    review finding)."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tdn:
        db, conn = _store(tdn)
        try:
            yield tdn, db, conn
        finally:
            conn.close()


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
        with _temp_store() as (tdn, _db, conn):
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
        with _temp_store() as (tdn, _db, conn):
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
        with _temp_store() as (tdn, _db, conn):
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


def _git_env() -> dict:
    """Ambient environment minus the Git context variables that would redirect
    a fixture repo (GIT_DIR & friends), with prompts disabled. A test git
    command must run against the temp directory it names, never a repository
    inherited from the caller's shell."""
    env = {
        key: value for key, value in __import__("os").environ.items()
        if key not in {
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR", "GIT_PREFIX",
        }
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid",
        "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
    })
    return env


def _bare(tdn: str) -> Path:
    """A bare fixture repository. `check=True` matters: a silently failed
    init would make a publish refusal look like a product verdict, or let a
    no-push leg pass vacuously."""
    bare = Path(tdn) / "snapshot.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        cwd=tdn, capture_output=True, timeout=120, env=_git_env(),
        check=True,
    )
    return bare


class PublishScanFaultTests(unittest.TestCase):
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
            bare = _bare(tdn)
            self.assertEqual(self._assert_refused(db, bare), "store_uninspectable")
            ls = subprocess.run(
                ["git", "ls-remote", str(bare), SNAPSHOT_REF],
                cwd=tdn, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(ls.returncode, 0, "ls-remote must succeed")
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
            bare = _bare(tdn)
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
            bare = _bare(tdn)
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
        with _temp_store() as (tdn, _db, conn):
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


class StrictIntegerTests(unittest.TestCase):
    """Review finding G1: int() coercion let a malformed listing row be
    ingested as another item — bools, floats and padded strings passed the
    issue-#88 validator and the run certified completeness. The strict check
    must skip, record and pin each shape, and never move a real item."""

    def test_bool_float_and_padded_number_are_recorded_not_ingested(self) -> None:
        with _temp_store() as (tdn, _db, conn):
            page = [
                issue(1),
                {"number": True, "id": 9001, "title": "bool"},
                {"number": 2.7, "id": 9002, "title": "float"},
                {"number": " 3 ", "id": 9003, "title": "padded"},
                issue(4),
            ]
            result = sync_repo(conn, _PageSource(page), REPO)
            self.assertEqual(_items(conn), [1, 4])
            self.assertEqual(result["ingested"], 2)
            logged = [
                r[0] for r in conn.execute(
                    "SELECT note FROM fetch_log"
                    " WHERE note LIKE 'skipped malformed listing row:%'"
                )
            ]
            # A pass loop re-walks the window, so the same row is observed
            # again (the bounded reason keeps that from bloating the log);
            # each SHAPE is recorded at least once.
            self.assertEqual(len(set(logged)), 3, logged)
            state = _state(conn)
            self.assertEqual(state["listing_floor_active"], 1)
            self.assertIsNotNone(state["last_error"])
            self.assertFalse(result["full"])

    def test_float_id_never_moves_a_real_item(self) -> None:
        """The id branch is the worst shape: the store's upsert matches on
        id, so a float id used to MOVE the real item's row to another
        number. The strict validator records the row and leaves item 1
        exactly where it was."""
        with _temp_store() as (tdn, _db, conn):
            _seed(conn)  # items 1-3; item 1 carries id 10001
            bad = {**issue(54), "id": 10001.9, "title": "collider"}
            sync_repo(conn, _PageSource([issue(1), bad]), REPO)
            self.assertEqual(_items(conn), [1, 2, 3])
            stored = conn.execute(
                "SELECT title FROM items WHERE repo = ? AND number = 1",
                (REPO,),
            ).fetchone()[0]
            self.assertNotEqual(stored, "collider")
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM items WHERE repo = ? AND number = 54",
                    (REPO,),
                ).fetchone()[0],
                0,
                "a float id never creates/moves an item",
            )
            self.assertEqual(_state(conn)["listing_floor_active"], 1)

    def test_oversized_numeric_values_are_recorded_not_raised(self) -> None:
        """The validator and its diagnostics run on untrusted payloads, so
        both must be TOTAL: a very long digit string makes int() raise, a
        huge integer overflows SQLite, and rendering a huge integer with
        str()/repr() raises in CPython. All are malformed rows, recorded
        like any other (Stage B review finding)."""
        with _temp_store() as (tdn, _db, conn):
            page = [
                issue(1),
                {"number": "9" * 5000, "id": 9001, "title": "long-string"},
                {"number": 10**40, "id": 9002, "title": "huge-int"},
                {"number": 10**5000, "id": 9003, "title": "diagnostic-int"},
                issue(2),
            ]
            result = sync_repo(conn, _PageSource(page), REPO)
            self.assertEqual(_items(conn), [1, 2])
            self.assertEqual(result["ingested"], 2)
            notes = {
                r[0] for r in conn.execute(
                    "SELECT note FROM fetch_log"
                    " WHERE note LIKE 'skipped malformed listing row:%'"
                )
            }
            self.assertGreaterEqual(
                len(notes), 3,
                "each hostile shape gets its own anchored record",
            )
            self.assertEqual(_state(conn)["listing_floor_active"], 1)


class FallbackMalformedTests(unittest.TestCase):
    """Review finding G2: a source without `get_issues` (the documented
    `GitHubSource` protocol allows it) filtered the fallback listing by
    number and silently dropped an unmatchable element — which then let the
    targeted pass mark the requested item deleted."""

    def test_targeted_fallback_records_and_never_fabricates_deletion(self) -> None:
        with _temp_store() as (tdn, _db, conn):
            _seed(conn)
            result = sync_repo(
                conn, _PageSource([issue(1), "junk", issue(3)]), REPO,
                numbers=[2],
            )
            state = conn.execute(
                "SELECT state FROM items WHERE repo = ? AND number = 2",
                (REPO,),
            ).fetchone()[0]
            self.assertEqual(state, "open", "the deletion verdict is suppressed")
            logged = conn.execute(
                "SELECT COUNT(*) FROM fetch_log"
                " WHERE note LIKE 'skipped malformed listing row:%'"
            ).fetchone()[0]
            self.assertEqual(logged, 1, "the non-dict element is recorded")
            self.assertEqual(_state(conn)["listing_floor_active"], 1)
            self.assertIsNotNone(_state(conn)["last_error"])
            self.assertFalse(result["full"])

    def test_targeted_fallback_pins_from_the_beginning(self) -> None:
        """An untimestamped malformed row on a targeted run may belong to an
        item OLDER than the stored watermark, so its floor must be
        FROM THE BEGINNING; the next ordinary run then re-drains and ingests
        a corrected old row (closeout review finding)."""
        late = "2026-02-01T00:00:00Z"
        with _temp_store() as (tdn, _db, conn):
            seed_src = FakeGitHubSource()
            for n in (1, 2, 3):
                seed_src.add_issue(
                    issue(n, title=f"original {n}", updated_at=late)
                )
            sync_repo(conn, seed_src, REPO)
            # Targeted fallback refresh: an unmatchable element arrives.
            sync_repo(
                conn, _PageSource(["junk", issue(2, updated_at=late)]),
                REPO, numbers=[2],
            )
            state = _state(conn)
            self.assertEqual(state["listing_floor_active"], 1)
            self.assertIsNone(
                state["listing_floor"],
                "a targeted fallback observation pins from the beginning",
            )
            # The next ordinary run re-drains that whole window and ingests a
            # corrected item whose stamp is OLDER than the old watermark.
            fixed = FakeGitHubSource()
            fixed.add_issue(
                issue(1, title="recovered", updated_at="2026-01-01T00:00:00Z")
            )
            fixed.add_issue(issue(2, updated_at=late))
            fixed.add_issue(issue(3, updated_at=late))
            sync_repo(conn, fixed, REPO)
            title = conn.execute(
                "SELECT title FROM items WHERE repo = ? AND number = 1",
                (REPO,),
            ).fetchone()[0]
            self.assertEqual(title, "recovered")

    def test_timestamped_malformed_dict_pins_at_its_own_stamp(self) -> None:
        """A malformed dict row WITH a usable updated_at pins at that stamp —
        the precise #88 candidate — while only an unattributable row pins
        from the beginning. This is the positive control for the rule the
        untimestamped test exercises (Stage B delta review)."""
        old = "2026-01-15T00:00:00Z"
        late = "2026-02-01T00:00:00Z"
        bad = {"number": "x", "id": 77, "updated_at": old, "title": "bad"}
        with _temp_store() as (tdn, _db, conn):
            seed_src = FakeGitHubSource()
            for n in (1, 2, 3):
                seed_src.add_issue(issue(n, updated_at=late))
            sync_repo(conn, seed_src, REPO)
            sync_repo(
                conn, _PageSource([bad, issue(2, updated_at=late)]),
                REPO, numbers=[2],
            )
            state = _state(conn)
            self.assertEqual(state["listing_floor_active"], 1)
            self.assertEqual(
                state["listing_floor"], old,
                "a malformed row's own updated_at is the precise floor",
            )


class DigestSpellingTests(unittest.TestCase):
    """Review finding G3: the digest derived its spellings from sync_state
    alone, so a case-variant row publish ACCEPTED was not bound by the
    corpus digest — publish and post-install agreed on a corpus neither
    read."""

    def test_case_variant_row_contributes_to_the_digest(self) -> None:
        with _temp_store() as (tdn, _db, conn):
            _seed(conn)
            conn.execute(
                "UPDATE items SET repo = ? WHERE repo = ? AND number = 2",
                (REPO.upper(), REPO),
            )
            conn.commit()
            with_row = _corpus_digest_conn(conn, REPO)
            conn.execute(
                "DELETE FROM items WHERE repo = ? AND number = 2",
                (REPO.upper(),),
            )
            conn.commit()
            without_row = _corpus_digest_conn(conn, REPO)
            self.assertNotEqual(
                with_row, without_row,
                "a case-variant row the ownership guard accepts must be"
                " bound by the digest",
            )

    def test_scan_fault_propagates_instead_of_digesting_nothing(self) -> None:
        with _temp_store() as (tdn, _db, conn):
            _seed(conn)
            with mock.patch(
                "zaxbygraph.snapshot._repo_owners",
                side_effect=sqlite3.OperationalError("boom"),
            ):
                with self.assertRaises(SnapshotError) as ctx:
                    _corpus_digest_conn(conn, REPO)
            self.assertEqual(ctx.exception.code, "store_uninspectable")


class TornRetryNonDictTests(unittest.TestCase):
    """Review finding G4: a torn-retry response that was not a listing row
    fell through silently. The pending row survives, but PRR-004 requires
    the fault to be recorded and to keep the readiness verdict from reading
    clean."""

    def test_non_dict_200_records_and_keeps_the_pending_row(self) -> None:
        class TornSource(_PageSource):
            def get_issues(self, numbers):
                return {n: (None, None) for n in numbers}

        with _temp_store() as (tdn, _db, conn):
            _seed(conn)
            write_pending(conn, REPO, [2], "torn")
            conn.commit()
            # An ORDINARY run retries the pending set through the
            # exact-number seam; the listing walk afterwards is healthy.
            sync_repo(conn, TornSource([issue(1), issue(2), issue(3)]), REPO)
            pending = conn.execute(
                "SELECT COUNT(*) FROM sync_pending"
                " WHERE repo = ? AND number = 2",
                (REPO,),
            ).fetchone()[0]
            self.assertEqual(pending, 1, "the pending debt survives")
            note = conn.execute(
                "SELECT note FROM fetch_log WHERE note LIKE 'torn retry:%'"
                " ORDER BY id DESC"
            ).fetchone()
            self.assertIsNotNone(note)
            self.assertIn("not a listing row", note[0])
            self.assertIsNotNone(_state(conn)["last_error"])


class BoundaryHashableTests(unittest.TestCase):
    """Review finding (boundary set): an unhashable `number` in the
    pagination-boundary verification raised TypeError and aborted the whole
    walk before the sync could record anything."""

    def test_unhashable_number_does_not_abort_the_walk(self) -> None:
        page = [{"number": [1], "id": 1}]
        page += [
            {"number": n, "id": n} for n in range(2, ISSUES_PAGE_SIZE + 1)
        ]
        short = [{"number": 1, "id": 1}]
        calls = {"n": 0}

        def fake_api(self, path, *args, **kwargs):
            calls["n"] += 1
            return page if calls["n"] <= 2 else short

        with mock.patch.object(GhApiSource, "_api", fake_api):
            pages = list(GhApiSource("acme", "forgegate").list_issues(None))
        self.assertEqual(len(pages), 2, "the walk completed both pages")


class FrozenSkipOracleTests(unittest.TestCase):
    """Replacement oracle for the frozen `_skip_records` helper.

    The frozen test file is byte-pinned by the trace manifest, so its helper
    keeps the broad "any non-children note" predicate. These tests pin the
    anchored behaviour the frozen scenarios rely on, and prove the broad
    predicate would count notes that are NOT malformed-row records (Stage B
    review finding)."""

    def test_unrelated_notes_do_not_satisfy_the_anchored_oracle(self) -> None:
        with _temp_store() as (tdn, _db, conn):
            _seed(conn)
            conn.execute(
                "INSERT INTO fetch_log(repo, resource, resource_id,"
                " fetched_at, note) VALUES (?, 'item', '3',"
                " '2026-01-01T00:00:00Z',"
                " 'skipped missing number: not found and never stored')",
                (REPO,),
            )
            conn.commit()
            anchored = conn.execute(
                "SELECT COUNT(*) FROM fetch_log"
                " WHERE note LIKE 'skipped malformed listing row:%'"
            ).fetchone()[0]
            broad = conn.execute(
                "SELECT COUNT(*) FROM fetch_log"
                " WHERE note IS NOT NULL AND note NOT LIKE 'children incomplete%'"
            ).fetchone()[0]
            self.assertEqual(anchored, 0)
            self.assertGreater(
                broad, 0, "the frozen predicate would have counted it"
            )

    def test_every_frozen_scenario_shape_writes_an_anchored_record(self) -> None:
        """The five shapes the frozen checks cover (missing number, non-int
        number, missing id, invalid id, non-dict) each produce the anchored
        record those scenarios assert — with no unrelated note needed."""
        with _temp_store() as (tdn, _db, conn):
            page = [
                issue(1),
                {"title": "no number"},
                {"number": "x", "id": 5002},
                {"number": 5},  # missing id
                {"number": 2, "id": "bad"},
                "not-a-dict",
                issue(4),
            ]
            sync_repo(conn, _PageSource(page), REPO)
            notes = {
                r[0] for r in conn.execute(
                    "SELECT note FROM fetch_log"
                    " WHERE note LIKE 'skipped malformed listing row:%'"
                )
            }
            self.assertTrue(any("missing number" in n for n in notes))
            self.assertTrue(any("non-integer number" in n for n in notes))
            self.assertTrue(any("missing id" in n for n in notes))
            self.assertTrue(any("non-dict" in n for n in notes))
            self.assertTrue(any("invalid id" in n for n in notes))
            self.assertEqual(_items(conn), [1, 4])

    def test_torn_refresh_missing_id_writes_an_anchored_record(self) -> None:
        """The frozen torn-refresh leg: a PR whose pull payload is newer
        than its listing row is re-fetched; the refreshed row has no id and
        must be skipped with an anchored record (the frozen oracle only
        asserts 'some skip note')."""
        class RefreshSource(FakeGitHubSource):
            def __init__(self, refreshed: dict) -> None:
                super().__init__()
                self.refreshed = refreshed

            def get_issues(self, numbers):
                return {n: (self.refreshed, 200) for n in numbers}

        with _temp_store() as (tdn, _db, conn):
            refreshed = dict(
                issue(2, kind="pr", updated_at="2026-01-01T12:01:00Z")
            )
            del refreshed["id"]
            src = RefreshSource(refreshed)
            pr_row = issue(2, kind="pr", updated_at="2026-01-01T12:00:10Z")
            pull_raw = fixture_pull(2)
            pull_raw["updated_at"] = "2026-01-01T12:00:50Z"
            src.add_pr(pr_row, pull_raw, files=[])
            src.add_issue(issue(1))
            sync_repo(conn, src, REPO)
            notes = {
                r[0] for r in conn.execute(
                    "SELECT note FROM fetch_log"
                    " WHERE note LIKE 'skipped malformed listing row:%'"
                )
            }
            self.assertTrue(any("missing id" in n for n in notes), notes)
            self.assertEqual(_items(conn), [1])
            self.assertEqual(_state(conn)["listing_floor_active"], 1)

    def test_pending_retry_missing_id_writes_an_anchored_record(self) -> None:
        """The frozen pending-retry leg: a durable torn obligation whose
        exact-number refresh answers a missing-id row must record the skip
        with the anchored note and keep the debt."""
        class PendingRetrySource(FakeGitHubSource):
            def get_issues(self, numbers):
                row = dict(issue(2))
                del row["id"]
                return {n: (row, 200) for n in numbers}

        with _temp_store() as (tdn, _db, conn):
            write_pending(conn, REPO, [2], "torn")
            conn.commit()
            src = PendingRetrySource()
            src.add_issue(issue(1))
            sync_repo(conn, src, REPO)
            notes = {
                r[0] for r in conn.execute(
                    "SELECT note FROM fetch_log"
                    " WHERE note LIKE 'skipped malformed listing row:%'"
                )
            }
            self.assertTrue(any("missing id" in n for n in notes), notes)
            self.assertEqual(_items(conn), [1])
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM sync_pending"
                    " WHERE repo = ? AND number = 2 AND reason = 'torn'",
                    (REPO,),
                ).fetchone()[0],
                1,
                "the pending torn row survives the skip",
            )

    def test_targeted_exact_number_non_row_writes_an_anchored_record(self) -> None:
        """The frozen targeted leg: a source answering (None, None) for the
        requested number records an anchored malformed observation."""
        class ExactNumberSource(FakeGitHubSource):
            def get_issues(self, numbers):
                return {n: (None, None) for n in numbers}

        with _temp_store() as (tdn, _db, conn):
            result = sync_repo(conn, ExactNumberSource(), REPO, numbers=[1])
            self.assertEqual(result["ingested"], 0)
            notes = [
                r[0] for r in conn.execute(
                    "SELECT note FROM fetch_log"
                    " WHERE note LIKE 'skipped malformed listing row:%'"
                )
            ]
            self.assertTrue(
                any("exact-number response was not a listing row" in n
                    for n in notes),
                notes,
            )
            self.assertEqual(_state(conn)["listing_floor_active"], 1)
            self.assertIsNotNone(result["last_error"])


if __name__ == "__main__":
    unittest.main()
