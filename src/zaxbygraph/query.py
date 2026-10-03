from __future__ import annotations

import re
import sqlite3
from collections import defaultdict, deque

from zaxbygraph.db import CURRENT_USER_VERSION
from zaxbygraph.repo import validate_slug

_FIRST_KW = re.compile(r"\s*([A-Za-z]+)", re.I)

#: U+FEFF is not whitespace per str.strip()/\s, so a UTF-8 BOM (common when
#: SQL arrives from a file saved by a Windows editor) would otherwise hide the
#: leading keyword and get a legitimate read rejected.
_BOM = "﻿"

#: Sane floor for any caller-supplied row limit that reaches SQL in this
#: module. SQLite treats a negative LIMIT as "no limit at all", so a
#: non-positive value must never be forwarded verbatim.
_MIN_LIMIT = 1


#: Small fixed English stopword set for the search fallback (issue #4). Kept
#: in code with no dependency; tokens are always quoted, so user input never
#: becomes FTS5 syntax regardless of this list.
_STOPWORDS = frozenset(
    "a an and are as at be been being but by for from has have had he her his "
    "i in is it its of on or she that the their them then there these they "
    "this those to was were what when where which who will with you your".split()
)

#: bm25 weights for items_fts columns (title, body, labels_text): the title
#: dominates; labels_text outranks body because a label is a short curated
#: topic tag, while a term appearing in body prose is often incidental.
#: Lower (more negative) bm25 output ranks first, so ORDER BY ... ASC.
_ITEM_BM25 = "bm25(items_fts, 10.0, 1.0, 3.0)"


def _quote_token(token: str) -> str:
    """Quote one token so AND/OR/NEAR/column filters stay literals."""
    return f'"{token.replace(chr(34), " ")}"'


def build_match_queries(raw: str) -> tuple[str, str]:
    """Build the strict (all tokens ANDed) and broadened (non-stopword tokens
    ORed) FTS5 MATCH strings for a user query.

    Every token is double-quoted, so user input is never interpreted as FTS5
    syntax. The two queries are equal when broadening cannot change the match
    set — a single token, or a query whose every token is a stopword — and
    the empty query yields the zero-token phrase for both.
    """
    tokens = [t for t in raw.split() if t]
    if not tokens:
        return '""', '""'
    all_query = " AND ".join(_quote_token(t) for t in tokens)
    content = [t for t in tokens if t.lower() not in _STOPWORDS]
    if not content or len(content) == 1 == len(tokens):
        return all_query, all_query
    any_query = " OR ".join(_quote_token(t) for t in content)
    return all_query, any_query


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _clamp_limit(limit: int) -> int:
    """Clamp a caller-supplied row limit to a sane positive minimum before
    it reaches SQL. SQLite interprets a negative LIMIT as "unlimited", so
    e.g. `search(limit=-1)` would otherwise return every row."""
    limit = int(limit)
    return limit if limit > 0 else _MIN_LIMIT


def _fold_repo(repo: str | None) -> str | None:
    """Canonical lowercase repo for every lookup (issue #1: one repo, one key).

    Truthy guard matches cli.main(): an empty --repo means "no filter",
    not an error.
    """
    return validate_slug(repo) if repo else None


def status(conn: sqlite3.Connection, repo: str | None = None) -> dict:
    repo = _fold_repo(repo)
    if repo:
        state = conn.execute("SELECT * FROM sync_state WHERE repo = ?", (repo,)).fetchone()
        states = [] if state is None else [_row_to_dict(state)]
    else:
        states = [_row_to_dict(r) for r in conn.execute("SELECT * FROM sync_state").fetchall()]
    items = conn.execute(
        "SELECT repo, kind, state, COUNT(*) AS c FROM items "
        + ("WHERE repo = ?" if repo else "")
        + " GROUP BY repo, kind, state",
        (repo,) if repo else (),
    ).fetchall()
    for row in states:
        row["complete"] = not row.get("full_sync_pending") and row.get("last_error") is None
    return {
        "repos": states,
        "counts": [dict(r) for r in items],
    }


