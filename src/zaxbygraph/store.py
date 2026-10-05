from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone

from zaxbygraph.extract import (
    collapse_edges,
    edges_from_comment,
    edges_from_files,
    edges_from_item,
    edges_from_pr_state,
    edges_from_review,
    edges_from_timeline,
    item_kind,
    revert_title_variants,
)

ISO_Z = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime(ISO_Z)


def _dumps(raw: dict) -> str:
    return json.dumps(raw, separators=(",", ":"), ensure_ascii=False)


def _login(raw: dict) -> str | None:
    user = raw.get("user") or raw.get("author")
    if isinstance(user, dict):
        login = user.get("login")
        return str(login) if login else None
    return None


def _labels_text(raw: dict) -> str:
    names: list[str] = []
    for label in raw.get("labels") or []:
        if isinstance(label, dict) and label.get("name"):
            names.append(str(label["name"]))
        elif isinstance(label, str) and label:
            names.append(label)
    return " ".join(names)


def upsert_actor(conn: sqlite3.Connection, login: str | None, html_url: str | None = None) -> None:
    if not login:
        return
    url = html_url or f"https://github.com/{login}"
    conn.execute(
        "INSERT INTO actors(login, html_url) VALUES (?, ?) "
        "ON CONFLICT(login) DO UPDATE SET html_url = COALESCE(excluded.html_url, actors.html_url)",
        (login, url),
    )


def replace_item_children(conn: sqlite3.Connection, repo: str, number: int) -> None:
    conn.execute("DELETE FROM labels WHERE repo = ? AND number = ?", (repo, number))
    conn.execute("DELETE FROM comments WHERE repo = ? AND number = ?", (repo, number))
    conn.execute("DELETE FROM reviews WHERE repo = ? AND number = ?", (repo, number))
    conn.execute("DELETE FROM pr_files WHERE repo = ? AND number = ?", (repo, number))


def delete_owned_edges(conn: sqlite3.Connection, repo: str, number: int) -> None:
    """Delete the edges this item OWNS, keeping everything else.

    Outbound text/payload edges are rebuilt from this item's payload, so they
    go. The two GraphQL-only link streams (timeline events, closingIssue
    references) are exempt (`source NOT IN ('timeline', 'closing_ref')`): a
    degraded re-ingest (REST source, files-section-missing pull fallback, the
    404 empty-children path) cannot re-derive them, and a closer PR's payload
    can never re-derive an edge whose evidence lives on the closed item's
    timeline. They are append-only once written — refreshed by upsert when
    the source provides the section again, never retracted (issue #6; the
    `--force` trade-off is documented in docs/schema.md). Inbound
    mentions/closes from other items always survive."""
    nid = str(number)
    conn.execute(
        "DELETE FROM edges WHERE repo = ? AND src_type = 'item' AND src_id = ? "
        "AND source NOT IN ('timeline', 'closing_ref')",
        (repo, nid),
    )
    conn.execute(
        "DELETE FROM edges WHERE repo = ? AND dst_type = 'item' AND dst_id = ? "
        "AND rel IN ('authored', 'commented', 'reviewed')",
        (repo, nid),
    )


def insert_edge(conn: sqlite3.Connection, repo: str, edge: tuple) -> None:
    src_type, src_id, rel, dst_type, dst_id, evidence, source = edge
    conn.execute(
        "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id, confidence, evidence, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'EXTRACTED', ?, ?) "
        "ON CONFLICT(repo, src_type, src_id, rel, dst_type, dst_id, source) DO UPDATE SET "
        "evidence = excluded.evidence, confidence = 'EXTRACTED'",
        (repo, src_type, src_id, rel, dst_type, dst_id, evidence, source),
    )


