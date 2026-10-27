from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone

from zaxbygraph.extract import (
    ABSENT,
    CLOSES_REF_MARKER,
    EVIDENCE_CAP,
    PULL_OWNED_EXTRACTIONS,
    branch_number_tokens,
    collapse_edges,
    edges_from_comment,
    edges_from_commit_messages,
    edges_from_files,
    edges_from_item,
    edges_from_links,
    edges_from_pr_state,
    edges_from_review,
    edges_from_timeline,
    extract_listing_signals,
    extract_pull_owned,
    item_kind,
    merge_absent,
    merge_evidence,
    normalize_incomplete_connections,
    parse_error_signatures,
    parse_path_refs,
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
    conn.execute("DELETE FROM assignees WHERE repo = ? AND number = ?", (repo, number))
    conn.execute(
        "DELETE FROM issue_field_values WHERE repo = ? AND number = ?", (repo, number)
    )
    # v9 text-derived references (issue #26): derived from this item's text
    # payload, so they replace with the other children on every re-ingest.
    conn.execute(
        "DELETE FROM item_path_refs WHERE repo = ? AND number = ?", (repo, number)
    )
    conn.execute(
        "DELETE FROM item_signatures WHERE repo = ? AND number = ?", (repo, number)
    )
    # item_events and pr_commits are deliberately NOT here: they replace only
    # when their GitHub-reported section was fetched complete (issue #27), so
    # a degraded re-ingest must leave the stored rows alone.


def snapshot_comment_associations(
    conn: sqlite3.Connection, repo: str, number: int
) -> tuple[dict, dict]:
    """Per-kind `{github_id: author_association}` maps for this item's stored
    comments, taken BEFORE replace_item_children deletes them. The two comment
    kinds have separate ID spaces (UNIQUE(repo, kind, github_id)), so one map
    per kind; a probe-degraded re-ingest resolves absent payload keys against
    the map instead of losing the REST-native value."""
    maps: tuple[dict, dict] = ({}, {})
    for index, kind in enumerate(("issue_comment", "review_comment")):
        for row in conn.execute(
            "SELECT github_id, author_association FROM comments"
            " WHERE repo = ? AND number = ? AND kind = ?"
            " AND author_association IS NOT NULL",
            (repo, number, kind),
        ):
            maps[index][row["github_id"]] = row["author_association"]
    return maps


def snapshot_revision_state(
    conn: sqlite3.Connection, repo: str, number: int, incoming_id: int
) -> dict:
    """Pre-replace state for revision pre-images (issue #25): the stored item
    text row and canonical child maps, read BEFORE replace_item_children
    deletes them. The item row resolves through the same two identities
    upsert_item_row will overwrite — (repo, number) first, then the incoming
    GitHub id — so an ID-conflict update cannot displace text without a
    pre-image. Child maps key by identity, last-writer-wins in row order
    (rows are already unique per identity; canonicalization matters for the
    incoming side)."""
    item = conn.execute(
        "SELECT title, body, created_at, updated_at FROM items"
        " WHERE repo = ? AND number = ?",
        (repo, number),
    ).fetchone()
    if item is None:
        item = conn.execute(
            "SELECT title, body, created_at, updated_at FROM items WHERE id = ?",
            (incoming_id,),
        ).fetchone()
    comments: dict = {}
    for row in conn.execute(
        "SELECT kind, github_id, body, created_at, updated_at FROM comments"
        " WHERE repo = ? AND number = ?",
        (repo, number),
    ):
        comments[(row["kind"], row["github_id"])] = (
            row["body"],
            row["created_at"],
            row["updated_at"],
        )
    reviews: dict = {}
    for row in conn.execute(
        "SELECT id, body, submitted_at FROM reviews WHERE repo = ? AND number = ?",
        (repo, number),
    ):
        reviews[row["id"]] = (row["body"], row["submitted_at"])
    return {"item": item, "comments": comments, "reviews": reviews}


def _insert_revision(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    entity: str,
    entity_id: int | None,
    kind: str | None,
    origin: str,
    content: str | None,
    valid_from: str,
    valid_to: str | None,
) -> None:
    """One append-only pre-image row (issue #25). Never updated or deleted by
    the ingest path; consolidation is the sanctioned rewrite."""
    conn.execute(
        "INSERT INTO revisions(repo, number, entity, entity_id, kind, origin,"
        " content, valid_from, valid_to)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (repo, number, entity, entity_id, kind, origin, content, valid_from, valid_to),
    )


def _canonical_comments(comments: list[dict]) -> dict:
    """`{github_id: rec}` last-writer-wins, so duplicate pagination rows
    collapse to the final state before any diff (issue #25)."""
    canonical: dict = {}
    for rec in comments:
        if not isinstance(rec, dict):
            continue
        gid = rec.get("id")
        if gid is None:
            continue
        canonical[int(gid)] = rec
    return canonical


def write_revisions(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    snap: dict,
    merged: dict,
    issue_comments: list[dict],
    review_comments: list[dict],
    reviews: list[dict],
    children_complete: dict[str, bool] | None,
) -> None:
    """Pre-images of the text this ingest displaces (issue #25). Runs inside
    ingest_item's caller-owned transaction, after upsert_item_row.

    Rules: a row is written only when the stored value and incoming value
    differ (exact NULL-aware string inequality — the issue's "hash differs"
    predicate; no content key exists, so A→B→A cannot wedge). Any changed
    stored value writes a pre-image, including to/from NULL and ""/NULL;
    content is exactly the displaced value. First ingest writes nothing.
    Tombstones (origin='deleted') fire only when the section's
    `children_complete` flag IS True (identity check, not truthiness) using
    the production section keys. Windows are GitHub times: items/comments
    from updated_at else created_at; reviews from submitted_at, else the
    stored item's created_at when NULL; valid_to is the incoming item's
    updated_at except for comment edits, which prefer the comment's own
    incoming updated_at.
    """
    prior_item = snap["item"]
    incoming_updated = merged.get("updated_at")
    rows: list[tuple] = []
    if prior_item is not None:
        item_valid_from = prior_item["updated_at"] or prior_item["created_at"]
        for entity, stored in (
            ("item_title", prior_item["title"]),
            ("item_body", prior_item["body"]),
        ):
            # Title is stored coerced (`raw.get("title") or ""`), so the
            # comparison coerces too — a payload without a title key is
            # unchanged, not an endless "" -> None edit (issue #25 AC3).
            incoming = (
                (merged.get("title") or "")
                if entity == "item_title"
                else merged.get("body")
            )
            if stored != incoming:
                rows.append(
                    (entity, None, None, "edit", stored, item_valid_from, incoming_updated)
                )
    incoming_issue = _canonical_comments(issue_comments)
    incoming_review_comments = _canonical_comments(review_comments)
    for kind_name, incoming in (
        ("issue_comment", incoming_issue),
        ("review_comment", incoming_review_comments),
    ):
        for gid in sorted(incoming):
            rec = incoming[gid]
            prior = snap["comments"].get((kind_name, gid))
            if prior is None:
                continue
            stored_body = prior[0]
            incoming_body = rec.get("body")
            if stored_body == incoming_body:
                continue
            valid_from = prior[2] or prior[1]
            valid_to = rec.get("updated_at") or incoming_updated
            rows.append(
                ("comment", gid, kind_name, "edit", stored_body, valid_from, valid_to)
            )
    incoming_reviews = {}
    for rec in reviews:
        if not isinstance(rec, dict):
            continue
        rid = rec.get("id")
        if rid is None:
            continue
        incoming_reviews[int(rid)] = rec
    for rid in sorted(incoming_reviews):
        rec = incoming_reviews[rid]
        prior = snap["reviews"].get(rid)
        if prior is None:
            continue
        stored_body = prior[0]
        if stored_body == rec.get("body"):
            continue
        item_created = prior_item["created_at"] if prior_item is not None else None
        valid_from = prior[1] or item_created
        rows.append(
            ("review", rid, None, "edit", stored_body, valid_from, incoming_updated)
        )
    complete = children_complete if isinstance(children_complete, dict) else {}
    for kind_name, section, incoming in (
        ("issue_comment", "issue_comments", incoming_issue),
        ("review_comment", "review_comments", incoming_review_comments),
    ):
        if complete.get(section) is not True:
            continue
        for (snap_kind, gid) in sorted(snap["comments"]):
            if snap_kind != kind_name or gid in incoming:
                continue
            body, created_at, updated_at = snap["comments"][(snap_kind, gid)]
            rows.append(
                ("comment", gid, kind_name, "deleted", body,
                 updated_at or created_at, incoming_updated)
            )
    if complete.get("reviews") is True:
        for rid in sorted(snap["reviews"]):
            if rid in incoming_reviews:
                continue
            body, submitted_at = snap["reviews"][rid]
            item_created = prior_item["created_at"] if prior_item is not None else None
            rows.append(
                ("review", rid, None, "deleted", body,
                 submitted_at or item_created, incoming_updated)
            )
    for entity, entity_id, kind, origin, content, valid_from, valid_to in rows:
        # A displacement whose GitHub-time window cannot be established from
        # the payload (NULL stamps on both sides) is not recorded: the
        # window columns are NOT NULL and an unusable or inverted window
        # would either abort the ingest or store an unqueryable row
        # (issue #25 review PRR-003).
        if valid_from is None or valid_to is None:
            continue
        _insert_revision(
            conn, repo, number, entity, entity_id, kind, origin, content,
            valid_from, valid_to,
        )


def write_text_refs(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    body: str | None,
    issue_comments: list[dict],
    review_comments: list[dict],
    reviews: list[dict],
) -> None:
    """Raw path refs and error signatures from this item's text (issue #26).
    Runs inside ingest_item's transaction after replace_item_children emptied
    the item's rows; every row derives from the text in hand with its origin
    ('body', 'issue_comment:<id>', 'review_comment:<id>', 'review:<id>')."""
    families: list[tuple[str, str | None]] = [("body", body)]
    for rec in issue_comments:
        gid = rec.get("id") or rec.get("github_id")
        if gid is not None:
            families.append((f"issue_comment:{gid}", rec.get("body")))
    for rec in review_comments:
        gid = rec.get("id") or rec.get("github_id")
        if gid is not None:
            families.append((f"review_comment:{gid}", rec.get("body")))
    for rec in reviews:
        rid = rec.get("id")
        if rid is not None:
            families.append((f"review:{rid}", rec.get("body")))
    for origin, text in families:
        for path, backticked in parse_path_refs(text, repo):
            conn.execute(
                "INSERT INTO item_path_refs(repo, number, origin, path, backticked)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(repo, number, origin, path) DO UPDATE SET"
                " backticked = MAX(item_path_refs.backticked, excluded.backticked)",
                (repo, number, origin, path, backticked),
            )
        for exception, error_line, frame_function in parse_error_signatures(text):
            conn.execute(
                "INSERT OR IGNORE INTO item_signatures(repo, number, exception,"
                " error_line, frame_function) VALUES (?, ?, ?, ?, ?)",
                (repo, number, exception, error_line, frame_function),
            )


def write_item_events(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    timeline: list[dict] | None,
    timeline_complete: bool,
) -> None:
    """Reopen events become item_events rows (issue #27 AC7). Fires only when
    the timeline section was PRESENT (None = never carried: plain REST, the
    404 path — touching nothing is the degraded rule); rows replace ONLY when
    the section was complete, so a truncated timeline never retracts stored
    events."""
    if timeline is None or not timeline_complete:
        return
    conn.execute(
        "DELETE FROM item_events WHERE repo = ? AND number = ?", (repo, number)
    )
    for event in timeline:
        if not isinstance(event, dict):
            continue
        if event.get("type") != "reopened":
            continue
        created_at = event.get("created_at")
        if not created_at:
            # The PK is NOT NULL on created_at (round-2 critic): a malformed
            # event is skipped, never stored with a fabricated stamp.
            continue
        conn.execute(
            "INSERT OR IGNORE INTO item_events(repo, number, type, created_at)"
            " VALUES (?, ?, 'reopened', ?)",
            (repo, number, created_at),
        )


def insert_pr_commits(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    commits: list[dict] | None,
    commits_complete: bool,
) -> None:
    """Commit messages become pr_commits rows (issue #27 AC5), replaced only
    when the commits section was fetched complete. Derived closes_keyword
    edges re-derive from these RETAINED rows on every ingest, so a degraded
    re-ingest never retracts them."""
    if commits is None or not commits_complete:
        return
    conn.execute(
        "DELETE FROM pr_commits WHERE repo = ? AND number = ?", (repo, number)
    )
    for rec in commits:
        if not isinstance(rec, dict):
            continue
        sha = rec.get("sha")
        if not isinstance(sha, str) or not sha.strip():
            continue
        message = rec.get("message")
        conn.execute(
            "INSERT OR IGNORE INTO pr_commits(repo, number, sha, message)"
            " VALUES (?, ?, ?, ?)",
            (repo, number, sha.strip(), message),
        )


#: The links-section connection keys and the edge relation each replaces.
_LINK_RELATIONS = {
    "blocked_by": "blocked_by",
    "parent": "child_of",
    "duplicate_of": "duplicate_of",
    "open_prs": "linked_pr",
}


def rebuild_links(
    conn: sqlite3.Connection, repo: str, number: int, links: dict | None,
    links_incomplete: object,
) -> None:
    """GitHub-reported link edges (issue #27): per connection, a COMPLETE
    fetch replaces this item's edges of that relation exactly; an OVERFLOWED
    connection (named in links_incomplete) unions without retraction; an
    ABSENT section (links is None, or the key missing from the dict) touches
    nothing — a degraded re-ingest can never retract a GitHub-reported link
    fact."""
    if links is None or not isinstance(links, dict):
        return
    incomplete = normalize_incomplete_connections(links_incomplete)
    emitted = edges_from_links(repo, number, links)
    for connection, relation in _LINK_RELATIONS.items():
        if connection not in links:
            continue
        if connection not in incomplete:
            conn.execute(
                "DELETE FROM edges WHERE repo = ? AND src_type = 'item'"
                " AND src_id = ? AND rel = ? AND source = 'link'",
                (repo, str(number), relation),
            )
    for edge in collapse_edges(emitted):
        insert_edge(conn, repo, edge)


def delete_owned_edges(conn: sqlite3.Connection, repo: str, number: int) -> None:
    """Delete the edges this item OWNS, keeping everything else.

    Outbound text/payload edges are rebuilt from this item's payload, so they
    go. The three GraphQL-only link streams (timeline events, closingIssue
    references, and the v10 GitHub-reported links) are exempt
    (`source NOT IN ('timeline', 'closing_ref', 'link')`): a degraded
    re-ingest (REST source, files-section-missing pull fallback, the 404
    empty-children path) cannot re-derive them, and a closer PR's payload can
    never re-derive an edge whose evidence lives on the closed item's
    timeline. They are append-only once written — refreshed by upsert when
    the source provides the section again, never retracted (issue #6; the
    `--force` trade-off is documented in docs/schema.md). Inbound
    mentions/closes from other items always survive."""
    nid = str(number)
    conn.execute(
        "DELETE FROM edges WHERE repo = ? AND src_type = 'item' AND src_id = ? "
        "AND source NOT IN ('timeline', 'closing_ref', 'link')",
        (repo, nid),
    )
    # INDEXED BY is a plan hint, not a predicate change: the rel IN (...) term
    # otherwise flips the planner to the repo-only unique-index scan, making
    # every item ingest visit the repo's whole edge set. The hint keeps the
    # (repo, dst_type, dst_id) seek on idx_edges_dst; the deleted row set is
    # identical either way.
    conn.execute(
        "DELETE FROM edges INDEXED BY idx_edges_dst "
        "WHERE repo = ? AND dst_type = 'item' AND dst_id = ? "
        "AND rel IN ('authored', 'commented', 'reviewed')",
        (repo, nid),
    )


def insert_edge(conn: sqlite3.Connection, repo: str, edge: tuple) -> None:
    src_type, src_id, rel, dst_type, dst_id, evidence, source = edge
    if rel == "closes":
        # One closes row per (repo, dst, src) pair (issue #19). The two
        # GitHub-reported streams — closingIssuesReferences (closing_ref)
        # and the timeline closed event (timeline) — corroborate ONE
        # closure fact, so they merge instead of each inserting. A
        # same-source refresh also unions evidence: re-ingesting one
        # stream must never drop the provenance the other stream merged
        # in. Evidence stays a payload (AGENTS): the surviving row carries
        # both strings, and the reserve keeps each stream's marker intact
        # even at the cap. The first insert is bounded by the same cap so
        # an oversized payload can never poison later merges into NULL.
        if evidence:
            evidence = merge_evidence(
                evidence, reserve=(CLOSES_REF_MARKER,)
            )
        row = conn.execute(
            "SELECT id, source, evidence FROM edges "
            "WHERE repo = ? AND src_type = ? AND src_id = ? AND rel = 'closes' "
            "AND dst_type = ? AND dst_id = ? LIMIT 1",
            (repo, src_type, src_id, dst_type, dst_id),
        ).fetchone()
        if row is not None:
            merged = merge_evidence(
                row["evidence"], evidence, reserve=(CLOSES_REF_MARKER,)
            )
            if row["source"] == source:
                conn.execute(
                    "UPDATE edges SET evidence = ? WHERE id = ?",
                    (merged, row["id"]),
                )
            else:
                survivor = (
                    "closing_ref"
                    if "closing_ref" in (source, row["source"])
                    else row["source"]
                )
                conn.execute(
                    "UPDATE edges SET evidence = ?, source = ? WHERE id = ?",
                    (merged, survivor, row["id"]),
                )
            return
    conn.execute(
        "INSERT INTO edges(repo, src_type, src_id, rel, dst_type, dst_id, confidence, evidence, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'EXTRACTED', ?, ?) "
        "ON CONFLICT(repo, src_type, src_id, rel, dst_type, dst_id, source) DO UPDATE SET "
        "evidence = excluded.evidence, confidence = 'EXTRACTED'",
        (repo, src_type, src_id, rel, dst_type, dst_id, evidence, source),
    )


#: The v7 payload-sourced columns written from source-owned extraction. Both
#: ON CONFLICT arms receive the same Python-merged values, so these stay
#: plain assignments (the ABSENT/present-null resolution happened above them,
#: against the stored row).
_V7_MERGEABLE_COLUMNS = (
    "reactions_total",
    "milestone",
    "item_type",
    "sub_issues_total",
    "sub_issues_completed",
    "dependencies_blocked_by",
    "dependencies_blocking",
    "dependencies_total_blocked_by",
    "dependencies_total_blocking",
    "author_association",
    "merged_by",
    "merge_commit",
    "merged_at",
)


def upsert_item_row(
    conn: sqlite3.Connection,
    repo: str,
    raw: dict,
    kind: str,
    *,
    list_raw: dict | None = None,
    pull_raw: dict | None = None,
) -> None:
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    author = user.get("login") if user else None
    upsert_actor(conn, author, user.get("html_url") if user else None)
    base = raw.get("base") if isinstance(raw.get("base"), dict) else {}
    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
    # Source-owned extraction (issue #24): listing signals come from the
    # listing payload only, the pull trio from the pull payload ONLY — a
    # pull_raw of None means "no pull section this ingest", which must
    # preserve the stored trio rather than re-read it from the listing, so
    # even a listing payload carrying pull-named keys can never overwrite
    # (or, via ABSENT-merge, null) the pull source's values. A key present
    # even with JSON null clears the column; an absent key preserves the
    # stored value; first sight stores None.
    listing_source = raw if list_raw is None else list_raw
    extracted = extract_listing_signals(listing_source)
    if pull_raw is None:
        extracted.update(
            {column: ABSENT for column, _, _ in PULL_OWNED_EXTRACTIONS}
        )
    else:
        extracted.update(extract_pull_owned(pull_raw))
    existing = conn.execute(
        "SELECT {} FROM items WHERE repo = ? AND number = ?".format(
            ", ".join(_V7_MERGEABLE_COLUMNS)
        ),
        (repo, int(raw["number"])),
    ).fetchone()
    if existing is None:
        existing = conn.execute(
            "SELECT {} FROM items WHERE id = ?".format(
                ", ".join(_V7_MERGEABLE_COLUMNS)
            ),
            (int(raw["id"]),),
        ).fetchone()
    v7_values = merge_absent(
        extracted, dict(zip(_V7_MERGEABLE_COLUMNS, tuple(existing))) if existing else {}
    )
    conn.execute(
        """
        INSERT INTO items(
            id, repo, number, kind, node_id, title, body, labels_text, state, state_reason,
            author, created_at, updated_at, closed_at, merged_at, merge_commit, draft, locked,
            base_ref, head_ref, additions, deletions, changed_files, commits, html_url, api_url,
            reactions_total, milestone, item_type, sub_issues_total, sub_issues_completed,
            dependencies_blocked_by, dependencies_blocking, dependencies_total_blocked_by,
            dependencies_total_blocking, author_association, merged_by, raw_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            repo=excluded.repo, number=excluded.number, kind=excluded.kind, node_id=excluded.node_id,
            title=excluded.title, body=excluded.body, labels_text=excluded.labels_text,
            state=excluded.state, state_reason=excluded.state_reason, author=excluded.author,
            created_at=excluded.created_at, updated_at=excluded.updated_at, closed_at=excluded.closed_at,
            merged_at=excluded.merged_at, merge_commit=excluded.merge_commit, draft=excluded.draft,
            locked=excluded.locked, base_ref=excluded.base_ref, head_ref=excluded.head_ref,
            additions=excluded.additions, deletions=excluded.deletions,
            changed_files=excluded.changed_files, commits=excluded.commits,
            html_url=excluded.html_url, api_url=excluded.api_url,
            reactions_total=excluded.reactions_total, milestone=excluded.milestone,
            item_type=excluded.item_type, sub_issues_total=excluded.sub_issues_total,
            sub_issues_completed=excluded.sub_issues_completed,
            dependencies_blocked_by=excluded.dependencies_blocked_by,
            dependencies_blocking=excluded.dependencies_blocking,
            dependencies_total_blocked_by=excluded.dependencies_total_blocked_by,
            dependencies_total_blocking=excluded.dependencies_total_blocking,
            author_association=excluded.author_association, merged_by=excluded.merged_by,
            raw_json=excluded.raw_json
        ON CONFLICT(repo, number) DO UPDATE SET
            id=excluded.id, kind=excluded.kind, node_id=excluded.node_id,
            title=excluded.title, body=excluded.body, labels_text=excluded.labels_text,
            state=excluded.state, state_reason=excluded.state_reason, author=excluded.author,
            created_at=excluded.created_at, updated_at=excluded.updated_at, closed_at=excluded.closed_at,
            merged_at=excluded.merged_at, merge_commit=excluded.merge_commit, draft=excluded.draft,
            locked=excluded.locked, base_ref=excluded.base_ref, head_ref=excluded.head_ref,
            additions=excluded.additions, deletions=excluded.deletions,
            changed_files=excluded.changed_files, commits=excluded.commits,
            html_url=excluded.html_url, api_url=excluded.api_url,
            reactions_total=excluded.reactions_total, milestone=excluded.milestone,
            item_type=excluded.item_type, sub_issues_total=excluded.sub_issues_total,
            sub_issues_completed=excluded.sub_issues_completed,
            dependencies_blocked_by=excluded.dependencies_blocked_by,
            dependencies_blocking=excluded.dependencies_blocking,
            dependencies_total_blocked_by=excluded.dependencies_total_blocked_by,
            dependencies_total_blocking=excluded.dependencies_total_blocking,
            author_association=excluded.author_association, merged_by=excluded.merged_by,
            raw_json=excluded.raw_json
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
            v7_values["merged_at"],
            v7_values["merge_commit"],
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
            v7_values["reactions_total"],
            v7_values["milestone"],
            v7_values["item_type"],
            v7_values["sub_issues_total"],
            v7_values["sub_issues_completed"],
            v7_values["dependencies_blocked_by"],
            v7_values["dependencies_blocking"],
            v7_values["dependencies_total_blocked_by"],
            v7_values["dependencies_total_blocking"],
            v7_values["author_association"],
            v7_values["merged_by"],
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
    associations: dict | None = None,
) -> None:
    """Insert one comment kind. `associations` maps github_id -> a stored
    author_association from the pre-replacement snapshot; a payload that
    carries the key wins (even explicit null), an absent key falls back to
    the snapshot so a probe-degraded re-ingest never loses the REST-native
    value (issue #24 AC7 + critic round 1 blocker 3)."""
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
        if "author_association" in rec:
            association = rec.get("author_association")
        else:
            association = (associations or {}).get(int(gid))
        conn.execute(
            """
            INSERT INTO comments(
                github_id, repo, number, kind, author, author_association,
                created_at, updated_at, body, html_url, in_reply_to, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo, kind, github_id) DO UPDATE SET
                number=excluded.number, author=excluded.author,
                author_association=excluded.author_association,
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
                association,
                rec.get("created_at"),
                rec.get("updated_at"),
                rec.get("body"),
                rec.get("html_url"),
                rec.get("in_reply_to_id"),
                _dumps(rec),
            ),
        )


def insert_assignees(
    conn: sqlite3.Connection, repo: str, number: int, raw: dict
) -> None:
    """Listing-payload assignees (issue #24 AC1); PK-dedup keeps duplicate
    payload entries idempotent."""
    for assignee in raw.get("assignees") or []:
        login = assignee.get("login") if isinstance(assignee, dict) else assignee
        if not login:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO assignees(repo, number, login) VALUES (?, ?, ?)",
            (repo, number, str(login)),
        )


def insert_issue_field_values(
    conn: sqlite3.Connection, repo: str, number: int, raw: dict
) -> None:
    """Listing-payload custom issue field values (issue #24 AC1); the item
    PK makes duplicate payload entries idempotent."""
    for field in raw.get("issue_field_values") or []:
        if not isinstance(field, dict) or field.get("name") is None:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO issue_field_values"
            "(repo, number, field_name, value) VALUES (?, ?, ?, ?)",
            (
                repo,
                number,
                str(field["name"]),
                None if field.get("value") is None else str(field["value"]),
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
    links: dict | None = None,
    links_incomplete: object = None,
) -> None:
    delete_owned_edges(conn, repo, number)
    # GitHub-reported links replace/union per connection BEFORE the keyword
    # edges insert (issue #27): the link stream's own exemption keeps a
    # degraded re-ingest from retracting them.
    rebuild_links(conn, repo, number, links, links_incomplete)

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

    def issue_number_lookup(numbers: list[int]) -> set[int]:
        placeholders = ",".join("?" * len(numbers))
        rows = conn.execute(
            "SELECT number FROM items WHERE repo = ? AND kind = 'issue'"
            f" AND number IN ({placeholders})",
            (repo, *numbers),
        ).fetchall()
        return {int(r[0]) for r in rows}

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
    collected.extend(edges_from_pr_state(repo, item_raw, title_lookup, issue_number_lookup))
    # Commit-message closes_keyword edges derive from the RETAINED pr_commits
    # rows, never from the payload in hand (issue #27): a REST re-ingest or an
    # overflowed commits fetch re-derives IDENTICAL edges from the stored rows,
    # so degradation can never retract them.
    commit_rows = conn.execute(
        "SELECT sha, message FROM pr_commits WHERE repo = ? AND number = ?",
        (repo, number),
    ).fetchall()
    if commit_rows:
        collected.extend(
            edges_from_commit_messages(
                repo,
                number,
                [{"sha": row["sha"], "message": row["message"]} for row in commit_rows],
            )
        )
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
    # Reverse branch reconciliation (issue #26 AC4, INSERT-ONLY): when THIS
    # item is an issue, any stored PR whose head_ref carries exactly the
    # number token N gains its branch_ref edge now, whichever ingests first.
    # No edge owned by another item is ever deleted here (plan-critic round 2);
    # a single token can never become ambiguous, so there is no retraction
    # path at all.
    if not (
        item_raw.get("pull_request") is not None
        or item_raw.get("merged_at") is not None
        or (isinstance(item_raw.get("base"), dict) and isinstance(item_raw.get("head"), dict))
    ):
        for row in conn.execute(
            "SELECT number, head_ref FROM items INDEXED BY idx_items_repo_head"
            " WHERE repo = ? AND kind = 'pr' AND head_ref IS NOT NULL AND number != ?",
            (repo, nid),
        ).fetchall():
            tokens = branch_number_tokens(row["head_ref"])
            if len(tokens) == 1 and tokens[0] == number:
                collected.append(
                    (
                        "item",
                        str(row["number"]),
                        "branch_ref",
                        "item",
                        nid,
                        f"head_ref {row['head_ref']}",
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
    targeted: bool = False,
    ingested_count: int = 0,
) -> None:
    """Stamp a clean finish.

    When a full sync is pending — this run started one, or it resumed an
    interrupted one — completing cleanly IS the completion of that full sync:
    stamp `last_full_sync_at` and clear the marker. Otherwise this was a plain
    incremental run. Truthful because each item commits with its own watermark
    bump in one transaction, so a run that drains the listing has covered
    everything at or below the watermark.

    With `targeted=True` (sync --numbers, issue #23) this is a TARGETED
    finish: only `last_error` clears and the rate columns update. Neither
    freshness stamp moves — `last_incr_sync_at` means a drained incremental
    listing whose coverage is bounded by the watermark, which a targeted
    refresh is not — and `full_sync_pending` is preserved so a never- or
    interrupted-full corpus still reads incomplete. A targeted run that
    ingested NOTHING (ingested_count == 0) leaves a previous `last_error`
    standing: a no-op refresh must not launder a failed sync's error into a
    clean readiness verdict (PR #37 review).

    The rate-limit columns carry the window this run slept through (NULL when
    it saw none), verbatim as the source reported it (issue #5 AC4).
    """
    now = utcnow()
    conn.execute("INSERT OR IGNORE INTO sync_state(repo) VALUES (?)", (repo,))
    if targeted:
        conn.execute(
            """
            UPDATE sync_state SET
                last_error = CASE WHEN :ingested > 0 THEN NULL ELSE last_error END,
                rate_limit_remaining = :rate_remaining,
                rate_limit_reset_at = :rate_reset_at
            WHERE repo = :repo
            """,
            {
                "ingested": ingested_count,
                "repo": repo,
                "rate_remaining": rate_limit_remaining,
                "rate_reset_at": rate_limit_reset_at,
            },
        )
        return
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
    children_complete: dict[str, bool] | None = None,
    links: dict | None = None,
    links_incomplete: object = None,
    pr_commits: list[dict] | None = None,
    advance_watermark: bool = True,
) -> None:
    """One item, caller owns the transaction.

    Identity and watermark come from the *issues list* payload. GET /pulls/{n}
    uses a different `id` and may have a different `updated_at`; those must
    not overwrite the list row (since filter is on issue updated_at).
    `timeline` carries the item's GraphQL timeline events; None means the
    source could not provide them, which leaves previously stored timeline
    edges untouched via the delete-owned exemption. The same
    presence-vs-absence convention governs `links` and `pr_commits` (issue
    #27): None = section never carried, so stored link edges, item_events and
    pr_commits rows survive untouched; a carried section replaces only when
    its completeness key is True.

    `children_complete` carries the per-section completeness map the sync
    resolved (issue #23): True where the bulk payload answered the section
    complete, False where a REST fallback, a truncation cap, or the 404 path
    produced it. It is accepted here so the ingest boundary can never silently
    present a fallback-resolved section as bulk-complete. Since #25 the map
    is consumed, not ignored: a section marked True gates tombstones for
    children that vanish (write_revisions), and the exact map rides the
    item_bundle sidecar for replay — it is still not stored in the database.
    With
    `advance_watermark=False` (sync --numbers) the watermark is left exactly
    as found: a targeted refresh says nothing about listing coverage.
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
    # Snapshot before replacement: replace_item_children deletes the stored
    # comment rows, and a probe-degraded re-ingest must not lose the
    # REST-native author_association values (issue #24).
    issue_associations, review_associations = snapshot_comment_associations(
        conn, repo, number
    )
    rev_snap = snapshot_revision_state(conn, repo, number, int(merged["id"]))
    replace_item_children(conn, repo, number)
    upsert_item_row(conn, repo, merged, kind, list_raw=list_raw, pull_raw=pull_raw)
    # Pre-images of the text this ingest displaced (issue #25): same
    # transaction, deterministic row order, append-only.
    write_revisions(
        conn,
        repo,
        number,
        rev_snap,
        merged,
        issue_comments,
        review_comments,
        reviews,
        children_complete,
    )
    # Text-derived references (issue #26): replaced with the children above.
    write_text_refs(
        conn, repo, number, merged.get("body"), issue_comments, review_comments, reviews
    )
    # GitHub-reported events and commit messages (issue #27): both gated on
    # presence AND completeness, so a degraded re-ingest leaves stored rows.
    complete_map = children_complete if isinstance(children_complete, dict) else {}
    write_item_events(
        conn, repo, number, timeline, complete_map.get("timeline") is True
    )
    insert_pr_commits(
        conn, repo, number, pr_commits, complete_map.get("pr_commits") is True
    )
    insert_labels(conn, repo, number, merged)
    insert_comments(
        conn, repo, number, issue_comments, "issue_comment", issue_associations
    )
    insert_comments(
        conn, repo, number, review_comments, "review_comment", review_associations
    )
    # Derived column: the stored-row count, immune to duplicate pagination
    # collapse and ID-less skips (issue #24; maintained post-child).
    conn.execute(
        "UPDATE items SET comment_count ="
        " (SELECT COUNT(*) FROM comments WHERE repo = ? AND number = ?)"
        " WHERE repo = ? AND number = ?",
        (repo, number, repo, number),
    )
    insert_assignees(conn, repo, number, list_raw)
    insert_issue_field_values(conn, repo, number, list_raw)
    insert_reviews(conn, repo, number, reviews)
    insert_pr_files(conn, repo, number, files, include_patches)
    # Derived column: paths_text mirrors pr_files truth exactly (issue #24
    # AC5) — an emptied files section empties it too, deterministic order.
    conn.execute(
        "UPDATE items SET paths_text = COALESCE(("
        "SELECT group_concat(path, ' ') FROM ("
        "SELECT path FROM pr_files WHERE repo = ? AND number = ? ORDER BY path)"
        "), '') WHERE repo = ? AND number = ?",
        (repo, number, repo, number),
    )
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
        links=links,
        links_incomplete=links_incomplete,
    )
    if advance_watermark:
        bump_watermark(conn, repo, list_raw.get("updated_at"))
    log_fetch(conn, repo, "item", str(number))
