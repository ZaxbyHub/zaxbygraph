"""Arm's-length acceptance checks for ZaxbyHub/zaxbygraph#31 (read truth).

Hermetic, stdlib-only, offline. Every test here asserts the FIXED contract
for issue #31; on the base tree each DISCRIMINATING test fails (RED) by
design. Run one check with:

    py -3.14 -m unittest discover -s tests -p "test_read_truth.py" \
        -k <test_name> -b

Seams this file freezes (the implementer must provide them):

- AC2: module-level ``zaxbygraph.db.assert_fts5_available()`` probed by
  ``open_existing`` and ``connect_readonly_query``; the SQLite floor helper
  stays the existing module-level ``zaxbygraph.db.assert_sqlite_supported``
  and the read-open path must call it (patching the module attribute must
  change read-open behavior).
- AC5: a named CLI helper (``zaxbygraph.cli._refresh_if_stale``) drives the
  --max-age refresh through the module-level sync runner
  ``zaxbygraph.cli._run_refresh_sync(*args, **kwargs)`` (returns 0 on
  success, raises on failure, stamps sync_state) so checks can stub the
  sync offline. The checks patch ONLY ``_run_refresh_sync``; staleness
  decisions and the envelope stay real.
- AC4: maintainer := comment author login equals the owner part of the repo
  slug, case-insensitive.

Never touches the real user store (explicit --db into a temp dir), never
the network, ASCII only.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from pathlib import Path
from unittest import mock

from zaxbygraph.db import connect, init_schema
from zaxbygraph.repo import _parse_remote_info
from zaxbygraph.store import ingest_item

REPO = "acme/forgegate"
PROXY_ENV = "ZAXBYGRAPH_PROXY_FORGE_HOST"
REPO_ROOT = Path(__file__).resolve().parents[1]
VECTORS_FILE = Path(__file__).resolve().parent / "fixtures_proxy_vectors.json"

OLD_STAMP = "2020-01-01T00:00:00Z"


def _ts(offset_s: int) -> str:
    base = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _issue_rec(
    number: int,
    *,
    title: str = "title",
    body: str = "plain body",
    state: str = "open",
    author: str = "alice",
    comments: int = 0,
    kind: str = "issue",
) -> dict:
    rec = {
        "id": 10_000 + number,
        "number": number,
        "node_id": f"I_{number}",
        "title": title,
        "body": body,
        "state": state,
        "user": {"login": author, "html_url": f"https://github.com/{author}"},
        "labels": [],
        "comments": comments,
        "created_at": _ts(number * 60),
        "updated_at": _ts(number * 60 + 30),
        "closed_at": None if state == "open" else _ts(number * 60 + 31),
        "locked": False,
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "url": f"https://api.github.com/repos/{REPO}/issues/{number}",
    }
    if kind == "pr":
        rec["pull_request"] = {
            "url": f"https://api.github.com/repos/{REPO}/pulls/{number}"
        }
        rec["html_url"] = f"https://github.com/{REPO}/pull/{number}"
    return rec


def _pull_rec(number: int) -> dict:
    return {
        "id": 20_000 + number,
        "number": number,
        "additions": 2,
        "deletions": 1,
        "changed_files": 1,
        "commits": 1,
        "draft": False,
        "merged_at": _ts(number * 60 + 40),
        "merge_commit_sha": "abc123",
        "base": {"ref": "main"},
        "head": {"ref": f"feat/{number}"},
        "user": {"login": "alice"},
    }


def _comment_rec(cid: int, author: str, body: str) -> dict:
    return {
        "id": cid,
        "body": body,
        "user": {"login": author, "html_url": f"https://github.com/{author}"},
        "created_at": _ts(9000 + cid % 900),
        "updated_at": _ts(9000 + cid % 900),
        "html_url": f"https://github.com/{REPO}/issues/1#issuecomment-{cid}",
    }


def _pr_file_rec(path: str) -> dict:
    return {
        "filename": path,
        "status": "modified",
        "additions": 2,
        "deletions": 1,
        "changes": 3,
        "sha": "deadbeef",
        "patch": "@@ -1 +1 @@\n-old\n+new\n",
    }


class ReadTruthStore(unittest.TestCase):
    """Base: a temp store with issue 1, PR 2 touching src/app.py, and (on
    demand) the one keyword-derived closure edge PR2 -> #1."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db_path = Path(self._td.name) / "history.db"
        self.conn = connect(self.db_path)
        init_schema(self.conn)
        self.addCleanup(self.conn.close)
        self.conn.execute("BEGIN")
        ingest_item(
            self.conn,
            REPO,
            _issue_rec(1, title="crash on start", state="closed"),
            pull_raw=None,
            issue_comments=[],
            review_comments=[],
            reviews=[],
            files=[],
            include_patches=False,
        )
        ingest_item(
            self.conn,
            REPO,
            _issue_rec(2, title="fix the crash", state="closed", kind="pr"),
            pull_raw=_pull_rec(2),
            issue_comments=[],
            review_comments=[],
            reviews=[],
            files=[_pr_file_rec("src/app.py")],
            include_patches=False,
        )
        self.conn.commit()

    def _add_keyword_edge(self) -> None:
        """The REST-only closure: PR #2's body said 'Fixes #1' so the only
        edge is closes_keyword with source='keyword' (no timeline/closing_ref
        stream ever ran)."""
        self.conn.execute(
            "INSERT INTO edges (repo, src_type, src_id, rel, dst_type, dst_id,"
            " confidence, evidence, source) VALUES"
            " (?, 'item', '2', 'closes_keyword', 'item', '1', 'EXTRACTED',"
            " 'PR #2 body: Fixes #1', 'keyword')",
            (REPO,),
        )
        self.conn.commit()

    def _ensure_sync_state(self) -> None:
        self.conn.execute(
            "INSERT INTO sync_state (repo) VALUES (?)"
            " ON CONFLICT (repo) DO NOTHING",
            (REPO,),
        )
        self.conn.commit()


