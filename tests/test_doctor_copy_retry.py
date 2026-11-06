"""Issue #56 frozen acceptance checks (C1-C7): store-integrity defects on
the `doctor --consolidate` / `snapshot pull` paths. Authored independently
of any fix as a frozen spec: every test is RED at base
80ea01c6ae202ea03373cc26ad9ff8cc561fe39a for the reason its docstring
names, and GREEN after the fix. All fault injection is in-process (a mock
never crosses a subprocess boundary); fixtures are local SQLite/temp-dir
builds — no network, no gh, no credentials.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fixtures import REPO, issue  # noqa: E402

import zaxbygraph.doctor as doctor_mod  # noqa: E402
import zaxbygraph.snapshot as snapshot_mod  # noqa: E402
from zaxbygraph.db import CURRENT_USER_VERSION, connect, init_schema  # noqa: E402
from zaxbygraph.snapshot import SnapshotError, publish, pull  # noqa: E402
from zaxbygraph.store import ingest_item  # noqa: E402


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _prep(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _item(n: int, title: str) -> dict:
    payload = issue(n, title=title, body=f"body {n}", created_at="2027-06-01T00:00:00Z")
    payload["id"] = 9000 + n
    return payload


def _drop_column_and_recreate(db: Path, table: str, column: str) -> None:
    """Rebuild `table` without `column` (ALTER TABLE DROP COLUMN is
    unreliable on this schema's CREATE text)."""
    conn = sqlite3.connect(str(db))
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    keep = [c for c in cols if c != column]
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
    lines = [ln for ln in ddl.splitlines() if ln.strip().split()[0] != column]
    for i in range(len(lines) - 2, -1, -1):  # drop the dangling comma
        stripped = lines[i].strip()
        if stripped.startswith("(") or not stripped:
            continue
        if lines[i].rstrip().endswith(",") and lines[i + 1].strip().startswith(")"):
            lines[i] = lines[i].rstrip()[:-1]
        break
    conn.execute("\n".join(lines))
    collist = ", ".join(keep)
    conn.execute(f"INSERT INTO {table} ({collist}) SELECT {collist} FROM {table}_old")
    conn.execute(f"DROP TABLE {table}_old")
    conn.commit()
    conn.close()


class _StoreIntegrityCase(unittest.TestCase):
    """Scratch tree, seeded stores, and a deterministically empty non-git
    cwd for every doctor() call (the repo's own .swarm/zaxbygraph/history.db
    would otherwise leak in via the process cwd)."""

    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="zgt56-check-"))
        self.addCleanup(_rmtree, self.scratch)

    def _seed_store(self, db: Path, numbers: tuple[int, ...], stamp: str) -> None:
        conn = connect(db)
        init_schema(conn)
        for n in numbers:
            ingest_item(conn, REPO, _item(n, f"t{n}"), pull_raw=None,
                        issue_comments=[], review_comments=[], reviews=[],
                        files=[], include_patches=False)
        conn.execute(
            "UPDATE sync_state SET last_full_sync_at = ?, full_sync_pending = 0,"
            " issues_since = ? WHERE repo = ?", (stamp, stamp, REPO))
        conn.commit(); conn.close()

    def _empty_cwd(self, root: Path) -> Path:
        return _prep(root / "cwd")

    def _counts(self, db: Path) -> dict:
        conn = sqlite3.connect(str(db))
        try:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE repo = ?",
                                    (REPO,)).fetchone()[0]
                    for t in ("releases", "items")}
        finally:
            conn.close()

    def _scanned_by_path(self, envelope: dict) -> dict:
        return {os.path.normcase(str(row.get("path", ""))): row
                for row in envelope.get("scanned") or []}

    def _divergent_pair(self, tag: str, diverge) -> tuple[Path, Path, Path, Path]:
        """(root, good, bad, store): a healthy candidate, a freshest complete
        divergent candidate (the consolidation winner), and a store path."""
        root = self.scratch / tag
        good = _prep(root / "good" / "history.db")
        self._seed_store(good, (1, 2), "2027-01-01T00:00:00Z")
        bad = _prep(root / "bad" / "history.db")
        self._seed_store(bad, (5, 6), "2027-03-01T00:00:00Z")
        diverge(bad)
        return root, good, bad, _prep(root / "store" / "history.db")

    def _assert_isolated(self, envelope: dict, good: Path, bad: Path) -> None:
        rows = self._scanned_by_path(envelope)
        self.assertIn(os.path.normcase(str(good)), rows,
                      "the remaining candidates were not scanned")
        entry = rows.get(os.path.normcase(str(bad)))
        self.assertIsNotNone(entry, "the divergent candidate vanished")
        self.assertTrue(entry.get("unreadable"), entry)
        self.assertTrue(entry.get("unreadable_reason"), entry)
        self.assertTrue(envelope.get("ok"), envelope)

    def _rigged_retry_copy(self, tag: str) -> dict:
        """`_faithful_copy` over a killed-writer candidate whose writer
        "finishes" (commits more rows on the SOURCE, then checkpoints its
        log away with a clean close) between attempt 1's copy and its
        recovery open, forcing exactly one retry; copy2 destinations are
        recorded with a spy."""
        from test_doctor_wal_copy import _spawn_writer

        crepo = "probe/clinic"  # the killed-writer fixture's repo slug
        base = self.scratch / tag
        base.mkdir(parents=True)
        cand = base / "history.db"
        # killed writer: 5 committed frames live only in the -wal
        self.assertEqual(_spawn_writer(cand, 1, 5, "kill", "2027-01-01T00:00:00Z"), 9)
        wal = cand.with_name(cand.name + "-wal")
        self.assertTrue(wal.exists() and wal.stat().st_size > 0)

        copy_dir = base / "copydir"
        copy_dir.mkdir()
        copy = copy_dir / "acopy.db"
        copy_norm = os.path.normcase(str(copy))
        real_connect = sqlite3.connect
        real_copy2 = shutil.copy2
        destinations: list[str] = []
        listing: list[str] = []
        copy_opens = {"n": 0}

        def hooked_connect(target, *a, **kw):
            if os.path.normcase(os.fspath(target)) == copy_norm:
                copy_opens["n"] += 1
                if copy_opens["n"] == 1:
                    # attempt 1's recovery open: the source's writer finishes
                    # now (items 6-10 commit; the clean close deletes the log),
                    # then the forced failure makes the copy retry
                    src = connect(cand)
                    try:
                        for n in range(6, 11):
                            ingest_item(src, crepo, _item(n, f"t{n}"),
                                        pull_raw=None, issue_comments=[],
                                        review_comments=[], reviews=[],
                                        files=[], include_patches=False)
                        src.commit()
                        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    finally:
                        src.close()
                    raise sqlite3.DatabaseError("forced retry (fixture)")
                if copy_opens["n"] == 2 and not listing:
                    listing.extend(sorted(p.name for p in copy_dir.iterdir()))
            return real_connect(target, *a, **kw)

        def spy_copy2(src, dst, *a, **kw):
            destinations.append(Path(str(dst)).name)
            return real_copy2(src, dst, *a, **kw)

        with mock.patch.object(doctor_mod.sqlite3, "connect", hooked_connect), \
                mock.patch.object(doctor_mod.shutil, "copy2", spy_copy2):
            faithful = doctor_mod._faithful_copy(cand, copy)

        def count_rows(db: Path) -> int:
            conn = sqlite3.connect(str(db))
            try:
                return conn.execute("SELECT COUNT(*) FROM items WHERE repo = ?",
                                    (crepo,)).fetchone()[0]
            finally:
                conn.close()

        return {"faithful": faithful, "destinations": destinations,
                "listing": listing, "copy_rows": count_rows(copy),
                "source_rows": count_rows(cand)}


