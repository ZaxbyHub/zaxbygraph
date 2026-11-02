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
    SNAPSHOT_BRANCH,
    SNAPSHOT_REF,
    SnapshotError,
    publish,
    pull,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(*argv, cwd):
    return subprocess.run(
        ["git", *argv], cwd=str(cwd), capture_output=True, timeout=120)


def _build_store(td: Path, return_source: bool = False):
    """A complete single-repo store built from FakeGitHubSource. With
    return_source=True, returns (db, src) so a verify-sample stub can
    serve payloads identical to what the store ingested."""
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
    return (db, src) if return_source else db


def _build_store_commented(td: Path, return_source: bool = False):
    """A store whose first item carries a comment - the verify-sample
    regression shape (children must re-fetch or the item false-drifts)."""
    db = td / "history.db"
    conn = connect(db)
    init_schema(conn)
    conn.row_factory = sqlite3.Row
    src = FakeGitHubSource()
    src.add_issue(issue(201, title="Report 201", body="body 201",
                        created_at="2027-06-01T00:00:00Z"))
    src.comment_on(201, "a comment that must re-fetch")
    for n in range(202, 205):
        src.add_issue(issue(n, title=f"Report {n}", body=f"body {n}",
                            created_at="2027-06-01T00:00:00Z"))
    sync_repo(conn, src, REPO)
    conn.close()
    return (db, src) if return_source else db


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
        # host derives from the store path / capability meta; the fixture
        # store lives in a bare temp dir with no capability meta, so the
        # derivation must answer "unknown" (the old constant would too —
        # the discriminating leg is test_host_derivation below)
        self.assertEqual(m["host"], "unknown")
        # deterministic bytes -> identical orphan commit (idempotent publish);
        # a content change moves the ref (replacement)
        p2 = publish(self.db, str(self.bare), publisher="tester")
        self.assertEqual(_ls_remote(self.bare, SNAPSHOT_REF), p2["commit"])
        self.assertEqual(p2["commit"], sha1)  # deterministic bytes, idempotent
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

    def test_publish_refuses_incomplete_errored_store(self) -> None:
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

    def test_publish_refuses_incomplete_pending_store(self) -> None:
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
            # status surfaces the provenance block (plan s10)
            from zaxbygraph.query import status as q_status
            qc = sqlite3.connect(str(db))
            qc.row_factory = sqlite3.Row
            snap_block = q_status(qc, REPO)["snapshot"]
            qc.close()
            self.assertEqual(snap_block["publisher"], "tester")
            self.assertTrue(snap_block["manifest_sha256"])
            self.assertTrue(snap_block["pulled_at"])
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


