"""Issue #55: a read command must open the database the caller named.

The two read-open helpers in `zaxbygraph.db` built the SQLite URI by
f-string interpolation (`f"file:{path}?mode=ro"`). `sqlite3.connect(...,
uri=True)` parses that as a URI rather than a path, so a filename carrying
a URI metacharacter was read as syntax:

* a `#` starts a fragment, truncating the path and swallowing `mode=ro`
  into it — the connection then opened the truncated path in default
  read-write-create mode, so a READ command CREATED a file and answered
  from it (a violation of issue #2's "reads stop creating empty ones");
* a `%XX` is a percent-escape, so `%61b.sqlite` silently resolved to
  `ab.sqlite` — a different existing database, while the envelope still
  reported the typed path.

`zaxbygraph.snapshot._open_readonly` already used `Path.as_uri()`, which
percent-encodes every character significant to the URI grammar; the fix
routes all three sites through one shared helper (`db._readonly_uri`).

Base behavior pinned here (RED at 6695b6d3): the metacharacter tests below
fail on the unfixed tree, the boundary tests fail with an OverflowError
traceback, and the snapshot tests fail with a raw exception / a published
null-watermark manifest.
"""
from __future__ import annotations

import gzip
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from urllib.parse import unquote_to_bytes

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from zaxbygraph import snapshot  # noqa: E402
from zaxbygraph.cli import main  # noqa: E402
from zaxbygraph.db import (  # noqa: E402
    _readonly_uri,
    connect,
    connect_readonly_query,
    init_schema,
    open_existing,
)

REPO = "probe/uri"


def _build_store(path: Path, title: str = "row") -> Path:
    """A minimal but complete store for `REPO` at `path`."""
    conn = connect(path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO items(repo, number, kind, title, state, raw_json)"
        " VALUES (?, 1, 'issue', ?, 'open', '{}')",
        (REPO, title),
    )
    conn.execute(
        "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
        " last_incr_sync_at, last_error, include_patches, full_sync_pending)"
        " VALUES (?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',"
        " '2026-01-01T00:00:00Z', NULL, 0, 0)",
        (REPO,),
    )
    conn.commit()
    conn.close()
    return path


class ReadonlyUriHelperTest(unittest.TestCase):
    """The shared helper itself: percent-encode what SQLite would parse."""

    def test_metacharacters_survive_the_uri_round_trip(self) -> None:
        # The property that matters: SQLite percent-decodes the URI path, so
        # the encoded form must decode back to the filename the caller gave.
        for name in ("graph#2.sqlite", "%61b.sqlite", "a?b.db", "a b.db",
                     "a%23b.db"):
            p = Path("C:/tmp") / name
            uri = _readonly_uri(p)
            self.assertTrue(uri.endswith("?mode=ro"), name)
            path_part = uri[len("file:///"):].split("?", 1)[0]
            # as_uri() renders the path with forward slashes.
            self.assertEqual(
                unquote_to_bytes(path_part),
                p.as_posix().encode("utf-8"),
                name,
            )

    def test_relative_path_is_made_absolute(self) -> None:
        # as_uri() raises on a relative path; the helper must not.
        uri = _readonly_uri(Path("rel.db"))
        self.assertTrue(uri.startswith("file:///"), uri)

    def test_drive_relative_path_on_another_drive_is_absolute(self) -> None:
        # `Path.cwd() / p` and `Path.absolute()` both leave a drive-relative
        # path on a different drive relative, and as_uri() then raises. This
        # is a Windows-only shape, so it is pinned only where it can exist.
        if sys.platform != "win32":
            self.skipTest("drive-relative paths are a Windows shape")
        # cwd is E: here; a 'C:...' drive-relative path must still resolve.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "drive.db"
            _build_store(target)
            drive, _, rest = str(target).partition(":")
            rel = drive + ":" + rest[1:] if rest.startswith("\\") else None
            if rel is None:
                self.skipTest("temp dir is not on a drive-relative shape")
            uri = _readonly_uri(Path(rel))
            self.assertTrue(uri.startswith("file:///"), uri)
            conn = open_existing(Path(rel))
            try:
                row = conn.execute("SELECT title FROM items").fetchone()
                self.assertEqual(row["title"], "row")
            finally:
                conn.close()

    def test_unc_path_keeps_the_backslash_spelling(self) -> None:
        # SQLite's URI parser rejects an authority component, and as_uri()
        # always emits one, so a UNC path needs the backslash form.
        unc = Path(r"\\server\share\history.db")
        uri = _readonly_uri(unc)
        self.assertTrue(uri.startswith("file:" + str(unc)), uri)
        self.assertNotIn("//server", uri)

    def test_unc_path_metacharacters_are_escaped(self) -> None:
        # The authority assertion above rejects the two obvious wrong fixes,
        # but would still pass an implementation that keeps backslashes and
        # skips escaping. Pin the exact escaped output instead.
        unc = Path(r"\\somehost\share\dir\a#b?c%61d%25e.db")
        uri = _readonly_uri(unc)
        self.assertEqual(
            uri,
            r"file:\\somehost\share\dir\a%23b%3Fc%2561d%2525e.db?mode=ro",
        )
        # And it must decode back to the original filename.
        path_part = uri[len("file:"):].split("?", 1)[0]
        self.assertEqual(
            unquote_to_bytes(path_part), str(unc).encode("utf-8")
        )

    def test_unc_uri_is_accepted_by_sqlite(self) -> None:
        # The discriminating half: SQLite accepts the helper's spelling as a
        # URI (it then fails only because the share does not exist here),
        # while the as_uri() spelling is rejected as a URI outright.
        unc = Path(r"\\server\share\history.db")
        with self.assertRaises(sqlite3.OperationalError) as ours:
            sqlite3.connect(_readonly_uri(unc), uri=True)
        self.assertIn("unable to open database file", str(ours.exception))
        with self.assertRaises(sqlite3.OperationalError) as theirs:
            sqlite3.connect(unc.as_uri() + "?mode=ro", uri=True)
        self.assertIn("invalid uri authority", str(theirs.exception))

    def test_unc_uri_round_trips_through_sqlite_decoding(self) -> None:
        # SQLite percent-decodes the URI path, so the encoded form must decode
        # back to the original filename for every metacharacter.
        for name in (
            r"\\server\share\history.db",
            r"\\server\share\my#store.db",
            r"\\server\share\my?store.db",
            r"\\server\share\my%store.db",
            r"\\server\share\my%23store.db",
        ):
            uri = _readonly_uri(Path(name))
            path_part = uri[len("file:"):].split("?", 1)[0].split("#", 1)[0]
            self.assertEqual(
                unquote_to_bytes(path_part),
                str(Path(name)).encode("utf-8"),
                name,
            )


