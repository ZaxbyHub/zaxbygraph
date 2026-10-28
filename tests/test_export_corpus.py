"""Deterministic corpus export (issue #28).

The six NEW-SURFACE acceptance checks (C1-C6 in the trace checkpoint) carry
the frozen names below; the remaining tests are supporting coverage from the
approved plan (guards, include matrix, snapshot isolation).
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from fixtures import (
    REPO,
    FakeGitHubSource,
    TempDBTest,
    issue,
    pr_file,
    pull,
    review,
)
from zaxbygraph import export as export_mod
from zaxbygraph.db import connect, init_schema, open_existing
from zaxbygraph.sync import sync_repo

#: Pinned cross-platform / replay golden hashes. These constants are part of
#: the contract asserted on BOTH CI legs (AC5) and against the replayed
#: fixture (AC6); regenerating them means an intentional format change and
#: needs a plan. (Regenerated once during review: per-item edge ordering was
#: corrected to the plan's full-unique-key law and a review row joined the
#: fixture store — 08-test-results.md records the round.)
GOLDEN_FIXTURE_SHA256 = (
    "9d0eba2712428ba6dc3be590353577b67b977e047291313a96ced06b06c3d046"
)


def _canonical(obj: object) -> bytes:
    """Test-local canonical encoder (deliberately NOT the production
    helper): the checks validate the emitted bytes against an independent
    re-serialization."""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")


def _corpus_bytes(data: bytes) -> bytes:
    """Everything after the manifest line (the corpus_sha256 domain)."""
    first_nl = data.index(b"\n")
    return data[first_nl + 1 :]


def _manifest_of(data: bytes) -> dict:
    return json.loads(data[: data.index(b"\n")].decode("ascii"))


def _content_lines(data: bytes) -> list[bytes]:
    return [line for line in _corpus_bytes(data).split(b"\n") if line]


def _verify_canonical_and_hashes(data: bytes) -> dict:
    """Independent law check: every line re-serializes identically under the
    test-local canonical encoder, every item content_hash matches a local
    sha256 of its content without the hash, and the manifest hash matches a
    local sha256 over the content lines. Returns the manifest."""
    manifest = _manifest_of(data)
    lines = _content_lines(data)
    hasher = hashlib.sha256()
    for line in lines:
        obj = json.loads(line.decode("ascii"))
        assert _canonical(obj) == line, "line is not canonical JSON"
        if obj.get("type") == "item":
            expected = hashlib.sha256(
                _canonical({k: v for k, v in obj.items() if k != "content_hash"})
            ).hexdigest()
            assert obj["content_hash"] == expected, "content_hash mismatch"
        hasher.update(line + b"\n")
    assert manifest["corpus_sha256"] == hasher.hexdigest(), (
        "corpus_sha256 does not match the emitted content bytes"
    )
    return manifest


def _export(conn: sqlite3.Connection, db_path: str, **kwargs) -> bytes:
    """Run write_corpus into a bytes buffer."""
    buf = io.BytesIO()
    export_mod.write_corpus(
        conn,
        kwargs.pop("repo", REPO),
        buf,
        kwargs.pop("out_path", None),
        frozenset(kwargs.pop("include", ())),
        kwargs.pop("require_complete", False),
        db_path,
    )
    return buf.getvalue()


def _rich_store(conn: sqlite3.Connection) -> FakeGitHubSource:
    """One issue (non-ASCII body, comment, label), one merged PR (file),
    one release: enough content for every exported block."""
    src = FakeGitHubSource()
    src.add_issue(
        issue(
            1,
            title="Probe ünïcode item",
            body="see src/app.py — déjà vu",
            labels=[{"name": "bug", "color": "ff0000"}],
        )
    )
    src.comment_on(1, "a comment with an em-dash — and ünïcode")
    src.add_pr(
        issue(2, title="Probe PR", body="", kind="pr"),
        pull(2, merged=True, additions=3, deletions=1),
        files=[pr_file("src/app.py")],
    )
    review_row = review(303, "APPROVED", "ship it — ok")
    review_row["html_url"] = f"https://github.com/{REPO}/pull/2#pullrequestreview-303"
    src.reviews[2] = [review_row]
    src.releases.append(
        {
            "id": 77,
            "tag_name": "v0.1.0",
            "name": "First release",
            "body": "notes — with em-dash",
            "draft": False,
            "prerelease": False,
            "author": {"login": "alice"},
            "created_at": "2026-01-01T00:00:00Z",
            "published_at": "2026-01-02T00:00:00Z",
            "html_url": f"https://github.com/{REPO}/releases/tag/v0.1.0",
        }
    )
    return src


class ExportCorpusTests(TempDBTest):
    """AC1 + AC2 + the independent law checks."""

    def _synced(self) -> FakeGitHubSource:
        src = _rich_store(self.conn)
        sync_repo(self.conn, src, REPO)
        return src

    def test_export_is_byte_identical(self) -> None:
        """AC1: two exports of one store are byte-identical, canonical, and
        hash-verified against independent recomputation."""
        self._synced()
        conn = open_existing(self.db_path)
        try:
            first = _export(conn, str(self.db_path))
            second = _export(conn, str(self.db_path))
        finally:
            conn.close()
        self.assertEqual(first, second)
        self.assertNotIn(b"\r", first)
        self.assertTrue(first.endswith(b"\n"))
        manifest = _verify_canonical_and_hashes(first)
        self.assertEqual(manifest["schema"], "zaxbygraph.corpus.v1")
        self.assertEqual(manifest["repo"], REPO)
        self.assertEqual(manifest["counts"]["items"], 2)
        self.assertEqual(manifest["counts"]["releases"], 1)

    def test_hash_stable_across_noop_sync(self) -> None:
        """AC2: a sync that fetches nothing new leaves corpus_sha256 equal
        while the churn precondition (pk/id rewrites, fetch_log growth) is
        proven, and the content lines stay byte-identical."""
        src = _rich_store(self.conn)
        sync_repo(self.conn, src, REPO)
        conn = open_existing(self.db_path)
        try:
            first = _export(conn, str(self.db_path))
            pks_before = [
                r["pk"] for r in conn.execute("SELECT pk FROM comments")
            ]
            ids_before = [r["id"] for r in conn.execute("SELECT id FROM edges")]
            fetch_before = conn.execute(
                "SELECT COUNT(*) FROM fetch_log WHERE repo = ?", (REPO,)
            ).fetchone()[0]
            # The no-op re-ingest: same unchanged source, inclusive boundary.
            sync_repo(self.conn, src, REPO)
            pks_after = [
                r["pk"] for r in conn.execute("SELECT pk FROM comments")
            ]
            ids_after = [r["id"] for r in conn.execute("SELECT id FROM edges")]
            fetch_after = conn.execute(
                "SELECT COUNT(*) FROM fetch_log WHERE repo = ?", (REPO,)
            ).fetchone()[0]
            self.assertNotEqual(pks_before, pks_after, "no-op sync must churn comments.pk")
            self.assertNotEqual(ids_before, ids_after, "no-op sync must churn edges.id")
            self.assertGreater(fetch_after, fetch_before, "provenance grew")
            second = _export(conn, str(self.db_path))
        finally:
            conn.close()
        m1, m2 = _manifest_of(first), _manifest_of(second)
        self.assertEqual(m1["corpus_sha256"], m2["corpus_sha256"])
        self.assertEqual(_corpus_bytes(first), _corpus_bytes(second))
        self.assertNotEqual(first, second, "provenance (fetch_log) may differ")


class EdgeRoutingTests(TempDBTest):
    """AC3: every edge exactly once, at its exact expected owner."""

    def _seed(self) -> None:
        # Two stored items: 1 (issue) and 2 (PR).
        src = FakeGitHubSource()
        src.add_issue(issue(1, title="one", body="body one"))
        src.add_issue(issue(2, title="two", body="body two"))
        sync_repo(self.conn, src, REPO)
        self.local = {"1", "2"}
        self.conn.execute(
            "INSERT INTO labels(repo, number, name, color) VALUES (?, 1, 'bug', 'ff0000')",
            (REPO,),
        )
        self.conn.commit()

    def _edge(self, src_type, src_id, rel, dst_type, dst_id, source, evidence=None):
        # OR IGNORE: the seed sync already wrote some of these shapes
        # (authored, touches, merged_commit); those are still routed and
        # asserted below — the insert only tops up shapes sync did not make.
        self.conn.execute(
            "INSERT OR IGNORE INTO edges(repo, src_type, src_id, rel,"
            " dst_type, dst_id, confidence, evidence, source)"
            " VALUES (?, ?, ?, ?, ?, ?, 'EXTRACTED', ?, ?)",
            (REPO, src_type, src_id, rel, dst_type, dst_id, evidence, source),
        )

    def _routes(self) -> dict[str, list[tuple]]:
        """keyed by 'item:N' or 'orphan' -> list of edge keys."""
        conn = open_existing(self.db_path)
        try:
            data = _export(conn, str(self.db_path))
        finally:
            conn.close()
        routes: dict[str, list[tuple]] = {}
        for line in _content_lines(data):
            obj = json.loads(line.decode("ascii"))
            key = f"item:{obj['number']}" if obj.get("type") == "item" else obj["type"]
            bucket = routes.setdefault(key, [])
            for e in obj.get("edges", []):
                bucket.append(
                    (e["src_type"], e["src_id"], e["rel"], e["dst_type"], e["dst_id"], e["source"])
                )
        return routes

    def test_every_edge_exactly_once(self) -> None:
        self._seed()
        # item->item closes, collapsed from two provenance streams (the #19
        # rule): ONE output edge carrying the stored evidence and survivor.
        self.conn.execute(
            "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id,"
            " confidence, evidence, source)"
            " VALUES (?, 'item', '1', 'closes', 'item', '2', 'EXTRACTED', ?,"
            " 'closing_ref')",
            (REPO, "closingIssuesReference at T2; event at T1"),
        )
        # actor->item (authored + commented)
        self._edge("actor", "alice", "authored", "item", "1", "payload")
        self._edge("actor", "bob", "commented", "item", "2", "payload")
        # foreign owner/repo#N item reference -> stored dst item
        self._edge("item", "other/repo#9", "cross_referenced", "item", "1", "timeline")
        # numeric missing src, stored dst
        self._edge("item", "999", "mentions", "item", "2", "payload")
        # item->label / item->file / item->commit / item->actor (merged_by)
        self._edge("item", "1", "has_label", "label", "bug", "payload")
        self._edge("item", "2", "touches", "file", "src/app.py", "payload")
        self._edge("item", "2", "merged_commit", "commit", "abc123", "payload")
        self._edge("item", "2", "merged_by", "actor", "dana", "payload")
        # self-incident edge (item->itself)
        self._edge("item", "1", "relates", "item", "1", "payload")
        # neither endpoint stored -> orphan
        self._edge("actor", "ghost", "authored", "item", "404", "payload")
        self._edge("item", "888", "mentions", "file", "ghost.py", "payload")
        self.conn.commit()

        routes = self._routes()

        def key_of(src_type, src_id, rel, dst_type, dst_id, source):
            return (src_type, src_id, rel, dst_type, dst_id, source)

        # Exact expected owner per edge (an all-orphans dump fails this).
        self.assertIn(
            key_of("item", "1", "closes", "item", "2", "closing_ref"), routes["item:1"]
        )
        self.assertEqual(
            len([e for e in routes["item:1"] if e[2] == "closes"]), 1,
            "collapsed closes emits exactly once",
        )
        self.assertIn(key_of("actor", "alice", "authored", "item", "1", "payload"), routes["item:1"])
        self.assertIn(key_of("actor", "bob", "commented", "item", "2", "payload"), routes["item:2"])
        self.assertIn(
            key_of("item", "other/repo#9", "cross_referenced", "item", "1", "timeline"),
            routes["item:1"],
        )
        self.assertIn(key_of("item", "999", "mentions", "item", "2", "payload"), routes["item:2"])
        self.assertIn(key_of("item", "1", "has_label", "label", "bug", "payload"), routes["item:1"])
        self.assertIn(key_of("item", "2", "touches", "file", "src/app.py", "payload"), routes["item:2"])
        self.assertIn(key_of("item", "2", "merged_commit", "commit", "abc123", "payload"), routes["item:2"])
        self.assertIn(key_of("item", "2", "merged_by", "actor", "dana", "payload"), routes["item:2"])
        self.assertIn(key_of("item", "1", "relates", "item", "1", "payload"), routes["item:1"])
        orphans = routes.get("orphan_edges", [])
        self.assertIn(key_of("actor", "ghost", "authored", "item", "404", "payload"), orphans)
        self.assertIn(key_of("item", "888", "mentions", "file", "ghost.py", "payload"), orphans)

        # Exactly once: union of routes == stored set, no duplicates.
        stored = {
            key_of(*row)
            for row in self.conn.execute(
                "SELECT src_type, src_id, rel, dst_type, dst_id, source FROM edges"
            )
        }
        routed = []
        for bucket in routes.values():
            routed.extend(bucket)
        self.assertEqual(len(routed), len(set(routed)), "duplicate emitted edge")
        self.assertEqual(set(routed), stored)
        manifest = None
        conn = open_existing(self.db_path)
        try:
            manifest = _manifest_of(_export(conn, str(self.db_path)))
        finally:
            conn.close()
        self.assertEqual(manifest["counts"]["edges"], len(stored))
        self.assertEqual(manifest["counts"]["orphan_edges"], len(orphans))


class IncompleteStoreTests(TempDBTest):
    """AC4: completeness flagged; --require-complete refuses before writing."""

    def _flag(self, pending: int, error: str | None) -> None:
        self.conn.execute(
            "UPDATE sync_state SET full_sync_pending = ?, last_error = ?"
            " WHERE repo = ?",
            (pending, error, REPO),
        )
        self.conn.commit()

    def test_incomplete_store_is_flagged(self) -> None:
        src = _rich_store(self.conn)
        sync_repo(self.conn, src, REPO)

        # Clean store: complete in the manifest; --require-complete exits 0.
        conn = open_existing(self.db_path)
        try:
            clean = _manifest_of(_export(conn, str(self.db_path)))
            self.assertTrue(clean["store"]["complete"])
            _export(conn, str(self.db_path), require_complete=True)

            # Pending-only branch (full_sync_pending=1, last_error=NULL).
            self._flag(1, None)
            pending = _manifest_of(_export(conn, str(self.db_path)))
            self.assertFalse(pending["store"]["complete"])
            self.assertEqual(pending["store"]["full_sync_pending"], 1)
            self.assertIsNone(pending["store"]["last_error"])
            with self.assertRaises(export_mod.ExportError) as ctx:
                _export(conn, str(self.db_path), require_complete=True)
            self.assertEqual(ctx.exception.code, "incomplete_store")

            # Error-only branch (full_sync_pending=0, last_error set).
            self._flag(0, "boom")
            errored = _manifest_of(_export(conn, str(self.db_path)))
            self.assertFalse(errored["store"]["complete"])
            self.assertEqual(errored["store"]["last_error"], "boom")
            with self.assertRaises(export_mod.ExportError) as ctx:
                _export(conn, str(self.db_path), require_complete=True)
            self.assertEqual(ctx.exception.code, "incomplete_store")
            self.assertEqual(ctx.exception.exit_code, 1)
        finally:
            self._flag(0, None)
            conn.close()

        # End-to-end: the CLI maps the refusal to a non-zero exit with the
        # structured envelope, and an existing --out file is byte-preserved
        # across the refusal (the destination is only opened after every
        # guard passes and the spool completes).
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "corpus.jsonl"
            out.write_bytes(b"PRECIOUS\n")
            conn = open_existing(self.db_path)
            try:
                self._flag(0, "boom")
            finally:
                conn.close()
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "zaxbygraph",
                    "export-corpus",
                    "--repo",
                    REPO,
                    "--db",
                    str(self.db_path),
                    "--require-complete",
                    "--out",
                    str(out),
                ],
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 1)
            env = json.loads(proc.stdout.decode("utf-8"))
            self.assertFalse(env["ok"])
            self.assertEqual(env["error"]["code"], "incomplete_store")
            self.assertEqual(out.read_bytes(), b"PRECIOUS\n")
            # A clean store with --require-complete exits 0 through the CLI.
            conn = open_existing(self.db_path)
            try:
                self._flag(0, None)
            finally:
                conn.close()
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "zaxbygraph",
                    "export-corpus",
                    "--repo",
                    REPO,
                    "--db",
                    str(self.db_path),
                    "--require-complete",
                    "--out",
                    str(out),
                ],
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertNotEqual(out.read_bytes(), b"PRECIOUS\n")
            self.assertTrue(out.read_bytes().endswith(b"\n"))


class CrossPlatformTests(TempDBTest):
    """AC5: binary writes, no text translation, stdout == file, and the
    pinned golden hash (asserted on BOTH CI legs)."""

    def test_hash_cross_platform(self) -> None:
        src = _rich_store(self.conn)
        sync_repo(self.conn, src, REPO)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "corpus.jsonl"
            conn = open_existing(self.db_path)
            try:
                export_mod.write_corpus(
                    conn, REPO, None, str(out), frozenset(), False, str(self.db_path)
                )
            finally:
                conn.close()
            raw = out.read_bytes()
            self.assertNotIn(b"\r", raw)
            self.assertTrue(raw.endswith(b"\n"))
            file_hash = _manifest_of(raw)["corpus_sha256"]
            self.assertEqual(file_hash, GOLDEN_FIXTURE_SHA256)

            # Real CLI subprocess: exercises sys.stdout.buffer end to end.
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "zaxbygraph",
                    "export-corpus",
                    "--repo",
                    REPO,
                    "--db",
                    str(self.db_path),
                ],
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
            self.assertEqual(proc.stderr, b"")
            stdout_manifest = _manifest_of(proc.stdout)
            self.assertEqual(stdout_manifest["corpus_sha256"], file_hash)


class ReplayFixtureTests(TempDBTest):
    """AC6: the committed sidecar replays to the pinned corpus hash."""

    FIXTURE = Path(__file__).resolve().parent / "corpus_fixture" / "events.jsonl"

    def test_replayed_fixture_hash(self) -> None:
        from replay import JsonlReplaySource  # tests-local helper (#23)

        replay_conn = connect(Path(self._td.name) / "replay.db")
        init_schema(replay_conn)
        try:
            replay_src = JsonlReplaySource(self.FIXTURE)
            sync_repo(replay_conn, replay_src, REPO)
            db_file = replay_conn.execute("PRAGMA database_list").fetchone()[2]
            export_conn = open_existing(Path(db_file))
            try:
                data = _export(export_conn, str(db_file))
            finally:
                export_conn.close()
            manifest = _verify_canonical_and_hashes(data)
            self.assertEqual(manifest["corpus_sha256"], GOLDEN_FIXTURE_SHA256)
        finally:
            replay_conn.close()


class GuardTests(TempDBTest):
    """Supporting coverage: include matrix, repo/destination/version guards,
    snapshot isolation (approved plan)."""

    def _synced(self) -> FakeGitHubSource:
        src = _rich_store(self.conn)
        sync_repo(self.conn, src, REPO)
        return src

    def test_include_flags_add_and_default_absent(self) -> None:
        self._synced()
        conn = open_existing(self.db_path)
        try:
            bare = _export(conn, str(self.db_path))
            with_raw = _export(conn, str(self.db_path), include=("raw",))
            with_rev = _export(
                conn,
                str(self.db_path),
                include=("revisions",),
                repo=REPO,
            )
            with_patches = _export(conn, str(self.db_path), include=("patches",))
        finally:
            conn.close()
        # Default: no raw, no patch, no revisions line.
        for data in (bare,):
            for line in _content_lines(data):
                obj = json.loads(line.decode("ascii"))
                self.assertNotIn("raw", obj)
                for c in obj.get("comments", []):
                    self.assertNotIn("raw", c)
                for r in obj.get("reviews", []):
                    self.assertNotIn("raw", r)
                for f in obj.get("pr_files", []):
                    self.assertNotIn("patch", f)
            self.assertFalse(
                any(b'"type":"revisions"' in line for line in _content_lines(data))
            )
        # raw propagates to items, nested comments AND reviews, releases.
        seen_raw = {"item": 0, "comment": 0, "review": 0, "release": 0}
        for line in _content_lines(with_raw):
            obj = json.loads(line.decode("ascii"))
            kind = obj.get("type")
            if kind == "item":
                if "raw" in obj:
                    seen_raw["item"] += 1
                for c in obj.get("comments", []):
                    if "raw" in c:
                        seen_raw["comment"] += 1
                for r in obj.get("reviews", []):
                    if "raw" in r:
                        seen_raw["review"] += 1
            elif kind == "release" and "raw" in obj:
                seen_raw["release"] += 1
        self.assertEqual(seen_raw["item"], 2)
        self.assertEqual(seen_raw["comment"], 1)
        self.assertEqual(seen_raw["review"], 1)
        self.assertEqual(seen_raw["release"], 1)
        # patches: the key appears (null unless synced with --include-patches).
        patch_keys = 0
        for line in _content_lines(with_patches):
            obj = json.loads(line.decode("ascii"))
            for f in obj.get("pr_files", []):
                self.assertIn("patch", f)
                patch_keys += 1
        self.assertEqual(patch_keys, 1)
        # revisions line present, id-free, ordered content-keyed.
        rev_lines = [
            json.loads(line.decode("ascii"))
            for line in _content_lines(with_rev)
            if b'"type":"revisions"' in line
        ]
        self.assertEqual(len(rev_lines), 1)
        for row in rev_lines[0]["rows"]:
            self.assertNotIn("id", row)

    def test_include_revisions_orders_by_content_key_and_carries_rows(self) -> None:
        """The plan's non-vacuous revisions leg: real rows (an empty corpus
        would make the id-free/order assertions iterate nothing), ordered by
        the content key — never by the SQLite-assigned id, which
        consolidation rewrites. The id order here deliberately CONTRADICTS
        the content-key order, so a regression to ORDER BY id fails."""
        self._synced()
        # Content key (number, entity, ...) order: item 1 comment, then
        # item_body, then item_title. Id order: 9001 title, 9002 body,
        # 9003 comment — the opposite.
        self.conn.executemany(
            "INSERT INTO revisions(id, repo, number, entity, entity_id, kind,"
            " origin, content, valid_from, valid_to)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    9001,
                    REPO,
                    1,
                    "item_title",
                    None,
                    None,
                    "edit",
                    "older title",
                    "2026-01-01T00:00:00Z",
                    "2026-01-02T00:00:00Z",
                ),
                (
                    9002,
                    REPO,
                    1,
                    "item_body",
                    None,
                    None,
                    "edit",
                    "older content",
                    "2026-01-01T00:00:00Z",
                    "2026-01-02T00:00:00Z",
                ),
                (
                    9003,
                    REPO,
                    1,
                    "comment",
                    50001,
                    "issue_comment",
                    "deleted",
                    "a deleted comment body",
                    "2026-01-01T00:00:00Z",
                    "2026-01-02T00:00:00Z",
                ),
                (
                    9004,
                    REPO,
                    2,
                    "item_title",
                    None,
                    None,
                    "edit",
                    "pr title preimage",
                    "2026-01-03T00:00:00Z",
                    "2026-01-04T00:00:00Z",
                ),
            ],
        )
        self.conn.commit()
        conn = open_existing(self.db_path)
        try:
            data = _export(conn, str(self.db_path), include=("revisions",))
        finally:
            conn.close()
        rev_lines = [
            json.loads(line.decode("ascii"))
            for line in _content_lines(data)
            if b'"type":"revisions"' in line
        ]
        self.assertEqual(len(rev_lines), 1)
        got = rev_lines[0]["rows"]
        self.assertEqual(len(got), 4)
        keys = [
            (
                r["number"],
                r["entity"],
                r["entity_id"],
                r["kind"],
                r["origin"],
                r["valid_from"],
                r["valid_to"],
                r["content"],
            )
            for r in got
        ]
        self.assertEqual(keys, sorted(keys), "revisions must follow the content key")
        # Content-key order: the item-1 comment row (id 9003, highest of the
        # item-1 ids) comes FIRST, where ORDER BY id would put it last.
        self.assertEqual(
            [r["entity"] for r in got[:3]],
            ["comment", "item_body", "item_title"],
            "id order (title, body, comment) must not leak through",
        )
        for row in got:
            self.assertNotIn("id", row)

    def test_include_patches_exports_stored_patch(self) -> None:
        """The stored (non-null) patch leg: a file synced WITH a patch under
        --include-patches exports that exact patch text; the same store
        without the flag omits the key; a no-patch sync exports null."""
        patched_file = {
            "filename": "src/app.py",
            "status": "modified",
            "additions": 3,
            "deletions": 1,
            "changes": 4,
            "sha": "abc123",
            "patch": "@@ -1,2 +1,4 @@\n line\n+added",
        }
        src = FakeGitHubSource()
        src.add_issue(issue(1, title="one", body="body"))
        src.add_pr(issue(2, title="PR", body="", kind="pr"), pull(2, merged=True), files=[patched_file])
        sync_repo(self.conn, src, REPO, include_patches=True)
        self.assertEqual(
            self.conn.execute("SELECT patch FROM pr_files WHERE number = 2").fetchone()["patch"],
            patched_file["patch"],
        )
        conn = open_existing(self.db_path)
        try:
            with_flag = _export(conn, str(self.db_path), include=("patches",))
            without_flag = _export(conn, str(self.db_path))
        finally:
            conn.close()
        for line in _content_lines(with_flag):
            obj = json.loads(line.decode("ascii"))
            for f in obj.get("pr_files", []):
                self.assertEqual(f["patch"], patched_file["patch"])
        found = False
        for line in _content_lines(without_flag):
            obj = json.loads(line.decode("ascii"))
            for f in obj.get("pr_files", []):
                self.assertNotIn("patch", f)
                found = True
        self.assertTrue(found, "fixture must contain pr_files rows")

    def test_text_mode_prints_stderr_identity_line(self) -> None:
        """The review-round contract: in text mode a successful export prints
        the same stderr trust summary as every other read; machine formats
        stay silent (AC5 already pins the silent leg)."""
        self._synced()
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "zaxbygraph",
                "export-corpus",
                "--repo",
                REPO,
                "--db",
                str(self.db_path),
                "--format",
                "text",
                "--out",
                str(Path(self._td.name) / "corpus.jsonl"),
            ],
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertTrue(proc.stderr.decode("utf-8").startswith("# db="))

    def test_per_item_edges_are_full_key_ordered(self) -> None:
        """The plan's ordering law: an item's routed edges are ordered by the
        full unique key across BOTH routing arms (not block-concatenated)."""
        src = _rich_store(self.conn)
        sync_repo(self.conn, src, REPO)
        # A dst-routed edge (actor authored) and src-routed edges (touches,
        # merged_commit) both live on item 2; full-key order puts actor first
        # (src_type 'actor' < 'item').
        conn = open_existing(self.db_path)
        try:
            data = _export(conn, str(self.db_path))
        finally:
            conn.close()
        for line in _content_lines(data):
            obj = json.loads(line.decode("ascii"))
            if obj.get("type") == "item" and obj["number"] == 2:
                keys = [
                    (e["src_type"], e["src_id"], e["rel"], e["dst_type"], e["dst_id"], e["source"])
                    for e in obj["edges"]
                ]
                self.assertEqual(keys, sorted(keys))

    def test_invalid_include_value(self) -> None:
        self._synced()
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "zaxbygraph",
                "export-corpus",
                "--repo",
                REPO,
                "--db",
                str(self.db_path),
                "--include",
                "bogus",
            ],
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 2)
        env = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "bad_include")

    def test_repo_required(self) -> None:
        self._synced()
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "zaxbygraph",
                "export-corpus",
                "--repo",
                "",
                "--db",
                str(self.db_path),
            ],
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 2)
        env = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(env["error"]["code"], "repo_required")

    def test_out_is_database_rejected_and_store_intact(self) -> None:
        self._synced()
        before = self.db_path.read_bytes()
        for target in (
            str(self.db_path),
            str(self.db_path) + "-wal",
            str(self.db_path) + "-shm",
        ):
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "zaxbygraph",
                    "export-corpus",
                    "--repo",
                    REPO,
                    "--db",
                    str(self.db_path),
                    "--out",
                    target,
                ],
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 2, target)
            env = json.loads(proc.stdout.decode("utf-8"))
            self.assertEqual(env["error"]["code"], "out_is_database")
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_store_too_old(self) -> None:
        self._synced()
        self.conn.execute("PRAGMA user_version = 9")
        self.conn.commit()
        conn = open_existing(self.db_path)
        try:
            with self.assertRaises(export_mod.ExportError) as ctx:
                _export(conn, str(self.db_path))
        finally:
            conn.close()
        self.assertEqual(ctx.exception.code, "store_too_old")

    def test_store_too_new_refuses_envelope_and_old_is_envelope_pinned(self) -> None:
        """Symmetric version guard (cubic CUB-1): a store NEWER than this
        build is refused — exporting it would silently omit fields this
        build does not know — and BOTH version refusals answer through the
        CLI as structured envelopes (the schema-notes carve-out credits
        these pins)."""
        self._synced()
        for version, expected_code in ((11, "store_too_new"), (9, "store_too_old")):
            self.conn.execute(f"PRAGMA user_version = {version}")
            self.conn.commit()
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "zaxbygraph",
                    "export-corpus",
                    "--repo",
                    REPO,
                    "--db",
                    str(self.db_path),
                ],
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 2, (version, proc.stderr))
            env = json.loads(proc.stdout.decode("utf-8"))
            self.assertFalse(env["ok"])
            self.assertEqual(env["error"]["code"], expected_code)

    def test_journal_sidecar_out_is_refused(self) -> None:
        """--out <db>-journal must be refused: a corpus written there is a
        fake hot journal that bricks every read-only open of the store until
        it is deleted by hand (executed brick probe, swarm review PRR-002)."""
        self._synced()
        before = self.db_path.read_bytes()
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "zaxbygraph",
                "export-corpus",
                "--repo",
                REPO,
                "--db",
                str(self.db_path),
                "--out",
                str(self.db_path) + "-journal",
            ],
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 2)
        env = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(env["error"]["code"], "out_is_database")
        self.assertFalse((Path(str(self.db_path) + "-journal")).exists())
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_device_path_out_is_refused(self) -> None:
        """--out NUL must be refused, not silently successful (a device
        target discards the corpus and reports exit 0)."""
        self._synced()
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "zaxbygraph",
                "export-corpus",
                "--repo",
                REPO,
                "--db",
                str(self.db_path),
                "--out",
                "NUL",
            ],
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 2)
        env = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(env["error"]["code"], "out_is_database")

    def test_empty_out_value_is_refused(self) -> None:
        """--out "" must not silently become "write to stdout" (PRR-004)."""
        self._synced()
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "zaxbygraph",
                "export-corpus",
                "--repo",
                REPO,
                "--db",
                str(self.db_path),
                "--out",
                "",
            ],
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 2)
        env = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(env["error"]["code"], "bad_flags")

    def test_destination_failure_answers_envelope_not_traceback(self) -> None:
        """--out into a missing directory must produce the structured error
        envelope, not a traceback (PRR-001 / cubic CUB-2)."""
        self._synced()
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "no-such-dir" / "corpus.jsonl"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "zaxbygraph",
                    "export-corpus",
                    "--repo",
                    REPO,
                    "--db",
                    str(self.db_path),
                    "--out",
                    str(missing),
                ],
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn(b"Traceback", proc.stderr)
            env = json.loads(proc.stdout.decode("utf-8"))
            self.assertFalse(env["ok"])

    def test_empty_store_exports_a_pinned_empty_corpus(self) -> None:
        """A synced-but-empty store exports a legitimate 2-line corpus
        (manifest + empty orphan trailer) — pinned so the behavior cannot
        drift silently (cubic CUB-3 adjudication)."""
        src = FakeGitHubSource()
        sync_repo(self.conn, src, REPO)
        self.conn.execute(
            "DELETE FROM comments"
        )  # nothing to delete; store has zero items already
        self.conn.commit()
        conn = open_existing(self.db_path)
        try:
            data = _export(conn, str(self.db_path))
        finally:
            conn.close()
        lines = _content_lines(data)
        manifest = _manifest_of(data)
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0].decode("ascii"))["type"], "orphan_edges")
        self.assertEqual(manifest["counts"]["items"], 0)
        self.assertTrue(manifest["store"]["complete"])

    def test_child_collections_render_seeded_rows_in_order(self) -> None:
        """The six child collections the fixture stores leave empty render
        seeded rows with the right mapping and content-key order (a wrong
        column name or sort would crash or misorder here)."""
        self._synced()
        self.conn.executemany(
            "INSERT INTO assignees(repo, number, login) VALUES (?, 1, ?)",
            [(REPO, "zed"), (REPO, "amy")],
        )
        self.conn.execute(
            "INSERT INTO issue_field_values(repo, number, field_name, value)"
            " VALUES (?, 1, 'priority', 'P1')",
            (REPO,),
        )
        self.conn.executemany(
            "INSERT INTO item_events(repo, number, type, created_at)"
            " VALUES (?, 1, ?, ?)",
            [
                (REPO, "closed", "2026-02-02T00:00:00Z"),
                (REPO, "referenced", "2026-02-01T00:00:00Z"),
            ],
        )
        self.conn.commit()
        conn = open_existing(self.db_path)
        try:
            data = _export(conn, str(self.db_path))
        finally:
            conn.close()
        for line in _content_lines(data):
            obj = json.loads(line.decode("ascii"))
            if obj.get("type") == "item" and obj["number"] == 1:
                self.assertEqual(obj["assignees"], ["amy", "zed"])
                self.assertEqual(
                    obj["issue_field_values"],
                    [{"field_name": "priority", "value": "P1"}],
                )
                self.assertEqual(
                    [e["type"] for e in obj["item_events"]],
                    ["closed", "referenced"],
                )
                break
        else:
            self.fail("item 1 not found in export")

    def test_concurrent_commit_cannot_split_the_snapshot(self) -> None:
        # WAL snapshot isolation: BEGIN DEFERRED snapshots on FIRST access,
        # so the injection point is the first _item_content call (after the
        # version/completeness/counts reads). The second connection inserts
        # a comment + edge belonging to the item being rendered — data the
        # render step's own LATER independent statements would read. Without
        # the transaction they would appear and change the hash; with it
        # they cannot. (R4-1: an inserted ITEM would hide behind the
        # already-open cursor and discriminate nothing.)
        self._synced()
        conn = open_existing(self.db_path)
        try:
            baseline = _export(conn, str(self.db_path))
        finally:
            conn.close()
        baseline_manifest = _manifest_of(baseline)

        injected = {"done": False}

        real_render = export_mod._item_content

        def render_with_writer(conn, repo, row, include, local):
            if not injected["done"]:
                injected["done"] = True
                writer = connect(Path(self._td.name) / "writer.db")
                try:
                    # Reopen the export target store through the writer.
                    writer.close()
                    writer = connect(self.db_path)
                    writer.execute(
                        "INSERT INTO comments(pk, github_id, repo, number,"
                        " kind, author, created_at, updated_at, body,"
                        " html_url, raw_json)"
                        " VALUES (NULL, 987654, ?, ?, 'issue_comment',"
                        " 'mallory', '2026-06-01T00:00:00Z',"
                        " '2026-06-01T00:00:00Z', 'injected comment', 'x', '{}')",
                        (repo, row["number"]),
                    )
                    writer.execute(
                        "INSERT INTO edges(repo, src_type, src_id, rel,"
                        " dst_type, dst_id, confidence, evidence, source)"
                        " VALUES (?, 'actor', 'mallory', 'commented', 'item',"
                        " ?, 'EXTRACTED', NULL, 'payload')",
                        (repo, str(row["number"])),
                    )
                    writer.commit()
                finally:
                    writer.close()
            return real_render(conn, repo, row, include, local)

        conn = open_existing(self.db_path)
        try:
            with mock.patch.object(
                export_mod, "_item_content", render_with_writer
            ):
                data = _export(conn, str(self.db_path))
        finally:
            conn.close()
        self.assertTrue(injected["done"], "injection never ran (vacuous test)")
        concurrent_manifest = _manifest_of(data)
        # Snapshot isolation held: same content hash, no injected rows.
        self.assertEqual(
            concurrent_manifest["corpus_sha256"],
            baseline_manifest["corpus_sha256"],
        )
        self.assertEqual(_corpus_bytes(data), _corpus_bytes(baseline))
        self.assertNotIn(b"987654", data)
        self.assertNotIn(b"mallory", data)

        # Non-vacuity: a fresh connection sees the injected rows, and a
        # fresh export now includes them (different hash).
        fresh = open_existing(self.db_path)
        try:
            seen = fresh.execute(
                "SELECT COUNT(*) FROM comments WHERE github_id = 987654"
            ).fetchone()[0]
            self.assertEqual(seen, 1)
            after = _export(fresh, str(self.db_path))
        finally:
            fresh.close()
        self.assertNotEqual(
            _manifest_of(after)["corpus_sha256"],
            baseline_manifest["corpus_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
