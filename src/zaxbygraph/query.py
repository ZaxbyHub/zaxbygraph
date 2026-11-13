from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from zaxbygraph.db import CURRENT_USER_VERSION, _V7_USER_VERSION, _V8_USER_VERSION
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

#: Ceiling for the row limit of a fetch that over-fetches by one to detect
#: truncation. CPython's sqlite3 converts `Cursor.fetchmany()`'s argument to
#: a C int, so any limit at or above 2**31-1 raises OverflowError before a
#: single row is fetched (issue #55 AC3/AC4). The tightest safe bound is
#: 2**31-2. This ceiling deliberately does NOT live in `_clamp_limit`: a
#: plain SQL `LIMIT ?` is bound as a 64-bit integer and accepts far larger
#: values, and search/churn/open_items route through `_clamp_limit`.
_MAX_FETCH_LIMIT = 2**31 - 2

#: Ceiling for ANY caller-supplied row limit, including the ones bound as a
#: SQL parameter. CPython's sqlite3 binds a Python int as a signed 64-bit
#: integer, so a limit above 2**63-1 raises OverflowError at the bind and
#: escapes as a traceback (issue #55, found by the PR #57 swarm review:
#: `search`/`open`/`churn --limit 99999999999999999999999` crashed at base
#: and at head). Every value at or below this bound is one SQLite already
#: accepts, so refusing above it removes a crash without changing any
#: working request.
_MAX_SQL_LIMIT = 2**63 - 1

#: Default wall-clock deadline (seconds) for one `run_sql` call, including
#: the fetch loop, the vocabulary-warning pass and the completion count.
#: The progress handler turns this into SQLite's `interrupted` abort, so a
#: query that would run forever is bounded instead of hanging the caller.
_DEFAULT_SQL_DEADLINE_S = 5.0

#: Upper clamp for the env override ZAXBYGRAPH_SQL_TIMEOUT_S (issue #71
#: AC1): a caller may extend the budget, not remove it.
_SQL_DEADLINE_MAX_S = 600.0

#: Default serialized-result budget (bytes, measured as rendered-cell
#: chars) for one `run_sql` call. The fetch loop stops once the budget is
#: exceeded and reports `truncated: true` (issue #71 AC2).
_DEFAULT_SQL_MAX_BYTES = 1048576

#: Per-cell cap: one oversized TEXT cell must not eat the whole budget or
#: the caller's context. The marker is explicit so a truncated cell is
#: never mistaken for the stored value.
_SQL_CELL_CAP = 4096
_SQL_CELL_MARKER = "…[cell truncated]"

#: Progress-handler cadence: sqlite3 invokes the handler every N VDBE
#: instructions, so this is the deadline's interrupt granularity.
_SQL_PROGRESS_OPS = 1000

#: Chunk size for the incremental fetch loop (bounded pulls; the loop still
#: never materializes more than limit+1 rows plus one in-flight chunk).
_FETCH_CHUNK = 256

#: DISTINCT sample bound for vocabulary warnings: enough to name the real
#: vocabulary of every enum/repo column without unbounded probing.
_VOCAB_DISTINCT_LIMIT = 100

#: Fixed enum vocabulary columns (issue #71 AC10): a literal compared to
#: one of these that matches no stored value is almost certainly a wrong
#: guess at the vocabulary, not a real filter. Repo columns are NOT listed
#: here — they are discovered from the live schema so every table carrying
#: a `repo` column warns, not just items.
_ENUM_VOCAB_COLUMNS = frozenset(
    {
        ("items", "kind"),
        ("items", "state"),
        ("comments", "kind"),
        ("edges", "rel"),
        ("edges", "source"),
    }
)



#: Small fixed English stopword set for the search fallback (issue #4). Kept
#: in code with no dependency; tokens are always quoted, so user input never
#: becomes FTS5 syntax regardless of this list.
_STOPWORDS = frozenset(
    "a an and are as at be been being but by for from has have had he her his "
    "i in is it its of on or she that the their them then there these they "
    "this those to was were what when where which who will with you your".split()
)