class HashInDbPathTest(unittest.TestCase):
    """AC1: a '#' in the --db path must not truncate the path."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db = _build_store(Path(self.tmp) / "graph#2.sqlite")

    def _names(self) -> list[str]:
        return sorted(p.name for p in Path(self.tmp).iterdir())

    def test_open_existing_opens_the_named_file_and_creates_nothing(self) -> None:
        before = self._names()
        conn = open_existing(self.db)
        try:
            row = conn.execute("SELECT title FROM items").fetchone()
            self.assertEqual(row["title"], "row")
        finally:
            conn.close()
        # Only the store's own WAL sidecars may appear (documented in
        # open_existing's docstring); a fragment-stripped sibling (`graph`)
        # is the defect.
        after = self._names()
        allowed = {
            "graph#2.sqlite", "graph#2.sqlite-wal", "graph#2.sqlite-shm",
        }
        self.assertEqual(set(after) - allowed, set(), "a read created a file")
        self.assertEqual(
            [n for n in after if n not in before and n not in allowed],
            [], "a read created a file",
        )

    def test_connect_readonly_query_opens_the_named_file(self) -> None:
        # The second read-open helper has its own callers (cli cmd_sql and
        # the MCP sql tool); it must not be left on the f-string form.
        conn = connect_readonly_query(self.db)
        try:
            row = conn.execute("SELECT title FROM items").fetchone()
            self.assertEqual(row["title"], "row")
        finally:
            conn.close()
        self.assertNotIn("graph", self._names())

    def test_the_open_is_read_only(self) -> None:
        # A fix that keeps the metacharacter working but drops mode=ro would
        # still pass the read assertions above; this one pins read-only.
        conn = open_existing(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE t(x)")
        finally:
            conn.close()

    def test_cli_reads_the_named_file(self) -> None:
        rc = main([
            "sql", "SELECT title FROM items",
            "--db", str(self.db), "--repo", REPO, "--format", "compact",
        ])
        self.assertEqual(rc, 0)
        self.assertNotIn("graph", self._names())


class PercentEscapeInDbPathTest(unittest.TestCase):
    """AC2: a '%XX' in the --db path must not resolve to another database."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.named = _build_store(Path(self.tmp) / "%61b.sqlite", "NAMED")
        _build_store(Path(self.tmp) / "ab.sqlite", "DECOY")

    def test_open_existing_reads_the_named_database(self) -> None:
        conn = open_existing(self.named)
        try:
            row = conn.execute("SELECT title FROM items").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["title"], "NAMED")

    def test_cli_reads_the_named_database(self) -> None:
        rc = main([
            "sql", "SELECT title FROM items",
            "--db", str(self.named), "--repo", REPO, "--format", "compact",
        ])
        self.assertEqual(rc, 0)


