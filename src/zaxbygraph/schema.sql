-- zaxbygraph schema v10 (`PRAGMA user_version` = 10; see db.py MIGRATIONS)
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
    -- v7 payload-signal columns (issue #24). Listing-owned columns are
    -- extracted from the /issues listing payload only; merged_by,
    -- merge_commit and merged_at are pull-owned. Re-ingest from a source
    -- that does not carry a column preserves the stored value (ABSENT-key
    -- semantics, store.upsert_item_row); a key present with JSON null
    -- clears it. comment_count and paths_text are derived: recomputed from
    -- the stored child rows on every ingest, never read from a payload.
    reactions_total               INTEGER,
    milestone                     TEXT,
    item_type                     TEXT,
    sub_issues_total              INTEGER,
    sub_issues_completed          INTEGER,
    dependencies_blocked_by       INTEGER,
    dependencies_blocking         INTEGER,
    dependencies_total_blocked_by INTEGER,
    dependencies_total_blocking   INTEGER,
    author_association            TEXT,
    merged_by                     TEXT,
    comment_count                 INTEGER NOT NULL DEFAULT 0,
    paths_text                    TEXT NOT NULL DEFAULT '',
    raw_json      TEXT NOT NULL,
    UNIQUE (repo, number)
);

CREATE INDEX IF NOT EXISTS idx_items_kind_state ON items(kind, state);
CREATE INDEX IF NOT EXISTS idx_items_updated ON items(updated_at);
CREATE INDEX IF NOT EXISTS idx_items_author ON items(author);
CREATE INDEX IF NOT EXISTS idx_items_repo_number ON items(repo, number);
CREATE INDEX IF NOT EXISTS idx_items_repo_merge ON items(repo, merge_commit);
-- Revert-title lookups during ingest (store.title_lookup and the reverse
-- revert pass) seek on (repo, title); without it they ride the merge index's
-- repo prefix and read every items row. Additive: init_schema re-runs this
-- script on every write-open, so existing stores gain it with no version bump.
CREATE INDEX IF NOT EXISTS idx_items_repo_title ON items(repo, title);

CREATE TABLE IF NOT EXISTS labels (
    repo   TEXT NOT NULL,
    number INTEGER NOT NULL,
    name   TEXT NOT NULL,
    color  TEXT,
    PRIMARY KEY (repo, number, name)
);

-- v7: listing-payload assignees and custom issue field values, one row per
-- payload entry, replaced with the other item-scoped children on re-ingest.
CREATE TABLE IF NOT EXISTS assignees (
    repo   TEXT NOT NULL,
    number INTEGER NOT NULL,
    login  TEXT NOT NULL,
    PRIMARY KEY (repo, number, login)
);

CREATE TABLE IF NOT EXISTS issue_field_values (
    repo       TEXT NOT NULL,
    number     INTEGER NOT NULL,
    field_name TEXT NOT NULL,
    value      TEXT,
    PRIMARY KEY (repo, number, field_name)
);

CREATE TABLE IF NOT EXISTS comments (
    pk          INTEGER PRIMARY KEY AUTOINCREMENT,
    github_id   INTEGER NOT NULL,
    repo        TEXT NOT NULL,
    number      INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('issue_comment', 'review_comment')),
    author      TEXT,
    author_association TEXT,
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
    source     TEXT NOT NULL,
    UNIQUE (repo, src_type, src_id, rel, dst_type, dst_id, source)
);

-- The v6 closes-pair index (at most one `closes` row per
-- (repo, src_type, src_id, dst_type, dst_id); issue #19) is created by
-- db.migrate_v5_to_v6, NOT here: init_schema runs this script BEFORE the
-- migration chain, and a UNIQUE index here would fail against a v5 store's
-- not-yet-collapsed duplicate pairs. Fresh databases get the index anyway —
-- they enter the forward chain at user_version 0.

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
    repo                 TEXT PRIMARY KEY,
    issues_since         TEXT,
    last_full_sync_at    TEXT,
    last_incr_sync_at    TEXT,
    last_error           TEXT,
    item_count           INTEGER NOT NULL DEFAULT 0,
    comment_count        INTEGER NOT NULL DEFAULT 0,
    edge_count           INTEGER NOT NULL DEFAULT 0,
    include_patches      INTEGER NOT NULL DEFAULT 0,
    full_sync_pending    INTEGER NOT NULL DEFAULT 0,
    rate_limit_remaining INTEGER,
    rate_limit_reset_at  TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title,
    body,
    labels_text,
    paths_text,
    tokenize = 'porter unicode61',
    content='items',
    content_rowid='id'
);

