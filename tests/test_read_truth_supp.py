"""Supplementary checks for ZaxbyHub/zaxbygraph#31 (read truth).

Complements the frozen tests/test_read_truth.py without touching it (the
checkpoint pins its bytes): forge-host grammar boundaries, --max-age
parsing and boundary semantics, comment-selection edges, the real ci.yml
matrix block (not any-text matching), and CLI-level environment-guard
envelopes. Same offline, hermetic, stdlib-only rules as the frozen file.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from zaxbygraph.repo import (
    _loopback_authority,
    _parse_remote_info,
    _valid_forge_host,
)

REPO = "acme/forgegate"
REPO_ROOT = Path(__file__).resolve().parents[1]


class ForgeGrammarTests(unittest.TestCase):
    def test_valid_forge_hosts(self) -> None:
        ok = [
            "gitlab.internal:8443",
            "gitlab.internal",
            "my_forge.internal",
            "gitea.example:0",
            "gitea.example:65535",
        ]
        for value in ok:
            with self.subTest(value=value):
                self.assertTrue(_valid_forge_host(value), value)

    def test_invalid_forge_hosts(self) -> None:
        bad = [
            "a..b",
            "-a.com",
            "a-.com",
            ".a.com",
            "a.com:8444444",
            "a.com:-1",
            "a.com:",
            "!!not a host!!",
            "",
            "   ",
            ":65535",
        ]
        for value in bad:
            with self.subTest(value=value):
                self.assertFalse(_valid_forge_host(value), value)

    def test_loopback_authority_forms(self) -> None:
        yes = [
            "127.0.0.1",
            "127.0.0.1:41233",
            "LOCALHOST",
            "localhost:8443",
            "::1",
            "[::1]",
            "[::1]:41233",
            "::1:41233",
        ]
        no = ["::2", "[::2]", "127.0.0.2", "example.com:1", "1.2.3.4:99999", ""]
        for value in yes:
            with self.subTest(value=value):
                self.assertTrue(_loopback_authority(value), value)
        for value in no:
            with self.subTest(value=value):
                self.assertFalse(_loopback_authority(value), value)

    def test_loopback_without_git_prefix_unchanged(self) -> None:
        info = _parse_remote_info("http://u@127.0.0.1:41233/org/repo")
        self.assertEqual(info, ("127.0.0.1_41233", "org/repo"))

    def test_forge_env_boundary_values(self) -> None:
        url = "http://u@127.0.0.1:41233/git/org/repo"
        for env_value, expected_host in [
            ("a..b", None),
            ("a-.com", None),
            ("gitlab.internal:8444444", None),
            ("my_forge.internal", "my_forge.internal"),
            ("Gitea.Example:3000", "gitea.example_3000"),
        ]:
            with self.subTest(env=env_value):
                with mock.patch.dict(os.environ):
                    os.environ["ZAXBYGRAPH_PROXY_FORGE_HOST"] = env_value
                    info = _parse_remote_info(url)
                if expected_host is None:
                    self.assertIsNone(info)
                else:
                    self.assertEqual(info[0], expected_host)


class MaxAgeParsingTests(unittest.TestCase):
    def _parse(self, value: str):
        from zaxbygraph.cli import _parse_max_age

        return _parse_max_age(value)

    def test_units(self) -> None:
        self.assertEqual(self._parse("90"), 90)
        self.assertEqual(self._parse("2h"), 7200)
        self.assertEqual(self._parse("1d"), 86400)
        self.assertEqual(self._parse("30m"), 1800)
        self.assertEqual(self._parse("45s"), 45)
        self.assertEqual(self._parse("0"), 0)

    def test_invalid(self) -> None:
        from argparse import ArgumentTypeError

        for value in ("abc", "-5", "", "d", "h", "1x", "1.5h"):
            with self.subTest(value=value):
                with self.assertRaises(ArgumentTypeError):
                    self._parse(value)


class CommentCapEdgeTests(unittest.TestCase):
    def _comments(self, authors):
        return [
            {"github_id": 9000 + i, "author": a, "created_at": f"2026-03-0{1 + i // 9}T12:00:{i:02d}Z"}
            for i, a in enumerate(authors)
        ]

    def test_multiple_maintainers(self) -> None:
        from zaxbygraph.query import cap_comments

        data = {"comments": self._comments(
            ["acme", "bob", "acme", "eve", "carol"]
        )}
        out = cap_comments(data, 3, "acme/forgegate")
        self.assertEqual([c["github_id"] for c in out["comments"]], [9000, 9002, 9004])
        self.assertEqual(out["comments_omitted"], 2)

    def test_k_below_preferred_set(self) -> None:
        from zaxbygraph.query import cap_comments

        data = {"comments": self._comments(["bob", "dave", "acme", "eve", "carol"])}
        out = cap_comments(data, 2, "acme/forgegate")
        self.assertEqual([c["github_id"] for c in out["comments"]], [9000, 9004])
        self.assertEqual(out["comments_omitted"], 3)

    def test_no_repo_no_maintainer_preference(self) -> None:
        from zaxbygraph.query import cap_comments

        data = {"comments": self._comments(["bob", "dave", "acme", "eve", "carol"])}
        out = cap_comments(data, 2, None)
        self.assertEqual([c["github_id"] for c in out["comments"]], [9000, 9004])
        self.assertEqual(out["comments_omitted"], 3)

    def test_history_payload_without_comments_is_untouched(self) -> None:
        from zaxbygraph.query import cap_comments

        data = {"revisions": [{"a": 1}]}
        out = cap_comments(data, 3, "acme/forgegate")
        self.assertEqual(out["comments_omitted"], 0)
        self.assertNotIn("comments", out)


class CiMatrixBlockTests(unittest.TestCase):
    def test_matrix_block_lists_versions_on_both_oses(self) -> None:
        text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        m = re.search(
            r"os:\s*\[([^\]]+)\]\s*python-version:\s*\[([^\]]+)\]", text
        )
        self.assertIsNotNone(m, "ci.yml must keep an os/python-version matrix block")
        oses = [o.strip() for o in m.group(1).split(",")]
        versions = [v.strip().strip('"') for v in m.group(2).split(",")]
        self.assertEqual(
            oses, ["windows-latest", "ubuntu-latest"]
        )
        self.assertEqual(
            versions, ["3.11", "3.12", "3.13", "3.14"]
        )


class _CliStore(unittest.TestCase):
    """A temp store for in-process CLI runs (mirrors the frozen harness)."""

    def setUp(self) -> None:
        from zaxbygraph.db import connect, init_schema
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

    def run_cli(self, argv):
        from zaxbygraph.cli import main

        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.dict(os.environ):
            os.environ.pop("ZAXBYGRAPH_DB", None)
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    code = main(argv)
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 2
        text = out.getvalue()
        try:
            payload = json.loads(text) if text.strip() else None
        except ValueError:
            payload = None
        return code, payload, text, err.getvalue()


class EnvironmentGuardEnvelopeTests(_CliStore):
    def test_floor_guard_failure_is_a_coded_envelope(self) -> None:
        from zaxbygraph import db as db_mod

        with mock.patch.object(
            db_mod, "assert_sqlite_supported",
            side_effect=RuntimeError("ZG-FLOOR-CODED"),
        ):
            code, payload, _out, err = self.run_cli(
                ["item", "1", "--repo", REPO, "--db", self.db, "--format", "json"]
            )
        self.assertEqual(code, 2)
        self.assertIsNotNone(payload)
        self.assertIs(payload.get("ok"), False)
        self.assertEqual(payload["error"]["code"], "environment_guard")
        self.assertIn("ZG-FLOOR-CODED", payload["error"]["message"])
        self.assertNotIn("Traceback", err)

    def test_fts5_guard_failure_is_a_coded_envelope(self) -> None:
        from zaxbygraph import db as db_mod

        with mock.patch.object(
            db_mod, "assert_fts5_available",
            side_effect=RuntimeError("ZG-FTS5-CODED"),
        ):
            code, payload, _out, err = self.run_cli(
                ["search", "hello", "--repo", REPO, "--db", self.db,
                 "--format", "json"]
            )
        self.assertEqual(code, 2)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["error"]["code"], "environment_guard")
        self.assertIn("ZG-FTS5-CODED", payload["error"]["message"])
        self.assertNotIn("Traceback", err)


class MaxAgeFaultPathTests(_CliStore):
    """4.5-review R1: the --max-age fault paths keep the coded-envelope
    contracts instead of escaping as tracebacks, and a refresh never seeds
    a corpus schema into a foreign SQLite file."""

    def test_corrupt_store_with_max_age_answers_store_unreadable(self) -> None:
        from zaxbygraph import cli as cli_mod

        junk = Path(self._td.name) / "junk.db"
        junk.write_bytes(b"this is not a sqlite database at all" * 64)
        with mock.patch.object(
            cli_mod, "_run_refresh_sync",
            side_effect=AssertionError("sync seam must not run on a corrupt store"),
        ):
            code, payload, _out, err = self.run_cli(
                ["item", "1", "--repo", REPO, "--db", str(junk),
                 "--format", "json", "--max-age", "60"]
            )
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        self.assertIsNotNone(payload)
        self.assertIs(payload.get("ok"), False)
        self.assertEqual(payload["error"]["code"], "store_unreadable")

    def test_foreign_sqlite_file_is_never_seeded_by_refresh(self) -> None:
        import sqlite3 as _sq

        foreign = Path(self._td.name) / "foreign.db"
        conn = _sq.connect(str(foreign))
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
        conn.close()
        from zaxbygraph import cli as cli_mod

        with mock.patch.object(
            cli_mod, "_run_refresh_sync",
            side_effect=AssertionError("sync seam must not run on a foreign file"),
        ):
            code, payload, _out, err = self.run_cli(
                ["item", "1", "--repo", REPO, "--db", str(foreign),
                 "--format", "json", "--max-age", "60"]
            )
        # No live sync ran (offline answer), no schema was created, and the
        # read answered the store_unreadable envelope.
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        conn = _sq.connect(str(foreign))
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(tables, {"t"}, "refresh must not seed a foreign file")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["error"]["code"], "store_unreadable")

    def test_waited_for_writer_counts_as_refreshed(self) -> None:
        from zaxbygraph import cli as cli_mod

        calls = []

        class _FakeLock:
            def release_owned(self):
                pass

        def fake_lock(db_path, wait=False):
            self.assertTrue(wait)
            # Another writer refreshes while we hold the lock.
            c = sqlite3.connect(self.db)
            try:
                c.execute(
                    "UPDATE sync_state SET last_incr_sync_at = ? WHERE repo = ?",
                    (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), REPO),
                )
                c.commit()
            finally:
                c.close()
            return _FakeLock()

        def runner(*a, **k):
            calls.append(1)
            return 0

        with mock.patch.object(cli_mod, "acquire_sync_lock", fake_lock):
            with mock.patch.object(cli_mod, "_run_refresh_sync", runner):
                code, payload, _out, err = self.run_cli(
                    ["item", "1", "--repo", REPO, "--db", self.db,
                     "--format", "json", "--max-age", "60"]
                )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            calls, [], "a store refreshed by the waited-for writer needs no second sync"
        )
        self.assertIs((payload or {}).get("freshness", {}).get("refreshed"), True)


class RefreshContractTests(_CliStore):
    """PR #47 review regressions: the refresh keeps the store's own
    retention, maps environment guards like the sync path, and never
    mutates a file it does not refresh."""

    def _set_stamp(self, seconds_ago: int) -> None:
        stamp = (
            datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute(
            "UPDATE sync_state SET last_incr_sync_at = ? WHERE repo = ?",
            (stamp, REPO),
        )
        self.conn.commit()

    def _set_include_patches(self, value: int) -> None:
        self.conn.execute(
            "UPDATE sync_state SET include_patches = ? WHERE repo = ?",
            (value, REPO),
        )
        self.conn.commit()

    def test_refresh_carries_include_patches(self) -> None:
        from zaxbygraph import cli as cli_mod

        self._set_stamp(10_000)
        self._set_include_patches(1)
        seen = {}

        def runner(conn, slug, *, include_patches=False, **kw):
            seen["include_patches"] = include_patches
            c = sqlite3.connect(self.db)
            try:
                c.execute(
                    "UPDATE sync_state SET last_incr_sync_at = ? WHERE repo = ?",
                    (
                        (datetime.now(timezone.utc) - timedelta(seconds=1))
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        REPO,
                    ),
                )
                c.commit()
            finally:
                c.close()
            return 0

        with mock.patch.object(cli_mod, "_run_refresh_sync", runner):
            code, payload, _out, err = self.run_cli(
                ["item", "1", "--repo", REPO, "--db", self.db,
                 "--format", "json", "--max-age", "60"]
            )
        self.assertEqual(code, 0, err)
        self.assertIs(seen.get("include_patches"), True,
                      "a patch-synced store must not lose patch retention")

    def test_refresh_store_too_new_is_environment_guard(self) -> None:
        from zaxbygraph import cli as cli_mod

        self._set_stamp(10_000)

        def runner(*a, **k):
            # Exactly what the real seam raises when init_schema sees a
            # store newer than this build supports.
            raise cli_mod._EnvironmentGuard(
                "database schema version 99 is newer than this build supports"
            )

        with mock.patch.object(cli_mod, "_run_refresh_sync", runner):
            code, payload, _out, err = self.run_cli(
                ["item", "1", "--repo", REPO, "--db", self.db,
                 "--format", "json", "--max-age", "60"]
            )
        self.assertEqual(code, 2, "store_too_new must exit 2 like the sync path")
        self.assertEqual((payload or {}).get("error", {}).get("code"),
                         "environment_guard")

    def test_foreign_file_journal_mode_untouched(self) -> None:
        import sqlite3 as _sq

        foreign = Path(self._td.name) / "foreign2.db"
        conn = _sq.connect(str(foreign))
        conn.execute("CREATE TABLE t (x)")
        conn.execute("PRAGMA journal_mode = DELETE")
        before = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
        conn.close()
        self.assertEqual(before, "delete")
        from zaxbygraph import cli as cli_mod

        with mock.patch.object(
            cli_mod, "_run_refresh_sync",
            side_effect=AssertionError("sync seam must not run"),
        ):
            self.run_cli(
                ["item", "1", "--repo", REPO, "--db", str(foreign),
                 "--format", "json", "--max-age", "60"]
            )
        conn = _sq.connect(str(foreign))
        try:
            after = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
        finally:
            conn.close()
        self.assertEqual(
            after, "delete",
            "a declined refresh must not convert a foreign file to WAL",
        )


class AsciiStrictnessTests(unittest.TestCase):
    """PR #47 review: non-ASCII digits and int() laxity must not parse."""

    def test_fullwidth_digit_ports_rejected(self) -> None:
        from zaxbygraph.repo import _loopback_authority, _valid_forge_host

        self.assertFalse(_loopback_authority("127.0.0.1:١٢٣"))
        self.assertFalse(_loopback_authority("[::1]:١٢٣"))
        self.assertFalse(_valid_forge_host("host:١٢"))

    def test_max_age_strict_forms(self) -> None:
        from argparse import ArgumentTypeError
        from zaxbygraph.cli import _parse_max_age

        for bad in ("1_0", "+5", " 5 ", "١٠", "1.5h"):
            with self.subTest(value=bad):
                with self.assertRaises(ArgumentTypeError):
                    _parse_max_age(bad)
        self.assertEqual(_parse_max_age("90"), 90)
        self.assertEqual(_parse_max_age("2h"), 7200)

    def test_max_comments_strict_forms(self) -> None:
        from argparse import ArgumentTypeError
        from zaxbygraph.cli import _parse_comment_cap

        for bad in ("1_0", "+5", "-1", "abc", "١"):
            with self.subTest(value=bad):
                with self.assertRaises(ArgumentTypeError):
                    _parse_comment_cap(bad)
        self.assertEqual(_parse_comment_cap("3"), 3)
        self.assertEqual(_parse_comment_cap("0"), 0)

    def test_cap_comments_negative_k_raises(self) -> None:
        from zaxbygraph.query import cap_comments

        with self.assertRaises(ValueError):
            cap_comments({"comments": []}, -1, "o/r")

    def test_redact_url_masks_scp_credentials(self) -> None:
        from zaxbygraph.repo import _redact_url

        self.assertEqual(
            _redact_url("user:pass@host.corp:owner/repo"),
            "***@host.corp:owner/repo",
        )
        self.assertEqual(
            _redact_url("https://user:pass@github.com/owner/repo"),
            "https://***@github.com/owner/repo",
        )
        self.assertEqual(
            _redact_url("git@github.com:org/repo"),
            "git@github.com:org/repo",
            "the passwordless scp form must stay readable",
        )