#: bm25 weights for items_fts columns. v7 (title, body, labels_text,
#: paths_text): the title dominates; labels_text outranks body because a
#: label is a short curated topic tag, while a term appearing in body prose
#: is often incidental; touched-path text ranks below all three (issue #24
#: AC5). The legacy weights apply to pre-v7 stores, whose items_fts has no
#: paths_text column (reads never migrate; see search's index_stale).
#: Lower (more negative) bm25 output ranks first, so ORDER BY ... ASC.
_ITEM_BM25 = "bm25(items_fts, 10.0, 1.0, 3.0, 0.5)"
_ITEM_BM25_LEGACY = "bm25(items_fts, 10.0, 1.0, 3.0)"


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
    e.g. `search(limit=-1)` would otherwise return every row.

    A limit above `_MAX_SQL_LIMIT` is refused rather than clamped: CPython's
    sqlite3 binds a Python int as a signed 64-bit integer, so a larger value
    raises OverflowError at the bind and escapes as a traceback. Every value
    at or below the ceiling is one SQLite already accepts.
    """
    limit = int(limit)
    if limit > _MAX_SQL_LIMIT:
        raise ValueError(
            f"row limit {limit} exceeds the maximum of {_MAX_SQL_LIMIT}"
        )
    return limit if limit > 0 else _MIN_LIMIT


def _clamp_fetch_limit(limit: int) -> int:
    """`_clamp_limit` plus the C-int ceiling for a fetch that over-fetches by
    one row to detect truncation (`fetchmany(limit + 1)`).

    Above the ceiling the value is refused rather than clamped, so the caller
    surfaces an error envelope instead of an OverflowError traceback
    (issue #55 AC3/AC4). Only the two `fetchmany(limit + 1)` sites use this;
    every other limit is bound as a SQL parameter, where 64-bit values are
    legal and the tighter ceiling here would be a behavior change.
    """
    limit = _clamp_limit(limit)
    if limit > _MAX_FETCH_LIMIT:
        raise ValueError(
            f"row limit {limit} exceeds the maximum of {_MAX_FETCH_LIMIT}"
        )
    return limit


def _fold_repo(repo: str | None) -> str | None:
    """Canonical lowercase repo for every lookup (issue #1: one repo, one key).

    Truthy guard matches cli.main(): an empty --repo means "no filter",
    not an error.
    """
    return validate_slug(repo) if repo else None


#: items columns whose NULL marks a pre-v7 (stale-payload) row. Restricted to
#: the two signals GitHub's listing payload ALWAYS carries with a non-null
#: value (`reactions.total_count` is an object field, `author_association` a
#: string — "NONE" among them), so their NULL unambiguously dates the payload
#: to before v7. milestone/item_type are excluded because present-null is
#: legitimate current state (unmilestoned/untyped issues); the dependency
#: summaries are excluded because PR rows never carry them; the pull trio is
#: excluded because it is NULL for unmerged PRs. Counting any of those would
#: flag ordinary rows (cubic CUB-3 + review round 2). The derived pair
#: comment_count/paths_text are NOT NULL by schema and never contribute.
_COVERAGE_NULL_COLUMNS = (
    "reactions_total",
    "author_association",
)


def _coverage(conn: sqlite3.Connection, repo: str | None, user_version: int) -> dict:
    """The status coverage block (issue #24 AC6): how much of the corpus the
    sync actually captured. The three derived counts follow status(repo)'s
    repo scoping; store_bytes is database-global. ``null_v7_rows`` counts
    items rows where any always-present listing signal is NULL — the mark of
    a payload that predated v7 — and is null on a pre-v7 store (reads never
    migrate, so the columns may not exist)."""
    repo_filter = " AND repo = ?" if repo else ""
    repo_params: tuple = (repo,) if repo else ()
    truncation_notes = {
        row["resource"]: row["n"]
        for row in conn.execute(
            "SELECT resource, COUNT(*) AS n FROM fetch_log"
            " WHERE note LIKE '%truncated%'" + repo_filter + " GROUP BY resource",
            repo_params,
        ).fetchall()
    }
    dropped_fields = conn.execute(
        "SELECT COUNT(*) FROM fetch_log WHERE note LIKE 'omitted %'" + repo_filter,
        repo_params,
    ).fetchone()[0]
    # v7 feature gates pin the slot that introduced the columns (issue #25):
    # a read-only store several slots behind keeps every feature its own
    # schema supports, so this must never ride CURRENT_USER_VERSION.
    if user_version >= _V7_USER_VERSION:
        null_v7_rows = conn.execute(
            "SELECT COUNT(*) FROM items WHERE ("
            + " OR ".join(f"{column} IS NULL" for column in _COVERAGE_NULL_COLUMNS)
            + ")"
            + (" AND repo = ?" if repo else ""),
            (repo,) if repo else (),
        ).fetchone()[0]
    else:
        null_v7_rows = None
    # v8 revision coverage: real UTF-8 byte counts (LENGTH on a BLOB cast
    # counts bytes where LENGTH on text counts characters), zero — never
    # null — on an empty v8 table, null on a pre-v8 store.
    if user_version >= _V8_USER_VERSION:
        revision_rows = conn.execute(
            "SELECT COUNT(*) FROM revisions" + (" WHERE repo = ?" if repo else ""),
            (repo,) if repo else (),
        ).fetchone()[0]
        revision_bytes = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(content AS BLOB))), 0) FROM revisions"
            + (" WHERE repo = ?" if repo else ""),
            (repo,) if repo else (),
        ).fetchone()[0]
    else:
        revision_rows = None
        revision_bytes = None
    # The LATEST capability-probe failure state (issue #65 AC9): one when
    # the last probe failed, zero once a later probe succeeds — never a
    # cumulative count, so a recovered host does not keep reporting.
    probe_failures = conn.execute(
        "SELECT COUNT(*) FROM meta"
        " WHERE key LIKE 'capability_probe_failed:%'"
        " AND value IS NOT NULL AND value != ''"
    ).fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    return {
        "truncation_notes": truncation_notes,
        "dropped_fields": dropped_fields,
        "probe_failures": probe_failures,
        "null_v7_rows": null_v7_rows,
        "revision_rows": revision_rows,
        "revision_bytes": revision_bytes,
        "store_bytes": page_size * page_count,
    }


def status(conn: sqlite3.Connection, repo: str | None = None) -> dict:
    repo = _fold_repo(repo)
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
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
    snapshot = None
    meta_rows = dict(conn.execute("SELECT key, value FROM meta WHERE key LIKE 'snapshot.%'").fetchall())
    if meta_rows:
        snapshot = {
            "publisher": meta_rows.get("snapshot.publisher"),
            "manifest_sha256": meta_rows.get("snapshot.manifest_sha256"),
            "pulled_at": meta_rows.get("snapshot.pulled_at"),
        }
    return {
        "repos": states,
        "counts": [dict(r) for r in items],
        "coverage": _coverage(conn, repo, user_version),
        "snapshot": snapshot,
    }


#: One merged, ranked search pass (issue #4): item-text hits (weighted bm25)
#: UNION ALL comment hits (plain bm25), grouped per item so each item appears
#: once with its best rank, an item-text-hit flag, and the EXACT count of its
#: matching comments. Rows carry repo and number as separate columns so the
#: phrase tier can join against item-text phrase hits (issue #72 C15).
_SEARCH_MERGED = """
    SELECT hit.repo AS repo, hit.number AS number,
           hit.repo || '#' || hit.number AS hit_key,
           MIN(hit.score) AS score,
           MAX(CASE WHEN hit.src = 1 THEN 1 ELSE 0 END) AS has_item_hit,
           SUM(CASE WHEN hit.src = 0 THEN 1 ELSE 0 END) AS matching_comments,
           SUM(CASE WHEN hit.src = 2 THEN 1 ELSE 0 END) AS matching_reviews,
           MAX(hit.upd) AS updated_at
    FROM (
        SELECT items.repo AS repo, items.number AS number,
               {item_bm25} AS score,
               1 AS src,
               items.updated_at AS upd
        FROM items_fts
        JOIN items ON items.id = items_fts.rowid
        WHERE items_fts MATCH :match{item_repo}{item_bots}
        UNION ALL
        SELECT comments.repo AS repo, comments.number AS number,
               bm25(comments_fts) AS score,
               0 AS src,
               items.updated_at AS upd
        FROM comments_fts
        JOIN comments ON comments.pk = comments_fts.rowid
        JOIN items ON items.repo = comments.repo AND items.number = comments.number
        WHERE comments_fts MATCH :match{comment_repo}{comment_bots}{comment_item_bots}{reviews_arm}
    ) AS hit
    GROUP BY hit.repo, hit.number
"""

#: Review-body hits (issue #24 AC4) merge into the owning item like comment
#: hits, with src = 2: has_item_hit is computed as MAX(CASE WHEN src = 1 …)
#: so review-only hits skip the item-text snippet lookup, and the
#: matching_comments count still tracks src = 0 only. Spliced into
#: _SEARCH_MERGED only on v7 stores; a pre-v7 store has no reviews_fts and
#: reads never migrate.
_REVIEWS_ARM = """
        UNION ALL
        SELECT reviews.repo AS repo, reviews.number AS number,
               bm25(reviews_fts) AS score,
               2 AS src,
               items.updated_at AS upd
        FROM reviews_fts
        JOIN reviews ON reviews.id = reviews_fts.rowid
        JOIN items ON items.repo = reviews.repo AND items.number = reviews.number
        WHERE reviews_fts MATCH :match{review_repo}
          AND items.repo = reviews.repo AND items.number = reviews.number{review_bots}{review_item_bots}
"""

#: Bot-exclusion semi-joins (issue #71 C6 / plan CF-D), spliced into every
#: search arm IN THE SQL — pre-ranking, so counts, snippets and pagination
#: are computed over the filtered set. Inner JOIN on the author's actors row
#: with is_bot = 1 EXCLUDES established bot rows only (FB-005: the inner
#: is_bot=0 join silently dropped NULL-author rows — ghost/deleted GitHub
#: accounts have no actors row — which are not established bots and were
#: never counted in bots_omitted). NOT EXISTS is NULL-safe: a row whose
#: author is NULL (or has no actors row) can never match the bot lookup and
#: is kept, exactly like brief's LEFT JOIN+COALESCE treatment. Each constant
#: is a WHERE fragment (" AND NOT EXISTS ..."), spliced INTO the host
#: clause — never between joins, where a WHERE would be a syntax error.
#: _ITEM_BOT_FILTER is ALSO spliced into the comment/review arms (08b review
#: Q1): a bot-authored ITEM leaves the stream even when a HUMAN comment or
#: review matched it — its row is omitted and counted in bots_omitted, not
#: surfaced under the bot's title.
_ITEM_BOT_FILTER = (
    " AND NOT EXISTS (SELECT 1 FROM actors ia WHERE ia.login = items.author"
    " AND ia.is_bot = 1)"
)
_COMMENT_BOT_FILTER = (
    " AND NOT EXISTS (SELECT 1 FROM actors ca WHERE ca.login = comments.author"
    " AND ca.is_bot = 1)"
)
_REVIEW_BOT_FILTER = (
    " AND NOT EXISTS (SELECT 1 FROM actors ra WHERE ra.login = reviews.author"
    " AND ra.is_bot = 1)"
)


def _require_bot_flags(conn: sqlite3.Connection) -> None:
    """actors.is_bot exists only from schema v12; reads never migrate, so a
    pre-v12 store cannot serve --no-bots/exclude_bots at all (FB-004). A
    structured refusal naming the remedy replaces what would otherwise be
    a raw 'no such column' OperationalError on the old store."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < 12:
        raise ValueError(
            "exclude_bots/--no-bots needs schema v12+ (actors.is_bot);"
            " this store is v%d — run 'zaxbygraph sync' to upgrade" % version
        )


def _content_tokens(raw: str) -> list[str]:
    """The query's non-stopword tokens (the broadened pass's vocabulary)."""
    return [t for t in raw.split() if t and t.lower() not in _STOPWORDS]


def build_phrase_query(raw: str) -> str | None:
    """The adjacent-phrase MATCH string for the ranking tier (issue #72 C15),
    or None when the query cannot form one (fewer than two content tokens —
    a single-token phrase is just the strict match). Tokens are quoted as one
    FTS5 phrase, so user input is never FTS5 syntax."""
    content = _content_tokens(raw)
    if len(content) < 2:
        return None
    phrase = " ".join(content)
    return f'"{phrase.replace(chr(34), " ")}"'


def _merged_search_sql(
    repo: str | None,
    *,
    legacy: bool = False,
    exclude_bots: bool = False,
) -> str:
    """The formatted _SEARCH_MERGED pass SQL (repo/number-grouped, bot
    semi-joins spliced in when exclude_bots)."""
    item_repo = " AND items.repo = :repo" if repo else ""
    comment_repo = " AND comments.repo = :repo" if repo else ""
    review_repo = " AND reviews.repo = :repo" if repo else ""
    return _SEARCH_MERGED.format(
        item_bm25=_ITEM_BM25_LEGACY if legacy else _ITEM_BM25,
        item_repo=item_repo,
        item_bots=_ITEM_BOT_FILTER if exclude_bots else "",
        comment_bots=_COMMENT_BOT_FILTER if exclude_bots else "",
        comment_item_bots=_ITEM_BOT_FILTER if exclude_bots else "",
        comment_repo=comment_repo,
        reviews_arm="" if legacy else _REVIEWS_ARM.format(
            review_repo=review_repo,
            review_bots=_REVIEW_BOT_FILTER if exclude_bots else "",
            review_item_bots=_ITEM_BOT_FILTER if exclude_bots else "",
        ),
    )


def _search_pass_total(
    conn: sqlite3.Connection,
    match: str,
    repo: str | None,
    *,
    legacy: bool = False,
    exclude_bots: bool = False,
) -> int:
    """The pass's distinct-item COUNT, independent of any page limit (issue
    #72 C14): the count queries materialize no rows."""
    merged = _merged_search_sql(repo, legacy=legacy, exclude_bots=exclude_bots)
    params: dict[str, object] = {"match": match}
    if repo:
        params["repo"] = repo
    return int(conn.execute(f"SELECT COUNT(*) FROM ({merged})", params).fetchone()[0])


def _run_search_pass(
    conn: sqlite3.Connection,
    match: str,
    repo: str | None,
    limit: int,
    *,
    legacy: bool = False,
    exclude_bots: bool = False,
    phrase: str | None = None,
    known_total: int | None = None,
) -> tuple[int, list[sqlite3.Row]]:
    """One MATCH pass over items+comments (+reviews on v7 stores): returns
    (total distinct items, top-`limit` rows). Total is computed pre-limit so
    the fallback decision and `total_matches` are honest for any limit.

    The page's ORDER BY is the deterministic surface-tier ranking (issue #72
    C15): item-text hits (title/body/labels/paths) above comment/review-only
    hits, an adjacent-phrase hit in the item text above scattered tokens
    within the item-text tier, then bm25, recency, and (repo, number) as the
    stable tie-breaks. The tier flags ride every row so hydration and the
    caller never re-derive them."""
    merged = _merged_search_sql(repo, legacy=legacy, exclude_bots=exclude_bots)
    params: dict[str, object] = {"match": match}
    if repo:
        params["repo"] = repo
    # A caller that just computed the pass total (the fallback's broadened
    # COUNT) hands it in: re-running the identical COUNT re-evaluated the
    # whole merged FTS pass a second time for a discarded value (FB-018).
    if known_total is not None:
        total = known_total
    else:
        total = conn.execute(f"SELECT COUNT(*) FROM ({merged})", params).fetchone()[0]
    if phrase is None:
        page_sql = (
            f"SELECT * FROM ({merged}) "
            "ORDER BY has_item_hit DESC, score ASC, updated_at DESC, "
            "repo ASC, number ASC LIMIT :limit"
        )
        page = conn.execute(page_sql, {**params, "limit": limit}).fetchall()
        return total, page
    # Phrase tier: item-text rows whose title/body carry the content tokens
    # as one adjacent phrase rank above scattered same-tier rows. Joined, not
    # correlated, so the ranking stays a single sorted pass.
    phrase_repo = " AND items.repo = :repo" if repo else ""
    page_sql = (
        "WITH phrase_hits(repo, number) AS ("
        "SELECT items.repo, items.number FROM items_fts "
        "JOIN items ON items.id = items_fts.rowid "
        f"WHERE items_fts MATCH :phrase{phrase_repo}) "
        f"SELECT m.*, CASE WHEN p.repo IS NULL THEN 0 ELSE 1 END AS phrase_hit "
        f"FROM ({merged}) AS m "
        "LEFT JOIN phrase_hits p ON p.repo = m.repo AND p.number = m.number "
        "ORDER BY m.has_item_hit DESC, phrase_hit DESC, m.score ASC, "
        "m.updated_at DESC, m.repo ASC, m.number ASC LIMIT :limit"
    )
    page = conn.execute(
        page_sql, {**params, "phrase": phrase, "limit": limit}
    ).fetchall()
    return total, page


def _search_bots_omitted(
    conn: sqlite3.Connection,
    match: str,
    repo: str | None,
    *,
    legacy: bool = False,
) -> int:
    """Count the bot-authored matches the exclusion filter removes (issue #71
    C6, amended by the 08b review): DISTINCT matched ITEMS whose own author
    is a bot — omitted as rows however they matched (own text, a human
    comment, a human review) — plus matching comment and review rows
    authored by bots, under the widest (broadened) match. Bounded COUNTs, no
    row materialization."""
    comment_repo = " AND comments.repo = :repo" if repo else ""
    review_repo = " AND reviews.repo = :repo" if repo else ""
    # One row per matched item (the merged pass GROUPs by repo+number), so
    # COUNT(*) over the bot-author join is the distinct omitted-item count.
    merged = _merged_search_sql(repo, legacy=legacy, exclude_bots=False)
    arms = [
        f"SELECT COUNT(*) FROM ({merged}) AS m "
        "JOIN items ON items.repo = m.repo AND items.number = m.number "
        "JOIN actors ON actors.login = items.author "
        "WHERE actors.is_bot = 1",
        "SELECT COUNT(*) FROM comments_fts "
        "JOIN comments ON comments.pk = comments_fts.rowid "
        "JOIN actors ON actors.login = comments.author "
        f"WHERE comments_fts MATCH :match AND actors.is_bot = 1{comment_repo}",
    ]
    if not legacy:
        arms.append(
            "SELECT COUNT(*) FROM reviews_fts "
            "JOIN reviews ON reviews.id = reviews_fts.rowid "
            "JOIN actors ON actors.login = reviews.author "
            f"WHERE reviews_fts MATCH :match AND actors.is_bot = 1{review_repo}"
        )
    params: dict[str, object] = {"match": match}
    if repo:
        params["repo"] = repo
    row = conn.execute(
        "SELECT " + " + ".join(f"({arm})" for arm in arms), params
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _hydrate_search_hit(
    conn: sqlite3.Connection,
    match: str,
    hit: sqlite3.Row,
    *,
    exclude_bots: bool = False,
) -> dict | None:
    """Materialize one merged hit: item columns, an item-text snippet when the
    item text matched, the exact matching-comment count, and the best
    matching comment's snippet when comments matched.

    `snippet` is the item-text snippet when present, else the comment
    snippet, else the review snippet (issue #72 C15): a comment-only or
    review-only match never answers an empty snippet while a matching body
    exists somewhere. The bot filter (issue #71 C6) is applied INSIDE the
    comment/review snippet lookups — before the fallback is selected — so a
    bot's comment can never feed the surfaced snippet."""
    repo = hit["repo"]
    number = int(hit["number"])
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
    rec["matching_comments"] = int(hit["matching_comments"] or 0)
    comment_snippet: str = ""
    if rec["matching_comments"]:
        comment_bots = _COMMENT_BOT_FILTER if exclude_bots else ""
        found = conn.execute(
            "SELECT snippet(comments_fts, -1, '«', '»', '…', 12) "
            "FROM comments_fts JOIN comments ON comments.pk = comments_fts.rowid "
            "WHERE comments_fts MATCH ? AND comments.repo = ? AND comments.number = ?"
            f"{comment_bots} "
            "ORDER BY bm25(comments_fts) ASC LIMIT 1",
            (match, repo, number),
        ).fetchone()
        comment_snippet = found[0] if found else ""
    rec["comment_snippet"] = comment_snippet
    rec["matching_reviews"] = int(hit["matching_reviews"] or 0)
    review_snippet: str = ""
    if rec["matching_reviews"]:
        review_bots = _REVIEW_BOT_FILTER if exclude_bots else ""
        found = conn.execute(
            "SELECT snippet(reviews_fts, -1, '«', '»', '…', 12) "
            "FROM reviews_fts JOIN reviews ON reviews.id = reviews_fts.rowid "
            "WHERE reviews_fts MATCH ? AND reviews.repo = ? AND reviews.number = ?"
            f"{review_bots} "
            "ORDER BY bm25(reviews_fts) ASC LIMIT 1",
            (match, repo, number),
        ).fetchone()
        review_snippet = found[0] if found else ""
    rec["review_snippet"] = review_snippet
    rec["snippet"] = snippet or comment_snippet or review_snippet
    return rec


def page_truncated(
    offset: int, page_len: int, limit: int, total: int | None
) -> bool:
    """The honest truncation flag for one paged search page (issue #72 AC5;
    08b review Q3): true when matches exist that this call did not return.

    That is the plain formula `offset + page_len < total` (matches remain
    after the page) PLUS the overshoot case: a page whose window ends PAST
    the end (`offset + page_len > total`) while returning fewer rows than
    asked means the offset skipped matches the caller never saw — 0 of 3 at
    offset 99 must not read as "everything shown". A page that ends exactly
    at the end stays false (nothing remains; a cursor follower must not
    loop on an empty terminal page)."""
    if not isinstance(total, int):
        return False
    if offset + page_len < total:
        return True
    return (
        total > 0
        and offset > 0
        and page_len < limit
        and offset + page_len > total
    )


def search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
    repo: str | None = None,
    *,
    offset: int = 0,
    exclude_bots: bool = False,
) -> dict:
    """Natural-language search over items and comments (issue #4).

    Tokens are quoted (never FTS5 syntax) and stemmed via the porter
    tokenizer. The result is a DEDUPLICATED STRICT-FIRST STREAM: every
    strict (all-tokens) hit precedes the broadened pass's non-duplicate hits,
    and `offset` walks that stream — later pages cannot be starved by strict
    duplicates. Within each pass the surface tiers apply (issue #72 C15):
    item-text hits above comment/review-only hits, an adjacent phrase in the
    item text above scattered tokens, then bm25, recency and (repo, number)
    tie-breaks. `matched_mode` reports "any" when the returned page carries a
    broadened-contributed row, else "all". Comment hits merge into their
    parent item with `matching_comments` and `comment_snippet`; `snippet`
    falls back item text -> comment -> review so a comment-only match never
    answers empty.

    Counts are limit-independent (issue #72 C14): `strict_matches` and
    `broadened_matches` come from COUNT queries over each pass (the broadened
    COUNT runs whenever the strict pass ran, even when its rows did not fill
    the page), and `total_matches` is the broadened total — the widest set a
    wider page could reach. `truncated` is true when matches exist that the
    page did not return (page_truncated: more remain after the page, or the
    offset overshot matches the caller never saw; the adapters promote it to
    the envelope).
    `exclude_bots` (issue #71 C6) filters bot-authored item/comment/review
    rows OUT of the match SQL — pre-ranking, so counts, snippets and paging
    are all computed over the filtered set — and reports the excluded
    bot-authored matches as `bots_omitted`. A bot-authored ITEM is omitted
    however it matched (own text, a human comment, a human review — the
    08b review Q1 rule): its row never surfaces under the bot's title.
    `corpus_items` is the scoped item
    count and `index_stale` flags a store predating the current schema (reads
    never migrate)."""
    repo = _fold_repo(repo)
    limit = _clamp_limit(limit)
    offset = max(0, int(offset))
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if exclude_bots:
        _require_bot_flags(conn)
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
            "strict_matches": 0,
            "broadened_matches": 0,
            "truncated": False,
            "offset": offset,
            "corpus_items": corpus_items,
            "index_stale": index_stale,
        }
    # Feature gate, not staleness (issue #25): the v7 search arms
    # (paths_text/reviews_fts) exist on any store at or past slot 7, so the
    # legacy decision pins _V7_USER_VERSION — a v7 store keeps those arms
    # after the build moves to v8. `index_stale` above keeps its own
    # current-schema meaning and stays a separate signal.
    legacy = user_version < _V7_USER_VERSION
    phrase = build_phrase_query(query)
    # The stream must cover offset+limit rows; every strict hit precedes the
    # broadened non-duplicates, so the strict pass fetches the whole window
    # and the broadened pass fills only what the strict set cannot.
    window = offset + limit
    strict_total, strict_rows = _run_search_pass(
        conn, all_query, repo, window, legacy=legacy,
        exclude_bots=exclude_bots, phrase=phrase,
    )
    if any_query != all_query:
        # The broadened COUNT runs whenever the strict pass ran, even when
        # the strict rows already fill the page (issue #72 C14): counts are
        # limit-independent by construction.
        broadened_total = _search_pass_total(
            conn, any_query, repo, legacy=legacy, exclude_bots=exclude_bots
        )
    else:
        broadened_total = strict_total
    total = broadened_total
    # The deduplicated stream: (match, row, strict?) in final order. The
    # strict rows keep their block so hydration uses the all-tokens match
    # (snippets show every term); broadened extras hydrate against the
    # broadened match.
    stream: list[tuple[str, sqlite3.Row, bool]] = [
        (all_query, row, True) for row in strict_rows
    ]
    if any_query != all_query and strict_total < window:
        need = window - len(strict_rows)
        # Fetch enough broadened rows that removing the strict duplicates
        # still leaves `need` fresh ones (broadened ⊇ strict at the item
        # level, so strict_total duplicates are the worst case).
        _b_total, or_rows = _run_search_pass(
            conn, any_query, repo, strict_total + need, legacy=legacy,
            exclude_bots=exclude_bots, phrase=phrase, known_total=broadened_total,
        )
        seen = {(row["repo"], int(row["number"])) for row in strict_rows}
        extra = [
            row
            for row in or_rows
            if (row["repo"], int(row["number"])) not in seen
        ][:need]
        stream.extend((any_query, row, False) for row in extra)
    page = stream[offset : offset + limit]
    items = []
    for block_match, hit, _strict in page:
        rec = _hydrate_search_hit(
            conn, block_match, hit, exclude_bots=exclude_bots
        )
        if rec is not None:
            items.append(rec)
    result: dict = {
        "items": items,
        "matched_mode": "any" if any(not s for _, _, s in page) else "all",
        "total_matches": total,
        "strict_matches": strict_total,
        "broadened_matches": broadened_total,
        "truncated": page_truncated(offset, len(items), limit, total),
        "offset": offset,
        "corpus_items": corpus_items,
        "index_stale": index_stale,
    }
    if exclude_bots:
        widest = any_query if any_query != all_query else all_query
        result["bots_omitted"] = _search_bots_omitted(
            conn, widest, repo, legacy=legacy
        )
    return result


def _edges_source_available(conn: sqlite3.Connection) -> bool:
    """True when the `edges` table carries the v5 `source` column.

    Read commands open existing databases read-only and never migrate
    (mirrors search's index_stale contract): a pre-v5 database has no
    `source` column, so edge projections must degrade instead of raising
    `no such column` on the upgrade-then-query path."""
    return any(row[1] == "source" for row in conn.execute("PRAGMA table_info(edges)"))


def _item_edge_rows(
    conn: sqlite3.Connection, repo: str, number: int | str, source_available: bool
) -> list[sqlite3.Row]:
    """Edges incident to one item, as two indexed equality seeks.

    The historic single OR predicate is served by a repo-prefix scan of
    `edges`; the UNION ALL of the two equality arms plans as full-key
    searches on idx_edges_src / idx_edges_dst. The dst arm excludes rows the
    src arm already returned — a self-incident edge with src = dst = number
    would otherwise appear twice — so the row multiset matches the old OR
    query exactly. No result order is promised beyond UNION ALL's
    arm-then-arm emission. Callers pass `source_available` (whether the
    edges table carries the v5 `source` column) so this probe is not
    re-run per frontier node on the related() BFS."""
    cols = "src_type, src_id, rel, dst_type, dst_id, confidence, evidence"
    if source_available:
        cols += ", source"
    nid = str(number)
    return conn.execute(
        f"SELECT {cols} FROM edges "
        "WHERE repo = ? AND src_type = 'item' AND src_id = ? "
        "UNION ALL "
        f"SELECT {cols} FROM edges "
        "WHERE repo = ? AND dst_type = 'item' AND dst_id = ? "
        "AND NOT (src_type = 'item' AND src_id = ?)",
        (repo, nid, repo, nid, nid),
    ).fetchall()


def item(
    conn: sqlite3.Connection,
    number: int,
    repo: str | None = None,
    *,
    exclude_bots: bool = False,
) -> dict | None:
    repo = _fold_repo(repo)
    if exclude_bots:
        # Gate BEFORE the lookup (cubic round 2): a missing item with
        # exclude_bots on a pre-v12 store answers the same structured
        # bad_request as search/brief, never a misleading not_found.
        _require_bot_flags(conn)
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
    # Bot exclusion (issue #71 C6 / plan CF-D) filters comments and reviews
    # IN THE QUERY, before any omission counting: the rows never reach the
    # payload, and bots_omitted counts exactly the excluded comment+review
    # rows. NOT EXISTS keeps NULL-author rows (not established bots — FB-005).
    comment_bots = _COMMENT_BOT_FILTER if exclude_bots else ""
    review_bots = _REVIEW_BOT_FILTER if exclude_bots else ""
    rec["labels"] = [
        _row_to_dict(x)
        for x in conn.execute(
            "SELECT name, color FROM labels WHERE repo = ? AND number = ?", (r, n)
        ).fetchall()
    ]
    rec["comments"] = [
        _row_to_dict(x)
        for x in conn.execute(
            "SELECT comments.pk AS pk, comments.github_id AS github_id,"
            " comments.kind AS kind, comments.author AS author,"
            " comments.created_at AS created_at, comments.body AS body,"
            " comments.html_url AS html_url "
            "FROM comments "
            f"WHERE comments.repo = ? AND comments.number = ?{comment_bots} "
            "ORDER BY created_at",
            (r, n),
        ).fetchall()
    ]
    rec["reviews"] = [
        _row_to_dict(x)
        for x in conn.execute(
            "SELECT reviews.id AS id, reviews.author AS author,"
            " reviews.state AS state, reviews.submitted_at AS submitted_at,"
            " reviews.body AS body, reviews.html_url AS html_url "
            "FROM reviews "
            f"WHERE reviews.repo = ? AND reviews.number = ?{review_bots} "
            "ORDER BY submitted_at",
            (r, n),
        ).fetchall()
    ]
    if exclude_bots:
        omitted = conn.execute(
            "SELECT"
            " (SELECT COUNT(*) FROM comments c JOIN actors a ON a.login = c.author"
            " WHERE c.repo = ? AND c.number = ? AND a.is_bot = 1)"
            " + (SELECT COUNT(*) FROM reviews v JOIN actors a ON a.login = v.author"
            " WHERE v.repo = ? AND v.number = ? AND a.is_bot = 1)",
            (r, n, r, n),
        ).fetchone()[0]
        rec["bots_omitted"] = int(omitted)
    rec["files"] = [
        _row_to_dict(x)
        for x in conn.execute(
            "SELECT path, status, additions, deletions, changes "
            "FROM pr_files WHERE repo = ? AND number = ? ORDER BY path",
            (r, n),
        ).fetchall()
    ]
    source_available = _edges_source_available(conn)
    rec["edges"] = [
        _row_to_dict(x) for x in _item_edge_rows(conn, r, n, source_available)
    ]
    # Mirrors search(): a pre-v5 database returns edges without provenance
    # and is flagged, on both polarities, so consumers get a stable envelope.
    rec["index_stale"] = not source_available
    return rec


def _parse_instant(value: str) -> datetime:
    """Parse a GitHub timestamp or user-supplied --as-of value into an aware
    UTC instant. Z-suffixed and mixed offsets both normalize; naive input is
    rejected (the caller turns that into a coded error)."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp is not timezone-aware: {value!r}")
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError as exc:
        # Boundary instants (e.g. 9999-12-31T23:59:59-01:00) overflow the
        # UTC conversion; normalize to the same coded-error family.
        raise ValueError(f"timestamp out of range: {value!r}") from exc


def _parse_instant_lenient(value: str | None) -> datetime | None:
    """Best-effort instant for stored window boundaries; a malformed stored
    stamp makes the row non-matching instead of failing the whole query."""
    if not value:
        return None
    try:
        return _parse_instant(value)
    except (ValueError, OverflowError):
        return None


_AS_OF_BASIS = {
    "windows": (
        "GitHub times, never the sync wall clock: items and comments run on"
        " updated_at else created_at; reviews on submitted_at, falling back"
        " to the owning item's created_at when NULL"
    ),
    "comparison": "aware UTC instants (mixed offsets and Z both accepted)",
    "selection": (
        "strict [valid_from, valid_to) containment; among overlapping"
        " revisions the highest id wins (most recently displaced); live"
        " validity starts at the latest revision's valid_to by sequence,"
        " else the entity's current-version timestamp"
    ),
    "first_sighting": (
        "a store-recorded version is treated as valid from its GitHub"
        " current-version time; text first seen later than T is absent at T"
    ),
}

_HISTORY_NOTE = "store predates revision capture (schema v8); no history was recorded"


def _revision_rows(
    conn: sqlite3.Connection, repo: str, number: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, entity, entity_id, kind, origin, content, valid_from, valid_to"
        " FROM revisions WHERE repo = ? AND number = ? ORDER BY id",
        (repo, number),
    ).fetchall()


def _matching_revision(
    revisions: list[sqlite3.Row], at: datetime
) -> sqlite3.Row | None:
    """Strict [valid_from, valid_to) containment; highest id wins among
    overlapping windows (equal/backwards GitHub timestamps)."""
    best: sqlite3.Row | None = None
    for row in revisions:
        start = _parse_instant_lenient(row["valid_from"])
        end = _parse_instant_lenient(row["valid_to"])
        if start is None or end is None:
            continue
        if start <= at < end and (best is None or row["id"] > best["id"]):
            best = row
    return best


def _live_start(revisions: list[sqlite3.Row], current_stamp: str | None) -> datetime | None:
    """Live validity start for one entity (issue #25): the LATEST valid_to
    across the entity's revisions — by instant, not insert sequence, so a
    displaced version whose valid_to regresses cannot back-date the live
    text — else the entity's current-version timestamp (updated_at else
    created_at; reviews' submitted_at fallback is resolved by the caller)."""
    ends = [
        instant
        for instant in (_parse_instant_lenient(row["valid_to"]) for row in revisions)
        if instant is not None
    ]
    if ends:
        return max(ends)
    return _parse_instant_lenient(current_stamp)


def _revision_version(row: sqlite3.Row) -> dict:
    version = {
        "version_source": "revision",
        "revision_id": row["id"],
        "entity": row["entity"],
        "entity_id": row["entity_id"],
        "kind": row["kind"],
        "origin": row["origin"],
        "content": row["content"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
    }
    return version


def _versioned_text(
    revisions: list[sqlite3.Row],
    live_text: str | None,
    live_stamp: str | None,
    at: datetime,
) -> tuple[str | None, dict]:
    """As-of text for one entity plus its source-tagged version object.
    Returns (None, absent-marker) when no version was valid at T."""
    match = _matching_revision(revisions, at)
    if match is not None:
        return match["content"], _revision_version(match)
    start = _live_start(revisions, live_stamp)
    if start is not None and at >= start:
        return live_text, {
            "version_source": "live",
            "entity": revisions[-1]["entity"] if revisions else None,
            "origin": "live",
        }
    return None, {"version_source": None, "origin": "no_version"}


def item_history(
    conn: sqlite3.Connection,
    number: int,
    repo: str | None = None,
    *,
    exclude_bots: bool = False,
) -> dict | None:
    """`item N --history` (issue #25): the standard item envelope (same
    nested collections as `item`) plus `revisions`, the item's revision
    sequence ordered by id. Pre-v8 stores read-degrade with an honest note;
    the plain (no-flag) item surface is untouched. `exclude_bots` (issue #71
    C6) carries into the underlying item fetch."""
    rec = item(conn, number, repo=repo, exclude_bots=exclude_bots)
    if rec is None:
        return None
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if user_version < _V8_USER_VERSION:
        rec["revisions"] = []
        rec["history_note"] = _HISTORY_NOTE
        return rec
    rec["revisions"] = [
        {
            "id": row["id"],
            "entity": row["entity"],
            "entity_id": row["entity_id"],
            "kind": row["kind"],
            "origin": row["origin"],
            "content": row["content"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
        }
        for row in _revision_rows(conn, rec["repo"], rec["number"])
    ]
    return rec


def item_as_of(
    conn: sqlite3.Connection,
    number: int,
    at: datetime,
    repo: str | None = None,
    *,
    exclude_bots: bool = False,
) -> dict | None:
    """`item N --as-of T` (issue #25): every text entity's version whose
    `[valid_from, valid_to)` contains T (or the live version when T is at or
    past its validity start), with as_of_basis disclosing every window rule.
    Edges and index_stale are omitted: edges carry no recorded history, so
    presenting the current graph beside an as-of text view would conflate
    two time bases. Pre-v8 stores reconstruct from live rows only, with the
    same note as --history. `exclude_bots` (issue #71 C6) carries into the
    underlying item fetch."""
    base = item(conn, number, repo=repo, exclude_bots=exclude_bots)
    if base is None:
        return None
    rec: dict = {k: v for k, v in base.items() if k not in ("edges", "index_stale")}
    rec["as_of"] = at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec["as_of_basis"] = dict(_AS_OF_BASIS)
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if user_version < _V8_USER_VERSION:
        rec["history_note"] = _HISTORY_NOTE
        rec["title_version"] = {"version_source": "live", "origin": "live"}
        rec["body_version"] = {"version_source": "live", "origin": "live"}
        return rec
    r = rec["repo"]
    n = rec["number"]
    revisions = _revision_rows(conn, r, n)
    by_entity: dict = defaultdict(list)
    for row in revisions:
        key = (row["entity"], row["entity_id"], row["kind"])
        by_entity[key].append(row)

    absent: list[dict] = []
    title_text, title_version = _versioned_text(
        by_entity[("item_title", None, None)], rec.get("title"),
        rec.get("updated_at") or rec.get("created_at"), at,
    )
    rec["title"] = title_text
    rec["title_version"] = title_version
    if title_version["version_source"] is None:
        absent.append({"entity": "item_title", "entity_id": None, "kind": None,
                       "version": None, "origin": "no_version"})
    body_text, body_version = _versioned_text(
        by_entity[("item_body", None, None)], rec.get("body"),
        rec.get("updated_at") or rec.get("created_at"), at,
    )
    rec["body"] = body_text
    rec["body_version"] = body_version
    if body_version["version_source"] is None:
        absent.append({"entity": "item_body", "entity_id": None, "kind": None,
                       "version": None, "origin": "no_version"})

    as_of_comments: list[dict] = []
    # Live-comment updated_at is not part of the plain item projection (AC6
    # byte-identity), so the as-of path fetches the current-version stamps
    # separately rather than widening the shared SELECT.
    comment_stamps = {
        row["pk"]: row["updated_at"]
        for row in conn.execute(
            "SELECT pk, updated_at FROM comments WHERE repo = ? AND number = ?",
            (r, n),
        )
    }
    for comment in rec.get("comments") or []:
        key = ("comment", comment["github_id"], comment["kind"])
        rows = by_entity.get(key, [])
        text, version = _versioned_text(
            rows, comment.get("body"),
            comment_stamps.get(comment["pk"]) or comment.get("created_at"), at,
        )
        if version["version_source"] is None:
            absent.append(
                {
                    "entity": "comment",
                    "entity_id": comment["github_id"],
                    "kind": comment["kind"],
                    "version": None,
                    "origin": (rows[-1]["origin"] if rows else "no_version"),
                }
            )
            continue
        as_of_comment = dict(comment)
        as_of_comment["body"] = text
        as_of_comment.update(
            {k: v for k, v in version.items() if k in ("version_source", "revision_id", "origin")}
        )
        as_of_comments.append(as_of_comment)
    # Deleted comments (no live row) whose tombstone window contains T come
    # back as revision-only objects; identity fields come from the revision.
    live_pairs = {(c["kind"], c["github_id"]) for c in rec.get("comments") or []}
    for key, rows in by_entity.items():
        entity, entity_id, kind = key
        if entity != "comment" or (kind, entity_id) in live_pairs:
            continue
        match = _matching_revision(rows, at)
        if match is None:
            # No version of this deleted entity was valid at T — reported
            # absent with the window that ended, never silently dropped.
            absent.append(
                {
                    "entity": "comment",
                    "entity_id": entity_id,
                    "kind": kind,
                    "version": None,
                    "origin": (rows[-1]["origin"] if rows else "no_version"),
                }
            )
            continue
        as_of_comments.append(
            {
                "github_id": entity_id,
                "kind": kind,
                "author": None,
                "created_at": match["valid_from"],
                "body": match["content"],
                "html_url": None,
                "version_source": "revision",
                "revision_id": match["id"],
                "origin": match["origin"],
            }
        )
    rec["comments"] = as_of_comments

    as_of_reviews: list[dict] = []
    for review in rec.get("reviews") or []:
        key = ("review", review["id"], None)
        rows = by_entity.get(key, [])
        text, version = _versioned_text(
            rows, review.get("body"),
            review.get("submitted_at") or rec.get("created_at"), at,
        )
        if version["version_source"] is None:
            absent.append(
                {
                    "entity": "review",
                    "entity_id": review["id"],
                    "kind": None,
                    "version": None,
                    "origin": (rows[-1]["origin"] if rows else "no_version"),
                }
            )
            continue
        as_of_review = dict(review)
        as_of_review["body"] = text
        as_of_review.update(
            {k: v for k, v in version.items() if k in ("version_source", "revision_id", "origin")}
        )
        as_of_reviews.append(as_of_review)
    live_review_ids = {rv["id"] for rv in rec.get("reviews") or []}
    for key, rows in by_entity.items():
        entity, entity_id, kind = key
        if entity != "review" or entity_id in live_review_ids:
            continue
        match = _matching_revision(rows, at)
        if match is None:
            absent.append(
                {
                    "entity": "review",
                    "entity_id": entity_id,
                    "kind": None,
                    "version": None,
                    "origin": (rows[-1]["origin"] if rows else "no_version"),
                }
            )
            continue
        as_of_reviews.append(
            {
                "id": entity_id,
                "author": None,
                "state": None,
                "submitted_at": match["valid_from"],
                "body": match["content"],
                "html_url": None,
                "version_source": "revision",
                "revision_id": match["id"],
                "origin": match["origin"],
            }
        )
    rec["reviews"] = as_of_reviews
    rec["absent_entities"] = absent
    return rec


#: Default budgets for related() (issue #71 C7 / plan CF-D): the expansion
#: stops at this many nodes or this much accumulated serialized payload
#: (JSON-length of the collected edge and node rows), whichever trips first.
_RELATED_MAX_NODES = 100
_RELATED_MAX_BYTES = 1048576


def _serialized_size(rec: dict) -> int:
    """Deterministic serialized size of one graph row for the byte budget."""
    return len(json.dumps(rec, sort_keys=True, ensure_ascii=True, default=str))


def related(
    conn: sqlite3.Connection,
    number: int,
    depth: int = 1,
    repo: str | None = None,
    *,
    max_nodes: int = _RELATED_MAX_NODES,
    max_bytes: int = _RELATED_MAX_BYTES,
) -> dict:
    """The item's graph neighborhood (BFS over stored edges).

    Bounded expansion (issue #71 C7 / plan CF-D): the walk stops once it has
    collected `max_nodes` distinct item nodes (the queried hub included) or
    the accumulated serialized size of the collected edge rows exceeds
    `max_bytes` (1 MiB by default; node rows also count against what
    remains, with the hub node always emitted), and reports
    `truncated: true` in the data when either budget cut something. The
    truncation order is deterministic: nodes are discovered and emitted
    breadth-first from the hub in edge order (src endpoint before dst), edges
    in discovery order, and the budgets simply stop the walk — the survivors
    are always a prefix of that order, so two calls at the same budget return
    the same neighborhood."""
    repo = _fold_repo(repo)
    if repo is None:
        row = conn.execute("SELECT repo FROM items WHERE number = ?", (number,)).fetchone()
        if row is None:
            # A ghost input (issue #72 C11): an unstored number answers
            # LookupError so the runners map it to not_found/exit 1 — never
            # an ok:true empty neighborhood.
            raise LookupError(f"item #{number} is not stored")
        repo = row["repo"]
    elif conn.execute(
        "SELECT 1 FROM items WHERE repo = ? AND number = ?", (repo, number)
    ).fetchone() is None:
        raise LookupError(f"item #{number} is not stored")
    budget_nodes = max(1, int(max_nodes))
    budget_bytes = max(1, int(max_bytes))
    seen_edges: list[dict] = []
    edges_bytes = 0
    frontier = [str(number)]
    visited = {str(number)}
    # Discovery order: hub first, then breadth-first neighbors — the
    # deterministic node order the budget cuts into.
    order: list[str] = [str(number)]
    truncated = False
    stop = False
    source_available = _edges_source_available(conn)
    index_stale = not source_available
    for _ in range(max(1, depth)):
        if stop:
            break
        nxt: list[str] = []
        for nid in frontier:
            if stop:
                break
            rows = _item_edge_rows(conn, repo, nid, source_available)
            for row in rows:
                rec = _row_to_dict(row)
                size = _serialized_size(rec)
                if edges_bytes + size > budget_bytes:
                    truncated = True
                    stop = True
                    break
                seen_edges.append(rec)
                edges_bytes += size
                for typ, ident in ((rec["src_type"], rec["src_id"]), (rec["dst_type"], rec["dst_id"])):
                    # Foreign repo-qualified ids are frontier dead ends: they
                    # carry no local edges of their own beyond the mirror row,
                    # and expanding them at depth >= 2 would connect unrelated
                    # local items through a third-party hub.
                    if typ == "item" and ident not in visited and ident.isdigit():
                        if len(order) >= budget_nodes:
                            truncated = True
                            stop = True
                            break
                        visited.add(ident)
                        order.append(ident)
                        nxt.append(ident)
                if stop:
                    break
        frontier = nxt
    nodes = []
    nodes_bytes = 0
    for index, nid in enumerate(order):
        # Repo-qualified foreign cross-reference ids (owner/repo#N, issue #6)
        # have no local items row and are not numeric: surface them as
        # unresolved placeholder nodes instead of crashing on int().
        try:
            number_value = int(nid)
        except ValueError:
            node = {"number": None, "id": nid, "kind": None, "title": None, "state": None}
        else:
            it = conn.execute(
                "SELECT number, kind, title, state FROM items WHERE repo = ? AND number = ?",
                (repo, number_value),
            ).fetchone()
            node = (
                _row_to_dict(it)
                if it
                else {"number": number_value, "kind": None, "title": None, "state": None}
            )
        if index > 0:
            size = _serialized_size(node)
            if nodes_bytes + size > budget_bytes - edges_bytes:
                truncated = True
                break
            nodes_bytes += size
        nodes.append(node)
    # Nodes the budget cut are gone from the payload; edges still naming them
    # would dangle (FB-015). Drop those edges — the fragment stays internally
    # consistent, and truncated:true already discloses the cut.
    emitted = {str(node["number"]) for node in nodes}
    def _endpoint_cut(kind: str, ident) -> bool:
        # Only a LOCAL item endpoint can dangle (its node was budget-cut);
        # repo-qualified foreign ids were never node candidates and actor/
        # file/commit endpoints live outside nodes[] by shape (FB-015).
        return (
            kind == "item"
            and isinstance(ident, str)
            and ident.isdigit()
            and ident not in emitted
        )
    seen_edges = [
        edge
        for edge in seen_edges
        if not (
            _endpoint_cut(edge["src_type"], edge["src_id"])
            or _endpoint_cut(edge["dst_type"], edge["dst_id"])
        )
    ]
    return {
        "number": number,
        "repo": repo,
        "nodes": nodes,
        "edges": seen_edges,
        "index_stale": index_stale,
        "truncated": truncated,
    }


def churn(
    conn: sqlite3.Connection,
    limit: int = 30,
    *,
    merged_only: bool = False,
    repo: str | None = None,
) -> list[dict]:
    """Files ranked by PR touch count. ``merged_only`` (default ``False`` =
    the historical behavior: every PR with stored files counts) joins
    ``items`` and keeps only rows whose PR actually merged, so a
    closed-without-merging PR's files drop out (issue #30 AC7). Keyword-
    only after ``limit``: a positional third argument used to bind
    ``repo`` onto ``merged_only`` silently (pr45 review K03)."""
    repo = _fold_repo(repo)
    sql = """
        SELECT pf.path AS path, COUNT(*) AS prs,
               SUM(pf.additions) AS additions, SUM(pf.deletions) AS deletions
        FROM pr_files pf
    """
    if merged_only:
        sql += " JOIN items i ON i.repo = pf.repo AND i.number = pf.number"
    clauses: list[str] = []
    params: list[object] = []
    if repo:
        clauses.append("pf.repo = ?")
        params.append(repo)
    if merged_only:
        clauses.append("i.merged_at IS NOT NULL")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " GROUP BY pf.path ORDER BY prs DESC, path ASC LIMIT ?"
    params.append(_clamp_limit(limit))
    return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def _linked_open_pr_numbers(
    conn: sqlite3.Connection, repo: str, numbers: list[int]
) -> dict[int, list[int]]:
    """Open PRs linked to each item: ``linked_pr`` edges the item owns plus
    ``closes_keyword``/``closes`` edges from OPEN PRs pointing at it (the
    same rule the triage ``linked_pr`` gate uses; numbers only). The input
    is chunked at 400 like the labels/assignees enrichment — the unlimited
    ``open`` mode passes every open item's number here, and one placeholder
    per item would cross SQLite's host-parameter bound on large stores
    (pr45 review K02)."""
    out: dict[int, list[int]] = {}
    for start in range(0, len(numbers), 400):
        chunk = numbers[start : start + 400]
        # edges src_id/dst_id are TEXT even for item numbers — bind strings.
        ids = [str(n) for n in chunk]
        placeholders = ",".join("?" * len(ids))
        for row in conn.execute(
            "SELECT CAST(e.src_id AS INTEGER) AS number,"
            " CAST(e.dst_id AS INTEGER) AS pr"
            " FROM edges e JOIN items pr ON pr.repo = e.repo AND pr.number = CAST(e.dst_id AS INTEGER)"
            f" WHERE e.repo = ? AND e.src_type = 'item' AND e.dst_type = 'item'"
            f" AND e.rel = 'linked_pr' AND e.src_id IN ({placeholders})"
            " AND pr.kind = 'pr' AND pr.state = 'open'",
            [repo, *ids],
        ):
            out.setdefault(row["number"], []).append(row["pr"])
        for row in conn.execute(
            "SELECT CAST(e.dst_id AS INTEGER) AS number,"
            " CAST(e.src_id AS INTEGER) AS pr"
            " FROM edges e JOIN items pr ON pr.repo = e.repo AND pr.number = CAST(e.src_id AS INTEGER)"
            f" WHERE e.repo = ? AND e.src_type = 'item' AND e.dst_type = 'item'"
            f" AND e.rel IN ('closes_keyword', 'closes') AND e.dst_id IN ({placeholders})"
            " AND pr.kind = 'pr' AND pr.state = 'open'",
            [repo, *ids],
        ):
            out.setdefault(row["number"], []).append(row["pr"])
    return {n: sorted(set(prs)) for n, prs in out.items()}


def open_items(
    conn: sqlite3.Connection,
    limit: int | None = None,
    offset: int = 0,
    *,
    kind: str | None = None,
    label: str | None = None,
    repo: str | None = None,
) -> list[dict]:
    """Open issues and PRs, newest first, enriched with ``labels`` (ordered
    by name), ``assignees`` (logins) and ``linked_open_prs`` (open PRs
    linked per the ``linked_pr``/open-closer rule above; numbers only).
    ``kind`` filters to ``issue``/``pr``; ``label`` keeps items carrying
    that exact label name. ``offset`` applies even when ``limit`` is None
    (the no-clause-preserving default governs the LIMIT only); a non-positive
    limit is clamped, never forwarded (a negative LIMIT means unlimited in
    SQLite). Keyword-only after ``offset``: positional flags past the
    documented page window used to bind ``repo`` onto ``kind`` and fail
    confusingly (pr45 review K03)."""
    if kind is not None and kind not in ("issue", "pr"):
        raise ValueError(f"kind must be 'issue' or 'pr', not {kind!r}")
    repo = _fold_repo(repo)
    sql = """
        SELECT repo, number, kind, title, author, updated_at, html_url
        FROM items WHERE state = 'open'
    """
    params: list[object] = []
    if repo:
        sql += " AND repo = ?"
        params.append(repo)
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    if label is not None:
        sql += (
            " AND EXISTS (SELECT 1 FROM labels l"
            " WHERE l.repo = items.repo AND l.number = items.number AND l.name = ?)"
        )
        params.append(label)
    # (updated_at, number) is a total order, so offset pages are stable.
    sql += " ORDER BY updated_at DESC, number DESC"
    offset = max(0, int(offset))
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([_clamp_limit(limit), offset])
    elif offset:
        # LIMIT -1 OFFSET n is SQLite's "all rows, skip n"; OFFSET without
        # LIMIT is not valid SQLite syntax.
        sql += " LIMIT -1 OFFSET ?"
        params.append(offset)
    rows = [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]

    # Row enrichment, batched per repo over the returned page only.
    labels_by: dict[tuple[str, int], list[str]] = {}
    assignees_by: dict[tuple[str, int], list[str]] = {}
    linked_by: dict[tuple[str, int], list[int]] = {}
    page: dict[tuple[str, int], dict] = {(r["repo"], r["number"]): r for r in rows}
    by_repo: dict[str, list[int]] = {}
    for row_repo, number in page:
        by_repo.setdefault(row_repo, []).append(number)
    # The enrichment tables are v7/v10 additions; reads never migrate, so a
    # store several slots behind keeps the base listing with empty enrichment
    # instead of failing the whole command (db.py's reads-never-migrate rule).
    have_enrichment = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        " AND name IN ('assignees', 'item_signatures')"
    ).fetchone()[0] >= 2
    for row_repo, numbers in by_repo.items():
        if not have_enrichment:
            continue
        for start in range(0, len(numbers), 400):
            chunk = numbers[start : start + 400]
            placeholders = ",".join("?" * len(chunk))
            for lr in conn.execute(
                "SELECT number, name FROM labels"
                f" WHERE repo = ? AND number IN ({placeholders})"
                " ORDER BY number, name",
                [row_repo, *chunk],
            ):
                labels_by.setdefault((row_repo, lr["number"]), []).append(lr["name"])
            for ar in conn.execute(
                "SELECT number, login FROM assignees"
                f" WHERE repo = ? AND number IN ({placeholders})"
                " ORDER BY number, login",
                [row_repo, *chunk],
            ):
                assignees_by.setdefault((row_repo, ar["number"]), []).append(
                    ar["login"]
                )
        for number, prs in _linked_open_pr_numbers(conn, row_repo, numbers).items():
            linked_by[(row_repo, number)] = prs
    for key, row in page.items():
        row["labels"] = labels_by.get(key, [])
        row["assignees"] = assignees_by.get(key, [])
        row["linked_open_prs"] = linked_by.get(key, [])
    return rows


#: Relations the `path` BFS traverses. Every relation the extractors emit
#: (extract.RELATIONS) is either structural here or in PATH_EXCLUDED_RELATIONS
#: — the Phase 4.2 guardrail test (tests/test_relation_registry.py) pins that
#: partition so a new relation cannot ship silently unhandled. `merged_by` is
#: excluded like the other actor relations: one login hub would join
#: unrelated items.
STRUCTURAL = frozenset(
    {
        "touches",
        "closes",
        "closes_keyword",
        "mentions",
        "cross_referenced",
        "closed_by_commit",
        "merged_commit",
        "reverts",
        # v9 (issue #26): a PR's own branch name links it to its issue.
        "branch_ref",
        # v10 (issue #27): GitHub-reported links and the duplicate text stream.
        "blocked_by",
        "child_of",
        "duplicate_of",
        "linked_pr",
        "duplicate_keyword",
    }
)
PATH_EXCLUDED_RELATIONS = frozenset(
    {"authored", "has_label", "commented", "reviewed", "merged_by"}
)


def path_between(conn: sqlite3.Connection, a: str, b: str, repo: str | None = None) -> dict:
    """Undirected BFS over the structural relations (see STRUCTURAL).

    Traverses item, file, and commit nodes. Note the `path` CLI endpoint
    grammar parses any non-digit argument as a file, so commit and foreign
    item endpoints are reachable only as intermediate hops, not as `path`
    arguments. A numeric endpoint that is not a stored item raises
    LookupError (issue #72 C11: a ghost input answers not_found, never an
    ok:true "no path"); file endpoints keep their lookup semantics."""
    repo = _fold_repo(repo)
    if repo is None:
        row = conn.execute("SELECT repo FROM items LIMIT 1").fetchone()
        if row is None:
            return {"a": a, "b": b, "path": None, "reason": "empty graph"}
        repo = row["repo"]
    for value in (a, b):
        if value.isdigit() and conn.execute(
            "SELECT 1 FROM items WHERE repo = ? AND number = ?", (repo, int(value))
        ).fetchone() is None:
            raise LookupError(f"item {value} is not stored")

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


class SqlTimeoutError(ValueError):
    """A statement (or one of run_sql's bounded passes) exceeded its
    wall-clock deadline and was interrupted by the progress handler.

    Subclasses ValueError so existing `except ValueError` mappings still
    catch it, while the CLI/MCP adapters can single it out for the
    dedicated `timeout` error code — never `runtime` (issue #71 AC1)."""

    def __init__(self, deadline: float) -> None:
        super().__init__(
            f"sql timeout: the statement exceeded its {deadline:g}s "
            "deadline and was interrupted"
        )


def sql_deadline_from_env() -> float:
    """ZAXBYGRAPH_SQL_TIMEOUT_S parsed and clamped to (0, 600] (issue #71
    AC1). Malformed, empty or non-positive values fall back to the 5s
    default — the env var may widen the budget, never remove it."""
    raw = os.environ.get("ZAXBYGRAPH_SQL_TIMEOUT_S")
    if raw is None or not raw.strip():
        return _DEFAULT_SQL_DEADLINE_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_SQL_DEADLINE_S
    if not value > 0.0:  # also rejects NaN
        return _DEFAULT_SQL_DEADLINE_S
    return min(value, _SQL_DEADLINE_MAX_S)


def sql_max_bytes_from_env() -> int:
    """ZAXBYGRAPH_SQL_MAX_BYTES parsed and clamped positive (issue #71
    AC2). Malformed, empty or non-positive values fall back to the 1 MiB
    default."""
    raw = os.environ.get("ZAXBYGRAPH_SQL_MAX_BYTES")
    if raw is None or not raw.strip():
        return _DEFAULT_SQL_MAX_BYTES
    try:
        value = int(raw, 10)
    except ValueError:
        return _DEFAULT_SQL_MAX_BYTES
    return value if value > 0 else _DEFAULT_SQL_MAX_BYTES


def _sql_error(
    exc: sqlite3.Error, deadline_at: float, deadline: float
) -> ValueError:
    """Map a stepping sqlite3 error: the progress handler's `interrupted`
    abort becomes SqlTimeoutError (so the adapters can code it `timeout`);
    every other sqlite error keeps the historical ValueError conversion.
    The interrupt is believed only when the deadline INSTANT has actually
    passed (Stage B: comparing monotonic() against the relative duration
    was an uptime bug) — an identifier that merely contains the word
    "interrupted" (a column named interrupted_at) keeps its real
    diagnostic (FB-009)."""
    if (
        isinstance(exc, sqlite3.OperationalError)
        and "interrupted" in str(exc).lower()
        and time.monotonic() >= deadline_at
    ):
        return SqlTimeoutError(deadline)
    return ValueError(str(exc))


def _render_cell(value):
    """One result cell as it rides out of run_sql: bytes values become hex
    strings (never the sqlite3 `b'...'` repr), and an oversized TEXT cell
    is capped at _SQL_CELL_CAP chars with an explicit marker (issue #71
    AC2). Other types (ints, floats, None) pass through unchanged."""
    if isinstance(value, bytes):
        hexed = value.hex()
        if len(hexed) > _SQL_CELL_CAP:
            return hexed[:_SQL_CELL_CAP] + _SQL_CELL_MARKER
        return hexed
    if isinstance(value, str) and len(value) > _SQL_CELL_CAP:
        return value[:_SQL_CELL_CAP] + _SQL_CELL_MARKER
    return value


def _quote_ident(name: str) -> str:
    """Safely double-quote a schema-derived identifier (names come from
    sqlite_master / cursor.description, never from user SQL)."""
    return '"' + name.replace('"', '""') + '"'


def store_table_names(conn: sqlite3.Connection) -> list[str]:
    """User tables of the live store, PRAGMA-free (AGENTS.md escape-hatch
    invariant: no path through the sql surface may issue a PRAGMA, so the
    table list is read from sqlite_master instead of PRAGMA table_list)."""
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [str(row[0]) for row in rows if row[0]]


def store_tables_hint(conn: sqlite3.Connection) -> str | None:
    """Hint text listing the store's tables for a no-such-table error
    (issue #71 AC11). Shared by the CLI and MCP error-mapping sites so the
    two surfaces cannot drift."""
    names = store_table_names(conn)
    if not names:
        return None
    return "tables in this store: " + ", ".join(names)


def _store_schema(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """table -> column names, discovered from the LIVE store PRAGMA-free:
    sqlite_master for the table list, then a safely-quoted zero-row
    `SELECT * FROM "table" LIMIT 0` read through cursor.description for
    each table's columns (the default-deny authorizer denies PRAGMA, so
    table_info is not an option — and never becomes one). A table whose
    probe fails (exotic virtual tables) is simply skipped."""
    schema: dict[str, list[str]] = {}
    for table in store_table_names(conn):
        try:
            probe = conn.execute(f"SELECT * FROM {_quote_ident(table)} LIMIT 0")
        except sqlite3.Error:
            continue
        if probe.description is not None:
            cols = [d[0] for d in probe.description]
            if cols:
                schema[table] = cols
    return schema


def _statement_for_wrap(sql: str) -> str | None:
    """The statement text as an inner subquery: comments stripped (they
    would not survive re-nesting legibly), trailing BOM/';'/whitespace
    removed. None when nothing remains."""
    inner = _strip_sql_comments(sql).strip().lstrip(_BOM).strip()
    while inner.endswith(";"):
        inner = inner[:-1].rstrip()
    return inner or None


def _completion_count(
    conn: sqlite3.Connection, sql: str, deadline_at: float, deadline: float
) -> int | None:
    """Fully evaluate the statement inside SQLite as
    `SELECT COUNT(*) FROM (<statement>)` (issue #71 AC1).

    Why run_sql needs this: SQLite steps a SELECT lazily, so fetching
    `limit+1` rows alone proves nothing about the statement's total cost —
    an unbounded recursive CTE happily yields five rows and would then run
    forever on the next fetch. The wrapped count runs the SAME statement to
    completion at C speed under the SAME deadline handler: a statement
    that cannot finish inside the deadline is interrupted (progress
    handler -> OperationalError('interrupted') -> SqlTimeoutError), and a
    statement that does finish yields an honest total for the truncation
    flag. Any sqlite error other than the interrupt (e.g. an EXPLAIN
    statement that cannot nest) skips the pass — truncation was already
    detected by the over-fetch."""
    inner = _statement_for_wrap(sql)
    if inner is None:
        return None
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM ({inner})").fetchone()
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower() and time.monotonic() >= deadline_at:
            raise SqlTimeoutError(deadline) from exc
        return None
    except sqlite3.Error:
        return None
    return int(row[0]) if row is not None else None


#: FROM/JOIN table reference with an optional alias. Applied to CODE spans
#: only (comment- and literal-aware) so words inside string literals never
#: masquerade as scope.
_SCOPE_TABLE_RE = re.compile(
    r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*))?",
    re.IGNORECASE,
)

#: Keywords that may legitimately follow a table name without being an
#: alias; a captured identifier in this set is a clause keyword, not a name.
_SCOPE_ALIAS_STOPWORDS = frozenset(
    {
        "and", "asc", "as", "between", "by", "collate", "cross", "desc",
        "except", "full", "from", "glob", "group", "having", "in", "inner",
        "intersect", "is", "join", "left", "like", "limit", "match", "natural",
        "not", "on", "or", "order", "outer", "regexp", "right", "set", "union",
        "using", "values", "where", "window",
    }
)


def _scope_tables(sql: str, schema: dict[str, list[str]]) -> tuple[list[str], dict[str, str]]:
    """(real tables referenced after FROM/JOIN, alias -> canonical table).

    Only names that exist in the live schema count as scope: CTE names and
    subquery aliases are not vocabulary-bearing store tables, so they are
    conservatively ignored."""
    lower_map = {t.lower(): t for t in schema}
    code = " ".join(text for kind, text in _sql_tokens(sql) if kind == "code")
    tables: list[str] = []
    aliases: dict[str, str] = {}
    for match in _SCOPE_TABLE_RE.finditer(code):
        name = lower_map.get(match.group(1).lower())
        if name is not None and name not in tables:
            tables.append(name)
        alias = match.group(2)
        if (
            name is not None
            and alias is not None
            and alias.lower() not in _SCOPE_ALIAS_STOPWORDS
        ):
            resolved = lower_map.get(alias.lower())
            if resolved is not None:
                # An alias that collides with a real table name would be
                # ambiguous; the table name wins (conservative).
                if alias.lower() not in lower_map:
                    aliases[alias.lower()] = name
            else:
                aliases[alias.lower()] = name
    return tables, aliases


def _vocabulary_warnings(
    conn: sqlite3.Connection, sql: str, deadline_at: float, deadline: float
) -> list[dict]:
    """Out-of-vocabulary literal warnings (issue #71 AC10).

    The statement is NEVER rewritten and the guard layers are untouched:
    this pass only reads. It compares string literals found next to a
    column comparison (`col = 'lit'`, bare or table/alias-qualified) against
    the DISTINCT stored values of exactly that table.column, for the fixed
    enum columns plus every `repo` column discovered from the live schema.
    A warning fires ONLY when the literal matches no stored value — rows
    are returned unchanged either way; the warning just names the column,
    the literal and the stored vocabulary (for a repo column that includes
    the case-differing stored slug). All probes run under the same deadline
    handler as the statement itself."""
    stripped = _strip_sql_comments(sql)
    if "'" not in stripped:
        # Fast path: no string literal anywhere in the statement can be
        # compared to a vocabulary column.
        return []
    schema = _store_schema(conn)
    if not schema:
        return []
    scope, aliases = _scope_tables(sql, schema)
    if not scope:
        return []

    # Which in-scope table(s) own each bare column name: a bare reference
    # is only attributed when exactly one in-scope table has the column.
    owners: dict[str, list[str]] = defaultdict(list)
    for table in scope:
        for column in schema[table]:
            owners[column].append(table)

    warnings: list[dict] = []
    seen: set[tuple[str, str]] = set()
    stored_cache: dict[tuple[str, str], list | None] = {}

    def _stored(table: str, column: str) -> list | None:
        key = (table, column)
        if key not in stored_cache:
            try:
                rows = conn.execute(
                    f"SELECT DISTINCT {_quote_ident(column)} FROM {_quote_ident(table)}"
                    f" WHERE {_quote_ident(column)} IS NOT NULL"
                    f" LIMIT {_VOCAB_DISTINCT_LIMIT}"
                ).fetchall()
                stored_cache[key] = [row[0] for row in rows]
            except sqlite3.OperationalError as exc:
                if (
                    "interrupted" in str(exc).lower()
                    and time.monotonic() >= deadline_at
                ):
                    raise SqlTimeoutError(deadline) from exc
                stored_cache[key] = None
            except sqlite3.Error:
                # A probe we cannot run must not fabricate an "empty
                # vocabulary" warning — skip the column instead.
                stored_cache[key] = None
        return stored_cache[key]

    vocab: list[tuple[str, str]] = []
    for table, columns in schema.items():
        for column in columns:
            if column == "repo" or (table, column) in _ENUM_VOCAB_COLUMNS:
                vocab.append((table, column))

    for table, column in vocab:
        # Pure-Python regex fan-out: the progress handler cannot interrupt
        # this loop, so the wall-clock bail lives HERE (Stage B ROOT-008).
        if time.monotonic() >= deadline_at:
            raise SqlTimeoutError(deadline)
        if table not in scope:
            continue
        refs: list[str] = [table]
        refs.extend(
            alias for alias, target in aliases.items() if target == table
        )
        patterns = [
            rf"\b{re.escape(ref)}\s*\.\s*{re.escape(column)}\s*=\s*'([^']*)'"
            for ref in refs
        ]
        if owners.get(column) == [table]:
            # Bare reference: not preceded by an identifier char or a dot,
            # so `items.kind = 'x'` is not re-matched by the bare pattern.
            patterns.append(rf"(?<![\w.]){re.escape(column)}\s*=\s*'([^']*)'")
        literals: list[str] = []
        for pattern in patterns:
            for m in re.finditer(pattern, stripped, re.IGNORECASE):
                # '([^']*)' stops at the first quote: the escaped shape
                # ('it''s fine') yields the prefix 'it', a string the
                # statement never compared (FB-026). A capture whose match
                # ends right before another quote is that ambiguous prefix;
                # skip it (the token-stream literal parser is the honest
                # fix, owned by the vocabulary-pass rework).
                if stripped[m.end(): m.end() + 1] == "'":
                    continue
                literal = m.group(1)[:_SQL_CELL_CAP]
                if literal and literal not in literals:
                    literals.append(literal)
        for literal in literals[:32]:
            key = (f"{table}.{column}", literal)
            if key in seen:
                continue
            seen.add(key)
            stored = _stored(table, column)
            if stored is not None and literal not in stored:
                warnings.append(
                    {
                        "column": f"{table}.{column}",
                        "literal": literal,
                        "stored_values": stored,
                    }
                )
    return warnings


def run_sql(
    conn: sqlite3.Connection,
    sql: str,
    limit: int = 200,
    repo: str | None = None,
    deadline: float = _DEFAULT_SQL_DEADLINE_S,
    max_bytes: int = _DEFAULT_SQL_MAX_BYTES,
) -> dict:
    assert_read_sql(sql)
    limit = _clamp_fetch_limit(limit)
    deadline = float(deadline)
    if not deadline > 0.0:
        raise ValueError("deadline must be a positive number of seconds")
    max_bytes = int(max_bytes)
    if max_bytes <= 0:
        raise ValueError("max_bytes must be a positive byte budget")

    deadline_at = time.monotonic() + deadline

    def _deadline_handler() -> int:
        # sqlite3 invokes this every _SQL_PROGRESS_OPS VDBE instructions;
        # a nonzero return aborts the stepping statement with
        # OperationalError('interrupted'). One handler covers the execute,
        # the fetch loop, the vocabulary pass and the completion count.
        return 1 if time.monotonic() >= deadline_at else 0

    conn.set_progress_handler(_deadline_handler, _SQL_PROGRESS_OPS)
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql)
        except sqlite3.Error as exc:
            raise _sql_error(exc, deadline_at, deadline) from exc
        if cur.description is None:
            return {"columns": [], "rows": [], "truncated": False}
        cols = [d[0] for d in cur.description]
        rows: list[list] = []
        truncated = False
        exhausted = False
        budget_left = max_bytes
        remaining = limit + 1
        # Fetch at most limit+1 rows so we can detect truncation without
        # ever materializing the full result set (a multi-million-row
        # recursive CTE must not be pulled to exhaustion just to return
        # `limit` rows — fetchall() would step the VDBE through every row
        # before we ever get to slice it down). Chunks stay bounded; the
        # serialized-cell budget stops the pull early when a result is
        # wide rather than long (issue #71 AC2).
        while remaining > 0:
            try:
                chunk = cur.fetchmany(min(remaining, _FETCH_CHUNK))
            except sqlite3.Error as exc:
                raise _sql_error(exc, deadline_at, deadline) from exc
            if not chunk:
                exhausted = True
                break
            for row in chunk:
                rendered = [_render_cell(value) for value in row]
                # Serialized size in UTF-8 bytes (FB-001: len(str) counted
                # characters, so non-ASCII rows undercounted against a byte
                # budget), and the over-budget row is NOT returned — the
                # caller's context bound must hold for every row shipped.
                row_bytes = sum(
                    len(str(value).encode("utf-8", "replace")) for value in rendered
                )
                if row_bytes > budget_left:
                    truncated = True
                    break
                budget_left -= row_bytes
                rows.append(rendered)
                remaining -= 1
            if truncated:
                break
        if not exhausted and not truncated and len(rows) > limit:
            truncated = True
        # Positional extraction only: sqlite3.Row resolves duplicate column
        # names to the FIRST match (row["body"] on a joined SELECT returns the
        # left table's value for both positions), so name-based access silently
        # substitutes values. list(row) keeps every duplicate position honest.
        rows = rows[:limit]
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
        # Auxiliary passes share the statement's deadline (frozen AC1/C1
        # pins a runaway statement answering the coded timeout, and a
        # runaway completion pass IS the statement still running — the
        # FB-016 reviewers' "judgment call" is settled by that contract):
        # a diagnostic overrun converts the call to `timeout` exactly as
        # the fetch phase does. Rows-then-timeout was probed and rejected
        # as a frozen-contract violation; recorded in the PR ledger.
        warnings = _vocabulary_warnings(conn, sql, deadline_at, deadline)
        result: dict = {"columns": cols, "rows": rows, "truncated": truncated}
        if warnings:
            result["warnings"] = warnings
        if truncated:
            total = _completion_count(conn, sql, deadline_at, deadline)
            if total is not None:
                result["total_rows"] = total
        return result
    finally:
        conn.set_progress_handler(None, 0)


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
        "SELECT src_type, src_id, rel, dst_type, dst_id, confidence, source FROM edges"
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
                # Provenance stream of the edge itself (the "source" key above
                # is the source ENDPOINT — different concept).
                "provenance": row["source"],
            }
        )
    return {"nodes": nodes, "edges": edges}