class FaithfulCopyRetryTests(_StoreIntegrityCase):
    def test_retry_does_not_leave_a_stale_sidecar(self) -> None:
        """AC1: when `_faithful_copy` retries because the source's sidecar
        set changed between the copy and the stability check, any sidecar an
        earlier attempt wrote into the copy directory is removed before the
        next attempt copies. RED at base, GREEN after the fix: at base the
        retry re-copies only the main file over attempt 1's `-wal`, so the
        stale sidecar is still there when attempt 2's recovery open runs
        (the copy directory is listed at that moment)."""
        rig = self._rigged_retry_copy("c1")
        self.assertTrue(rig["faithful"], "the copy must still complete")
        self.assertEqual(
            rig["destinations"], ["acopy.db-wal", "acopy.db", "acopy.db"],
            "the forced retry must have happened: attempt 1 copies the pair,"
            " attempt 2 re-copies only the main file (its log is gone)")
        self.assertTrue(rig["listing"],
                        "attempt 2's recovery open never ran; the retry was"
                        " not exercised")
        self.assertNotIn(
            "acopy.db-wal", rig["listing"],
            "a sidecar from an earlier attempt survived the retry: the copy"
            f" directory held {rig['listing']} when attempt 2's recovery open"
            " ran")

    def test_copy_after_writer_finishes_keeps_every_committed_row(self) -> None:
        """AC2: when `_faithful_copy` completes on a candidate whose writer
        finished between attempts, the copy's committed rows equal the
        source's. RED at base, GREEN after the fix: at base the stale `-wal`
        left beside attempt 2's newer main reverts the source's last
        committed frames on recovery, so the copy serves fewer rows."""
        rig = self._rigged_retry_copy("c2")
        self.assertTrue(rig["faithful"], "the copy must still complete")
        self.assertEqual(rig["source_rows"], 10,
                         "the fixture's writer must have finished on the"
                         " source")
        self.assertEqual(
            rig["copy_rows"], rig["source_rows"],
            f"copy rows {rig['copy_rows']} != source rows"
            f" {rig['source_rows']}: the sidecar the earlier attempt left in"
            " the copy directory reverted committed frames on recovery")