#: One merged, ranked search pass (issue #4): item-text hits (weighted bm25)
#: UNION ALL comment hits (plain bm25), grouped per item so each item appears
#: once with its best rank, an item-text-hit flag, and the EXACT count of its
#: matching comments. `total_matches` counts this grouping pre-limit; the
#: page is `ORDER BY score ASC, updated_at DESC LIMIT ?` — recency breaks
#: ties between equal bm25 scores.
_SEARCH_MERGED = """
    SELECT hit_key, MIN(score) AS score, MAX(src) AS has_item_hit,
           SUM(CASE WHEN src = 0 THEN 1 ELSE 0 END) AS matching_comments,
           MAX(upd) AS updated_at
    FROM (
        SELECT items.repo || '#' || items.number AS hit_key,
               {item_bm25} AS score,
               1 AS src,
               items.updated_at AS upd
        FROM items_fts
        JOIN items ON items.id = items_fts.rowid
        WHERE items_fts MATCH :match{item_repo}
        UNION ALL
        SELECT comments.repo || '#' || comments.number AS hit_key,
               bm25(comments_fts) AS score,
               0 AS src,
               items.updated_at AS upd
        FROM comments_fts
        JOIN comments ON comments.pk = comments_fts.rowid
        JOIN items ON items.repo = comments.repo AND items.number = comments.number
        WHERE comments_fts MATCH :match{comment_repo}
    )
    GROUP BY hit_key
"""


def _run_search_pass(
    conn: sqlite3.Connection, match: str, repo: str | None, limit: int
) -> tuple[int, list[sqlite3.Row]]:
    """One MATCH pass over items+comments: returns (total distinct items,
    top-`limit` rows by best score). Total is computed pre-limit so the
    fallback decision and `total_matches` are honest for any limit."""
    item_repo = " AND items.repo = :repo" if repo else ""
    comment_repo = " AND comments.repo = :repo" if repo else ""
    merged = _SEARCH_MERGED.format(
        item_bm25=_ITEM_BM25, item_repo=item_repo, comment_repo=comment_repo
    )
    params: dict[str, object] = {"match": match}
    if repo:
        params["repo"] = repo
    total = conn.execute(f"SELECT COUNT(*) FROM ({merged})", params).fetchone()[0]
    page = conn.execute(
        f"{merged} ORDER BY score ASC, updated_at DESC LIMIT :limit",
        {**params, "limit": limit},
    ).fetchall()
    return total, page


def _hydrate_search_hit(
    conn: sqlite3.Connection, match: str, hit: sqlite3.Row
) -> dict | None:
    """Materialize one merged hit: item columns, an item-text snippet when the
    item text matched, the exact matching-comment count, and the best
    matching comment's snippet when comments matched."""
    repo, _, number = str(hit["hit_key"]).rpartition("#")
    number = int(number)
    row = conn.execute(
        "SELECT repo, number, kind, title, state, author, updated_at, html_url "
        "FROM items WHERE repo = ? AND number = ?",
        (repo, number),
    ).fetchone()
    if row is None:
        return None
    rec = _row_to_dict(row)
    snippet: str = ""
    if hit["has_item_hit"]:
        found = conn.execute(
            "SELECT snippet(items_fts, -1, '«', '»', '…', 12) "
            "FROM items_fts JOIN items ON items.id = items_fts.rowid "
            "WHERE items_fts MATCH ? AND items.repo = ? AND items.number = ? "
            "LIMIT 1",
            (match, repo, number),
        ).fetchone()
        snippet = found[0] if found else ""
    rec["snippet"] = snippet
    rec["matching_comments"] = int(hit["matching_comments"] or 0)
    comment_snippet: str = ""
    if rec["matching_comments"]:
        found = conn.execute(
            "SELECT snippet(comments_fts, -1, '«', '»', '…', 12) "
            "FROM comments_fts JOIN comments ON comments.pk = comments_fts.rowid "
            "WHERE comments_fts MATCH ? AND comments.repo = ? AND comments.number = ? "
            "ORDER BY bm25(comments_fts) ASC LIMIT 1",
            (match, repo, number),
        ).fetchone()
        comment_snippet = found[0] if found else ""
    rec["comment_snippet"] = comment_snippet
    return rec