CREATE VIRTUAL TABLE IF NOT EXISTS comments_fts USING fts5(
    body,
    tokenize = 'porter unicode61',
    content='comments',
    content_rowid='pk'
);

-- v7: review bodies join search (issue #24 AC4). Built and repopulated by
-- migrate_v6_to_v7 for existing stores; fresh stores get the shape here.
CREATE VIRTUAL TABLE IF NOT EXISTS reviews_fts USING fts5(
    body,
    tokenize = 'porter unicode61',
    content='reviews',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, body, labels_text, paths_text)
    VALUES (new.id, new.title, new.body, new.labels_text, new.paths_text);
END;

CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body, labels_text, paths_text)
    VALUES ('delete', old.id, old.title, old.body, old.labels_text, old.paths_text);
END;

CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body, labels_text, paths_text)
    VALUES ('delete', old.id, old.title, old.body, old.labels_text, old.paths_text);
    INSERT INTO items_fts(rowid, title, body, labels_text, paths_text)
    VALUES (new.id, new.title, new.body, new.labels_text, new.paths_text);
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

CREATE TRIGGER IF NOT EXISTS reviews_ai AFTER INSERT ON reviews BEGIN
    INSERT INTO reviews_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TRIGGER IF NOT EXISTS reviews_ad AFTER DELETE ON reviews BEGIN
    INSERT INTO reviews_fts(reviews_fts, rowid, body)
    VALUES ('delete', old.id, old.body);
END;

CREATE TRIGGER IF NOT EXISTS reviews_au AFTER UPDATE ON reviews BEGIN
    INSERT INTO reviews_fts(reviews_fts, rowid, body)
    VALUES ('delete', old.id, old.body);
    INSERT INTO reviews_fts(rowid, body) VALUES (new.id, new.body);
END;

-- Evidence history (issue #25, migration v8): pre-images of displaced text.
-- Append-only at ingest — rows are never updated or deleted by the sync
-- path; doctor --consolidate replaces the whole corpus snapshot (the one
-- sanctioned rewrite, with fresh ids). `content` is exactly the displaced
-- stored value (NULL legal: the text was absent before). Windows are GitHub
-- times, never the sync wall clock; per-entity provenance in docs/schema.md.
CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    entity TEXT NOT NULL CHECK (entity IN ('item_title','item_body','comment','review')),
    entity_id INTEGER,
    kind TEXT,
    origin TEXT NOT NULL CHECK (origin IN ('edit','deleted')),
    content TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_revisions_item ON revisions(repo, number);

-- Text-derived references (issue #26, migration v9): raw path strings and
-- error signatures mentioned in item text. Paths are stored RAW and resolved
-- against the file vocabulary at READ time (issue #29) — resolving at ingest
-- would make rows depend on sync order. Replaced with the other item-scoped
-- children on every re-ingest; doctor --consolidate is the sanctioned rewrite.
CREATE TABLE IF NOT EXISTS item_path_refs (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    origin TEXT NOT NULL,
    path TEXT NOT NULL,
    backticked INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, number, origin, path)
);

CREATE INDEX IF NOT EXISTS idx_item_path_refs_path ON item_path_refs(path);

CREATE TABLE IF NOT EXISTS item_signatures (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    exception TEXT NOT NULL,
    error_line TEXT NOT NULL,
    frame_function TEXT,
    PRIMARY KEY (repo, number, exception, error_line)
);

-- GitHub-reported links, events and commit messages (issue #27, migration
-- v10). item_events rows are replaced only when the timeline section was
-- fetched complete; pr_commits rows only when the commits section was — a
-- degraded re-ingest touches neither. Reopen events never become edges; commit
-- messages derive closes_keyword edges, re-derived from the RETAINED rows.
CREATE TABLE IF NOT EXISTS item_events (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (repo, number, type, created_at)
);

CREATE TABLE IF NOT EXISTS pr_commits (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    sha TEXT NOT NULL,
    message TEXT,
    PRIMARY KEY (repo, number, sha)
);

CREATE INDEX IF NOT EXISTS idx_items_repo_head ON items(repo, head_ref)
    WHERE kind = 'pr' AND head_ref IS NOT NULL;