class DivergentSchemaTests(_StoreIntegrityCase):
    def test_divergent_candidate_missing_table_is_refused_not_adopted(self) -> None:
        """AC3: a consolidation candidate whose schema lacks a repo-keyed
        table the destination store has is refused with an unreadable
        reason, and the store's rows for that table are not deleted. RED at
        base, GREEN after the fix: at base the delete loop runs
        unconditionally while the copy helper silently skips the missing
        table, so `releases` rows are deleted with nothing copied back and
        the report still claims `adopted`."""
        root = self.scratch / "c3"
        store = _prep(root / "store" / "history.db")
        self._seed_store(store, (1, 2), "2027-01-01T00:00:00Z")
        conn = sqlite3.connect(str(store))
        for n in (1, 2):
            conn.execute(
                "INSERT INTO releases(repo, id, tag_name, name, raw_json)"
                " VALUES (?, ?, ?, ?, '{}')", (REPO, n, f"v{n}", f"v{n}"))
        conn.commit(); conn.close()

        cand = _prep(root / "cand" / "history.db")
        self._seed_store(cand, (7,), "2027-02-01T00:00:00Z")
        conn = sqlite3.connect(str(cand))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute("DROP TABLE releases")
        conn.commit(); conn.close()
        self.assertEqual(version, CURRENT_USER_VERSION,
                         "the candidate must sit at the current user_version"
                         " so no migration re-adds the dropped table")

        before = self._counts(store)
        envelope = doctor_mod.doctor(self._empty_cwd(root), REPO, store,
                                     extra_scans=[cand], consolidate_flag=True)
        after = self._counts(store)

        self.assertEqual(
            after["releases"], before["releases"],
            "the store's releases rows were deleted and nothing was copied"
            f" back ({before['releases']} -> {after['releases']}) although"
            " the candidate lacks the table")
        self.assertEqual(after["items"], before["items"],
                         "the refused adoption still mutated items:"
                         f" {before['items']} -> {after['items']}")
        self.assertTrue(envelope.get("ok"), envelope)
        self.assertIsNone(envelope.get("adopted"), envelope)
        entry = self._scanned_by_path(envelope).get(os.path.normcase(str(cand)))
        self.assertIsNotNone(entry, "the candidate vanished from the scan")
        self.assertTrue(entry.get("unreadable"), entry)
        self.assertTrue(entry.get("unreadable_reason"), entry)

    def test_missing_sync_state_column_isolates_candidate(self) -> None:
        """AC4: a candidate whose `sync_state` lacks a column the
        consolidation reads is isolated as unreadable and the scan of the
        remaining candidates continues. RED at base, GREEN after the fix:
        at base the unguarded `sqlite3.Row` access raises IndexError, which
        is not a sqlite3.DatabaseError, so it escapes candidate isolation
        and aborts the whole command with no report."""
        root, good, bad, store = self._divergent_pair(
            "c4", lambda bad: _drop_column_and_recreate(
                bad, "sync_state", "last_error"))
        envelope = None
        try:
            # the divergent candidate is the freshest complete one, so it is
            # the winner and consolidate() reads its sync_state
            envelope = doctor_mod.doctor(self._empty_cwd(root), REPO, store,
                                         extra_scans=[good, bad],
                                         consolidate_flag=True)
        except IndexError as exc:
            self.fail(f"IndexError escaped candidate isolation and aborted"
                      f" the whole command: {exc!r}")
        self._assert_isolated(envelope, good, bad)

    def test_missing_sync_state_column_isolates_candidate_in_the_fold(self) -> None:
        """AC5: a multi-cased candidate whose `sync_state` lacks a column
        `_fold_sync_state` reads is isolated as unreadable and the command
        continues. RED at base, GREEN after the fix: at base the fold's
        unguarded `sqlite3.Row` access inside `_MigratedCopy` raises
        IndexError, which the sqlite3.DatabaseError handler does not
        convert, so it escapes candidate isolation the same way."""

        def multi_case(bad: Path) -> None:
            _drop_column_and_recreate(bad, "sync_state", "include_patches")
            conn = sqlite3.connect(str(bad))
            shared = ("issues_since, last_full_sync_at, last_incr_sync_at,"
                      " last_error, full_sync_pending, item_count,"
                      " comment_count, edge_count")
            conn.execute(
                f"INSERT INTO sync_state(repo, {shared})"
                f" SELECT 'ACME/FORGEGATE', {shared} FROM sync_state"
                " WHERE repo = ?", (REPO,))
            conn.commit()
            conn.close()

        root, good, bad, store = self._divergent_pair("c5", multi_case)
        envelope = None
        try:
            # the multi-cased divergent candidate is the freshest complete
            # one, so the fold inside its _MigratedCopy runs
            envelope = doctor_mod.doctor(self._empty_cwd(root), REPO, store,
                                         extra_scans=[good, bad],
                                         consolidate_flag=True)
        except IndexError as exc:
            self.fail(f"IndexError escaped candidate isolation inside the"
                      f" fold and aborted the whole command: {exc!r}")
        self._assert_isolated(envelope, good, bad)


    @staticmethod
    def _drop_table(db: Path, table: str) -> None:
        conn = sqlite3.connect(str(db))
        conn.execute(f"DROP TABLE {table}")
        conn.commit()
        conn.close()

    def test_missing_actors_table_isolates_candidate(self) -> None:
        """AC9: a consolidation candidate whose schema lacks the `actors`
        table is isolated as unreadable and the scan of the remaining
        candidates continues. RED at base, GREEN after the fix: at base the
        wholesale actors copy (doctor.py:609) reads the candidate outside
        both conversion regions and `actors` is not in `_COPY_TABLES`, so
        `no such table: actors` (sqlite3.OperationalError) escapes candidate
        isolation and aborts the whole command (plan critic round 1,
        finding 2 — the same isolation class as AC4/AC5 at a sibling
        site)."""
        root, good, bad, store = self._divergent_pair(
            "c9", lambda bad: self._drop_table(bad, "actors"))
        envelope = None
        try:
            # the divergent candidate is the freshest complete one, so it is
            # the winner and the adoption reaches the actors copy
            envelope = doctor_mod.doctor(self._empty_cwd(root), REPO, store,
                                         extra_scans=[good, bad],
                                         consolidate_flag=True)
        except sqlite3.OperationalError as exc:
            self.fail(f"OperationalError escaped candidate isolation and"
                      f" aborted the whole command: {exc!r}")
        self._assert_isolated(envelope, good, bad)