def _require_item(conn: sqlite3.Connection, number: int, repo: str | None) -> None:
    """Raise LookupError when `number` is not a stored item (issue #7 AC4).

    The callers map this to the not_found error contract rather than
    answering with an empty result that reads like 'no matches'. A falsy
    repo is rejected outright: these queries are number-keyed, and a
    no-filter scan collapses distinct same-numbered items from different
    repos into one unlabeled answer."""
    if not repo:
        raise ValueError(
            "a repo is required for this query; pass --repo OWNER/REPO"
        )
    if repo:
        row = conn.execute(
            "SELECT number FROM items WHERE repo = ? AND number = ?", (repo, number)
        ).fetchone()
    else:
        row = conn.execute("SELECT number FROM items WHERE number = ?", (number,)).fetchone()
    if row is None:
        raise LookupError(f"item #{number} not found")


def pr_overlap(conn: sqlite3.Connection, numbers, repo: str | None = None) -> dict:
    """Shared file paths for each unordered pair of the given items.

    One pair entry per i<j combination of the caller's number list, in the
    caller's order; "shared" is the sorted intersection of the two items'
    pr_files paths ([] for disjoint pairs). Numbers naming no stored item
    raise LookupError; an existing issue simply contributes an empty file
    set."""
    numbers = [int(n) for n in numbers]
    # Order-preserving dedupe: a repeated number must not produce a
    # self-pair whose "shared" is that PR's whole file set.
    numbers = list(dict.fromkeys(numbers))
    repo = _fold_repo(repo)
    paths: dict[int, set[str]] = {}
    for n in numbers:
        if n in paths:
            continue
        _require_item(conn, n, repo)
        if repo:
            rows = conn.execute(
                "SELECT path FROM pr_files WHERE repo = ? AND number = ?", (repo, n)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT path FROM pr_files WHERE number = ?", (n,)
            ).fetchall()
        paths[n] = {r["path"] for r in rows}
    pairs = []
    for i in range(len(numbers)):
        for j in range(i + 1, len(numbers)):
            a, b = numbers[i], numbers[j]
            pairs.append({"a": a, "b": b, "shared": sorted(paths[a] & paths[b])})
    return {"pairs": pairs}