def upsert_item_row(conn: sqlite3.Connection, repo: str, raw: dict, kind: str) -> None:
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    author = user.get("login") if user else None
    upsert_actor(conn, author, user.get("html_url") if user else None)
    base = raw.get("base") if isinstance(raw.get("base"), dict) else {}
    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
    conn.execute(
        """
        INSERT INTO items(
            id, repo, number, kind, node_id, title, body, labels_text, state, state_reason,
            author, created_at, updated_at, closed_at, merged_at, merge_commit, draft, locked,
            base_ref, head_ref, additions, deletions, changed_files, commits, html_url, api_url, raw_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            repo=excluded.repo, number=excluded.number, kind=excluded.kind, node_id=excluded.node_id,
            title=excluded.title, body=excluded.body, labels_text=excluded.labels_text,
            state=excluded.state, state_reason=excluded.state_reason, author=excluded.author,
            created_at=excluded.created_at, updated_at=excluded.updated_at, closed_at=excluded.closed_at,
            merged_at=excluded.merged_at, merge_commit=excluded.merge_commit, draft=excluded.draft,
            locked=excluded.locked, base_ref=excluded.base_ref, head_ref=excluded.head_ref,
            additions=excluded.additions, deletions=excluded.deletions,
            changed_files=excluded.changed_files, commits=excluded.commits,
            html_url=excluded.html_url, api_url=excluded.api_url, raw_json=excluded.raw_json
        ON CONFLICT(repo, number) DO UPDATE SET
            id=excluded.id, kind=excluded.kind, node_id=excluded.node_id,
            title=excluded.title, body=excluded.body, labels_text=excluded.labels_text,
            state=excluded.state, state_reason=excluded.state_reason, author=excluded.author,
            created_at=excluded.created_at, updated_at=excluded.updated_at, closed_at=excluded.closed_at,
            merged_at=excluded.merged_at, merge_commit=excluded.merge_commit, draft=excluded.draft,
            locked=excluded.locked, base_ref=excluded.base_ref, head_ref=excluded.head_ref,
            additions=excluded.additions, deletions=excluded.deletions,
            changed_files=excluded.changed_files, commits=excluded.commits,
            html_url=excluded.html_url, api_url=excluded.api_url, raw_json=excluded.raw_json
        """,
        (
            int(raw["id"]),
            repo,
            int(raw["number"]),
            kind,
            raw.get("node_id"),
            raw.get("title") or "",
            raw.get("body"),
            _labels_text(raw),
            raw.get("state") or "open",
            raw.get("state_reason"),
            author,
            raw.get("created_at"),
            raw.get("updated_at"),
            raw.get("closed_at"),
            raw.get("merged_at"),
            (raw.get("merge_commit_sha") or "").strip() or None,
            1 if raw.get("draft") else 0,
            1 if raw.get("locked") else 0,
            base.get("ref") if base else None,
            head.get("ref") if head else None,
            raw.get("additions"),
            raw.get("deletions"),
            raw.get("changed_files"),
            raw.get("commits"),
            raw.get("html_url"),
            raw.get("url"),
            _dumps(raw),
        ),
    )


def insert_labels(conn: sqlite3.Connection, repo: str, number: int, raw: dict) -> None:
    for label in raw.get("labels") or []:
        if isinstance(label, dict):
            name = label.get("name")
            color = label.get("color")
        elif isinstance(label, str):
            name, color = label, None
        else:
            continue
        if not name:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO labels(repo, number, name, color) VALUES (?, ?, ?, ?)",
            (repo, number, str(name), color),
        )


def insert_comments(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    comments: Iterable[dict],
    kind: str,
) -> None:
    for rec in comments:
        gid = rec.get("id")
        if gid is None:
            continue
        author = _login(rec)
        upsert_actor(
            conn,
            author,
            (rec.get("user") or {}).get("html_url") if isinstance(rec.get("user"), dict) else None,
        )
        conn.execute(
            """
            INSERT INTO comments(
                github_id, repo, number, kind, author, created_at, updated_at,
                body, html_url, in_reply_to, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo, kind, github_id) DO UPDATE SET
                number=excluded.number, author=excluded.author,
                created_at=excluded.created_at, updated_at=excluded.updated_at,
                body=excluded.body, html_url=excluded.html_url,
                in_reply_to=excluded.in_reply_to, raw_json=excluded.raw_json
            """,
            (
                int(gid),
                repo,
                number,
                kind,
                author,
                rec.get("created_at"),
                rec.get("updated_at"),
                rec.get("body"),
                rec.get("html_url"),
                rec.get("in_reply_to_id"),
                _dumps(rec),
            ),
        )


