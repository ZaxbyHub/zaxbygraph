from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from zaxbygraph.db import CURRENT_USER_VERSION, _schema_sql, connect, init_schema
from zaxbygraph.query import status, search

#: The schema as it stood at base e2ef892 (v1): no `PRAGMA user_version`, no
#: `full_sync_pending`. Frozen verbatim from that commit so this suite can
#: build genuine v1 databases; it must never track later schema edits.
V1_SCHEMA_SQL = r"""-- zaxbygraph schema v1
-- Raw GitHub issue/PR corpus + EXTRACTED graph edges.
-- Derived/interpreted tables are NEVER written by the fetcher.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actors (
    login    TEXT PRIMARY KEY,
    html_url TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    repo          TEXT NOT NULL,
    number        INTEGER NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('issue', 'pr')),
    node_id       TEXT,
    title         TEXT NOT NULL,
    body          TEXT,
    labels_text   TEXT,
    state         TEXT NOT NULL,
    state_reason  TEXT,
    author        TEXT,
    created_at    TEXT,
    updated_at    TEXT,
    closed_at     TEXT,
    merged_at     TEXT,
    merge_commit  TEXT,
    draft         INTEGER NOT NULL DEFAULT 0,
    locked        INTEGER NOT NULL DEFAULT 0,
    base_ref      TEXT,
    head_ref      TEXT,
    additions     INTEGER,
    deletions     INTEGER,
    changed_files INTEGER,
    commits       INTEGER,
    html_url      TEXT,
    api_url       TEXT,
    raw_json      TEXT NOT NULL,
    UNIQUE (repo, number)
);

CREATE INDEX IF NOT EXISTS idx_items_kind_state ON items(kind, state);
CREATE INDEX IF NOT EXISTS idx_items_updated ON items(updated_at);
CREATE INDEX IF NOT EXISTS idx_items_author ON items(author);
CREATE INDEX IF NOT EXISTS idx_items_repo_number ON items(repo, number);

CREATE TABLE IF NOT EXISTS labels (
    repo   TEXT NOT NULL,
    number INTEGER NOT NULL,
    name   TEXT NOT NULL,
    color  TEXT,
    PRIMARY KEY (repo, number, name)
);

CREATE TABLE IF NOT EXISTS comments (
    pk          INTEGER PRIMARY KEY AUTOINCREMENT,
    github_id   INTEGER NOT NULL,
    repo        TEXT NOT NULL,
    number      INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('issue_comment', 'review_comment')),
    author      TEXT,
    created_at  TEXT,
    updated_at  TEXT,
    body        TEXT,
    html_url    TEXT,
    in_reply_to INTEGER,
    raw_json    TEXT NOT NULL,
    UNIQUE (repo, kind, github_id)
);

CREATE INDEX IF NOT EXISTS idx_comments_item ON comments(repo, number);

CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY,
    repo         TEXT NOT NULL,
    number       INTEGER NOT NULL,
    author       TEXT,
    state        TEXT,
    submitted_at TEXT,
    body         TEXT,
    html_url     TEXT,
    raw_json     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviews_item ON reviews(repo, number);

CREATE TABLE IF NOT EXISTS pr_files (
    repo      TEXT NOT NULL,
    number    INTEGER NOT NULL,
    path      TEXT NOT NULL,
    status    TEXT,
    additions INTEGER,
    deletions INTEGER,
    changes   INTEGER,
    sha       TEXT,
    patch     TEXT,
    PRIMARY KEY (repo, number, path)
);

CREATE INDEX IF NOT EXISTS idx_pr_files_path ON pr_files(path);

CREATE TABLE IF NOT EXISTS releases (
    id           INTEGER PRIMARY KEY,
    repo         TEXT NOT NULL,
    tag_name     TEXT NOT NULL,
    name         TEXT,
    body         TEXT,
    draft        INTEGER NOT NULL DEFAULT 0,
    prerelease   INTEGER NOT NULL DEFAULT 0,
    author       TEXT,
    created_at   TEXT,
    published_at TEXT,
    html_url     TEXT,
    raw_json     TEXT NOT NULL,
    UNIQUE (repo, tag_name)
);

CREATE TABLE IF NOT EXISTS edges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    repo       TEXT NOT NULL,
    src_type   TEXT NOT NULL,
    src_id     TEXT NOT NULL,
    rel        TEXT NOT NULL,
    dst_type   TEXT NOT NULL,
    dst_id     TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('EXTRACTED')),
    evidence   TEXT,
    UNIQUE (repo, src_type, src_id, rel, dst_type, dst_id)
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(repo, src_type, src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(repo, dst_type, dst_id);
CREATE INDEX IF NOT EXISTS idx_edges_rel ON edges(rel);

CREATE TABLE IF NOT EXISTS fetch_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo         TEXT NOT NULL,
    resource     TEXT NOT NULL,
    resource_id  TEXT,
    fetched_at   TEXT NOT NULL,
    http_status  INTEGER,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
    repo               TEXT PRIMARY KEY,
    issues_since       TEXT,
    last_full_sync_at  TEXT,
    last_incr_sync_at  TEXT,
    last_error         TEXT,
    item_count         INTEGER NOT NULL DEFAULT 0,
    comment_count      INTEGER NOT NULL DEFAULT 0,
    edge_count         INTEGER NOT NULL DEFAULT 0,
    include_patches    INTEGER NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title,
    body,
    labels_text,
    content='items',
    content_rowid='id'
);

CREATE VIRTUAL TABLE IF NOT EXISTS comments_fts USING fts5(
    body,
    content='comments',
    content_rowid='pk'
);

CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, body, labels_text)
    VALUES (new.id, new.title, new.body, new.labels_text);
END;

CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body, labels_text)
    VALUES ('delete', old.id, old.title, old.body, old.labels_text);
END;

CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body, labels_text)
    VALUES ('delete', old.id, old.title, old.body, old.labels_text);
    INSERT INTO items_fts(rowid, title, body, labels_text)
    VALUES (new.id, new.title, new.body, new.labels_text);
END;

CREATE TRIGGER IF NOT EXISTS comments_ai AFTER INSERT ON comments BEGIN
    INSERT INTO comments_fts(rowid, body) VALUES (new.pk, new.body);
END;

CREATE TRIGGER IF NOT EXISTS comments_ad AFTER DELETE ON comments BEGIN
    INSERT INTO comments_fts(comments_fts, rowid, body)
    VALUES ('delete', old.pk, old.body);
END;

CREATE TRIGGER IF NOT EXISTS comments_au AFTER UPDATE ON comments BEGIN
    INSERT INTO comments_fts(comments_fts, rowid, body)
    VALUES ('delete', old.pk, old.body);
    INSERT INTO comments_fts(rowid, body) VALUES (new.pk, new.body);
END;
"""