class UnknownFilePathError(Exception):
    """file-history's ghost-input refusal (issue #72 AC2/C11), shared by the
    CLI and MCP adapters: the normalized path matches no stored path
    exactly. Carries the message and a suggestion hint
    (case-insensitive/basename near-misses over the stored vocabulary,
    never a disk resolve)."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def file_path_suggestions(normalized: str, stored: list[str]) -> list[str]:
    """Up to 5 stored paths that look like `normalized` (issue #72 AC2/C11):
    equal case-insensitively, or sharing the basename. Pure string
    comparison over the stored vocabulary — nothing is resolved on disk."""
    norm_lower = normalized.lower()
    base = normalized.rsplit("/", 1)[-1].lower()
    hits = sorted(
        p
        for p in stored
        if p.lower() == norm_lower or p.rsplit("/", 1)[-1].lower() == base
    )
    return hits[:5]


def resolve_file_history_path(
    conn: sqlite3.Connection, repo: str | None, raw_path: str
) -> str:
    """The file-history input gate BOTH adapters share (issue #72 AC2; 08b
    review finding 3): backslashes normalize, then the lookup is EXACT-CASE
    against the stored pr_files vocabulary for the repo. Returns the
    normalized path on a hit; raises UnknownFilePathError on a miss with
    the suggestion hint — never a silent case-fold, never an empty-success
    answer, never a disk resolve. A None repo skips the gate and keeps the
    query layer's own repo-required guard."""
    if not repo:
        return raw_path.replace("\\", "/")
    normalized = raw_path.replace("\\", "/")
    stored = sorted(
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT path FROM pr_files WHERE repo = ?", (repo,)
        )
    )
    if normalized not in set(stored):
        suggestions = file_path_suggestions(normalized, stored)
        hint = (
            "stored paths that look similar: " + ", ".join(suggestions)
            if suggestions
            else "no stored path looks similar; churn or"
            " `sql \"SELECT DISTINCT path FROM pr_files\"` lists the"
            " stored vocabulary"
        )
        raise UnknownFilePathError(f"unknown path: {normalized}", hint)
    return normalized