def _ints(value) -> list[int]:
    """Tolerantly coerce a closes-style list (ints or {'number': n} dicts)."""
    if value is None:
        return []
    out = []
    for entry in value:
        if isinstance(entry, dict):
            out.append(int(entry.get("number")))
        else:
            out.append(int(entry))
    return out


class KeywordClosureTests(ReadTruthStore):
    # C1 (AC1): file_history entry-level keyword_closes
    def test_keyword_closes_in_file_history(self) -> None:
        from zaxbygraph.query import file_history

        self._add_keyword_edge()
        res = file_history(self.conn, "src/app.py", repo=REPO)
        entries = res.get("entries") or []
        self.assertTrue(entries, "PR 2 touched src/app.py, entry expected")
        entry = entries[0]
        self.assertEqual(entry["number"], 2)
        self.assertIn(
            "keyword_closes",
            entry,
            "file_history must report keyword-derived closures under"
            " keyword_closes (issue #31 AC1)",
        )
        self.assertEqual(_ints(entry["keyword_closes"]), [1])
        self.assertEqual(
            entry["closed_issues"],
            [],
            "closed_issues stays GitHub-reported closes only",
        )

    # C2 (AC2 of AC1): what_closed payload-level keyword_closes + prs stay closes-only
    def test_keyword_closes_in_what_closed(self) -> None:
        from zaxbygraph.query import what_closed

        self._add_keyword_edge()
        payload = what_closed(self.conn, 1, repo=REPO)
        self.assertIn(
            "keyword_closes",
            payload,
            "what_closed must report keyword-derived closures under"
            " keyword_closes (issue #31 AC1)",
        )
        self.assertIn(2, _ints(payload["keyword_closes"]))
        self.assertEqual(
            payload["prs"],
            [],
            "prs stays GitHub-reported closes (closing_ref/timeline) only",
        )


class ReadOpenEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db_path = Path(self._td.name) / "history.db"
        conn = connect(self.db_path)
        init_schema(conn)
        conn.close()

    # C3 (AC2): read-open enforces the SQLite floor
    def test_read_open_enforces_floor_and_fts5(self) -> None:
        from zaxbygraph import db as db_mod

        leaked = None
        try:
            with mock.patch.object(
                db_mod,
                "assert_sqlite_supported",
                side_effect=RuntimeError("ZG-FLOOR-PROBE-3-35"),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    leaked = db_mod.open_existing(self.db_path)
            self.assertIn(
                "ZG-FLOOR-PROBE-3-35",
                str(ctx.exception),
                "open_existing must surface the SQLite-floor failure, not a"
                " bare SQL error (issue #31 AC2)",
            )
        finally:
            if leaked is not None:
                leaked.close()

    # C4 (AC2): read-open probes FTS5 on both read connection factories
    def test_read_open_probes_fts5(self) -> None:
        from zaxbygraph import db as db_mod

        probe = getattr(db_mod, "assert_fts5_available", None)
        self.assertIsNotNone(
            probe,
            "zaxbygraph.db must expose module-level assert_fts5_available()"
            " and the read-open path must call it (issue #31 AC2 seam)",
        )
        for opener in ("open_existing", "connect_readonly_query"):
            with self.subTest(opener=opener):
                leaked = None
                try:
                    with mock.patch.object(
                        db_mod,
                        "assert_fts5_available",
                        side_effect=RuntimeError("ZG-FTS5-PROBE"),
                    ):
                        with self.assertRaises(RuntimeError) as ctx:
                            leaked = getattr(db_mod, opener)(self.db_path)
                    self.assertIn("ZG-FTS5-PROBE", str(ctx.exception))
                finally:
                    if leaked is not None:
                        leaked.close()


PROXY_URL = "http://u@127.0.0.1:41233/git/org/repo"


def _env_mark(mark):
    """Translate a vector 'env' marker into (key, value-or-DELETED)."""
    if mark is None:
        return None
    if isinstance(mark, str) and mark.startswith("valid:"):
        return mark[len("valid:"):]
    if mark == "unparseable":
        return "!!not a host!!"
    if mark == "empty":
        return "   "
    raise AssertionError(f"unknown env marker: {mark!r}")


class ProxyRemoteTests(unittest.TestCase):
    def _parse(self, url: str, env_value):
        with mock.patch.dict(os.environ):
            os.environ.pop(PROXY_ENV, None)
            if env_value is not None:
                os.environ[PROXY_ENV] = env_value
            return _parse_remote_info(url)

    # C5 (AC3): all four loopback host spellings resolve (env unset)
    def test_loopback_proxy_remote_resolves(self) -> None:
        cases = [
            (
                "http://local_proxy@127.0.0.1:41233/git/ZaxbyHub/zaxbygraph",
                "zaxbyhub/zaxbygraph",
            ),
            ("http://u@localhost:41233/git/org/repo", "org/repo"),
            ("http://u@[::1]:41233/git/org/repo", "org/repo"),
            ("http://u@::1:41233/git/org/repo", "org/repo"),
        ]
        for url, slug in cases:
            with self.subTest(url=url):
                info = self._parse(url, None)
                self.assertIsNotNone(
                    info,
                    "loopback git-proxy remote must resolve (issue #31 AC3)",
                )
                self.assertEqual(info[0], "github.com")
                self.assertEqual(info[1].lower(), slug)

    # C6 (AC3): ZAXBYGRAPH_PROXY_FORGE_HOST four states
    def test_proxy_forge_env_states(self) -> None:
        cases = [
            (None, ("github.com", "org/repo")),
            ("gitlab.internal:8443", ("gitlab.internal_8443", "org/repo")),
            ("!!not a host!!", None),
            ("   ", None),
        ]
        for env_value, expected in cases:
            with self.subTest(env=env_value):
                info = self._parse(PROXY_URL, env_value)
                if expected is None:
                    self.assertIsNone(
                        info,
                        "rewrite must be silently disabled for this env"
                        " state; the URL keeps failing to parse",
                    )
                else:
                    self.assertIsNotNone(info)
                    self.assertEqual(info[0], expected[0])
                    self.assertEqual(info[1].lower(), expected[1])

    # C7 (AC3): every vector in tests/fixtures_proxy_vectors.json passes
    def test_proxy_normalization_vectors(self) -> None:
        self.assertTrue(
            VECTORS_FILE.exists(), f"missing vector file: {VECTORS_FILE}"
        )
        data = json.loads(VECTORS_FILE.read_text(encoding="ascii"))
        vectors = data["vectors"]
        self.assertGreater(len(vectors), 10, "vector file is unexpectedly thin")
        for i, vec in enumerate(vectors):
            with self.subTest(index=i, url=vec["url"], env=vec["env"]):
                info = self._parse(vec["url"], _env_mark(vec["env"]))
                expect = vec["expect"]
                if expect is None:
                    self.assertIsNone(info)
                else:
                    self.assertIsNotNone(
                        info, "expected a parse but got None"
                    )
                    self.assertEqual(info[0], expect["host"])
                    self.assertEqual(info[1].lower(), expect["slug"].lower())


class CliHarness(unittest.TestCase):
    """Base for in-process CLI runs against a temp store."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db = str(Path(self._td.name) / "history.db")
        conn = connect(Path(self.db))
        init_schema(conn)
        self.addCleanup(conn.close)
        self.conn = conn
        conn.execute("BEGIN")
        ingest_item(
            self.conn,
            REPO,
            _issue_rec(1, title="one", body="hello"),
            pull_raw=None,
            issue_comments=[_comment_rec(9001, "bob", "first")],
            review_comments=[],
            reviews=[],
            files=[],
            include_patches=False,
        )
        conn.commit()
        self._set_sync_state()

    def _set_sync_state(self, *, stamp: str | None = OLD_STAMP) -> None:
        self.conn.execute(
            "INSERT INTO sync_state (repo, last_incr_sync_at, full_sync_pending,"
            " last_error) VALUES (?, ?, 0, NULL)"
            " ON CONFLICT (repo) DO UPDATE SET last_incr_sync_at = excluded"
            ".last_incr_sync_at, full_sync_pending = 0, last_error = NULL",
            (REPO, stamp),
        )
        self.conn.commit()

    def run_cli(self, argv: list[str]) -> tuple[int, dict | None, str, str]:
        from zaxbygraph.cli import main

        out = io.StringIO()
        err = io.StringIO()
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


class ItemMaxCommentsTests(CliHarness):
    def _ingest_five_comments(self) -> None:
        """Re-ingest item 1 with five comments (item-scoped child rows are
        replaced on re-ingest). Only the middle one is the maintainer's
        (login ACME vs owner 'acme', case-insensitive by contract); ids
        ascend chronologically 9001..9005."""
        self.conn.execute("BEGIN")
        ingest_item(
            self.conn,
            REPO,
            _issue_rec(1, title="one", body="hello", comments=5),
            pull_raw=None,
            issue_comments=[
                _comment_rec(9001, "bob", "first"),
                _comment_rec(9002, "dave", "mid 2"),
                _comment_rec(9003, "ACME", "maintainer note"),
                _comment_rec(9004, "eve", "mid 4"),
                _comment_rec(9005, "carol", "last"),
            ],
            review_comments=[],
            reviews=[],
            files=[],
            include_patches=False,
        )
        self.conn.commit()

    # C8 (AC4): cap + preference order (first, last, maintainer)
    def test_item_max_comments_caps_with_preference(self) -> None:
        self._ingest_five_comments()
        code, payload, _out, err = self.run_cli(
            ["item", "1", "--repo", REPO, "--db", self.db,
             "--format", "json", "--max-comments", "3"]
        )
        self.assertEqual(code, 0, err)
        data = payload["data"]
        comments = data["comments"]
        self.assertLessEqual(
            len(comments), 3, "--max-comments 3 must cap the comment list"
        )
        ids = [c["github_id"] for c in comments]
        self.assertEqual(
            ids,
            [9001, 9003, 9005],
            "with K=3 the preferred set is first + maintainer + last,"
            " chronological order (issue #31 AC4)",
        )
        self.assertEqual(
            data.get("comments_omitted"),
            2,
            "top-level comments_omitted must report the dropped count",
        )

    # C9 (AC4): K=0 empties the list; a non-integer K is a usage error
    def test_item_max_comments_edges(self) -> None:
        self._ingest_five_comments()
        code, payload, _out, err = self.run_cli(
            ["item", "1", "--repo", REPO, "--db", self.db,
             "--format", "json", "--max-comments", "0"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["data"]["comments"], [])
        self.assertEqual(payload["data"].get("comments_omitted"), 5)

        code, _payload, _out, _err = self.run_cli(
            ["item", "1", "--repo", REPO, "--db", self.db,
             "--format", "json", "--max-comments", "abc"]
        )
        self.assertEqual(
            code, 2, "a non-integer --max-comments value must exit 2"
        )


class MaxAgeTests(CliHarness):
    """AC5 seam contract: the CLI helper _refresh_if_stale drives the
    refresh through module-level zaxbygraph.cli._run_refresh_sync so the
    checks below stub the sync offline. Only _run_refresh_sync is patched;
    staleness decisions and the envelope stay real."""

    def _runner(self, calls: list):
        from zaxbygraph import cli as cli_mod

        runner = getattr(cli_mod, "_run_refresh_sync", None)
        self.assertIsNotNone(
            runner,
            "cli must expose module-level _run_refresh_sync (driven by"
            " _refresh_if_stale) as the --max-age sync seam (issue #31 AC5)",
        )

        def fake_sync(*args, **kwargs):
            calls.append(1)
            c = sqlite3.connect(self.db)
            try:
                c.execute(
                    "UPDATE sync_state SET last_incr_sync_at = ?,"
                    " last_error = NULL, full_sync_pending = 0"
                    " WHERE repo = ?",
                    (_now_iso(), REPO),
                )
                c.commit()
            finally:
                c.close()
            return 0

        return mock.patch.object(cli_mod, "_run_refresh_sync", fake_sync)

    # C10 (AC5): stale + --max-age refreshes once; fresh + --max-age does not
    def test_max_age_refreshes_only_when_asked(self) -> None:
        self._set_sync_state(stamp=OLD_STAMP)  # years stale
        calls: list = []
        with self._runner(calls):
            code, payload, _out, err = self.run_cli(
                ["item", "1", "--repo", REPO, "--db", self.db,
                 "--format", "json", "--max-age", "60"]
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            len(calls), 1, "a stale store under --max-age 60 must sync once"
        )
        self.assertIs(
            (payload or {}).get("freshness", {}).get("refreshed"),
            True,
            "envelope freshness.refreshed must be true after the refresh",
        )

        # Fresh store: stamp is now, budget is one day -> no sync at all.
        self._set_sync_state(
            stamp=(datetime.now(timezone.utc) - timedelta(seconds=30))
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        calls.clear()
        with self._runner(calls):
            code, payload, _out, err = self.run_cli(
                ["item", "1", "--repo", REPO, "--db", self.db,
                 "--format", "json", "--max-age", "1d"]
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            len(calls), 0, "a fresh store must not sync under --max-age"
        )
        self.assertIsNot(
            (payload or {}).get("freshness", {}).get("refreshed"),
            True,
            "freshness.refreshed must not be true when no refresh ran",
        )

    # C11 (AC5F): a failed refresh answers the sync error envelope, exit 1
    def test_max_age_failed_refresh_reports_error(self) -> None:
        from zaxbygraph import cli as cli_mod

        self._set_sync_state(stamp=OLD_STAMP)
        self.assertIsNotNone(
            getattr(cli_mod, "_run_refresh_sync", None),
            "cli must expose module-level _run_refresh_sync (driven by"
            " _refresh_if_stale) as the --max-age sync seam (issue #31 AC5)",
        )

        def boom(*args, **kwargs):
            raise RuntimeError("ZG-REFRESH-SYNC-FAILED")

        with mock.patch.object(cli_mod, "_run_refresh_sync", boom):
            code, payload, _out, err = self.run_cli(
                ["item", "1", "--repo", REPO, "--db", self.db,
                 "--format", "json", "--max-age", "3600"]
            )
        self.assertEqual(
            code, 1, "a failed --max-age refresh must exit 1"
        )
        self.assertIsNotNone(payload, "the failure must answer an envelope")
        self.assertIs(payload.get("ok"), False)
        self.assertIsNone(
            payload.get("data"),
            "a failed refresh must never answer a stale data payload",
        )
        self.assertIn(
            "ZG-REFRESH-SYNC-FAILED",
            json.dumps(payload),
            "the sync error must be reported to the caller",
        )


class DocsAndCiTests(unittest.TestCase):
    # C13 (AC6): CI matrix lists 3.11-3.14 on windows-latest and ubuntu-latest
    def test_ci_matrix_versions(self) -> None:
        ci = REPO_ROOT / ".github" / "workflows" / "ci.yml"
        text = ci.read_text(encoding="utf-8")
        for version in ("3.11", "3.12", "3.13", "3.14"):
            self.assertIn(
                version,
                text,
                f"CI matrix must list Python {version} (issue #31 AC6)",
            )
        self.assertIn("windows-latest", text)
        self.assertIn("ubuntu-latest", text)

    # C14 (AC7): both documented check forms collect and pass
    def test_documented_check_forms_import(self) -> None:
        env = dict(os.environ)
        # Deliberately ONLY src on PYTHONPATH: inheriting the leg's tests
        # entry would mask the import defect this check exists to catch.
        env["PYTHONPATH"] = str(REPO_ROOT / "src")

        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_store"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        self.assertEqual(
            proc.returncode,
            0,
            "python -m unittest tests.test_store must collect and pass:"
            f"\n{proc.stdout[-800:]}\n{proc.stderr[-800:]}",
        )
        self.assertNotIn(
            "ModuleNotFoundError",
            proc.stdout + proc.stderr,
        )

        if find_spec("pytest") is None:
            self.skipTest("pytest not importable on this interpreter")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_store.py", "-q"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        self.assertEqual(
            proc.returncode,
            0,
            "python -m pytest tests/test_store.py -q must collect and pass:"
            f"\n{proc.stdout[-800:]}\n{proc.stderr[-800:]}",
        )
        self.assertNotIn(
            "ModuleNotFoundError",
            proc.stdout + proc.stderr,
        )

    # C15 (AC7): the CONTRIBUTING argv note names a form that works
    def test_contributing_argv_note_works(self) -> None:
        text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        note_lines = [ln for ln in text.splitlines() if "argv conventions" in ln]
        self.assertTrue(
            note_lines, "CONTRIBUTING must keep an argv-conventions note"
        )
        spans = re.findall(r"`([^`]+)`", " ".join(note_lines))
        commands = [s.strip() for s in spans if s.strip().startswith("python -m ")]
        self.assertTrue(
            commands,
            "the argv note must name at least one concrete python -m command",
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        for command in commands:
            with self.subTest(command=command):
                concrete = (
                    command.replace("<file>", "tests/test_store.py")
                    .replace("<module>", "test_store")
                    .replace("<test>", "test_upsert_idempotent")
                )
                self.assertFalse(
                    "<" in concrete or ">" in concrete,
                    f"the note must name a concrete command, got placeholders:"
                    f" {command!r}",
                )
                argv = shlex.split(concrete)
                argv[0] = sys.executable
                proc = subprocess.run(
                    argv,
                    cwd=str(REPO_ROOT),
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300,
                )
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"documented form {command!r} must work:"
                    f"\n{proc.stdout[-800:]}\n{proc.stderr[-800:]}",
                )

    # C16 (AC8): SKILL.md carries skill-version equal to __version__
    def test_skill_version_stamp_matches_cli(self) -> None:
        from zaxbygraph import __version__

        skill = REPO_ROOT / "skills" / "zaxbygraph" / "SKILL.md"
        head = "\n".join(skill.read_text(encoding="utf-8").splitlines()[:40])
        match = re.search(
            r"skill-version:\s*v?(\d+(?:\.\d+){2})", head, re.IGNORECASE
        )
        self.assertIsNotNone(
            match,
            "skills/zaxbygraph/SKILL.md must carry a skill-version X.Y.Z"
            " stamp near the top (issue #31 AC8)",
        )
        self.assertEqual(match.group(1), __version__)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
