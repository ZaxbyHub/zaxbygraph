-- zaxbygraph schema v1
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
