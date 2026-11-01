"""Snapshot transport tests (issue #34, collated trace 34-46).

Frozen check names:
    C1 test_publish_single_orphan_commit
    C2 test_publish_refuses_incomplete
    C3 test_pull_refuses_tampered_or_newer
    C4 test_pull_keeps_newer_local
    C5 test_round_trip_hash

Hermetic: local bare repos only (git init --bare), no network, no gh.
Stdlib only; unittest.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, FakeGitHubSource, issue

from zaxbygraph.db import CURRENT_USER_VERSION, connect, init_schema
from zaxbygraph.sync import sync_repo
from zaxbygraph.snapshot import (
    MANIFEST_FORMAT,
    SNAPSHOT_REF,
    SnapshotError,
    publish,
    pull,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(*argv, cwd):
    return subprocess.run(
        ["git", *argv], cwd=str(cwd), capture_output=True, timeout=120)


def _build_store(td: Path) -> Path:
    """A complete single-repo store built from FakeGitHubSource."""
    db = td / "history.db"
    conn = connect(db)
    init_schema(conn)
    conn.row_factory = sqlite3.Row
    src = FakeGitHubSource()
    for n in range(201, 205):
        src.add_issue(issue(n, title=f"Report {n}", body=f"body {n}",
                            created_at="2027-06-01T00:00:00Z"))
    sync_repo(conn, src, REPO)
    conn.close()
    return db


def _bare(td: Path, name="snapshot.git") -> Path:
    b = td / name
    b.mkdir()
    _git("init", "--bare", "-b", "main", str(b), cwd=td)
    return b


def _ls_remote(target: Path, ref: str) -> str | None:
    p = _git("ls-remote", str(target), ref, cwd=target.parent)
    line = p.stdout.decode().splitlines()[0].split()[0] if p.stdout.strip() else ""
    return line or None
class SnapshotPublishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.td = Path(tempfile.mkdtemp(prefix="snap_"))
        cls.db = _build_store(cls.td)
        cls.bare = _bare(cls.td)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_publish_single_orphan_commit(self) -> None:
        p = publish(self.db, str(self.bare), publisher="tester")
        sha1 = p["commit"]
        self.assertEqual(_ls_remote(self.bare, SNAPSHOT_REF), sha1)
        body = _git("cat-file", "commit", sha1, cwd=self.td).stdout.decode()
        self.assertNotIn("parent ", body)
        m = p["manifest"]
        for key in ("format", "user_version", "repo", "host", "watermark",
                    "corpus_sha256", "db_sha256", "gz_sha256", "parts",
                    "publisher"):
            self.assertIn(key, m)
        self.assertEqual(m["format"], MANIFEST_FORMAT)
        self.assertEqual(m["repo"], REPO)
        self.assertEqual(m["user_version"], CURRENT_USER_VERSION)
        # deterministic bytes -> identical orphan commit (idempotent publish);
        # a content change moves the ref (replacement)
        p2 = publish(self.db, str(self.bare), publisher="tester")
        self.assertEqual(_ls_remote(self.bare, SNAPSHOT_REF), p2["commit"])
        conn = sqlite3.connect(str(self.db))
        conn.execute("INSERT OR IGNORE INTO labels(repo, number, name) VALUES (?, 201, 'x')",
                     (REPO,))
        conn.commit()
        conn.close()
        p3 = publish(self.db, str(self.bare), publisher="tester")
        self.assertEqual(_ls_remote(self.bare, SNAPSHOT_REF), p3["commit"])
        self.assertNotEqual(p3["commit"], p2["commit"])
    def test_publish_refuses_incomplete(self) -> None:
        # (a) empty store: repo_not_synced
        empty = self.td / "empty.db"
        conn = connect(empty)
        init_schema(conn)
        conn.close()
        with self.assertRaises(SnapshotError) as ctx:
            publish(empty, str(self.bare), publisher="t")
        self.assertEqual(ctx.exception.code, "repo_not_synced")

    def test_publish_refuses_errored_store(self) -> None:
        # (b) last_error set: store_incomplete
        err_db = self.td / "err.db"
        conn = connect(err_db)
        init_schema(conn)
        conn.row_factory = sqlite3.Row
        src = FakeGitHubSource()
        src.add_issue(issue(301, created_at="2027-06-01T00:00:00Z"))
        sync_repo(conn, src, REPO)
        conn.execute(
            "UPDATE sync_state SET last_error = 'boom' WHERE repo = ?", (REPO,))
        conn.commit()
        conn.close()
        with self.assertRaises(SnapshotError) as ctx:
            publish(err_db, str(self.bare), publisher="t")
        self.assertEqual(ctx.exception.code, "store_incomplete")

    def test_publish_refuses_pending_store(self) -> None:
        # (c) full_sync_pending set: store_incomplete
        pend = self.td / "pend.db"
        conn = connect(pend)
        init_schema(conn)
        conn.row_factory = sqlite3.Row
        src = FakeGitHubSource()
        src.add_issue(issue(302, created_at="2027-06-01T00:00:00Z"))
        sync_repo(conn, src, REPO)
        conn.execute(
            "UPDATE sync_state SET full_sync_pending = 1 WHERE repo = ?", (REPO,))
        conn.commit()
        conn.close()
        with self.assertRaises(SnapshotError) as ctx:
            publish(pend, str(self.bare), publisher="t")
        self.assertEqual(ctx.exception.code, "store_incomplete")
class SnapshotPullTests(unittest.TestCase):
    """AC3-AC5: verify-then-install. Each test builds an independent store
    pair and a bare target; refusals must leave the local store byte-
    identical; installs land under the sync lock with provenance."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.td = Path(tempfile.mkdtemp(prefix="pull_"))
        cls.db = _build_store(cls.td)
        cls.bare = _bare(cls.td)

    @staticmethod
    def _pair(td: Path, wmark: str) -> tuple[Path, Path, Path]:
        """(db, bare_target, tmp_dir) with the store's watermark pinned."""
        db = _build_store(td)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE sync_state SET issues_since = ? WHERE repo = ?", (wmark, REPO))
        conn.commit()
        conn.close()
        bare = _bare(td)
        publish(db, str(bare), publisher="tester")
        return db, bare, td

    @staticmethod
    def _db_bytes(db: Path) -> bytes:
        return db.read_bytes()

    def _assert_refused_intact(self, db: Path, bare: Path) -> SnapshotError:
        before = self._db_bytes(db)
        with self.assertRaises(SnapshotError) as ctx:
            pull(db, str(bare), force=False)
        self.assertEqual(self._db_bytes(db), before)
        return ctx.exception
    def test_pull_refuses_tampered_or_newer(self) -> None:
        td = Path(tempfile.mkdtemp(prefix="tamper_"))
        try:
            db, bare, _ = self._pair(td, "2027-06-01T00:00:00Z")
            # craft a second snapshot commit with a WRONG part sha
            work = td / "work"
            work.mkdir()
            _git("init", "-b", "main", str(work), cwd=td)
            real = publish(self.db, str(self.bare), publisher="x")["manifest"]
            bogus = dict(real)
            bogus["parts"] = [{"name": "snapshot-0000.gz",
                               "sha256": "0" * 64, "bytes": 1}]
            (work / "manifest.json").write_text(
                json.dumps(bogus, sort_keys=True), encoding="utf-8")
            (work / "snapshot-0000.gz").write_bytes(b"\x1f\x8b\x08\x00junk")
            _git("add", "-A", cwd=work)
            tree = _git("write-tree", cwd=work).stdout.decode().strip()
            commit = _git("commit-tree", tree, cwd=work).stdout.decode().strip()
            _git("push", str(bare), commit + ":" + SNAPSHOT_REF, "--force", cwd=work)
            exc = self._assert_refused_intact(db, bare)
            self.assertEqual(exc.code, "part_sha_mismatch")
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_pull_refuses_tampered_or_newer_corrupt_db(self) -> None:
        td = Path(tempfile.mkdtemp(prefix="corrupt_"))
        try:
            db, bare, _ = self._pair(td, "2027-06-01T00:00:00Z")
            # craft a snapshot whose gz holds non-sqlite bytes (shas consistent)
            work = td / "work2"
            work.mkdir()
            _git("init", "-b", "main", str(work), cwd=td)
            import gzip
            import io
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as g:
                g.write(b"definitely not a sqlite db" * 100)
            gz = buf.getvalue()
            man = {
                "format": MANIFEST_FORMAT,
                "user_version": CURRENT_USER_VERSION,
                "repo": REPO,
                "host": "github.com",
                "watermark": "2027-06-01T00:00:00Z",
                "corpus_sha256": "0" * 64,
                "db_sha256": hashlib.sha256(b"definitely not a sqlite db" * 100).hexdigest(),
                "gz_sha256": hashlib.sha256(gz).hexdigest(),
                "parts": [{"name": "snapshot-0000.gz",
                           "sha256": hashlib.sha256(gz).hexdigest(),
                           "bytes": len(gz)}],
                "publisher": "bad",
            }
            (work / "manifest.json").write_text(
                json.dumps(man, sort_keys=True), encoding="utf-8")
            (work / "snapshot-0000.gz").write_bytes(gz)
            _git("add", "-A", cwd=work)
            tree = _git("write-tree", cwd=work).stdout.decode().strip()
            commit = _git("commit-tree", tree, cwd=work).stdout.decode().strip()
            _git("push", str(bare), commit + ":" + SNAPSHOT_REF, "--force", cwd=work)
            exc = self._assert_refused_intact(db, bare)
            self.assertEqual(exc.code, "quick_check_failed")
        finally:
            shutil.rmtree(td, ignore_errors=True)
    def test_pull_refuses_tampered_or_newer_newer_schema(self) -> None:
        td = Path(tempfile.mkdtemp(prefix="schema_"))
        try:
            db, bare, _ = self._pair(td, "2027-06-01T00:00:00Z")
            # bump the published store's user_version beyond this build
            bumped = self.td / "bumped.db"
            shutil.copyfile(db, bumped)
            c = sqlite3.connect(str(bumped))
            c.execute(f"PRAGMA user_version = {CURRENT_USER_VERSION + 1}")
            c.commit()
            c.close()
            publish(bumped, str(bare), publisher="t")
            exc = self._assert_refused_intact(db, bare)
            self.assertEqual(exc.code, "store_too_new")
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_pull_keeps_newer_local(self) -> None:
        # AC4: newer local + no --force -> refuse, bytes intact; --force -> installs
        td = Path(tempfile.mkdtemp(prefix="newer_"))
        try:
            db, bare, _ = self._pair(td, "2027-06-01T00:00:00Z")
            conn = sqlite3.connect(str(db))
            conn.execute(
                "UPDATE sync_state SET issues_since = ? WHERE repo = ?",
                ("2027-07-01T00:00:00Z", REPO))
            conn.commit()
            conn.close()
            exc = self._assert_refused_intact(db, bare)
            self.assertEqual(exc.code, "local_newer")
            pull(db, str(bare), force=True)
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT issues_since FROM sync_state WHERE repo = ?", (REPO,)).fetchone()
            # provenance landed (meta keys) and the watermark came from the snapshot
            prov = conn.execute(
                "SELECT value FROM meta WHERE key = 'snapshot.publisher'").fetchone()
            conn.close()
            self.assertEqual(row["issues_since"], "2027-06-01T00:00:00Z")
            self.assertEqual(prov["value"], "tester")
        finally:
            shutil.rmtree(td, ignore_errors=True)
    def test_round_trip_hash(self) -> None:
        # AC5: publish A -> pull into a fresh dir -> digests equal, both
        # sides independently recomputed
        td = Path(tempfile.mkdtemp(prefix="round_"))
        try:
            src_db = _build_store(td)
            src_conn = sqlite3.connect(str(src_db))
            src_conn.execute(
                "UPDATE sync_state SET issues_since = ? WHERE repo = ?",
                ("2027-06-01T00:00:00Z", REPO))
            src_conn.commit()
            src_conn.close()
            bare = _bare(td)
            out = publish(src_db, str(bare), publisher="rt")
            wmark = out["manifest"]["watermark"]
            self.assertEqual(wmark, "2027-06-01T00:00:00Z")
            dst_dir = td / "dst"
            dst_dir.mkdir()
            dst_db = dst_dir / "history.db"
            pull(dst_db, str(bare), force=False)
            # independent recomputation on both sides
            from zaxbygraph.snapshot import _corpus_digest
            self.assertEqual(_corpus_digest(src_db, REPO),
                             out["manifest"]["corpus_sha256"])
            self.assertEqual(_corpus_digest(dst_db, REPO),
                             out["manifest"]["corpus_sha256"])
        finally:
            shutil.rmtree(td, ignore_errors=True)