#: The schema as it stood at base 4789858 (v2): `full_sync_pending` present,
#: FTS tables still on the default unicode61 tokenizer (no porter). Frozen
#: verbatim from that commit's src/zaxbygraph/schema.sql so this suite can
#: build genuine v2 databases; it must never track later schema edits. The
#: v2 schema.sql does not itself stamp `PRAGMA user_version` -- a seeder
#: must set `PRAGMA user_version = 2` explicitly after executing it.
V2_SCHEMA_SQL = r"""-- zaxbygraph schema v2 (`PRAGMA user_version` = 2; see db.py MIGRATIONS)
-- Raw GitHub issue/PR corpus + EXTRACTED graph edges.
-- Derived/interpreted tables are NEVER written by the fetcher.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actors (
    login    TEXT PRIMARY KEY,
    html_url TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    repo          TEXT NOT NULL,
    number        INTEGER NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('issue', 'pr')),
    node_id       TEXT,
    title         TEXT NOT NULL,
    body          TEXT,
    labels_text   TEXT,
    state         TEXT NOT NULL,
    state_reason  TEXT,
    author        TEXT,
    created_at    TEXT,
    updated_at    TEXT,
    closed_at     TEXT,
    merged_at     TEXT,
    merge_commit  TEXT,
    draft         INTEGER NOT NULL DEFAULT 0,
    locked        INTEGER NOT NULL DEFAULT 0,
    base_ref      TEXT,
    head_ref      TEXT,
    additions     INTEGER,
    deletions     INTEGER,
    changed_files INTEGER,
    commits       INTEGER,
    html_url      TEXT,
    api_url       TEXT,
    raw_json      TEXT NOT NULL,
    UNIQUE (repo, number)
);

CREATE INDEX IF NOT EXISTS idx_items_kind_state ON items(kind, state);
CREATE INDEX IF NOT EXISTS idx_items_updated ON items(updated_at);
CREATE INDEX IF NOT EXISTS idx_items_author ON items(author);
CREATE INDEX IF NOT EXISTS idx_items_repo_number ON items(repo, number);

CREATE TABLE IF NOT EXISTS labels (
    repo   TEXT NOT NULL,
    number INTEGER NOT NULL,
    name   TEXT NOT NULL,
    color  TEXT,
    PRIMARY KEY (repo, number, name)
);

CREATE TABLE IF NOT EXISTS comments (
    pk          INTEGER PRIMARY KEY AUTOINCREMENT,
    github_id   INTEGER NOT NULL,
    repo        TEXT NOT NULL,
    number      INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('issue_comment', 'review_comment')),
    author      TEXT,
    created_at  TEXT,
    updated_at  TEXT,
    body        TEXT,
    html_url    TEXT,
    in_reply_to INTEGER,
    raw_json    TEXT NOT NULL,
    UNIQUE (repo, kind, github_id)
);

CREATE INDEX IF NOT EXISTS idx_comments_item ON comments(repo, number);

CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY,
    repo         TEXT NOT NULL,
    number       INTEGER NOT NULL,
    author       TEXT,
    state        TEXT,
    submitted_at TEXT,
    body         TEXT,
    html_url     TEXT,
    raw_json     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviews_item ON reviews(repo, number);

CREATE TABLE IF NOT EXISTS pr_files (
    repo      TEXT NOT NULL,
    number    INTEGER NOT NULL,
    path      TEXT NOT NULL,
    status    TEXT,
    additions INTEGER,
    deletions INTEGER,
    changes   INTEGER,
    sha       TEXT,
    patch     TEXT,
    PRIMARY KEY (repo, number, path)
);

CREATE INDEX IF NOT EXISTS idx_pr_files_path ON pr_files(path);

CREATE TABLE IF NOT EXISTS releases (
    id           INTEGER PRIMARY KEY,
    repo         TEXT NOT NULL,
    tag_name     TEXT NOT NULL,
    name         TEXT,
    body         TEXT,
    draft        INTEGER NOT NULL DEFAULT 0,
    prerelease   INTEGER NOT NULL DEFAULT 0,
    author       TEXT,
    created_at   TEXT,
    published_at TEXT,
    html_url     TEXT,
    raw_json     TEXT NOT NULL,
    UNIQUE (repo, tag_name)
);

CREATE TABLE IF NOT EXISTS edges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    repo       TEXT NOT NULL,
    src_type   TEXT NOT NULL,
    src_id     TEXT NOT NULL,
    rel        TEXT NOT NULL,
    dst_type   TEXT NOT NULL,
    dst_id     TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('EXTRACTED')),
    evidence   TEXT,
    UNIQUE (repo, src_type, src_id, rel, dst_type, dst_id)
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(repo, src_type, src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(repo, dst_type, dst_id);
CREATE INDEX IF NOT EXISTS idx_edges_rel ON edges(rel);

CREATE TABLE IF NOT EXISTS fetch_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo         TEXT NOT NULL,
    resource     TEXT NOT NULL,
    resource_id  TEXT,
    fetched_at   TEXT NOT NULL,
    http_status  INTEGER,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
    repo               TEXT PRIMARY KEY,
    issues_since       TEXT,
    last_full_sync_at  TEXT,
    last_incr_sync_at  TEXT,
    last_error         TEXT,
    item_count         INTEGER NOT NULL DEFAULT 0,
    comment_count      INTEGER NOT NULL DEFAULT 0,
    edge_count         INTEGER NOT NULL DEFAULT 0,
    include_patches    INTEGER NOT NULL DEFAULT 0,
    full_sync_pending  INTEGER NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title,
    body,
    labels_text,
    content='items',
    content_rowid='id'
);

CREATE VIRTUAL TABLE IF NOT EXISTS comments_fts USING fts5(
    body,
    content='comments',
    content_rowid='pk'
);

CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, body, labels_text)
    VALUES (new.id, new.title, new.body, new.labels_text);
END;

CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body, labels_text)
    VALUES ('delete', old.id, old.title, old.body, old.labels_text);
END;

CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body, labels_text)
    VALUES ('delete', old.id, old.title, old.body, old.labels_text);
    INSERT INTO items_fts(rowid, title, body, labels_text)
    VALUES (new.id, new.title, new.body, new.labels_text);
END;

CREATE TRIGGER IF NOT EXISTS comments_ai AFTER INSERT ON comments BEGIN
    INSERT INTO comments_fts(rowid, body) VALUES (new.pk, new.body);
END;

CREATE TRIGGER IF NOT EXISTS comments_ad AFTER DELETE ON comments BEGIN
    INSERT INTO comments_fts(comments_fts, rowid, body)
    VALUES ('delete', old.pk, old.body);
END;

CREATE TRIGGER IF NOT EXISTS comments_au AFTER UPDATE ON comments BEGIN
    INSERT INTO comments_fts(comments_fts, rowid, body)
    VALUES ('delete', old.pk, old.body);
    INSERT INTO comments_fts(rowid, body) VALUES (new.pk, new.body);
END;
"""


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        self.db_path = Path(td.name) / "history.db"
        v1 = sqlite3.connect(str(self.db_path))
        v1.executescript(V1_SCHEMA_SQL)
        v1.commit()
        v1.close()

    def _open_current(self) -> sqlite3.Connection:
        conn = connect(self.db_path)
        self.addCleanup(conn.close)
        init_schema(conn)
        return conn

    def _seed_case_split(self, v1: sqlite3.Connection) -> None:
        """The R4 mixed-key shape plus a one-set/one-NULL sync_state merge."""
        v1.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at, last_error,"
            " item_count, comment_count, edge_count, include_patches)"
            " VALUES ('ZaxbyHub/TrainingApp', '2026-01-02T00:00:00Z', NULL, 'interrupted', 1, 0, 1, 0)"
        )
        v1.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at, last_error,"
            " item_count, comment_count, edge_count, include_patches)"
            " VALUES ('zaxbyhub/trainingapp', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL, 1, 0, 0, 0)"
        )
        # items: id 1001 already rewritten to lowercase by ON CONFLICT(id) (the
        # R4 discovery); item 2 only ever synced under the upper casing.
        v1.execute(
            "INSERT INTO items(id, repo, number, kind, title, state, updated_at, raw_json)"
            " VALUES (1001, 'zaxbyhub/trainingapp', 1, 'issue', 'one', 'open', '2026-01-05T00:00:00Z', '{}')"
        )
        v1.execute(
            "INSERT INTO items(id, repo, number, kind, title, state, updated_at, raw_json)"
            " VALUES (1002, 'ZaxbyHub/TrainingApp', 2, 'issue', 'two', 'open', '2026-01-04T00:00:00Z', '{}')"
        )
        # labels for number 1 under BOTH casings (conflicting colors)
        v1.execute("INSERT INTO labels(repo, number, name, color) VALUES ('ZaxbyHub/TrainingApp', 1, 'bug', 'ff0000')")
        v1.execute("INSERT INTO labels(repo, number, name, color) VALUES ('zaxbyhub/trainingapp', 1, 'bug', '00ff00')")
        # a comment and a release under the upper casing only
        v1.execute(
            "INSERT INTO comments(github_id, repo, number, kind, author, body, raw_json)"
            " VALUES (5001, 'ZaxbyHub/TrainingApp', 2, 'issue_comment', 'bob', 'hello two', '{}')"
        )
        # releases: same tag under both casings, distinct ids (releases.id is
        # the global PK, so a real v1 sync could never hold one id twice)
        v1.execute(
            "INSERT INTO releases(id, repo, tag_name, raw_json)"
            " VALUES (7001, 'ZaxbyHub/TrainingApp', 'v1.0.0', '{}')"
        )
        v1.execute(
            "INSERT INTO releases(id, repo, tag_name, raw_json)"
            " VALUES (7002, 'zaxbyhub/trainingapp', 'v1.0.0', '{}')"
        )
        # edges under both casings
        v1.execute(
            "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id, confidence, evidence)"
            " VALUES ('ZaxbyHub/TrainingApp', 'item', '2', 'mentions', 'item', '1', 'EXTRACTED', 'body #N')"
        )
        v1.execute(
            "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id, confidence, evidence)"
            " VALUES ('zaxbyhub/trainingapp', 'actor', 'alice', 'authored', 'item', '1', 'EXTRACTED', 'user.login')"
        )
        # fetch_log under both casings (no unique key on repo)
        v1.execute("INSERT INTO fetch_log(repo, resource, resource_id, fetched_at) VALUES ('ZaxbyHub/TrainingApp', 'item', '2', '2026-01-04T00:00:00Z')")
        v1.execute("INSERT INTO fetch_log(repo, resource, resource_id, fetched_at) VALUES ('zaxbyhub/trainingapp', 'item', '1', '2026-01-05T00:00:00Z')")
        # an unrelated repo that must survive untouched
        v1.execute("INSERT INTO sync_state(repo, issues_since) VALUES ('other/repo', '2026-02-01T00:00:00Z')")
        v1.execute(
            "INSERT INTO items(id, repo, number, kind, title, state, updated_at, raw_json)"
            " VALUES (2001, 'other/repo', 9, 'issue', 'nine', 'open', '2026-02-01T00:00:00Z', '{}')"
        )
        v1.commit()

    def test_v1_db_upgrades_and_merges_case_split_repos(self) -> None:
        v1 = sqlite3.connect(str(self.db_path))
        self._seed_case_split(v1)
        v1.close()

        conn = self._open_current()

        # upgraded in place, user_version stamped
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM sync_state WHERE repo LIKE 'zaxbyhub/%'").fetchone()[0], 1
        )
        row = conn.execute(
            "SELECT * FROM sync_state WHERE repo = 'zaxbyhub/trainingapp'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["issues_since"], "2026-01-02T00:00:00Z")  # winner by watermark
        self.assertEqual(row["last_full_sync_at"], "2026-01-01T00:00:00Z")  # MAX merge
        self.assertEqual(row["full_sync_pending"], 0)  # derived from MAX timestamp
        self.assertEqual(row["last_error"], "interrupted")  # winner's
        self.assertEqual(row["include_patches"], 0)

        # items folded: both rows survive under one lowercase casing
        self.assertEqual(
            [r[0] for r in conn.execute("SELECT DISTINCT repo FROM items WHERE repo LIKE 'zaxbyhub/%'")],
            ["zaxbyhub/trainingapp"],
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM items WHERE repo LIKE 'zaxbyhub/%'").fetchone()[0], 2
        )
        # labels: one folded row, deterministic survivor color
        label = conn.execute("SELECT color FROM labels WHERE number = 1").fetchone()
        self.assertEqual(label[0], "00ff00")  # composite-key DESC: lowercase casing first
        # comments / releases / edges / fetch_log folded
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM comments WHERE number = 2").fetchone()[0], 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM releases WHERE tag_name = 'v1.0.0'").fetchone()[0], 1
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM edges WHERE repo LIKE 'zaxbyhub/%'").fetchone()[0], 2
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM fetch_log WHERE repo LIKE 'zaxbyhub/%'").fetchone()[0], 2
        )
        # counts recount()ed from the folded corpus
        self.assertEqual(row["item_count"], 2)
        # the unrelated repo is untouched
        self.assertIsNotNone(
            conn.execute("SELECT 1 FROM items WHERE repo = 'other/repo' AND number = 9").fetchone()
        )
        # FTS stayed consistent through the fold (triggers fired on clear/reinsert)
        hits = search(conn, "two", repo="zaxbyhub/trainingapp")
        self.assertTrue(hits["items"], "FTS must find folded content")

    def test_legacy_null_full_sync_rows_pending(self) -> None:
        v1 = sqlite3.connect(str(self.db_path))
        v1.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at)"
            " VALUES ('solo/repo', '2026-03-01T00:00:00Z', NULL)"
        )
        v1.execute(
            "INSERT INTO items(id, repo, number, kind, title, state, updated_at, raw_json)"
            " VALUES (31001, 'solo/repo', 1, 'issue', 'solo', 'open', '2026-03-01T00:00:00Z', '{}')"
        )
        v1.commit()
        v1.close()

        conn = self._open_current()
        # conservative seed: completeness is unprovable for a legacy row, so the
        # migration asserts nothing and leaves complete=false for a clean run to earn
        row = conn.execute("SELECT * FROM sync_state WHERE repo = 'solo/repo'").fetchone()
        self.assertEqual(row["full_sync_pending"], 1)
        self.assertEqual(status(conn, "solo/repo")["repos"][0]["complete"], False)

    def test_distinct_id_collision_drops_losers_children(self) -> None:
        v1 = sqlite3.connect(str(self.db_path))
        # delete-and-recreate shape: same (repo, number), distinct ids, lower fresher
        v1.execute(
            "INSERT INTO items(id, repo, number, kind, title, state, updated_at, raw_json)"
            " VALUES (4001, 'ZaxbyHub/TrainingApp', 7, 'issue', 'old seven', 'closed', '2026-01-01T00:00:00Z', '{}')"
        )
        v1.execute(
            "INSERT INTO items(id, repo, number, kind, title, state, updated_at, raw_json)"
            " VALUES (4002, 'zaxbyhub/trainingapp', 7, 'issue', 'new seven', 'open', '2026-01-02T00:00:00Z', '{}')"
        )
        v1.execute("INSERT INTO labels(repo, number, name, color) VALUES ('ZaxbyHub/TrainingApp', 7, 'bug', 'ff0000')")
        v1.execute("INSERT INTO labels(repo, number, name, color) VALUES ('zaxbyhub/trainingapp', 7, 'bug', '00ff00')")
        # loser's outbound authored edge and an inbound mention edge at the loser casing
        v1.execute(
            "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id, confidence, evidence)"
            " VALUES ('ZaxbyHub/TrainingApp', 'actor', 'old-bot', 'authored', 'item', '7', 'EXTRACTED', 'user.login')"
        )
        v1.execute(
            "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id, confidence, evidence)"
            " VALUES ('ZaxbyHub/TrainingApp', 'item', '2', 'mentions', 'item', '7', 'EXTRACTED', 'body #N')"
        )
        # winner's edge at the lower casing survives
        v1.execute(
            "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id, confidence, evidence)"
            " VALUES ('zaxbyhub/trainingapp', 'actor', 'new-bot', 'authored', 'item', '7', 'EXTRACTED', 'user.login')"
        )
        v1.commit()
        v1.close()

        conn = self._open_current()
        # freshest item survives; the loser item row is gone
        row = conn.execute(
            "SELECT id, title FROM items WHERE repo = 'zaxbyhub/trainingapp' AND number = 7"
        ).fetchone()
        self.assertEqual(row["id"], 4002)
        self.assertEqual(row["title"], "new seven")
        # loser's children and number-scoped edges (outbound AND inbound) deleted
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM labels WHERE number = 7").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT color FROM labels WHERE number = 7").fetchone()[0], "00ff00")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM edges WHERE repo LIKE 'zaxbyhub/%' AND (src_id = '7' OR dst_id = '7')").fetchone()[0],
            1,
        )

    def test_tie_break_agrees_between_fold_and_collision_winner(self) -> None:
        v1 = sqlite3.connect(str(self.db_path))
        # identical updated_at, distinct ids: the higher id must win in BOTH the
        # collision cleanup and the fold ordering
        v1.execute(
            "INSERT INTO items(id, repo, number, kind, title, state, updated_at, raw_json)"
            " VALUES (3001, 'ZaxbyHub/TrainingApp', 5, 'issue', 'lower id', 'closed', '2026-06-01T00:00:00Z', '{}')"
        )
        v1.execute(
            "INSERT INTO items(id, repo, number, kind, title, state, updated_at, raw_json)"
            " VALUES (3002, 'zaxbyhub/trainingapp', 5, 'issue', 'higher id', 'open', '2026-06-01T00:00:00Z', '{}')"
        )
        v1.execute("INSERT INTO labels(repo, number, name, color) VALUES ('ZaxbyHub/TrainingApp', 5, 'bug', 'ff0000')")
        v1.execute("INSERT INTO labels(repo, number, name, color) VALUES ('zaxbyhub/trainingapp', 5, 'bug', '00ff00')")
        v1.commit()
        v1.close()

        conn = self._open_current()
        row = conn.execute(
            "SELECT id FROM items WHERE repo = 'zaxbyhub/trainingapp' AND number = 5"
        ).fetchone()
        self.assertEqual(row["id"], 3002)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM labels WHERE number = 5").fetchone()[0], 1)

    def test_init_schema_is_idempotent(self) -> None:
        v1 = sqlite3.connect(str(self.db_path))
        self._seed_case_split(v1)
        v1.close()

        conn = self._open_current()
        before = [
            conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0],
        ]
        init_schema(conn)  # second run must be a no-op
        after = [
            conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0],
        ]
        self.assertEqual(before, after)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )

    def test_newer_user_version_refuses(self) -> None:
        conn = self._open_current()
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        conn2 = connect(self.db_path)
        self.addCleanup(conn2.close)
        with self.assertRaises(RuntimeError):
            init_schema(conn2)

    def test_migration_failure_rolls_back(self) -> None:
        v1 = sqlite3.connect(str(self.db_path))
        self._seed_case_split(v1)
        v1.close()

        conn = connect(self.db_path)
        self.addCleanup(conn.close)
        from unittest.mock import patch

        calls = {"n": 0}

        def explode_on_second_fold(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("fold exploded")
            return None

        with patch(
            "zaxbygraph.db._fold_dedupe_table",
            side_effect=explode_on_second_fold,
        ):
            with self.assertRaises(RuntimeError):
                init_schema(conn)
        # The mock replaces _fold_dedupe_table entirely (no wraps=), so call 1
        # is a no-op; the destructive statement that ran inside the transaction
        # is the ALTER TABLE. These assertions therefore only hold if the
        # rollback actually happened (neutered or committing rollback leaves
        # the column and stamps visible).
        self.assertEqual(calls["n"], 2)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sync_state)")]
        self.assertNotIn("full_sync_pending", cols)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 3
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0], 3
        )
        conn.close()
        # A later clean run migrates fully.
        conn2 = connect(self.db_path)
        self.addCleanup(conn2.close)
        init_schema(conn2)
        self.assertEqual(
            conn2.execute("PRAGMA user_version").fetchone()[0], CURRENT_USER_VERSION
        )
        self.assertEqual(
            conn2.execute("SELECT COUNT(*) FROM items WHERE repo LIKE 'zaxbyhub/%'").fetchone()[0],
            2,
        )

    def test_legacy_completed_row_reports_complete(self) -> None:
        v1 = sqlite3.connect(str(self.db_path))
        v1.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at, last_error)"
            " VALUES ('done/repo', '2026-04-01T00:00:00Z', '2026-04-01T00:00:00Z', NULL)"
        )
        v1.execute(
            "INSERT INTO items(id, repo, number, kind, title, state, updated_at, raw_json)"
            " VALUES (41001, 'done/repo', 1, 'issue', 'done', 'open', '2026-04-01T00:00:00Z', '{}')"
        )
        v1.commit()
        v1.close()

        conn = self._open_current()
        # A legacy row that completed a full sync and recorded no error is
        # complete:true after the upgrade (the dual of the pending-seeding test).
        row = conn.execute("SELECT * FROM sync_state WHERE repo = 'done/repo'").fetchone()
        self.assertEqual(row["full_sync_pending"], 0)
        self.assertEqual(status(conn, "done/repo")["repos"][0]["complete"], True)

    def test_v3_db_gains_rate_limit_columns(self) -> None:
        """Issue #5 / PR-13 review TC-07: a genuine v3 database (no rate
        columns, user_version = 3) migrates to v4 with the two new
        sync_state columns present — an ALTER that silently no-ops would
        leave upgrading users with NULL-forever columns while fresh
        databases still pass every other test."""
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        db_path = Path(td.name) / "history.db"
        v3 = sqlite3.connect(str(db_path))
        # Build the CURRENT shape, then wind it back to the exact v3 shape:
        # v4's two columns must not exist when the migration runs.
        v3.executescript(_schema_sql())
        v3.execute("ALTER TABLE sync_state DROP COLUMN rate_limit_remaining")
        v3.execute("ALTER TABLE sync_state DROP COLUMN rate_limit_reset_at")
        v3.execute("PRAGMA user_version = 3")
        v3.execute(
            "INSERT INTO sync_state(repo, issues_since, last_full_sync_at,"
            " last_error, item_count, full_sync_pending)"
            " VALUES ('acme/forgegate', '2026-01-03T00:00:00Z', NULL, NULL, 0, 1)"
        )
        v3.commit()
        v3.close()

        conn = connect(db_path)
        self.addCleanup(conn.close)
        init_schema(conn)

        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0],
            CURRENT_USER_VERSION,
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sync_state)")}
        self.assertIn("rate_limit_remaining", cols)
        self.assertIn("rate_limit_reset_at", cols)
        row = conn.execute(
            "SELECT rate_limit_remaining, rate_limit_reset_at FROM sync_state"
            " WHERE repo = 'acme/forgegate'"
        ).fetchone()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])

    def test_fts_tokenizer_migration_rebuilds_in_place(self) -> None:
        """Issue #4 AC6: opening a v2 database (unicode61 FTS) migrates the
        FTS tables to the porter tokenizer in place -- no resync -- and every
        pre-migration row stays searchable."""
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        db_path = Path(td.name) / "history.db"
        v2 = sqlite3.connect(str(db_path))
        v2.executescript(V2_SCHEMA_SQL)
        # the v2 schema.sql never stamps user_version itself; a genuine v2
        # database carries it from the migration that produced it
        v2.execute("PRAGMA user_version = 2")
        v2.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state, updated_at, raw_json)"
            " VALUES (1001, 'acme/forgegate', 1, 'issue', 'WebSocket reconnect leaks memory',"
            " 'every reconnect leaks a timer', 'open', '2026-01-03T00:00:00Z', '{}')"
        )
        v2.execute(
            "INSERT INTO items(id, repo, number, kind, title, body, state, updated_at, raw_json)"
            " VALUES (1002, 'acme/forgegate', 2, 'issue', 'Reconnection drops timers',"
            " 'reconnection and reconnecting both drop the timer', 'open', '2026-01-04T00:00:00Z', '{}')"
        )
        v2.execute(
            "INSERT INTO comments(github_id, repo, number, kind, author, body, raw_json)"
            " VALUES (5001, 'acme/forgegate', 2, 'issue_comment', 'bob', 'the watermark quokka', '{}')"
        )
        v2.commit()
        v2.close()

        conn = connect(db_path)
        self.addCleanup(conn.close)
        init_schema(conn)

        # (a) the migration framework stamped the new schema version
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0],
            CURRENT_USER_VERSION,
            "v2 DB must migrate to the current user_version (porter FTS rebuild among them)",
        )
        # (b) the rebuilt FTS DDL carries the porter tokenizer
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'items_fts'"
        ).fetchone()[0]
        self.assertIn("porter", ddl.lower())
        # (c) a term now matching only by stemming (stored "reconnection",
        # query "reconnect") finds item 2 -- item 2 never contains the literal
        hits = search(conn, "reconnect", repo="acme/forgegate")
        self.assertIn(2, [i["number"] for i in hits["items"]])
        # (d) every pre-migration row stays searchable by a literal term
        lit1 = search(conn, "leaks", repo="acme/forgegate")
        self.assertIn(1, [i["number"] for i in lit1["items"]])
        lit2 = search(conn, "drops", repo="acme/forgegate")
        self.assertIn(2, [i["number"] for i in lit2["items"]])
        crows = conn.execute(
            "SELECT comments.number FROM comments_fts"
            " JOIN comments ON comments.pk = comments_fts.rowid"
            " WHERE comments_fts MATCH 'quokka'"
        ).fetchall()
        self.assertEqual([r[0] for r in crows], [2])
        # (e) no resync happened: the items content is unchanged
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)
        self.assertEqual(
            conn.execute("SELECT title FROM items WHERE number = 1").fetchone()[0],
            "WebSocket reconnect leaks memory",
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