class FetchLimitBoundaryTest(unittest.TestCase):
    """AC3/AC4: fetchmany(limit + 1) must not overflow a C int."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db = _build_store(Path(self.tmp) / "g.db")
        conn = connect(self.db)
        conn.execute(
            "INSERT INTO pr_files(repo, number, path) VALUES (?, 1, ?)",
            (REPO, "src/zaxbygraph/db.py"),
        )
        conn.commit()
        conn.close()

    def _run(self, argv: list[str]) -> int:
        return main(argv)

    def test_sql_limit_at_the_c_int_boundary_is_refused(self) -> None:
        rc = self._run([
            "sql", "SELECT number FROM items",
            "--db", str(self.db), "--repo", REPO,
            "--limit", "2147483647", "--format", "compact",
        ])
        self.assertNotEqual(rc, 0)

    def test_sql_limit_just_below_the_boundary_still_works(self) -> None:
        # The off-by-one guard: a fix that refuses one value too many breaks
        # every legitimate large query, and a fix that accepts one too many
        # still crashes.
        rc = self._run([
            "sql", "SELECT number FROM items",
            "--db", str(self.db), "--repo", REPO,
            "--limit", "2147483646", "--format", "compact",
        ])
        self.assertEqual(rc, 0)

    def test_file_history_limit_at_the_c_int_boundary_is_refused(self) -> None:
        rc = self._run([
            "file-history", "src/zaxbygraph/db.py",
            "--db", str(self.db), "--repo", REPO,
            "--limit", "2147483647", "--format", "compact",
        ])
        self.assertNotEqual(rc, 0)

    def test_file_history_limit_just_below_the_boundary_still_works(self) -> None:
        rc = self._run([
            "file-history", "src/zaxbygraph/db.py",
            "--db", str(self.db), "--repo", REPO,
            "--limit", "2147483646", "--format", "compact",
        ])
        self.assertEqual(rc, 0)


class CorruptDeflateStreamTest(unittest.TestCase):
    """AC5: a corrupt deflate body must be a coded envelope, not a traceback."""

    def test_corrupt_deflate_reports_gz_bad(self) -> None:
        payload = b"this-is-the-store-bytes" * 64
        gz = gzip.compress(payload)
        # Corrupt only the deflate body: the header, the CRC32 trailer and the
        # ISIZE trailer stay intact, so gzip raises zlib.error rather than
        # OSError/EOFError.
        raw = bytearray(gz)
        for i in range(12, min(len(raw) - 8, 12 + 400)):
            raw[i] ^= 0xFF
        raw = bytes(raw)

        manifest = {
            "format": "zaxbygraph-snapshot/1",
            "user_version": 11,
            "repo": REPO,
            "host": "github.com",
            "watermark": "2026-01-01T00:00:00Z",
            "gz_sha256": hashlib.sha256(raw).hexdigest(),
            "db_sha256": hashlib.sha256(payload).hexdigest(),
            "db_bytes": len(payload),
            "parts": [{
                "name": "db.gz",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "uncompressed_bytes": len(payload),
            }],
        }
        staged = Path(tempfile.mkdtemp()) / "staged.db"
        staged.write_bytes(payload)

        with self.assertRaises(snapshot.SnapshotError) as ctx:
            snapshot._verify_staged(staged, raw, manifest)
        self.assertEqual(ctx.exception.code, "gz_bad")
        # The cause must be the zlib error the widened tuple now names.
        self.assertIsInstance(ctx.exception.__cause__, zlib.error)


class PublishNullWatermarkTest(unittest.TestCase):
    """AC6: publish must refuse a store whose watermark is null."""

    def test_publish_refuses_a_null_watermark(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        remote = tmp / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", str(remote)], check=True,
            capture_output=True,
        )
        store = tmp / "store.db"
        conn = connect(store)
        init_schema(conn)
        conn.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_incr_sync_at, last_error, include_patches, full_sync_pending)"
            " VALUES (?, NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',"
            " NULL, 0, 0)",
            (REPO,),
        )
        conn.commit()
        conn.close()

        with self.assertRaises(snapshot.SnapshotError) as ctx:
            snapshot.publish(store, str(remote), "trace-test", expected_repo=REPO)
        self.assertEqual(ctx.exception.code, "bad_manifest")
        # Nothing may have been pushed.
        pushed = subprocess.run(
            ["git", "-C", str(remote), "ls-remote",
             "refs/zaxbygraph/snapshot"],
            capture_output=True, text=True,
        )
        self.assertEqual(pushed.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