class SnapshotInstallTruthfulnessTests(_StoreIntegrityCase):
    def test_post_replace_sidecar_failure_reports_the_truth(self) -> None:
        """AC6: when the post-replace sidecar cleanup fails after
        `os.replace` succeeded, the store is reported as replaced and the
        post-install verification still runs. RED at base, GREEN after the
        fix: at base the cleanup's OSError is folded into the replace's
        refusal envelope, so the command claims the store was NOT replaced
        while the staged bytes provably landed, and the post-install check
        never runs. The unlink failure is injected STRUCTURALLY
        (in-process `pathlib.Path.unlink` refusing the destination's
        `-wal`/`-shm` only): a chmod-read-only sidecar would be a
        Windows-only fault, since on Linux unlink checks the directory, not
        the file; for the same reason the best-effort quiesce is failed
        once in the rig."""
        from test_doctor_wal_copy import _spawn_writer

        crepo = "probe/clinic"
        root = self.scratch / "c6"
        root.mkdir(parents=True)
        old_db = root / "history.db"
        # killed writer: the OLD corpus's frames live only in the -wal
        self.assertEqual(
            _spawn_writer(old_db, 3, 4, "kill", "2026-01-01T00:00:00Z"), 9)
        old_wal = old_db.with_name(old_db.name + "-wal")
        self.assertTrue(old_wal.exists() and old_wal.stat().st_size > 0)

        # a NEWER snapshot for the same repo, published from a separate store
        new_db = _prep(root / "new" / "history.db")
        conn = connect(new_db)
        init_schema(conn)
        for n in range(201, 205):
            ingest_item(conn, crepo, _item(n, f"t{n}"), pull_raw=None,
                        issue_comments=[], review_comments=[], reviews=[],
                        files=[], include_patches=False)
        conn.execute(
            "UPDATE sync_state SET last_full_sync_at = '2027-06-01T00:00:00Z',"
            " full_sync_pending = 0, issues_since = '2027-06-01T00:00:00Z'"
            " WHERE repo = ?", (crepo,))
        conn.commit(); conn.close()
        bare = root / "bare.git"
        bare.mkdir()
        subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                       capture_output=True)
        publish(new_db, str(bare), publisher="probe")

        dest_norm = os.path.normcase(str(old_db.resolve()))
        sidecars = {dest_norm + "-wal", dest_norm + "-shm"}
        real_unlink, real_connect = pathlib.Path.unlink, sqlite3.connect
        real_install = snapshot_mod._install
        real_post = snapshot_mod._post_install_check
        post_install_ran: list[bool] = []
        quiesce_refused = {"n": 0}

        def refusing_unlink(self_path, *a, **kw):
            if os.path.normcase(str(self_path)) in sidecars:
                raise OSError(13, "Permission denied (fixture)", str(self_path))
            return real_unlink(self_path, *a, **kw)

        def quiesce_refusing_connect(target, *a, **kw):
            # the best-effort quiesce fails once, exactly as on an
            # unreadable destination (pull is the recovery path)
            if (not kw.get("uri")
                    and os.path.normcase(os.fspath(target)) == dest_norm):
                quiesce_refused["n"] += 1
                if quiesce_refused["n"] == 1:
                    raise sqlite3.OperationalError(
                        "unable to quiesce destination (fixture)")
            return real_connect(target, *a, **kw)

        def rigged_install(staged, db_path):
            with mock.patch.object(sqlite3, "connect", quiesce_refusing_connect):
                return real_install(staged, db_path)

        def recording_post(db_path, manifest):
            post_install_ran.append(True)
            return real_post(db_path, manifest)

        exc = None
        result = None
        with mock.patch.object(pathlib.Path, "unlink", refusing_unlink), \
                mock.patch.object(snapshot_mod, "_install", rigged_install), \
                mock.patch.object(snapshot_mod, "_post_install_check",
                                  recording_post):
            try:
                result = pull(old_db, str(bare), force=False)
            except SnapshotError as caught:
                exc = caught

        # immutable=1 reads the main file ALONE (no WAL recovery), so a
        # stale sidecar cannot hide the replaced bytes from this proof
        try:
            probe = sqlite3.connect(f"file:{old_db}?immutable=1", uri=True)
            try:
                landed = probe.execute(
                    "SELECT COUNT(*) FROM items WHERE repo = ? AND number >= 201",
                    (crepo,)).fetchone()[0]
            finally:
                probe.close()
        except sqlite3.Error as read_exc:
            # A destination that was never replaced cannot answer this
            # query; report that as landed=0 so the intended diagnostic
            # fires instead of an opaque "no such table: items"
            # (cubic review, PR #58).
            landed = 0
            read_failure = repr(read_exc)
        else:
            read_failure = None

        self.assertGreater(landed, 0,
                           "the replace never committed, so this run did not"
                           " exercise the AC6 scenario"
                           + (f" (probe read failed: {read_failure})"
                              if read_failure else ""))
        self.assertFalse(
            exc is not None
            and "destination store could not be replaced" in str(
                getattr(exc, "message", "")),
            "the refusal envelope claimed 'destination store could not be"
            " replaced' although the store WAS already replaced (the staged"
            f" corpus is in the destination main): exc={exc!r}"
            f" result={result!r}")
        # The truthful outcome is the post-install check's own verdict: the
        # stale sidecar reverts the pulled corpus, so the digest check runs
        # and refuses with corpus_mismatch, disclosing that the store was
        # already replaced. Assert the concrete outcome, not merely the
        # absence of one phrase (plan critic round 1, finding 4).
        self.assertTrue(post_install_ran,
                        "the post-install verification never ran after the"
                        " replace")
        self.assertIsNotNone(exc,
                             "the pull neither refused truthfully nor"
                             f" returned a result: result={result!r}")
        # A surviving non-empty -wal is refused as dest_sidecar_stale
        # (swarm-pr-review N-07): the corpus digest covers items, children,
        # releases and orphan edges, so a WAL touching only revisions /
        # sync_state / fetch_log / actors would leave the digest matching
        # while the destination served the pre-replace corpus — the exact
        # silent-revert class this PR exists to close.
        self.assertEqual(getattr(exc, "code", None), "dest_sidecar_stale",
                         "the post-install check did not refuse the stale"
                         f" -wal it exists to catch: exc={exc!r}")
        self.assertIn(
            "already replaced",
            f"{getattr(exc, 'message', '')} {getattr(exc, 'hint', '')}",
            "the truthful refusal did not disclose that the store was"
            f" already replaced: exc={exc!r}")

    def test_post_replace_single_sidecar_failure_still_drops_the_others(
            self) -> None:
        """AC6 (swarm-pr-review N-04): when the cleanup fails for ONE
        sidecar, the OTHERS are still dropped — the per-unlink guard added
        in b790e3c, which a single shared try/except around the loop would
        silently undo. The fixture refuses ONLY the `-wal` (the `-shm`
        unlink succeeds), so the two implementations diverge observably:
        with the per-unlink guard the `-shm` is gone; with a shared try the
        first refusal skips it and a stale `-shm` is left beside the
        replaced store. RED at the shared-try mutant, GREEN at head.

        The surviving `-wal` also proves N-07's refusal: the pull refuses
        with dest_sidecar_stale rather than reporting success."""
        from test_doctor_wal_copy import _spawn_writer

        crepo = "probe/clinic"
        root = self.scratch / "c6b"
        root.mkdir(parents=True)
        old_db = root / "history.db"
        # killed writer: the OLD corpus's frames live only in the -wal
        self.assertEqual(
            _spawn_writer(old_db, 3, 4, "kill", "2026-01-01T00:00:00Z"), 9)
        old_wal = old_db.with_name(old_db.name + "-wal")
        old_shm = old_db.with_name(old_db.name + "-shm")
        self.assertTrue(old_wal.exists() and old_wal.stat().st_size > 0)
        self.assertTrue(old_shm.exists(), "the rig needs a -shm to survive")

        new_db = _prep(root / "new" / "history.db")
        conn = connect(new_db)
        init_schema(conn)
        for n in range(201, 205):
            ingest_item(conn, crepo, _item(n, f"t{n}"), pull_raw=None,
                        issue_comments=[], review_comments=[], reviews=[],
                        files=[], include_patches=False)
        conn.execute(
            "UPDATE sync_state SET last_full_sync_at = '2027-06-01T00:00:00Z',"
            " full_sync_pending = 0, issues_since = '2027-06-01T00:00:00Z'"
            " WHERE repo = ?", (crepo,))
        conn.commit(); conn.close()
        bare = root / "bare.git"
        bare.mkdir()
        subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                       capture_output=True)
        publish(new_db, str(bare), publisher="probe")

        dest_norm = os.path.normcase(str(old_db.resolve()))
        wal_norm = dest_norm + "-wal"
        real_unlink = pathlib.Path.unlink
        real_connect = sqlite3.connect
        real_install = snapshot_mod._install
        real_post = snapshot_mod._post_install_check
        quiesce_refused = {"n": 0}
        # Recorded INSIDE the post-install check, before its own read-only
        # reopen: that reopen materializes a fresh -shm/-wal pair (the
        # documented cost of observing what the destination really serves,
        # doctor.py:5-8), so the -shm's existence AFTER the pull says
        # nothing about the cleanup. Its existence at this moment does.
        shm_at_check: list[bool] = []

        def wal_refusing_unlink(self_path, *a, **kw):
            if os.path.normcase(str(self_path)) == wal_norm:
                raise OSError(13, "Permission denied (fixture)", str(self_path))
            return real_unlink(self_path, *a, **kw)

        def recording_post(db_path, manifest):
            shm_at_check.append(old_shm.exists())
            return real_post(db_path, manifest)

        def quiesce_refusing_connect(target, *a, **kw):
            # the best-effort quiesce fails once, exactly as on an
            # unreadable destination, so the killed writer's frames stay in
            # the -wal and the install's cleanup has real work to do
            if (not kw.get("uri")
                    and os.path.normcase(os.fspath(target)) == dest_norm):
                quiesce_refused["n"] += 1
                if quiesce_refused["n"] == 1:
                    raise sqlite3.OperationalError(
                        "unable to quiesce destination (fixture)")
            return real_connect(target, *a, **kw)

        def rigged_install(staged, db_path):
            with mock.patch.object(sqlite3, "connect", quiesce_refusing_connect):
                return real_install(staged, db_path)

        exc = None
        result = None
        with mock.patch.object(pathlib.Path, "unlink", wal_refusing_unlink),                 mock.patch.object(snapshot_mod, "_install", rigged_install),                 mock.patch.object(snapshot_mod, "_post_install_check",
                                  recording_post):
            try:
                result = pull(old_db, str(bare), force=False)
            except SnapshotError as caught:
                exc = caught

        self.assertTrue(shm_at_check, "the post-install check never ran")
        # the -shm WAS dropped despite the -wal's refusal — the per-unlink
        # guard, which is what distinguishes it from a shared try
        self.assertFalse(
            shm_at_check[0],
            "the -wal's refusal stopped the -shm being dropped: the cleanup"
            " is one shared try/except, not one guard per sidecar")
        # the replaced bytes provably landed (immutable=1 reads the main
        # alone, so the surviving stale sidecar cannot hide them)
        try:
            probe = sqlite3.connect(f"file:{old_db}?immutable=1", uri=True)
            try:
                landed = probe.execute(
                    "SELECT COUNT(*) FROM items WHERE repo = ? AND number >= 201",
                    (crepo,)).fetchone()[0]
            finally:
                probe.close()
        except sqlite3.Error as read_exc:
            landed = 0
            read_failure = repr(read_exc)
        else:
            read_failure = None
        self.assertGreater(landed, 0,
                           "the replace never committed, so this run did not"
                           " exercise the AC6 scenario"
                           + (f" (probe read failed: {read_failure})"
                              if read_failure else ""))
        # and the surviving stale -wal is refused, not reported as success
        self.assertIsNotNone(exc,
                             "the pull neither refused truthfully nor"
                             f" returned a result: result={result!r}")
        self.assertEqual(getattr(exc, "code", None), "dest_sidecar_stale",
                         "a surviving stale -wal was not refused:"
                         f" exc={exc!r}")