class ParserSurfacePinTests(unittest.TestCase):
    """Final-critic finding 1: the --max-age surface is pinned so it cannot
    drift silently. Every read command accepts it; sql, the writers, doctor,
    where, mcp and sync never do."""

    def test_max_age_surface(self) -> None:
        from zaxbygraph.cli import build_parser

        parser = build_parser()
        subs = {
            a.dest: a
            for a in parser._actions
            if isinstance(a, __import__("argparse")._SubParsersAction)
        }["cmd"].choices
        expect_with = {
            "status", "search", "item", "related", "churn", "open", "path",
            "overlap", "file-history", "what-closed", "similar", "explain",
            "coupling", "eval", "triage", "bundle", "brief", "schema",
        }
        expect_without = {"sync", "sql", "export-graph", "export-corpus",
                          "where", "doctor", "mcp"}
        for name in sorted(set(subs) | expect_with | expect_without):
            with self.subTest(cmd=name):
                self.assertIn(name, subs, f"command {name} must exist")
                opts = {o for a in subs[name]._actions for o in a.option_strings}
                if name in expect_with:
                    self.assertIn("--max-age", opts)
                else:
                    self.assertNotIn("--max-age", opts)


class ReadNeverWritePinTests(_CliStore):
    """Final-critic finding 4: suite-level pins for the two properties that
    previously existed only as live probes."""

    def test_flagless_read_leaves_store_byte_identical(self) -> None:
        import hashlib

        before = hashlib.sha256(Path(self.db).read_bytes()).hexdigest()
        code, _payload, _out, err = self.run_cli(
            ["item", "1", "--repo", REPO, "--db", self.db, "--format", "json"]
        )
        self.assertEqual(code, 0, err)
        after = hashlib.sha256(Path(self.db).read_bytes()).hexdigest()
        self.assertEqual(
            before, after, "a flagless read must not write the store"
        )

    def test_sql_junk_file_answers_store_unreadable(self) -> None:
        junk = Path(self._td.name) / "junk.db"
        junk.write_bytes(b"this is not a sqlite database at all" * 64)
        code, payload, _out, err = self.run_cli(
            ["sql", "SELECT 1", "--repo", REPO, "--db", str(junk),
             "--format", "json"]
        )
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["error"]["code"], "store_unreadable")