def search(conn: sqlite3.Connection, query: str, limit: int = 20, repo: str | None = None) -> dict:
    """Natural-language search over items and comments (issue #4).

    Tokens are quoted (never FTS5 syntax) and stemmed via the porter
    tokenizer. Ranking is bm25 (title-weighted) with `updated_at` as the
    tie-break. When the strict all-tokens pass finds fewer hits than the
    requested page, the page keeps every strict hit first and the broadened
    pass (stopwords removed, tokens OR-joined) fills the remaining slots;
    `matched_mode` reports whether broadening contributed ("any") or the
    strict pass answered alone ("all"). Comment hits merge into their parent
    item with `matching_comments` and `comment_snippet`; `total_matches`
    counts the broadened set pre-limit, and `corpus_items` the scoped item
    count — together they distinguish "no hits in N items" from an empty
    corpus. `index_stale` is true when the database predates the current
    schema (reads never migrate), so a zero-hit result on an un-migrated v2
    index is not mistaken for prior-art absence.
    """
    repo = _fold_repo(repo)
    limit = _clamp_limit(limit)
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    index_stale = user_version < CURRENT_USER_VERSION
    corpus_items = conn.execute(
        "SELECT COUNT(*) FROM items" + (" WHERE repo = ?" if repo else ""),
        (repo,) if repo else (),
    ).fetchone()[0]
    all_query, any_query = build_match_queries(query)
    if all_query == '""':
        return {
            "items": [],
            "matched_mode": "all",
            "total_matches": 0,
            "corpus_items": corpus_items,
            "index_stale": index_stale,
        }
    mode = "all"
    match = all_query
    total, page = _run_search_pass(conn, match, repo, limit)
    # Blocks keep the page honest under the fallback: the strict pass's page
    # is kept whole (its rows hydrated against the all-tokens match, so their
    # snippets show every term), and the broadened pass fills the remaining
    # slots with rows the strict page does not already have (hydrated against
    # the broadened match). Without this, a top-`limit` broadened page could
    # silently evict the very items the strict pass matched.
    blocks: list[tuple[str, list[sqlite3.Row]]] = [(match, page)]
    if total < limit and any_query != all_query:
        mode = "any"
        match = any_query
        total, or_page = _run_search_pass(conn, match, repo, limit)
        seen = {row["hit_key"] for row in page}
        extra = [
            row
            for row in or_page
            if row["hit_key"] not in seen
        ][: max(0, limit - len(page))]
        blocks.append((match, extra))
    items = []
    for block_match, block_rows in blocks:
        for hit in block_rows:
            rec = _hydrate_search_hit(conn, block_match, hit)
            if rec is not None:
                items.append(rec)
    return {
        "items": items,
        "matched_mode": mode,
        "total_matches": total,
        "corpus_items": corpus_items,
        "index_stale": index_stale,
    }