def file_history(
    conn: sqlite3.Connection, path: str, limit: int = 30, repo: str | None = None
) -> dict:
    """The PRs that touched `path`, newest first, each with its closed issues.

    Newest first is items.updated_at DESC with the number as a stable
    tiebreak. closed_issues collects the PR's `closes` edges (GitHub-reported
    only); keyword_closes collects its `closes_keyword` edges separately, so
    a REST-only store whose only closure evidence is keyword-derived still
    answers truthfully (issue #31 AC1). Over-fetches limit+1 rows so callers
    can set the envelope truncated flag without a COUNT."""
    limit = _clamp_fetch_limit(limit)
    repo = _fold_repo(repo)
    if not repo:
        # Number/path-keyed like its siblings: a no-filter scan would mix
        # rows from every repo in the store (review F8).
        raise ValueError(
            "a repo is required for this query; pass --repo OWNER/REPO"
        )
    if repo:
        cur = conn.execute(
            "SELECT i.number, i.title, i.state, i.updated_at, i.html_url "
            "FROM pr_files f JOIN items i ON i.repo = f.repo AND i.number = f.number "
            "WHERE f.repo = ? AND f.path = ? AND i.kind = 'pr' "
            "ORDER BY i.updated_at DESC, i.number DESC",
            (repo, path),
        )
    else:
        cur = conn.execute(
            "SELECT i.number, i.title, i.state, i.updated_at, i.html_url "
            "FROM pr_files f JOIN items i ON i.repo = f.repo AND i.number = f.number "
            "WHERE f.path = ? AND i.kind = 'pr' "
            "ORDER BY i.updated_at DESC, i.number DESC",
            (path,),
        )
    fetched = cur.fetchmany(limit + 1)
    source_available = _edges_source_available(conn)
    source_col = ", source" if source_available else ""
    entries = []
    for row in fetched[:limit]:
        n = row["number"]
        if repo:
            closes = conn.execute(
                f"SELECT dst_id{source_col} FROM edges "
                "WHERE repo = ? AND src_type = 'item' AND src_id = ? "
                "AND rel = 'closes' AND dst_type = 'item'",
                (repo, str(n)),
            ).fetchall()
            kw_closes = conn.execute(
                f"SELECT dst_id{source_col} FROM edges "
                "WHERE repo = ? AND src_type = 'item' AND src_id = ? "
                "AND rel = 'closes_keyword' AND dst_type = 'item'",
                (repo, str(n)),
            ).fetchall()
        else:
            closes = conn.execute(
                f"SELECT dst_id{source_col} FROM edges "
                "WHERE src_type = 'item' AND src_id = ? "
                "AND rel = 'closes' AND dst_type = 'item'",
                (str(n),),
            ).fetchall()
            kw_closes = conn.execute(
                f"SELECT dst_id{source_col} FROM edges "
                "WHERE src_type = 'item' AND src_id = ? "
                "AND rel = 'closes_keyword' AND dst_type = 'item'",
                (str(n),),
            ).fetchall()
        entries.append(
            {
                "number": n,
                "title": row["title"],
                "state": row["state"],
                "updated_at": row["updated_at"],
                "html_url": row["html_url"],
                "closed_issues": sorted({int(c["dst_id"]) for c in closes}),
                "keyword_closes": sorted({int(c["dst_id"]) for c in kw_closes}),
            }
        )
    return {
        "entries": entries,
        "truncated": len(fetched) > limit,
        "index_stale": not source_available,
    }


