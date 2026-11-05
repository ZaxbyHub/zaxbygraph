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
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import ast
import zlib
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlsplit

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


def _uri_path(uri: str) -> bytes:
    """The decoded path component of a `file:` URI, read the way SQLite does.

    `urlsplit` is used rather than slicing off a literal "file:///" prefix,
    because a POSIX absolute path is `file:///tmp/x` (three slashes, the
    first belonging to the path) while a Windows one is `file:///C:/tmp/x`
    (the drive letter follows the authority). Slicing the prefix strips the
    POSIX path's leading slash and silently compares the wrong string.
    """
    path = urlsplit(uri).path
    if sys.platform == "win32" and re.match(r"^/[A-Za-z]:", path):
        # as_uri() emits 'file:///C:/...', so the path arrives as '/C:/...'
        # and SQLite strips that leading slash for the drive form.
        path = path[1:]
    return unquote_to_bytes(path)


class ReadonlyUriHelperTest(unittest.TestCase):
    """The shared helper itself: percent-encode what SQLite would parse."""

    def test_metacharacters_survive_the_uri_round_trip(self) -> None:
        # The property that matters: SQLite percent-decodes the URI path, so
        # the encoded form must decode back to the filename the caller gave.
        # The base is the host's own temp root so the test is constructible
        # on Windows and POSIX alike (a 'C:/tmp' spelling is merely a
        # relative path on POSIX).
        base = Path("C:/tmp") if sys.platform == "win32" else Path("/tmp")
        for name in ("graph#2.sqlite", "%61b.sqlite", "a b.db", "a%23b.db"):
            p = base / name
            uri = _readonly_uri(p)
            self.assertTrue(uri.endswith("?mode=ro"), name)
            self.assertEqual(_uri_path(uri), p.as_posix().encode("utf-8"), name)

    def test_relative_path_is_made_absolute(self) -> None:
        # as_uri() raises on a relative path; the helper must not.
        uri = _readonly_uri(Path("rel.db"))
        self.assertTrue(uri.startswith("file:///"), uri)

    def test_relative_path_is_not_lexically_collapsed(self) -> None:
        # os.path.abspath collapses '..' lexically, but opening the original
        # spelling resolves a symlink first and then applies '..' -- on POSIX
        # those select different files. The helper must therefore normalize
        # only the Windows drive-relative shape and leave every other
        # relative spelling intact.
        uri = _readonly_uri(Path("a/../b.db"))
        self.assertIn("a/../b.db", uri)

    def test_drive_relative_path_is_made_absolute(self) -> None:
        # A Windows drive-relative spelling ('E:drive.db') is not absolute to
        # pathlib and as_uri() refuses it. On POSIX a drive is always empty,
        # so the ordinary relative spelling is the same branch and the
        # assertion holds there too.
        #
        # The spelling is built on a drive OTHER than the process cwd's, so
        # the abspath branch is actually observable: on a same-drive spelling
        # `Path.cwd() / p` and `os.path.abspath(p)` agree, which is why the
        # earlier version of this test could not fail.
        if sys.platform == "win32":
            cwd_drive = Path(os.getcwd()).drive
            other = next(
                (d for d in "CDEFGHIJKLMNOPQRSTUVWXYZ"
                 if (d + ":") != cwd_drive),
                None,
            )
            if other is None:
                self.skipTest("no second drive letter available")
            rel = other + ":drive.db"
            uri = _readonly_uri(Path(rel))
            # abspath resolves a drive-relative spelling against that drive's
            # own current directory, so the URI must name the other drive.
            self.assertTrue(uri.startswith("file:///"), uri)
            self.assertTrue(
                uri.startswith("file:///%s:/" % other), uri
            )
            self.assertIn("drive.db", uri)
        else:
            uri = _readonly_uri(Path("drive.db"))
            self.assertTrue(uri.startswith("file:///"), uri)
            self.assertIn("drive.db", uri)

    def test_unc_path_keeps_the_backslash_spelling(self) -> None:
        # SQLite's URI parser rejects an authority component, and as_uri()
        # always emits one, so a UNC path needs the backslash form.
        unc = Path(r"\\server\share\history.db")
        uri = _readonly_uri(unc)
        if sys.platform == "win32":
            self.assertTrue(uri.startswith("file:" + str(unc)), uri)
            self.assertNotIn("//server", uri)
        else:
            # On POSIX a UNC spelling is an ordinary relative filename, so the
            # helper joins it to the cwd; the URI must round-trip to that.
            self.assertEqual(
                _uri_path(uri), (Path.cwd() / unc).as_posix().encode("utf-8")
            )

    def test_unc_path_metacharacters_are_escaped(self) -> None:
        # The authority assertion above rejects the two obvious wrong fixes,
        # but would still pass an implementation that keeps backslashes and
        # skips escaping. Pin the exact escaped output instead.
        unc = Path(r"\\somehost\share\dir\a#b?c%61d%25e.db")
        uri = _readonly_uri(unc)
        if sys.platform == "win32":
            self.assertEqual(
                uri,
                r"file:\\somehost\share\dir\a%23b%3Fc%2561d%2525e.db?mode=ro",
            )
            self.assertEqual(_uri_path(uri), str(unc).encode("utf-8"))
        else:
            # On POSIX the spelling is an ordinary relative filename, so pin
            # the round-trip rather than a Windows URI spelling.
            self.assertEqual(
                _uri_path(uri), (Path.cwd() / unc).as_posix().encode("utf-8")
            )

    def test_unc_uri_is_accepted_by_sqlite(self) -> None:
        # The discriminating half: SQLite accepts the helper's spelling as a
        # URI (it then fails only because the share does not exist here),
        # while the as_uri() spelling is rejected as a URI outright. A
        # non-localhost host is required: SQLite accepts 'localhost' as an
        # authority, so only a remote host proves the rejection.
        unc = Path(r"\\somehost\share\history.db")
        uri = _readonly_uri(unc)
        with self.assertRaises(sqlite3.OperationalError) as ours:
            sqlite3.connect(uri, uri=True)
        self.assertIn("unable to open database file", str(ours.exception))
        if sys.platform == "win32":
            with self.assertRaises(sqlite3.OperationalError) as theirs:
                sqlite3.connect(unc.as_uri() + "?mode=ro", uri=True)
            self.assertIn("invalid uri authority", str(theirs.exception))
        else:
            # On POSIX the spelling is a relative filename and as_uri() simply
            # refuses it, which is the same class of wrong answer.
            with self.assertRaises(ValueError):
                unc.as_uri()

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
            if sys.platform == "win32":
                self.assertEqual(
                    _uri_path(uri), str(Path(name)).encode("utf-8"), name
                )
            else:
                # On POSIX the spelling is an ordinary relative filename.
                self.assertEqual(
                    _uri_path(uri),
                    (Path.cwd() / name).as_posix().encode("utf-8"),
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

    def test_cli_reads_a_relative_db_path(self) -> None:
        # AC7's end-to-end leg: the CLI must accept a relative --db, which is
        # the shape the plan critic named as the regression surface for the
        # as_uri() ValueError. A helper-only unit test would not catch a
        # CLI-to-helper seam break.
        previous = os.getcwd()
        os.chdir(self.tmp)
        try:
            rc = main([
                "sql", "SELECT title FROM items",
                "--db", "graph#2.sqlite", "--repo", REPO, "--format", "compact",
            ])
        finally:
            os.chdir(previous)
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
        # AC2's CLI leg must assert WHICH database was read, not merely that
        # the command succeeded: reverting connect_readonly_query to the
        # f-string leaves rc==0 while the envelope answers from the decoy.
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([
                "sql", "SELECT title FROM items",
                "--db", str(self.named), "--repo", REPO, "--format", "compact",
            ])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("NAMED", out)
        self.assertNotIn("DECOY", out)


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

    def _run_capture(self, argv: list[str]) -> tuple[int, str]:
        """Run the CLI capturing stdout, so a refusal can be identified by its
        envelope rather than by an exit code shared with unrelated failures."""
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(argv)
        return rc, buf.getvalue()

    def test_sql_limit_at_the_c_int_boundary_is_refused(self) -> None:
        # The envelope must name the limit, not just exit non-zero: exit 1 is
        # also what an unrelated store failure returns, so an exit-code-only
        # assertion would pass for the wrong reason.
        rc, out = self._run_capture([
            "sql", "SELECT number FROM items",
            "--db", str(self.db), "--repo", REPO,
            "--limit", "2147483647", "--format", "compact",
        ])
        self.assertEqual(rc, 2)
        self.assertIn("bad_request", out)
        self.assertIn("row limit 2147483647 exceeds", out)

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
        rc, out = self._run_capture([
            "file-history", "src/zaxbygraph/db.py",
            "--db", str(self.db), "--repo", REPO,
            "--limit", "2147483647", "--format", "compact",
        ])
        self.assertEqual(rc, 2)
        self.assertIn("bad_request", out)
        self.assertIn("row limit 2147483647 exceeds", out)

    def test_file_history_limit_just_below_the_boundary_still_works(self) -> None:
        rc = self._run([
            "file-history", "src/zaxbygraph/db.py",
            "--db", str(self.db), "--repo", REPO,
            "--limit", "2147483646", "--format", "compact",
        ])
        self.assertEqual(rc, 0)

    def test_sql_limit_above_the_sql_bind_range_is_refused(self) -> None:
        # The 64-bit sibling of the C-int ceiling: a limit above 2**63-1
        # overflows the SQL bind, which escaped as a traceback at base and at
        # head (found by the PR #57 swarm review). Both read commands must
        # refuse it as a caller error.
        for argv in (
            ["sql", "SELECT number FROM items"],
            ["file-history", "src/zaxbygraph/db.py"],
        ):
            rc, out = self._run_capture(
                argv + ["--db", str(self.db), "--repo", REPO,
                        "--limit", "99999999999999999999999",
                        "--format", "compact"]
            )
            self.assertEqual(rc, 2, argv[0])
            self.assertIn("bad_request", out)
            self.assertIn("exceeds the maximum of", out)

    def test_search_limit_above_the_sql_bind_range_is_refused(self) -> None:
        # search/open/churn bind their limit as a SQL parameter and reach the
        # same 64-bit ceiling through _clamp_limit.
        rc, out = self._run_capture([
            "search", "row",
            "--db", str(self.db), "--repo", REPO,
            "--limit", "99999999999999999999999", "--format", "compact",
        ])
        self.assertEqual(rc, 2)
        self.assertIn("bad_request", out)


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
        # The cause must be one of the corruption family the widened tuple
        # names. Asserting zlib.error specifically is zlib-build dependent:
        # where the corruption lands, gzip raises zlib.error (85 windows),
        # gzip.BadGzipFile (23) or EOFError (3) - and the latter two were
        # already covered before this PR, so pinning the class would fail a
        # CI leg whose zlib reports a different member for the same bytes.
        self.assertIsInstance(
            ctx.exception.__cause__, (zlib.error, gzip.BadGzipFile, EOFError)
        )


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
        # A store-state code distinct from both bad_manifest (a malformed
        # RECEIVED manifest, pull side) and repo_not_synced (no sync_state
        # row, which an empty repository already has) - one code spanning
        # three conditions sends a consumer to the wrong remedy.
        self.assertEqual(ctx.exception.code, "no_watermark")
        # Nothing may have been pushed. `git -C <repo> ls-remote <ref>` reads
        # the REF as the repository and exits 128 with empty stdout, so the
        # emptiness assertion used to be unable to fail; the repository is
        # the positional argument and the ref is a pattern.
        for ref in ("refs/zaxbygraph/snapshot",
                    "refs/heads/zaxbygraph-snapshot"):
            pushed = subprocess.run(
                ["git", "ls-remote", str(remote), ref],
                capture_output=True, text=True,
            )
            self.assertEqual(pushed.returncode, 0, pushed.stderr)
            self.assertEqual(pushed.stdout.strip(), "", ref)


class SiblingGuardrailTest(unittest.TestCase):
    """Phase 4.2 guardrail for the class: a guard applied at one site but not
    at its siblings.

    All four defects in issue #55 were instances of that class - `as_uri()`
    fixed at `snapshot.py`, `_MAX_PAGE` at `mcp_server.py`, the gzip tuple,
    the watermark guard - and in each case the fix landed at the reported
    site rather than at the family. These two tests pin the family directly
    in source, so a future sibling added with the old spelling fails here
    rather than shipping.

    Both scans are AST-based: a text scan also matches the docstrings and
    comments that describe the very calls being pinned.
    """

    def _sources(self):
        return sorted((ROOT / "src" / "zaxbygraph").rglob("*.py"))

    def _calls(self, path):
        import ast

        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                yield node

    def _enclosing_function(self, path, lineno):
        import ast

        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                    return node
        return None

    def _is_readonly_uri_arg(self, path, node, arg) -> bool:
        """True when `arg` is, or resolves within its function to, a
        `_readonly_uri(...)` call.

        The direct call is the common shape. A name is resolved to the LAST
        assignment to that name BEFORE the call site, so a correct refactor
        that hoists the URI into a local passes (cubic run 1 flagged the
        text-only form), while a later rebinding to a hand-built URI is still
        caught (the earlier form accepted any assignment anywhere in the
        function, ignoring order).
        """
        import ast

        if isinstance(arg, ast.Call):
            return ast.unparse(arg.func).split(".")[-1] == "_readonly_uri"
        if not isinstance(arg, ast.Name):
            return False
        fn = self._enclosing_function(path, node.lineno)
        if fn is None:
            return False
        # Collect every binding of this name with its line number, then take
        # the last one that precedes the connect call.
        bindings = []
        for stmt in ast.walk(fn):
            if isinstance(stmt, ast.Assign):
                targets, value, line = stmt.targets, stmt.value, stmt.lineno
            elif isinstance(stmt, ast.AnnAssign):
                targets, value, line = [stmt.target], stmt.value, stmt.lineno
            else:
                continue
            if not isinstance(value, ast.Call):
                continue
            if ast.unparse(value.func).split(".")[-1] != "_readonly_uri":
                continue
            for tgt in targets:
                if isinstance(tgt, ast.Name) and tgt.id == arg.id:
                    bindings.append(line)
        if not bindings:
            return False
        return max(bindings) < node.lineno

    def _uri_connects(self):
        """Every `sqlite3.connect(..., uri=<truthy>)` call in src/, as
        (path, node) pairs.

        Truthy rather than the literal `True`: `uri=1` is honoured by sqlite3
        and opens the same file, so a scan that requires the exact spelling
        lets a hand-built URI through. The callee is matched on any spelling
        that resolves to sqlite3's connect - `sqlite3.connect`,
        `dbapi2.connect`, or a direct `from sqlite3 import connect` - because
        a scan keyed on the literal attribute path is evaded by an alias.
        """
        import ast

        out = []
        for path in self._sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
                    imported.update(a.name for a in node.names)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = ast.unparse(node.func)
                if fn not in ("sqlite3.connect", "connect") and \
                        not fn.endswith(".connect"):
                    continue
                if fn == "connect" and "connect" not in imported:
                    continue
                kw = {k.arg: ast.unparse(k.value) for k in node.keywords}
                if kw.get("uri") not in ("True", "1"):
                    continue
                out.append((path, node))
        return out

    def test_no_read_open_builds_its_uri_by_hand(self) -> None:
        offenders = []
        for path, node in self._uri_connects():
            if node.args and self._is_readonly_uri_arg(path, node, node.args[0]):
                continue
            offenders.append("%s:%d" % (path.name, node.lineno))
        self.assertEqual(offenders, [])

    def test_the_uri_guard_finds_every_uri_connect(self) -> None:
        # Positive control (cubic run 1): a guard that silently matches
        # nothing is indistinguishable from a clean tree. The scan must find
        # the shipped read-open sites.
        found = [path.name for path, _ in self._uri_connects()]
        self.assertEqual(len(found), 3)
        self.assertEqual(sorted(found), ["db.py", "db.py", "snapshot.py"])

    def test_the_uri_guard_rejects_a_hand_built_uri(self) -> None:
        # Negative control: without one, neutering the offender check leaves
        # the whole module green, so the guard's bite is unverified. The scan
        # must flag a planted hand-built read-open URI.
        import ast

        planted = (
            "import sqlite3\n"
            "def _open(p):\n"
            "    return sqlite3.connect(f\"file:{p}?mode=ro\", uri=True)\n"
        )
        path = self._sources()[0]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(ast.parse(planted)):
            if not isinstance(node, ast.Call):
                continue
            if ast.unparse(node.func) != "sqlite3.connect":
                continue
            kw = {k.arg: ast.unparse(k.value) for k in node.keywords}
            if kw.get("uri") != "True":
                continue
            if node.args and self._is_readonly_uri_arg(path, node, node.args[0]):
                continue
            offenders.append(node.lineno)
        self.assertEqual(offenders, [3], "the guard did not flag a planted URI")

    def test_the_uri_guard_rejects_a_rebound_local(self) -> None:
        # The same-name-rebinding shape: assign from _readonly_uri, then
        # rebind to a hand-built URI before the connect. The resolver must
        # take the LAST binding before the call, not any binding.
        import ast

        planted = (
            "import sqlite3\n"
            "def _open(p):\n"
            "    uri = _readonly_uri(p)\n"
            "    uri = f\"file:{p}?mode=ro\"\n"
            "    return sqlite3.connect(uri, uri=True)\n"
        )
        path = self._sources()[0]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(ast.parse(planted)):
            if not isinstance(node, ast.Call):
                continue
            if ast.unparse(node.func) != "sqlite3.connect":
                continue
            kw = {k.arg: ast.unparse(k.value) for k in node.keywords}
            if kw.get("uri") != "True":
                continue
            if node.args and self._is_readonly_uri_arg(path, node, node.args[0]):
                continue
            offenders.append(node.lineno)
        self.assertEqual(offenders, [5], "the guard accepted a rebound local")

    def test_every_over_fetch_site_uses_the_ceiling(self) -> None:
        # A fetch site whose enclosing function normalizes with _clamp_limit
        # instead of _clamp_fetch_limit re-opens AC3/AC4.
        import ast

        offenders = []
        scanned = []
        for path in self._sources():
            for node in self._calls(path):
                if ast.unparse(node.func).split(".")[-1] != "fetchmany":
                    continue
                # Every fetchmany counts, not only the ones whose argument
                # text contains "limit": a sibling spelled `fetchmany(n + 1)`
                # was invisible to the earlier filter.
                scanned.append("%s:%d" % (path.name, node.lineno))
                if not any("limit" in ast.unparse(a) for a in node.args):
                    continue
                fn = self._enclosing_function(path, node.lineno)
                # Check the function's ASSIGNMENTS, not its unparsed text:
                # ast.unparse emits the docstring, so a prose mention of
                # _clamp_fetch_limit satisfied the earlier predicate.
                clamped = False
                if fn is not None:
                    for stmt in ast.walk(fn):
                        if isinstance(stmt, ast.Assign):
                            values = [stmt.value]
                        elif isinstance(stmt, ast.AnnAssign):
                            values = [stmt.value]
                        else:
                            continue
                        for v in values:
                            if (isinstance(v, ast.Call)
                                    and ast.unparse(v.func).split(".")[-1]
                                    == "_clamp_fetch_limit"):
                                clamped = True
                if not clamped:
                    offenders.append("%s:%d" % (path.name, node.lineno))
        # Positive control: the scan must actually have found both shipped
        # over-fetch sites, or an empty offenders list proves nothing.
        self.assertEqual(len(scanned), 2)
        self.assertEqual(offenders, [])

    def test_the_fetch_guard_rejects_a_docstring_mention(self) -> None:
        # Negative control for the prose-defeat: a function whose docstring
        # names _clamp_fetch_limit but whose body uses _clamp_limit must be
        # flagged.
        import ast

        planted = (
            "def _sibling(conn, limit):\n"
            "    \"\"\"Uses _clamp_fetch_limit for the ceiling.\"\"\"\n"
            "    limit = _clamp_limit(limit)\n"
            "    return conn.execute('SELECT 1').fetchmany(limit + 1)\n"
        )
        fn = next(n for n in ast.walk(ast.parse(planted))
                  if isinstance(n, ast.FunctionDef))
        clamped = False
        for stmt in ast.walk(fn):
            if isinstance(stmt, ast.Assign):
                values = [stmt.value]
            elif isinstance(stmt, ast.AnnAssign):
                values = [stmt.value]
            else:
                continue
            for v in values:
                if (isinstance(v, ast.Call)
                        and ast.unparse(v.func).split(".")[-1]
                        == "_clamp_fetch_limit"):
                    clamped = True
        self.assertFalse(clamped, "a docstring mention satisfied the guard")


if __name__ == "__main__":
    unittest.main()