def insert_reviews(conn: sqlite3.Connection, repo: str, number: int, reviews: Iterable[dict]) -> None:
    for rec in reviews:
        rid = rec.get("id")
        if rid is None:
            continue
        author = _login(rec)
        upsert_actor(
            conn,
            author,
            (rec.get("user") or {}).get("html_url") if isinstance(rec.get("user"), dict) else None,
        )
        conn.execute(
            """
            INSERT INTO reviews(id, repo, number, author, state, submitted_at, body, html_url, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                repo=excluded.repo, number=excluded.number, author=excluded.author,
                state=excluded.state, submitted_at=excluded.submitted_at, body=excluded.body,
                html_url=excluded.html_url, raw_json=excluded.raw_json
            """,
            (
                int(rid),
                repo,
                number,
                author,
                rec.get("state"),
                rec.get("submitted_at"),
                rec.get("body"),
                rec.get("html_url"),
                _dumps(rec),
            ),
        )


def insert_pr_files(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    files: Iterable[dict],
    include_patches: bool,
) -> None:
    for rec in files:
        path = rec.get("filename")
        if not path:
            continue
        conn.execute(
            """
            INSERT INTO pr_files(repo, number, path, status, additions, deletions, changes, sha, patch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo, number, path) DO UPDATE SET
                status=excluded.status, additions=excluded.additions,
                deletions=excluded.deletions, changes=excluded.changes,
                sha=excluded.sha, patch=excluded.patch
            """,
            (
                repo,
                number,
                path,
                rec.get("status"),
                rec.get("additions"),
                rec.get("deletions"),
                rec.get("changes"),
                rec.get("sha"),
                rec.get("patch") if include_patches else None,
            ),
        )


def rebuild_edges(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    item_raw: dict,
    issue_comments: list[dict],
    review_comments: list[dict],
    reviews: list[dict],
    files: list[dict],
    timeline: list[dict] | None = None,
) -> None:
    delete_owned_edges(conn, repo, number)

    def merge_commit_lookup(sha: str) -> int | None:
        row = conn.execute(
            "SELECT number FROM items WHERE repo = ? AND merge_commit = ?", (repo, sha)
        ).fetchone()
        return None if row is None else int(row[0])

    def title_lookup(title: str) -> list[int]:
        rows = conn.execute(
            "SELECT number FROM items WHERE repo = ? AND title = ? AND number != ?",
            (repo, title, number),
        ).fetchall()
        return [int(r[0]) for r in rows]

    collected: list[tuple] = []
    collected.extend(edges_from_item(repo, item_raw))
    for rec in issue_comments:
        collected.extend(edges_from_comment(repo, number, rec))
    for rec in review_comments:
        collected.extend(edges_from_comment(repo, number, rec))
    for rec in reviews:
        collected.extend(edges_from_review(repo, number, rec))
    collected.extend(edges_from_files(number, files))
    collected.extend(edges_from_timeline(repo, number, timeline or [], merge_commit_lookup))
    collected.extend(edges_from_pr_state(repo, item_raw, title_lookup))
    # Symmetric merge-close derivation (issue #6): a PR merged as commit X
    # closes every issue whose timeline already recorded closed_by_commit X,
    # whichever item ingests first. Self-excluded like the forward lookup,
    # evidence copied so both orders upsert the identical 7-tuple.
    nid = str(number)
    merge_sha = item_raw.get("merge_commit_sha")
    if isinstance(merge_sha, str) and merge_sha.strip():
        nid = str(number)
        for row in conn.execute(
            "SELECT src_id, evidence FROM edges WHERE repo = ? AND rel = 'closed_by_commit' "
            "AND dst_type = 'commit' AND dst_id = ? AND src_id != ?",
            (repo, merge_sha.strip(), nid),
        ).fetchall():
            collected.append(
                ("item", nid, "closes", "item", str(row["src_id"]), row["evidence"], "timeline")
            )
    # Symmetric title-revert derivation: this item's stored title completes
    # any already-stored revert PR that quotes it, whichever ingests first
    # (same order-independence as the merge-close pass above).
    title = item_raw.get("title")
    if isinstance(title, str) and title:
        evidence = "title reverts quoted item title"
        for variant in revert_title_variants(title):
            for row in conn.execute(
                "SELECT number FROM items WHERE repo = ? AND title = ? AND number != ?",
                (repo, variant, number),
            ).fetchall():
                collected.append(
                    (
                        "item",
                        str(row["number"]),
                        "reverts",
                        "item",
                        nid,
                        evidence,
                        "keyword",
                    )
                )
    for edge in collapse_edges(collected):
        insert_edge(conn, repo, edge)