def what_closed(conn: sqlite3.Connection, number: int, repo: str | None = None) -> dict:
    """What closed issue `number`: closing PRs and closing commits.

    The response carries the item's own `kind`, so querying a PR (which
    has no closers by definition) is distinguishable from an issue that
    nothing closed.

    PRs come from `closes` edges whose destination is this item (the source
    endpoint is the closing PR, provenance closing_ref/timeline); commits
    come from `closed_by_commit` edges sourced at this item (destination is
    the commit sha, provenance timeline). keyword_closes reports the
    keyword-derived closures (`closes_keyword` edges) separately, so a
    REST-only store whose only closure evidence is keyword-derived still
    answers truthfully (issue #31 AC1); `prs` stays GitHub-reported only.
    Every entry carries its edge's
    `source`; after reopen/re-close cycles multiple closers coexist. A
    pre-v5 database degrades with index_stale like item/related."""
    number = int(number)
    repo = _fold_repo(repo)
    _require_item(conn, number, repo)
    kind = conn.execute(
        "SELECT kind FROM items WHERE repo = ? AND number = ?", (repo, number)
    ).fetchone()["kind"]
    source_available = _edges_source_available(conn)
    source_col = ", source" if source_available else ""
    if repo:
        # INDEXED BY pins the (repo, dst_type, dst_id) seek so the plan cannot
        # drift back toward a repo-prefix scan on another SQLite build or
        # after sqlite_stat1 appears (issue #22).
        pr_rows = conn.execute(
            f"SELECT src_id, evidence{source_col} FROM edges INDEXED BY idx_edges_dst "
            "WHERE repo = ? AND src_type = 'item' AND rel = 'closes' "
            "AND dst_type = 'item' AND dst_id = ?",
            (repo, str(number)),
        ).fetchall()
        keyword_rows = conn.execute(
            f"SELECT src_id, evidence{source_col} FROM edges INDEXED BY idx_edges_dst "
            "WHERE repo = ? AND src_type = 'item' AND rel = 'closes_keyword' "
            "AND dst_type = 'item' AND dst_id = ?",
            (repo, str(number)),
        ).fetchall()
        commit_rows = conn.execute(
            f"SELECT dst_id, evidence{source_col} FROM edges "
            "WHERE repo = ? AND rel = 'closed_by_commit' AND src_type = 'item' AND src_id = ?",
            (repo, str(number)),
        ).fetchall()
    else:
        pr_rows = conn.execute(
            f"SELECT src_id, evidence{source_col} FROM edges "
            "WHERE src_type = 'item' AND rel = 'closes' "
            "AND dst_type = 'item' AND dst_id = ?",
            (str(number),),
        ).fetchall()
        keyword_rows = conn.execute(
            f"SELECT src_id, evidence{source_col} FROM edges "
            "WHERE src_type = 'item' AND rel = 'closes_keyword' "
            "AND dst_type = 'item' AND dst_id = ?",
            (str(number),),
        ).fetchall()
        commit_rows = conn.execute(
            f"SELECT dst_id, evidence{source_col} FROM edges "
            "WHERE rel = 'closed_by_commit' AND src_type = 'item' AND src_id = ?",
            (str(number),),
        ).fetchall()
    prs = []
    for row in pr_rows:
        entry = {
            "number": int(row["src_id"]),
            "evidence": row["evidence"],
        }
        if source_available:
            entry["source"] = row["source"]
        prs.append(entry)
    keyword_closes = []
    for row in keyword_rows:
        entry = {
            "number": int(row["src_id"]),
            "evidence": row["evidence"],
        }
        if source_available:
            entry["source"] = row["source"]
        keyword_closes.append(entry)
    commits = []
    for row in commit_rows:
        entry = {
            "sha": row["dst_id"],
            "evidence": row["evidence"],
        }
        if source_available:
            entry["source"] = row["source"]
        commits.append(entry)
    return {
        "number": number,
        "kind": kind,
        "prs": prs,
        "keyword_closes": keyword_closes,
        "commits": commits,
        "index_stale": not source_available,
    }