class MaxAgeBoundaryTests(_CliStore):
    def _set_stamp(self, seconds_ago: int) -> None:
        stamp = (
            datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute(
            "UPDATE sync_state SET last_incr_sync_at = ? WHERE repo = ?",
            (stamp, REPO),
        )
        self.conn.commit()

    def test_age_within_budget_does_not_refresh(self) -> None:
        # Deterministic margin instead of an exact-equality boundary: the
        # age is second-truncated wall clock re-read inside the CLI, so an
        # exact ==-budget stamp flips with sub-second startup latency (the
        # windows-3.13 CI red). 30s of margin still proves the "<= means no
        # refresh" comparison without racing the clock (PR #47 review).
        from zaxbygraph import cli as cli_mod

        calls = []
        self._set_stamp(570)
        with mock.patch.object(
            cli_mod, "_run_refresh_sync",
            side_effect=lambda *a, **k: calls.append(1) or 0,
        ):
            code, payload, _out, err = self.run_cli(
                ["item", "1", "--repo", REPO, "--db", self.db,
                 "--format", "json", "--max-age", "600"]
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            calls, [], "age under budget is not older-than; no sync"
        )
        self.assertIsNot(
            (payload or {}).get("freshness", {}).get("refreshed"), True
        )

    def test_no_corpus_does_not_create_or_refresh(self) -> None:
        # The true no-corpus case: the DB file itself is absent. --max-age is
        # a no-op there (refresh never creates a corpus) and the read answers
        # the no-corpus envelope. Never touches the network.
        missing = str(Path(self._td.name) / "absent" / "history.db")
        self.assertFalse(Path(missing).exists())
        code, payload, _out, err = self.run_cli(
            ["item", "1", "--repo", REPO, "--db", missing,
             "--format", "json", "--max-age", "60"]
        )
        self.assertEqual(code, 3)
        self.assertIs((payload or {}).get("ok"), False)
        self.assertEqual((payload or {}).get("error", {}).get("code"), "no_corpus")
        self.assertFalse(
            Path(missing).exists(), "--max-age must not create a corpus"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