def bump_watermark(conn: sqlite3.Connection, repo: str, updated_at: str | None) -> None:
    row = conn.execute(
        "SELECT issues_since FROM sync_state WHERE repo = ?", (repo,)
    ).fetchone()
    current = row["issues_since"] if row else None
    nxt = current
    if updated_at and (current is None or updated_at > current):
        nxt = updated_at
    conn.execute(
        """
        INSERT INTO sync_state(repo, issues_since, last_error)
        VALUES (?, ?, NULL)
        ON CONFLICT(repo) DO UPDATE SET
            issues_since = excluded.issues_since,
            last_error = NULL
        """,
        (repo, nxt),
    )


def recount(conn: sqlite3.Connection, repo: str) -> None:
    items = conn.execute("SELECT COUNT(*) AS c FROM items WHERE repo = ?", (repo,)).fetchone()["c"]
    comments = conn.execute(
        "SELECT COUNT(*) AS c FROM comments WHERE repo = ?", (repo,)
    ).fetchone()["c"]
    edges = conn.execute("SELECT COUNT(*) AS c FROM edges WHERE repo = ?", (repo,)).fetchone()["c"]
    conn.execute(
        """
        INSERT INTO sync_state(repo, item_count, comment_count, edge_count)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(repo) DO UPDATE SET
            item_count = excluded.item_count,
            comment_count = excluded.comment_count,
            edge_count = excluded.edge_count
        """,
        (repo, items, comments, edges),
    )


def set_last_error(conn: sqlite3.Connection, repo: str, message: str) -> None:
    conn.execute(
        """
        INSERT INTO sync_state(repo, last_error)
        VALUES (?, ?)
        ON CONFLICT(repo) DO UPDATE SET last_error = excluded.last_error
        """,
        (repo, message),
    )


def mark_sync_finished(
    conn: sqlite3.Connection,
    repo: str,
    *,
    rate_limit_remaining: int | None = None,
    rate_limit_reset_at: str | None = None,
) -> None:
    """Stamp a clean finish.

    When a full sync is pending — this run started one, or it resumed an
    interrupted one — completing cleanly IS the completion of that full sync:
    stamp `last_full_sync_at` and clear the marker. Otherwise this was a plain
    incremental run. Truthful because each item commits with its own watermark
    bump in one transaction, so a run that drains the listing has covered
    everything at or below the watermark.

    The rate-limit columns carry the window this run slept through (NULL when
    it saw none), verbatim as the source reported it (issue #5 AC4).
    """
    now = utcnow()
    conn.execute("INSERT OR IGNORE INTO sync_state(repo) VALUES (?)", (repo,))
    conn.execute(
        """
        UPDATE sync_state SET
            last_full_sync_at = CASE WHEN full_sync_pending = 1
                                THEN :now ELSE last_full_sync_at END,
            last_incr_sync_at = CASE WHEN full_sync_pending = 0
                                THEN :now ELSE last_incr_sync_at END,
            full_sync_pending = 0,
            last_error = NULL,
            rate_limit_remaining = :rate_remaining,
            rate_limit_reset_at = :rate_reset_at
        WHERE repo = :repo
        """,
        {
            "now": now,
            "repo": repo,
            "rate_remaining": rate_limit_remaining,
            "rate_reset_at": rate_limit_reset_at,
        },
    )