class UninspectableCandidateTests(_StoreIntegrityCase):
    def test_uninspectable_candidate_is_reported(self) -> None:
        """AC7: a candidate that cannot be inspected is included in the scan
        report with an unreadable reason. RED at base, GREEN after the fix:
        at base `inspect` returning None makes doctor skip the entry, so one
        garbage candidate yields the same report as no candidates."""
        root = self.scratch / "c7"
        scan = root / "scan"
        scan.mkdir(parents=True)
        garbage = scan / "history.db"
        garbage.write_bytes(b"this is not a database at all")
        store = _prep(root / "store" / "history.db")
        envelope = doctor_mod.doctor(self._empty_cwd(root), REPO, store,
                                     extra_scans=[scan],
                                     consolidate_flag=False)
        scanned = envelope.get("scanned") or []
        self.assertEqual(
            len(scanned), 1,
            "the uninspectable candidate was silently dropped from the"
            " report, indistinguishable from 'no candidates exist':"
            f" {json.dumps(envelope)}")
        entry = scanned[0]
        self.assertEqual(os.path.normcase(str(entry.get("path", ""))),
                         os.path.normcase(str(garbage)), entry)
        self.assertTrue(entry.get("unreadable"), entry)
        self.assertTrue(entry.get("unreadable_reason"), entry)
        self.assertTrue(envelope.get("ok"), envelope)
        self.assertIsNone(envelope.get("adopted"), envelope)


if __name__ == "__main__":
    unittest.main()