def item(conn: sqlite3.Connection, number: int, repo: str | None = None) -> dict | None:
    repo = _fold_repo(repo)
    if repo:
        row = conn.execute(
            "SELECT * FROM items WHERE repo = ? AND number = ?", (repo, number)
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM items WHERE number = ?", (number,)).fetchone()
    if row is None:
        return None
    rec = _row_to_dict(row)
    rec.pop("raw_json", None)
    r = rec["repo"]
    n = rec["number"]
    rec["labels"] = [
        _row_to_dict(x)
        for x in conn.execute(
            "SELECT name, color FROM labels WHERE repo = ? AND number = ?", (r, n)
        ).fetchall()
    ]
    rec["comments"] = [
        _row_to_dict(x)
        for x in conn.execute(
            "SELECT pk, github_id, kind, author, created_at, body, html_url "
            "FROM comments WHERE repo = ? AND number = ? ORDER BY created_at",
            (r, n),
        ).fetchall()
    ]
    rec["reviews"] = [
        _row_to_dict(x)
        for x in conn.execute(
            "SELECT id, author, state, submitted_at, body, html_url "
            "FROM reviews WHERE repo = ? AND number = ? ORDER BY submitted_at",
            (r, n),
        ).fetchall()
    ]
    rec["files"] = [
        _row_to_dict(x)
        for x in conn.execute(
            "SELECT path, status, additions, deletions, changes "
            "FROM pr_files WHERE repo = ? AND number = ? ORDER BY path",
            (r, n),
        ).fetchall()
    ]
    rec["edges"] = [
        _row_to_dict(x)
        for x in conn.execute(
            "SELECT src_type, src_id, rel, dst_type, dst_id, confidence, evidence "
            "FROM edges WHERE repo = ? AND ("
            "(src_type = 'item' AND src_id = ?) OR (dst_type = 'item' AND dst_id = ?)"
            ")",
            (r, str(n), str(n)),
        ).fetchall()
    ]
    return rec


def related(conn: sqlite3.Connection, number: int, depth: int = 1, repo: str | None = None) -> dict:
    repo = _fold_repo(repo)
    if repo is None:
        row = conn.execute("SELECT repo FROM items WHERE number = ?", (number,)).fetchone()
        if row is None:
            return {"number": number, "nodes": [], "edges": []}
        repo = row["repo"]
    seen_edges: list[dict] = []
    frontier = {str(number)}
    visited = set(frontier)
    for _ in range(max(1, depth)):
        nxt: set[str] = set()
        for nid in frontier:
            rows = conn.execute(
                "SELECT src_type, src_id, rel, dst_type, dst_id, confidence, evidence "
                "FROM edges WHERE repo = ? AND ("
                "(src_type = 'item' AND src_id = ?) OR (dst_type = 'item' AND dst_id = ?)"
                ")",
                (repo, nid, nid),
            ).fetchall()
            for row in rows:
                rec = _row_to_dict(row)
                seen_edges.append(rec)
                for typ, ident in ((rec["src_type"], rec["src_id"]), (rec["dst_type"], rec["dst_id"])):
                    if typ == "item" and ident not in visited:
                        nxt.add(ident)
                        visited.add(ident)
        frontier = nxt
    nodes = []
    for nid in visited:
        it = conn.execute(
            "SELECT number, kind, title, state FROM items WHERE repo = ? AND number = ?",
            (repo, int(nid)),
        ).fetchone()
        if it:
            nodes.append(_row_to_dict(it))
        else:
            nodes.append({"number": int(nid), "kind": None, "title": None, "state": None})
    return {"number": number, "repo": repo, "nodes": nodes, "edges": seen_edges}


def churn(conn: sqlite3.Connection, limit: int = 30, repo: str | None = None) -> list[dict]:
    repo = _fold_repo(repo)
    sql = """
        SELECT path, COUNT(*) AS prs,
               SUM(additions) AS additions, SUM(deletions) AS deletions
        FROM pr_files
    """
    params: list[object] = []
    if repo:
        sql += " WHERE repo = ?"
        params.append(repo)
    sql += " GROUP BY path ORDER BY prs DESC, path ASC LIMIT ?"
    params.append(_clamp_limit(limit))
    return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def open_items(conn: sqlite3.Connection, repo: str | None = None) -> list[dict]:
    repo = _fold_repo(repo)
    sql = """
        SELECT repo, number, kind, title, author, updated_at, html_url
        FROM items WHERE state = 'open'
    """
    params: list[object] = []
    if repo:
        sql += " AND repo = ?"
        params.append(repo)
    sql += " ORDER BY updated_at DESC"
    return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def path_between(conn: sqlite3.Connection, a: str, b: str, repo: str | None = None) -> dict:
    """Undirected BFS over item↔item and item↔file edges."""
    repo = _fold_repo(repo)
    if repo is None:
        row = conn.execute("SELECT repo FROM items LIMIT 1").fetchone()
        if row is None:
            return {"a": a, "b": b, "path": None, "reason": "empty graph"}
        repo = row["repo"]

    def node_key(kind: str, ident: str) -> str:
        return f"{kind}:{ident}"

    def parse_endpoint(value: str) -> tuple[str, str]:
        if value.isdigit():
            return "item", value
        return "file", value

    start_t, start_id = parse_endpoint(a)
    goal_t, goal_id = parse_endpoint(b)
    start = node_key(start_t, start_id)
    goal = node_key(goal_t, goal_id)

    STRUCTURAL = {"touches", "closes", "mentions"}
    adj: dict[str, list[tuple[str, str]]] = defaultdict(list)
    rows = conn.execute(
        "SELECT src_type, src_id, rel, dst_type, dst_id FROM edges WHERE repo = ?",
        (repo,),
    ).fetchall()
    for row in rows:
        if row["rel"] not in STRUCTURAL:
            continue
        src = node_key(row["src_type"], row["src_id"])
        dst = node_key(row["dst_type"], row["dst_id"])
        adj[src].append((dst, row["rel"]))
        adj[dst].append((src, row["rel"]))

    if start == goal:
        return {"a": a, "b": b, "repo": repo, "path": [{"type": start_t, "id": start_id}]}

    prev: dict[str, tuple[str, str] | None] = {start: None}
    q: deque[str] = deque([start])
    found = False
    while q:
        cur = q.popleft()
        if cur == goal:
            found = True
            break
        for nxt, rel in adj.get(cur, []):
            if nxt not in prev:
                prev[nxt] = (cur, rel)
                q.append(nxt)
    if not found:
        return {"a": a, "b": b, "repo": repo, "path": None}

    chain: list[dict] = []
    cur = goal
    while cur is not None:
        typ, ident = cur.split(":", 1)
        chain.append({"type": typ, "id": ident})
        step = prev[cur]
        if step is None:
            break
        cur = step[0]
    chain.reverse()
    return {"a": a, "b": b, "repo": repo, "path": chain}


def _sql_tokens(sql: str) -> list[tuple[str, str]]:
    """Single-pass, string/comment-aware scan of `sql` into (kind, text)
    spans that together reconstruct the original string.

    kind is one of:
      "code"    - ordinary SQL text, outside any string/identifier/comment.
                  Only spans of this kind may contain a syntactically
                  significant ';', '--' or '/*'.
      "string"  - a single-quoted string literal (with '' escaping), a
                  double-quoted identifier (with "" escaping), a
                  `backtick`-quoted identifier, or a [bracketed] identifier.
      "comment" - a `--` line comment or a `/* ... */` block comment.

    This replaces the old regex-based comment stripper, which stripped a
    '--' or ';' found *inside* a string literal as if it were live SQL —
    that both hid a stacked statement behind a string (`SELECT '--';
    DELETE ...`) and wrongly rejected legal SQL containing a literal ';'
    (`SELECT ';' AS x`).
    """
    tokens: list[tuple[str, str]] = []
    n = len(sql)
    i = 0
    start = 0

    def flush(end: int) -> None:
        if end > start:
            tokens.append(("code", sql[start:end]))

    while i < n:
        c = sql[i]
        if c == "'" or c == '"':
            flush(i)
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            else:
                j = n
            tokens.append(("string", sql[i:j]))
            i = start = j
            continue
        if c == "`":
            flush(i)
            j = sql.find("`", i + 1)
            j = n if j == -1 else j + 1
            tokens.append(("string", sql[i:j]))
            i = start = j
            continue
        if c == "[":
            flush(i)
            j = sql.find("]", i + 1)
            j = n if j == -1 else j + 1
            tokens.append(("string", sql[i:j]))
            i = start = j
            continue
        if c == "-" and sql[i + 1 : i + 2] == "-":
            flush(i)
            j = sql.find("\n", i)
            j = n if j == -1 else j
            tokens.append(("comment", sql[i:j]))
            i = start = j
            continue
        if c == "/" and sql[i + 1 : i + 2] == "*":
            flush(i)
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            tokens.append(("comment", sql[i:j]))
            i = start = j
            continue
        i += 1
    flush(n)
    return tokens


def _strip_sql_comments(sql: str) -> str:
    """Comment-and-literal-aware comment stripper: `--`/`/* */` comments
    become a single space each; string and identifier literals are left
    completely untouched."""
    return "".join(" " if kind == "comment" else text for kind, text in _sql_tokens(sql))


def _has_stacked_statement(sql: str) -> bool:
    """True if `sql` contains more than one statement outside of comments
    and string/identifier literals. A lone trailing ';' plus trailing
    whitespace/comments is fine (that's just normal statement termination);
    anything else after the first real ';' is a stacked statement."""
    neutral = "".join(
        " " * len(text) if kind in ("comment", "string") else text for kind, text in _sql_tokens(sql)
    )
    neutral = neutral.strip()
    return ";" in neutral.rstrip(";")


def assert_read_sql(sql: str) -> None:
    stripped = _strip_sql_comments(sql).strip().lstrip(_BOM).strip()
    if not stripped:
        raise ValueError("empty SQL")
    if _has_stacked_statement(sql):
        raise ValueError("multiple statements are not allowed")
    match = _FIRST_KW.match(stripped)
    if match is None:
        raise ValueError("SQL must start with SELECT, WITH, or EXPLAIN")
    kw = match.group(1).upper()
    if kw not in {"SELECT", "WITH", "EXPLAIN"}:
        raise ValueError("SQL must start with SELECT, WITH, or EXPLAIN")


def run_sql(conn: sqlite3.Connection, sql: str, limit: int = 200, repo: str | None = None) -> dict:
    assert_read_sql(sql)
    limit = _clamp_limit(limit)
    cur = conn.cursor()
    try:
        cur.execute(sql)
    except sqlite3.Error as exc:
        raise ValueError(str(exc)) from exc
    if cur.description is None:
        return {"columns": [], "rows": [], "truncated": False}
    cols = [d[0] for d in cur.description]
    try:
        # Fetch at most limit+1 rows so we can detect truncation without
        # ever materializing the full result set (a multi-million-row
        # recursive CTE must not be pulled to exhaustion just to return
        # `limit` rows — fetchall() would step the VDBE through every row
        # before we ever get to slice it down).
        fetched = cur.fetchmany(limit + 1)
    except sqlite3.Error as exc:
        raise ValueError(str(exc)) from exc
    truncated = len(fetched) > limit
    # Positional extraction only: sqlite3.Row resolves duplicate column
    # names to the FIRST match (row["body"] on a joined SELECT returns the
    # left table's value for both positions), so name-based access silently
    # substitutes values. list(row) keeps every duplicate position honest.
    rows = [list(row) for row in fetched[:limit]]
    if repo is not None and "repo" in cols:
        # Issue #2 AC4: rows that carry a repo column show only the resolved
        # repo. Post-fetch and column-name based; a projection without a
        # repo column cannot be filtered (per-slug stores make that
        # store-scoped by construction — documented in README).
        # truncated reports the PRE-filter rowset (PRR-004): the unfiltered
        # query genuinely had more rows than the limit, so raising the limit
        # always converges; the flag must not be recomputed post-filter.
        idx = cols.index("repo")
        rows = [row for row in rows if row[idx] == repo]
    return {"columns": cols, "rows": rows, "truncated": truncated}


def export_graph(conn: sqlite3.Connection, repo: str | None = None) -> dict:
    repo = _fold_repo(repo)
    item_rows = conn.execute(
        "SELECT repo, number, kind, title, state FROM items" + (" WHERE repo = ?" if repo else ""),
        (repo,) if repo else (),
    ).fetchall()
    nodes: list[dict] = []
    seen: set[str] = set()
    for row in item_rows:
        nid = f"item:{row['number']}"
        nodes.append(
            {
                "id": nid,
                "type": "item",
                "label": f"#{row['number']} {row['title']}",
                "kind": row["kind"],
                "state": row["state"],
            }
        )
        seen.add(nid)
    edge_rows = conn.execute(
        "SELECT src_type, src_id, rel, dst_type, dst_id, confidence FROM edges"
        + (" WHERE repo = ?" if repo else ""),
        (repo,) if repo else (),
    ).fetchall()
    edges = []
    for row in edge_rows:
        src = f"{row['src_type']}:{row['src_id']}"
        dst = f"{row['dst_type']}:{row['dst_id']}"
        if src not in seen:
            nodes.append({"id": src, "type": row["src_type"], "label": row["src_id"]})
            seen.add(src)
        if dst not in seen:
            nodes.append({"id": dst, "type": row["dst_type"], "label": row["dst_id"]})
            seen.add(dst)
        edges.append(
            {
                "source": src,
                "target": dst,
                "rel": row["rel"],
                "confidence": row["confidence"],
            }
        )
    return {"nodes": nodes, "edges": edges}