class SnapshotReviewFixTests(unittest.TestCase):
    """Implementation-review round 1 fixes: fallback coverage, discriminating
    quick_check, fresh-directory pull, active-reader refusal, consolidation
    and status pins."""

    def test_publish_falls_back_to_branch(self) -> None:
        # AC6 fallback leg: a target that refuses refs/zaxbygraph/* gets the
        # snapshot on the branch fallback instead
        td = Path(tempfile.mkdtemp(prefix="fb_"))
        try:
            db = _build_store(td)
            bare = _bare(td)
            hook = bare / "hooks" / "pre-receive"
            hook.write_text(
                "#!/bin/sh\n"
                'echo "custom snapshot namespace refused" >&2\n'
                'while read old new ref; do\n'
                '  case "$ref" in refs/zaxbygraph/*) exit 1 ;; esac\n'
                'done\n',
                encoding="utf-8")
            import os as _os
            _os.chmod(hook, 0o755)  # POSIX find_hook requires X_OK
            out = publish(db, str(bare), publisher="fb")
            self.assertEqual(out["pushed_ref"], "refs/heads/" + SNAPSHOT_BRANCH)
            remote = _ls_remote(bare, "refs/heads/" + SNAPSHOT_BRANCH)
            self.assertEqual(remote, out["commit"])
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_pull_accepts_branch_only_source(self) -> None:
        # the source carries ONLY the branch fallback: a hooked target
        # refuses the custom ref, publish lands on the branch, and pull
        # installs from it
        td = Path(tempfile.mkdtemp(prefix="branchonly_"))
        try:
            db = _build_store(td)
            bare = _bare(td)
            hook = bare / "hooks" / "pre-receive"
            hook.write_text(
                "#!/bin/sh" + chr(10)
                + 'while read old new ref; do' + chr(10)
                + '  case "$ref" in refs/zaxbygraph/*) exit 1 ;; esac' + chr(10)
                + 'done' + chr(10),
                encoding="utf-8")
            import os as _os
            _os.chmod(hook, 0o755)
            publish(db, str(bare), publisher="bo")
            self.assertIsNone(_ls_remote(bare, SNAPSHOT_REF),
                              "the custom ref must be absent on this target")
            dst = td / "dst"
            dst.mkdir()
            pull(dst / "history.db", str(bare))
            self.assertTrue((dst / "history.db").exists())
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_pull_refuses_header_valid_corrupt_db(self) -> None:
        # AC3 discriminating pin: a header-valid corrupt page trips
        # quick_check while part shas and user_version still pass
        td = Path(tempfile.mkdtemp(prefix="corruptpage_"))
        try:
            db = _build_store(td)
            bare = _bare(td)
            # corrupt a page WITHOUT touching the 100-byte header
            raw = bytearray(db.read_bytes())
            page = 0x1000  # far past the header, mid-file
            raw[page:page + 64] = b"\xde\xad" * 32
            corrupt = td / "corrupt.db"
            corrupt.write_bytes(bytes(raw))
            import gzip as _gzip
            import io as _io
            buf = _io.BytesIO()
            with _gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as g:
                g.write(bytes(raw))
            gz = buf.getvalue()
            man = {
                "format": MANIFEST_FORMAT,
                "user_version": CURRENT_USER_VERSION,
                "repo": REPO,
                "host": "github.com",
                "watermark": "2027-06-01T00:00:00Z",
                "corpus_sha256": "0" * 64,
                "db_sha256": hashlib.sha256(bytes(raw)).hexdigest(),
                "gz_sha256": hashlib.sha256(gz).hexdigest(),
                "parts": [{"name": "snapshot-0000.gz",
                           "sha256": hashlib.sha256(gz).hexdigest(),
                           "bytes": len(gz)}],
                "publisher": "corrupt",
            }
            work = td / "w"
            work.mkdir()
            _git("init", "-b", "main", str(work), cwd=td)
            (work / "manifest.json").write_text(json.dumps(man, sort_keys=True),
                                                encoding="utf-8")
            (work / "snapshot-0000.gz").write_bytes(gz)
            _git("add", "-A", cwd=work)
            tree = _git("write-tree", cwd=work).stdout.decode().strip()
            commit = _git("commit-tree", tree, "-m", "corrupt", cwd=work).stdout.decode().strip()
            _git("push", str(bare), commit + ":" + SNAPSHOT_REF, "--force", cwd=work)
            before = db.read_bytes()
            with self.assertRaises(SnapshotError) as ctx:
                pull(db, str(bare))
            self.assertEqual(ctx.exception.code, "quick_check_failed")
            self.assertEqual(db.read_bytes(), before)
        finally:
            shutil.rmtree(td, ignore_errors=True)


    def test_pull_into_fresh_directory(self) -> None:
        # reviewer finding: pull must create the destination's parent dirs
        td = Path(tempfile.mkdtemp(prefix="fresh_"))
        try:
            db = _build_store(td)
            bare = _bare(td)
            publish(db, str(bare), publisher="fr")
            dst = td / "fresh" / "nested" / "store"
            pull(dst / "history.db", str(bare))
            self.assertTrue((dst / "history.db").exists())
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_pull_refuses_when_reader_holds_dest(self) -> None:
        # platform-conditional (PR #42 convention, asymmetry disclosed):
        # Windows refuses an open-reader destination (dest_open; the busy
        # checkpoint fires when sqlite reports pending frames, the file
        # lock otherwise), while POSIX safely installs over a live reader.
        # The refusal leg runs where the platform refuses; the install leg
        # documents the POSIX behavior.
        td = Path(tempfile.mkdtemp(prefix="reader_"))
        try:
            db = _build_store(td)
            bare = _bare(td)
            pub = publish(db, str(bare), publisher="rd")
            wmark = pub["manifest"]["watermark"]
            if sys.platform == "win32":
                before = db.read_bytes()
                reader = sqlite3.connect(str(db))
                try:
                    reader.execute("SELECT COUNT(*) FROM items").fetchone()
                    with self.assertRaises(SnapshotError) as ctx:
                        pull(db, str(bare), force=True)
                    self.assertEqual(ctx.exception.code, "dest_open")
                    self.assertEqual(db.read_bytes(), before)
                finally:
                    reader.close()
            else:
                # POSIX: unlink/replace succeed over a live reader, so the
                # install lands (disclosed asymmetry; the refusal leg runs
                # on Windows)
                reader = sqlite3.connect(str(db))
                try:
                    reader.execute("SELECT COUNT(*) FROM items").fetchone()
                    pull(db, str(bare), force=True)
                    conn = sqlite3.connect(str(db))
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT issues_since FROM sync_state WHERE repo = ?",
                        (REPO,)).fetchone()
                    conn.close()
                    self.assertEqual(row["issues_since"], wmark)
                finally:
                    reader.close()
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_pull_refuses_watermarkless_manifest(self) -> None:
        # AC4 closure: watermark is a required key; without it the
        # local-newer guard short-circuits and a newer local store would
        # be overwritten without --force
        td = Path(tempfile.mkdtemp(prefix="wm_"))
        try:
            db = _build_store(td)
            bare = _bare(td)
            publish(db, str(bare), publisher="wm")
            work = td / "w"
            work.mkdir()
            _git("init", "-b", "main", str(work), cwd=td)
            import gzip as _gzip
            import io as _io
            raw = db.read_bytes()
            buf = _io.BytesIO()
            with _gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as g:
                g.write(raw)
            gz = buf.getvalue()
            man = {
                "format": MANIFEST_FORMAT,
                "user_version": CURRENT_USER_VERSION,
                "repo": REPO,
                "host": "github.com",
                # watermark deliberately absent
                "corpus_sha256": "0" * 64,
                "db_sha256": hashlib.sha256(raw).hexdigest(),
                "gz_sha256": hashlib.sha256(gz).hexdigest(),
                "parts": [{"name": "snapshot-0000.gz",
                           "sha256": hashlib.sha256(gz).hexdigest(),
                           "bytes": len(gz)}],
                "publisher": "wmless",
            }
            (work / "manifest.json").write_text(json.dumps(man, sort_keys=True),
                                                encoding="utf-8")
            (work / "snapshot-0000.gz").write_bytes(gz)
            _git("add", "-A", cwd=work)
            tree = _git("write-tree", cwd=work).stdout.decode().strip()
            commit = _git("commit-tree", tree, "-m", "wmless", cwd=work).stdout.decode().strip()
            _git("push", str(bare), commit + ":" + SNAPSHOT_REF, "--force", cwd=work)
            before = db.read_bytes()
            with self.assertRaises(SnapshotError) as ctx:
                pull(db, str(bare))
            self.assertEqual(ctx.exception.code, "bad_manifest")
            self.assertEqual(db.read_bytes(), before)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_pull_refuses_malformed_parts(self) -> None:
        # parts presence is not shape: a non-list or dict-missing part
        # entries must refuse as bad_manifest, not traceback
        td = Path(tempfile.mkdtemp(prefix="parts_"))
        try:
            db = _build_store(td)
            bare = _bare(td)
            publish(db, str(bare), publisher="pt")
            work = td / "w"
            work.mkdir()
            _git("init", "-b", "main", str(work), cwd=td)
            for parts_shape in ("nope", [{"name": "snapshot-0000.gz"}]):
                man = {"format": MANIFEST_FORMAT, "repo": REPO,
                       "user_version": CURRENT_USER_VERSION,
                       "watermark": "2027-06-01T00:00:00Z",
                       "corpus_sha256": "0" * 64, "db_sha256": "0" * 64,
                       "gz_sha256": "0" * 64, "parts": parts_shape,
                       "publisher": "pt"}
                (work / "manifest.json").write_text(json.dumps(man, sort_keys=True),
                                                    encoding="utf-8")
                (work / "snapshot-0000.gz").write_bytes(b"junk")
                _git("add", "-A", cwd=work)
                tree = _git("write-tree", cwd=work).stdout.decode().strip()
                commit = _git("commit-tree", tree, "-m", "parts", cwd=work).stdout.decode().strip()
                _git("push", str(bare), commit + ":" + SNAPSHOT_REF, "--force", cwd=work)
                before = db.read_bytes()
                with self.assertRaises(SnapshotError) as ctx:
                    pull(db, str(bare))
                self.assertEqual(ctx.exception.code, "bad_manifest")
                self.assertEqual(db.read_bytes(), before)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_pull_refuses_wrong_corpus_digest(self) -> None:
        # staged-first digest pin: a manifest whose corpus_sha256 disagrees
        # with the (valid) snapshot bytes refuses BEFORE anything replaces
        # the local store; deleting _check_staged_digest makes this red
        td = Path(tempfile.mkdtemp(prefix="digest_"))
        try:
            db = _build_store(td)
            bare = _bare(td)
            publish(db, str(bare), publisher="dg")
            work = td / "w"
            work.mkdir()
            _git("init", "-b", "main", str(work), cwd=td)
            import gzip as _gzip
            import io as _io
            raw = db.read_bytes()
            buf = _io.BytesIO()
            with _gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as g:
                g.write(raw)
            gz = buf.getvalue()
            man = {
                "format": MANIFEST_FORMAT,
                "user_version": CURRENT_USER_VERSION,
                "repo": REPO,
                "host": "github.com",
                "watermark": "2027-06-01T00:00:00Z",
                "corpus_sha256": "f" * 64,
                "db_sha256": hashlib.sha256(raw).hexdigest(),
                "gz_sha256": hashlib.sha256(gz).hexdigest(),
                "parts": [{"name": "snapshot-0000.gz",
                           "sha256": hashlib.sha256(gz).hexdigest(),
                           "bytes": len(gz)}],
                "publisher": "dg",
            }
            (work / "manifest.json").write_text(json.dumps(man, sort_keys=True),
                                                encoding="utf-8")
            (work / "snapshot-0000.gz").write_bytes(gz)
            _git("add", "-A", cwd=work)
            tree = _git("write-tree", cwd=work).stdout.decode().strip()
            commit = _git("commit-tree", tree, "-m", "digest", cwd=work).stdout.decode().strip()
            _git("push", str(bare), commit + ":" + SNAPSHOT_REF, "--force", cwd=work)
            before = db.read_bytes()
            with self.assertRaises(SnapshotError) as ctx:
                pull(db, str(bare))
            self.assertEqual(ctx.exception.code, "corpus_mismatch")
            self.assertEqual(db.read_bytes(), before)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_pull_replaces_corrupt_destination(self) -> None:
        # pull is the recovery path for a corrupt store: a non-sqlite
        # destination must not traceback; the quiesce falls through and
        # the validated snapshot replaces the unreadable bytes
        td = Path(tempfile.mkdtemp(prefix="corruptdest_"))
        try:
            db = _build_store(td)
            bare = _bare(td)
            publish(db, str(bare), publisher="cd")
            dst = td / "dst"
            dst.mkdir()
            dest = dst / "history.db"
            dest.write_bytes(b"this is not a sqlite database at all" * 100)
            pull(dest, str(bare))
            check = sqlite3.connect(str(dest))
            try:
                (q,) = check.execute("PRAGMA quick_check").fetchone()
            finally:
                check.close()
            self.assertEqual(q, "ok")
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_consolidation_drops_snapshot_provenance(self) -> None:
        # plan s11 pin: consolidate deletes snapshot.* meta keys
        td = Path(tempfile.mkdtemp(prefix="cons_"))
        try:
            db = _build_store(td)
            c = sqlite3.connect(str(db))
            for key, val in (("snapshot.publisher", "x"),
                             ("snapshot.manifest_sha256", "abc"),
                             ("snapshot.pulled_at", "t")):
                c.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, val))
            c.commit()
            c.close()
            from zaxbygraph.doctor import consolidate
            consolidate(db, db, REPO)
            c = sqlite3.connect(str(db))
            rows = c.execute(
                "SELECT key FROM meta WHERE key LIKE 'snapshot.%'").fetchall()
            c.close()
            self.assertEqual(rows, [])
        finally:
            shutil.rmtree(td, ignore_errors=True)


    def test_pull_verify_sample(self) -> None:
        # issue #34 Scope In: --verify-sample N re-fetches N random items
        # from the live source and compares content hashes; the injected
        # stub source returns the SAME payloads the store holds, so the
        # hashes must match (matched == checked)
        td = Path(tempfile.mkdtemp(prefix="vs_"))
        try:
            db, seeded = _build_store_commented(td, return_source=True)
            bare = _bare(td)
            publish(db, str(bare), publisher="vs")

            class _StubSource:
                """Serves the SAME payloads the snapshot store holds (the
                payload-level sampling comparison must match — children
                never enter the projection)."""

                def get_issues(self, numbers):
                    out = {}
                    for n in numbers:
                        out[n] = (seeded.issues[n], None)
                    return out

            dst = td / "dst"
            dst.mkdir()
            result = pull(dst / "history.db", str(bare),
                          verify_sample=2, source_factory=_StubSource)
            vs = result["verify_sample"]
            self.assertEqual(vs["requested"], 2)
            self.assertEqual(vs["checked"], 2)
            self.assertEqual(vs["matched"], 2)
            self.assertEqual(vs["changed"], 0)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_host_derivation(self) -> None:
        # the capability-probe meta key supplies the host; without it a
        # bare temp path derives "unknown" (a "github.com" constant would
        # fail the second leg only)
        td = Path(tempfile.mkdtemp(prefix="host_"))
        try:
            db = _build_store(td)
            from zaxbygraph.snapshot import _host_of
            self.assertEqual(_host_of(db), "unknown")
            c = sqlite3.connect(str(db))
            c.execute("INSERT INTO meta(key, value) VALUES "
                      "('capability_probe:ghe.corp:v3', '{}')")
            c.commit()
            c.close()
            self.assertEqual(_host_of(db), "ghe.corp")
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_verify_sample_detects_drift(self) -> None:
        # the DETECTION half pinned (critic round 4): a stub serving a
        # MUTATED payload must report changed == checked — a mutant
        # collapsing the projections to a constant would pass the
        # matched-only test and fail this one. EVERY sampled item drifts,
        # so the assertion holds regardless of which items the sampler
        # picks.
        td = Path(tempfile.mkdtemp(prefix="vsd_"))
        try:
            db, seeded = _build_store(td, return_source=True)
            bare = _bare(td)
            publish(db, str(bare), publisher="vsd")

            class _DriftedSource:
                """Serves the stored payloads with every title mutated —
                GitHub moved on after the snapshot was taken."""

                def get_issues(self, numbers):
                    out = {}
                    for n in numbers:
                        payload = dict(seeded.issues[n])
                        payload["title"] = payload["title"] + " (edited upstream)"
                        out[n] = (payload, None)
                    return out

            dst = td / "dst"
            dst.mkdir()
            result = pull(dst / "history.db", str(bare), verify_sample=2,
                          source_factory=_DriftedSource)
            vs = result["verify_sample"]
            self.assertEqual(vs["requested"], 2)
            self.assertEqual(vs["checked"], 2)
            self.assertEqual(vs["matched"], 0)
            self.assertEqual(vs["changed"], 2)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_verify_sample_detects_field_drift(self) -> None:
        # a body mutation alone (every sampled item) reports drift: the
        # projected field set discriminates beyond the title
        td = Path(tempfile.mkdtemp(prefix="vsb_"))
        try:
            db, seeded = _build_store(td, return_source=True)
            bare = _bare(td)
            publish(db, str(bare), publisher="vsb")

            class _BodyDriftSource:
                def get_issues(self, numbers):
                    out = {}
                    for n in numbers:
                        payload = dict(seeded.issues[n])
                        payload["body"] = payload["body"] + " - revised"
                        out[n] = (payload, None)
                    return out

            dst = td / "dst"
            dst.mkdir()
            result = pull(dst / "history.db", str(bare), verify_sample=2,
                          source_factory=_BodyDriftSource)
            vs = result["verify_sample"]
            self.assertEqual(vs["checked"], 2)
            self.assertEqual(vs["changed"], 2)
            self.assertEqual(vs["matched"], 0)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_cli_snapshot_envelopes(self) -> None:
        # envelope-guard docstring reason: the CLI publish/pull envelopes
        # are pinned here (schema-notes pattern: exclude + pin elsewhere)
        td = Path(tempfile.mkdtemp(prefix="env_"))
        try:
            db = _build_store(td)
            bare = _bare(td)
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join(
                [str(REPO_ROOT / "src"), str(REPO_ROOT / "tests")])
            import subprocess
            for argv in (
                ["snapshot", "publish", str(bare), "--db", str(db),
                 "--repo", REPO, "--publisher", "env", "--format", "json"],
                ["snapshot", "pull", str(bare), "--db",
                 str(td / "dst2" / "h.db"), "--repo", REPO,
                 "--force", "--format", "json"],
            ):
                (td / ("dst2" if argv[1] == "pull" else "dst2")).mkdir(
                    parents=True, exist_ok=True)
                proc = subprocess.run(
                    [sys.executable, "-m", "zaxbygraph", *argv],
                    capture_output=True, env=env, cwd=str(REPO_ROOT),
                    timeout=120)
                self.assertEqual(proc.returncode, 0, proc.stderr[:200])
                payload = json.loads(proc.stdout.decode("utf-8"))
                for key in ("ok", "db", "repo", "freshness", "data",
                            "truncated"):
                    self.assertIn(key, payload)
        finally:
            shutil.rmtree(td, ignore_errors=True)