def log_fetch(
    conn: sqlite3.Connection,
    repo: str,
    resource: str,
    resource_id: str | None,
    note: str | None = None,
    status: int | None = 200,
) -> None:
    conn.execute(
        "INSERT INTO fetch_log(repo, resource, resource_id, fetched_at, http_status, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (repo, resource, resource_id, utcnow(), status, note),
    )


def replace_releases(conn: sqlite3.Connection, repo: str, releases: list[dict]) -> None:
    conn.execute("DELETE FROM releases WHERE repo = ?", (repo,))
    for rec in releases:
        rid = rec.get("id")
        tag = rec.get("tag_name")
        if rid is None or not tag:
            continue
        author = _login(rec)
        upsert_actor(conn, author)
        conn.execute(
            """
            INSERT INTO releases(
                id, repo, tag_name, name, body, draft, prerelease, author,
                created_at, published_at, html_url, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(rid),
                repo,
                tag,
                rec.get("name"),
                rec.get("body"),
                1 if rec.get("draft") else 0,
                1 if rec.get("prerelease") else 0,
                author,
                rec.get("created_at"),
                rec.get("published_at"),
                rec.get("html_url"),
                _dumps(rec),
            ),
        )


def ingest_item(
    conn: sqlite3.Connection,
    repo: str,
    list_raw: dict,
    *,
    pull_raw: dict | None,
    issue_comments: list[dict],
    review_comments: list[dict],
    reviews: list[dict],
    files: list[dict],
    include_patches: bool,
    timeline: list[dict] | None = None,
) -> None:
    """One item, caller owns the transaction.

    Identity and watermark come from the *issues list* payload. GET /pulls/{n}
    uses a different `id` and may have a different `updated_at`; those must
    not overwrite the list row (since filter is on issue updated_at).
    `timeline` carries the item's GraphQL timeline events (closed /
    cross-referenced); None means the source could not provide them, which
    leaves previously stored timeline edges untouched via the delete-owned
    exemption.
    """
    merged = dict(list_raw)
    if pull_raw:
        list_keys = {
            "id",
            "number",
            "node_id",
            "title",
            "body",
            "labels",
            "state",
            "state_reason",
            "user",
            "created_at",
            "updated_at",
            "closed_at",
            "html_url",
            "url",
            "comments",
            "locked",
            "pull_request",
        }
        for key, value in pull_raw.items():
            if key not in list_keys:
                merged[key] = value
    kind = item_kind(merged)
    number = int(merged["number"])
    replace_item_children(conn, repo, number)
    upsert_item_row(conn, repo, merged, kind)
    insert_labels(conn, repo, number, merged)
    insert_comments(conn, repo, number, issue_comments, "issue_comment")
    insert_comments(conn, repo, number, review_comments, "review_comment")
    insert_reviews(conn, repo, number, reviews)
    insert_pr_files(conn, repo, number, files, include_patches)
    rebuild_edges(
        conn,
        repo,
        number,
        merged,
        issue_comments,
        review_comments,
        reviews,
        files,
        timeline=timeline,
    )
    bump_watermark(conn, repo, list_raw.get("updated_at"))
    recount(conn, repo)
    log_fetch(conn, repo, "item", str(number))