def cap_comments(data: dict, max_comments: int, repo: str | None = None) -> dict:
    """Cap `data['comments']` to `max_comments` entries (issue #31 AC4).

    Preference: the first comment, the last comment, then every maintainer
    comment (author login equals the repo slug's owner, case-insensitive);
    any remaining slots fill most-recent-first. The survivors stay
    chronological (item comments are stored ORDER BY created_at).
    `comments_omitted` reports the dropped count — 0 when nothing was
    capped, and `total` when K is 0."""
    if max_comments < 0:
        # A negative cap is a caller bug, not a truncation request: slicing
        # would silently keep total-1 comments with a misleading counter.
        raise ValueError("max_comments must be >= 0")
    data["comments_omitted"] = 0
    comments = data.get("comments")
    if not isinstance(comments, list) or len(comments) <= max_comments:
        return data
    total = len(comments)
    owner = repo.split("/", 1)[0].lower() if repo and "/" in repo else None
    maintainer = [
        i
        for i, c in enumerate(comments)
        if owner is not None and str((c or {}).get("author") or "").lower() == owner
    ]
    seen: set[int] = set()
    slots: list[int] = []
    for i in (0, total - 1, *maintainer):
        if 0 <= i < total and i not in seen:
            seen.add(i)
            slots.append(i)
    slots.extend(i for i in range(total - 1, -1, -1) if i not in seen)
    keep = set(slots[:max_comments])
    data["comments"] = [c for i, c in enumerate(comments) if i in keep]
    data["comments_omitted"] = total - len(data["comments"])
    return data


#: Default item read bounds (issue #71 C5 / plan CF-D): the plain item
#: surface bounds the item body to 20 KiB (with an explicit truncation
#: marker carrying the omitted count) and the comment/review lists to 5
#: entries each, reporting comments_omitted/reviews_omitted. Explicit
#: --max-body-chars/--max-comments flags stay authoritative when given.
_ITEM_BODY_CHARS_DEFAULT = 20 * 1024
_ITEM_COMMENTS_DEFAULT = 5
_ITEM_REVIEWS_DEFAULT = 5


def _cap_reviews(data: dict, max_reviews: int) -> None:
    """The review-list analog of cap_comments: keep the first `max_reviews`
    in stored (submitted_at) order, report reviews_omitted (always set — 0
    when nothing was capped)."""
    if max_reviews < 0:
        raise ValueError("max_reviews must be >= 0")
    reviews = data.get("reviews")
    if not isinstance(reviews, list):
        return
    if len(reviews) <= max_reviews:
        data["reviews_omitted"] = 0
        return
    data["reviews"] = reviews[:max_reviews]
    data["reviews_omitted"] = len(reviews) - max_reviews


def apply_item_bounds(
    data: dict,
    repo: str | None = None,
    *,
    max_body_chars: int | None = None,
    max_comments: int | None = None,
    max_reviews: int | None = None,
) -> bool:
    """Bound one item payload (issue #71 C5 / plan CF-D). Used by BOTH read
    adapters (CLI `item` and MCP `get_item`) so the defaults cannot drift.

    Flags given (max_body_chars/max_comments/max_reviews) keep their
    explicit-cap semantics — max_body_chars is NOT applied here (the caller
    has already run _truncate_bodies with it); flags absent fall to the
    documented defaults: body <= 20 KiB with an appended truncation marker
    carrying the omitted char count, comments <= 5 (cap_comments' first/last/
    maintainer preference), reviews <= 5. comments_omitted/reviews_omitted
    are ALWAYS set (0 when nothing was capped). Returns whether anything was
    cut, for the envelope's truncated flag."""
    if not isinstance(data, dict) or data.get("not_found"):
        # A per-row not_found marker (issue #72 C22) is not an item payload.
        return False
    cut = False
    cap = max_comments if max_comments is not None else _ITEM_COMMENTS_DEFAULT
    before = len(data.get("comments") or []) if isinstance(data.get("comments"), list) else 0
    cap_comments(data, cap, repo)
    if int(data.get("comments_omitted") or 0) > 0 or before != len(data.get("comments") or []):
        cut = True
    _cap_reviews(data, max_reviews if max_reviews is not None else _ITEM_REVIEWS_DEFAULT)
    if int(data.get("reviews_omitted") or 0) > 0:
        cut = True
    if max_body_chars is None:
        body = data.get("body")
        if isinstance(body, str) and len(body) > _ITEM_BODY_CHARS_DEFAULT:
            omitted = len(body) - _ITEM_BODY_CHARS_DEFAULT
            data["body"] = (
                body[:_ITEM_BODY_CHARS_DEFAULT]
                + f"…[body truncated: {omitted} chars omitted]"
            )
            data["body_chars_omitted"] = omitted
            data["truncated"] = True
            cut = True
    return cut
