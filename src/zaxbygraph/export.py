"""Deterministic corpus export (issue #28).

`export-corpus` writes a schema-versioned JSONL snapshot of one repo
(`zaxbygraph.corpus.v1`): a manifest line, then one canonical-JSON line per
item and per release, an optional revisions line, and an `orphan_edges`
trailer that is always the final content line.

Determinism contract (see README and the issue-#28 trace):

- Lines are canonical JSON: ``json.dumps(sort_keys=True, ensure_ascii=True,
  separators=(",", ":"))`` encoded ASCII (a UTF-8 subset; lone surrogates in
  stored mojibake become ``\\ud800``-style escapes instead of encode
  failures), terminated with ``\\n``, written to a binary target.
- Row order is content-keyed only. SQLite-assigned identities
  (``comments.pk``, ``edges.id``, ``revisions.id``) never appear in output
  and never influence order. ``comments.pk`` and ``edges.id`` are rewritten
  by the inclusive-boundary re-ingest every sync performs (child rows and
  owned edges are replaced); ``revisions.id`` is excluded as a
  SQLite-assigned surrogate even though revisions themselves are
  append-only. ``items.id``/``reviews.id``/``releases.id`` are
  GitHub-assigned stable ids and stay.
- ``corpus_sha256`` is the sha256 over the concatenated content-line bytes —
  everything after the manifest line. The manifest carries an unhashed
  ``provenance`` block (fetch_log, sync_state, meta), which is why a no-op
  sync changes manifest bytes but not the hash.
- Every edge is emitted exactly once: under its src item when the src is a
  stored local item, else under its dst item when the dst is, else in the
  trailer.
- All reads inside one export run under one ``BEGIN DEFERRED`` snapshot so a
  concurrent sync cannot split the corpus (WAL readers snapshot on first
  read). The manifest's ``fetch_log`` array streams element-wise into the
  line, so neither memory nor the dict ever materializes the whole table.
- Content order: manifest, items (by number), releases (by tag_name),
  optional revisions line, orphan trailer last.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from typing import IO, Iterator

from zaxbygraph.db import CURRENT_USER_VERSION

SCHEMA_NAME = "zaxbygraph.corpus.v1"

#: Stores older than v10 lack item_events/pr_commits (issue #27); a read-only
#: open never migrates, so exporting them would either traceback on missing
#: tables or silently drop sections. Refuse instead; a re-run of sync
#: migrates in place.
MIN_EXPORT_VERSION = 10

INCLUDE_CHOICES = ("raw", "patches", "revisions")

_EDGE_COLUMNS = "src_type, src_id, rel, dst_type, dst_id, source, evidence"
_EDGE_ORDER = "src_type, src_id, rel, dst_type, dst_id, source"


class ExportError(Exception):
    """A refusal before or during export; ``code`` names the error key the
    CLI renders, ``exit_code`` is the process exit."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code


def canonical(obj: object) -> bytes:
    """The one canonical JSON encoding for corpus lines."""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")


@contextmanager
def _read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """One database snapshot for every read of one export.

    Deferred BEGIN is valid on the mode=ro connection; on WAL the first read
    fixes the snapshot, so a concurrent sync committing mid-export cannot
    make later statements see newer rows than earlier ones. The connection
    is switched to autocommit isolation so the module never interleaves its
    own implicit BEGIN with ours; the caller's previous isolation level is
    restored on exit (and switching levels can itself commit — the CLI only
    ever hands over a connection with no transaction open, and the read
    tier writes nothing, so nothing of the caller's is ever committed).
    """
    previous_isolation = conn.isolation_level
    conn.isolation_level = None
    conn.execute("BEGIN DEFERRED")
    try:
        yield
    finally:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        conn.isolation_level = previous_isolation


def _reject_database_alias(out_path: str, db_path: str) -> None:
    """--out must never name the database or its sidecars: the destination is
    opened with a plain writable open after the read connection exists, and
    ``open(path, "wb")`` would truncate an aliased store despite SQLite
    running mode=ro. A stray ``-journal`` file is the worst case: SQLite
    treats it as a hot journal and refuses every subsequent read-only open
    until it is deleted by hand."""
    candidates = [
        db_path,
        db_path + "-wal",
        db_path + "-shm",
        db_path + "-journal",
    ]
    out_real = os.path.realpath(out_path)
    if out_real.startswith("\\\\.\\") or out_real.startswith("/dev/"):
        raise ExportError(
            "out_is_database",
            f"--out {out_path} resolves to a device, not a file;"
            " refusing to discard the corpus",
        )
    for cand in candidates:
        if os.path.realpath(cand) == out_real:
            raise ExportError(
                "out_is_database",
                f"--out {out_path} resolves to the database (or a sidecar);"
                " refusing to overwrite the store",
            )
        if os.path.exists(out_path) and os.path.exists(cand):
            if os.path.samefile(out_path, cand):
                raise ExportError(
                    "out_is_database",
                    f"--out {out_path} is a hard link (or alias) of the"
                    " database; refusing to overwrite the store",
                )


def _check_completeness(
    conn: sqlite3.Connection, repo: str, require_complete: bool
) -> sqlite3.Row:
    """Completeness is the status-derived rule, not a column:
    ``not full_sync_pending AND last_error IS NULL``."""
    state = conn.execute(
        "SELECT * FROM sync_state WHERE repo = ?", (repo,)
    ).fetchone()
    if state is None:
        raise ExportError(
            "incomplete_store",
            f"no sync_state row for {repo}; nothing has ever been synced",
            exit_code=1,
        )
    pending = bool(state["full_sync_pending"])
    errored = state["last_error"] is not None
    if require_complete and (pending or errored):
        why = []
        if pending:
            why.append("a full sync has not completed")
        if errored:
            why.append("the last sync recorded an error")
        raise ExportError(
            "incomplete_store",
            f"store for {repo} is incomplete: {'; '.join(why)}",
            hint="re-run sync, or export without --require-complete",
            exit_code=1,
        )
    return state


def _edge_row(edge: sqlite3.Row) -> dict:
    return {
        "src_type": edge["src_type"],
        "src_id": edge["src_id"],
        "rel": edge["rel"],
        "dst_type": edge["dst_type"],
        "dst_id": edge["dst_id"],
        "source": edge["source"],
        "evidence": edge["evidence"],
    }


def _src_routed_edges(
    conn: sqlite3.Connection, repo: str, number: int
) -> list[dict]:
    """Edges whose source is this (stored, local) item, whatever the
    destination endpoint type."""
    return [
        _edge_row(r)
        for r in conn.execute(
            f"SELECT {_EDGE_COLUMNS} FROM edges"
            " WHERE repo = ? AND src_type = 'item' AND src_id = ?"
            f" ORDER BY {_EDGE_ORDER}",
            (repo, str(number)),
        )
    ]


def _dst_routed_edges(
    conn: sqlite3.Connection, repo: str, number: int, local: set[str]
) -> list[dict]:
    """Edges pointing at this item from a source that is not a stored local
    item (actor, commit, label, file, or a foreign ``owner/repo#N`` item
    reference). Edges whose src IS a stored local item were already emitted
    under that src."""
    return [
        _edge_row(r)
        for r in conn.execute(
            f"SELECT {_EDGE_COLUMNS} FROM edges"
            " WHERE repo = ? AND dst_type = 'item' AND dst_id = ?"
            f" ORDER BY {_EDGE_ORDER}",
            (repo, str(number)),
        )
        if not (r["src_type"] == "item" and r["src_id"] in local)
    ]


def _item_content(
    conn: sqlite3.Connection,
    repo: str,
    row: sqlite3.Row,
    include: frozenset[str],
    local: set[str],
) -> dict:
    """The full content object for one item line, WITHOUT content_hash.
    Column set mirrors the items table (schema v10) minus raw_json, which
    rides the include flag."""
    content = {
        "type": "item",
        "repo": row["repo"],
        "number": row["number"],
        "id": row["id"],
        "kind": row["kind"],
        "node_id": row["node_id"],
        "title": row["title"],
        "body": row["body"],
        "labels_text": row["labels_text"],
        "state": row["state"],
        "state_reason": row["state_reason"],
        "author": row["author"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "closed_at": row["closed_at"],
        "merged_at": row["merged_at"],
        "merge_commit": row["merge_commit"],
        "draft": row["draft"],
        "locked": row["locked"],
        "base_ref": row["base_ref"],
        "head_ref": row["head_ref"],
        "additions": row["additions"],
        "deletions": row["deletions"],
        "changed_files": row["changed_files"],
        "commits": row["commits"],
        "html_url": row["html_url"],
        "api_url": row["api_url"],
        "reactions_total": row["reactions_total"],
        "milestone": row["milestone"],
        "item_type": row["item_type"],
        "sub_issues_total": row["sub_issues_total"],
        "sub_issues_completed": row["sub_issues_completed"],
        "dependencies_blocked_by": row["dependencies_blocked_by"],
        "dependencies_blocking": row["dependencies_blocking"],
        "dependencies_total_blocked_by": row["dependencies_total_blocked_by"],
        "dependencies_total_blocking": row["dependencies_total_blocking"],
        "author_association": row["author_association"],
        "merged_by": row["merged_by"],
        "comment_count": row["comment_count"],
        "paths_text": row["paths_text"],
        **({"raw": row["raw_json"]} if "raw" in include else {}),
        "labels": [
            {"name": r["name"], "color": r["color"]}
            for r in conn.execute(
                "SELECT name, color FROM labels WHERE repo = ? AND number = ?"
                " ORDER BY name",
                (repo, row["number"]),
            )
        ],
        "comments": [
            {
                "github_id": r["github_id"],
                "kind": r["kind"],
                "author": r["author"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "body": r["body"],
                "html_url": r["html_url"],
                "in_reply_to": r["in_reply_to"],
                "author_association": r["author_association"],
                **({"raw": r["raw_json"]} if "raw" in include else {}),
            }
            for r in conn.execute(
                "SELECT github_id, kind, author, created_at, updated_at,"
                " body, html_url, in_reply_to, author_association, raw_json"
                " FROM comments WHERE repo = ? AND number = ?"
                " ORDER BY kind, github_id",
                (repo, row["number"]),
            )
        ],
        "reviews": [
            {
                "id": r["id"],
                "author": r["author"],
                "state": r["state"],
                "submitted_at": r["submitted_at"],
                "body": r["body"],
                "html_url": r["html_url"],
                **({"raw": r["raw_json"]} if "raw" in include else {}),
            }
            for r in conn.execute(
                "SELECT id, author, state, submitted_at, body, html_url,"
                " raw_json FROM reviews WHERE repo = ? AND number = ?"
                " ORDER BY id",
                (repo, row["number"]),
            )
        ],
        "pr_files": [
            {
                "path": r["path"],
                "status": r["status"],
                "additions": r["additions"],
                "deletions": r["deletions"],
                "changes": r["changes"],
                "sha": r["sha"],
                **({"patch": r["patch"]} if "patches" in include else {}),
            }
            for r in conn.execute(
                "SELECT path, status, additions, deletions, changes, sha,"
                " patch FROM pr_files WHERE repo = ? AND number = ?"
                " ORDER BY path",
                (repo, row["number"]),
            )
        ],
        "assignees": [
            r["login"]
            for r in conn.execute(
                "SELECT login FROM assignees WHERE repo = ? AND number = ?"
                " ORDER BY login",
                (repo, row["number"]),
            )
        ],
        "issue_field_values": [
            {"field_name": r["field_name"], "value": r["value"]}
            for r in conn.execute(
                "SELECT field_name, value FROM issue_field_values"
                " WHERE repo = ? AND number = ? ORDER BY field_name",
                (repo, row["number"]),
            )
        ],
        "item_path_refs": [
            {
                "origin": r["origin"],
                "path": r["path"],
                "backticked": r["backticked"],
            }
            for r in conn.execute(
                "SELECT origin, path, backticked FROM item_path_refs"
                " WHERE repo = ? AND number = ? ORDER BY origin, path",
                (repo, row["number"]),
            )
        ],
        "item_signatures": [
            {
                "exception": r["exception"],
                "error_line": r["error_line"],
                "frame_function": r["frame_function"],
            }
            for r in conn.execute(
                "SELECT exception, error_line, frame_function"
                " FROM item_signatures WHERE repo = ? AND number = ?"
                " ORDER BY exception, error_line",
                (repo, row["number"]),
            )
        ],
        "item_events": [
            {"type": r["type"], "created_at": r["created_at"]}
            for r in conn.execute(
                "SELECT type, created_at FROM item_events"
                " WHERE repo = ? AND number = ? ORDER BY type, created_at",
                (repo, row["number"]),
            )
        ],
        "pr_commits": [
            {"sha": r["sha"], "message": r["message"]}
            for r in conn.execute(
                "SELECT sha, message FROM pr_commits"
                " WHERE repo = ? AND number = ? ORDER BY sha",
                (repo, row["number"]),
            )
        ],
        "edges": sorted(
            _src_routed_edges(conn, repo, row["number"])
            + _dst_routed_edges(conn, repo, row["number"], local),
            key=lambda e: (
                e["src_type"],
                e["src_id"],
                e["rel"],
                e["dst_type"],
                e["dst_id"],
                e["source"],
            ),
        ),
    }
    return content


def _release_content(row: sqlite3.Row, include: frozenset[str]) -> dict:
    content = {
        "type": "release",
        "repo": row["repo"],
        "id": row["id"],
        "tag_name": row["tag_name"],
        "name": row["name"],
        "body": row["body"],
        "draft": row["draft"],
        "prerelease": row["prerelease"],
        "author": row["author"],
        "created_at": row["created_at"],
        "published_at": row["published_at"],
        "html_url": row["html_url"],
    }
    if "raw" in include:
        content["raw"] = row["raw_json"]
    return content


def iter_corpus_content_lines(
    conn: sqlite3.Connection,
    repo: str,
    include: frozenset[str],
    routing: dict | None = None,
) -> Iterator[bytes]:
    """Yield the exact content-line byte pieces ``write_corpus`` spools, in
    order: item lines (by number, each carrying its ``content_hash``),
    release lines (by ``tag_name``), the optional ``revisions`` line, and the
    ``orphan_edges`` trailer last.

    ``include`` changes bytes exactly as documented for ``write_corpus``
    (``raw``/``patches`` alter item lines; ``revisions`` adds a whole line).
    Transaction ownership stays with the caller: the iterator never opens,
    commits, or rolls back — run it inside your own read snapshot so the
    pieces belong to one corpus state. When ``routing`` is a dict, it
    receives the ``items``/``releases``/``orphan_edges`` counts.
    """
    local = {
        str(r[0]) for r in conn.execute("SELECT number FROM items WHERE repo = ?", (repo,))
    }
    items = 0
    for row in conn.execute(
        "SELECT * FROM items WHERE repo = ? ORDER BY number", (repo,)
    ):
        content = _item_content(conn, repo, row, include, local)
        digest = hashlib.sha256(canonical(content)).hexdigest()
        content["content_hash"] = digest
        yield canonical(content) + b"\n"
        items += 1
    releases = 0
    for row in conn.execute(
        "SELECT * FROM releases WHERE repo = ? ORDER BY tag_name", (repo,)
    ):
        yield canonical(_release_content(row, include)) + b"\n"
        releases += 1
    if "revisions" in include:
        # Streamed element-wise: the rows array is never materialized.
        # revisions.id (SQLite-assigned) is excluded; order is the content
        # key, not the id.
        rev_order = (
            "number, entity, entity_id, kind, origin, valid_from, valid_to, content"
        )
        yield b'{"rows":['
        first = True
        for r in conn.execute(
            "SELECT number, entity, entity_id, kind, origin, content,"
            " valid_from, valid_to FROM revisions WHERE repo = ?"
            f" ORDER BY {rev_order}",
            (repo,),
        ):
            element = canonical(
                {
                    "number": r["number"],
                    "entity": r["entity"],
                    "entity_id": r["entity_id"],
                    "kind": r["kind"],
                    "origin": r["origin"],
                    "content": r["content"],
                    "valid_from": r["valid_from"],
                    "valid_to": r["valid_to"],
                }
            )
            if not first:
                yield b","
            yield element
            first = False
        yield b'],"type":"revisions"}\n'
    # Orphan trailer: streamed from one ordered pass, element-wise, never
    # materialized. Always the final content line.
    yield b'{"edges":['
    first = True
    orphans = 0

    def _is_orphan(r: sqlite3.Row) -> bool:
        src_local = r["src_type"] == "item" and r["src_id"] in local
        dst_local = r["dst_type"] == "item" and r["dst_id"] in local
        return not src_local and not dst_local

    for r in conn.execute(
        f"SELECT {_EDGE_COLUMNS} FROM edges WHERE repo = ? ORDER BY {_EDGE_ORDER}",
        (repo,),
    ):
        if not _is_orphan(r):
            continue
        element = canonical(_edge_row(r))
        if not first:
            yield b","
        yield element
        orphans += 1
        first = False
    yield b'],"type":"orphan_edges"}\n'
    if routing is not None:
        routing["items"] = items
        routing["releases"] = releases
        routing["orphan_edges"] = orphans


def corpus_sha256_from_lines(pieces: Iterator[bytes]) -> str:
    """Digest the exact content-line byte pieces into ``corpus_sha256``.

    Feeding this the output of :func:`iter_corpus_content_lines` for one
    snapshot yields the same digest ``write_corpus`` records for that
    snapshot (parity pinned by tests).
    """
    hasher = hashlib.sha256()
    for piece in pieces:
        hasher.update(piece)
    return hasher.hexdigest()


def write_corpus(
    conn: sqlite3.Connection,
    repo: str,
    target: IO[bytes] | None,
    out_path: str | None,
    include: frozenset[str],
    require_complete: bool,
    db_path: str,
) -> dict:
    """Export ``repo``'s corpus. Returns the manifest dict.

    ``target`` is an open binary stream (stdout buffer) or None when
    ``out_path`` is given; the destination is touched only after every guard
    passes and the spool is complete. Raises ExportError on refusal — the
    store and any existing destination file are untouched on refusal.
    """
    if not repo:
        raise ExportError(
            "repo_required",
            "export-corpus requires a resolved repository;"
            " --repo '' is not supported here",
        )
    if out_path is None and target is None:
        raise ExportError(
            "no_target",
            "write_corpus needs an open binary target or an --out path",
        )
    if out_path == "":
        raise ExportError(
            "no_target",
            "--out requires a non-empty path",
        )
    if out_path:
        _reject_database_alias(out_path, db_path)

    fd, spool_path = tempfile.mkstemp(prefix="zaxbygraph-export-")
    os.close(fd)
    fd, manifest_path = tempfile.mkstemp(prefix="zaxbygraph-export-manifest-")
    os.close(fd)
    try:
        # Snapshot phase: BEGIN DEFERRED is taken before the first read and
        # held across every read of one export (spool + manifest), then
        # RELEASED before the destination-copy phase — a WAL read-mark is
        # never pinned across the disk write (plan REV 3, R2-2).
        with _read_snapshot(conn), open(spool_path, "wb") as spool:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version < MIN_EXPORT_VERSION:
                raise ExportError(
                    "store_too_old",
                    f"store schema is v{version}; export-corpus needs v"
                    f"{MIN_EXPORT_VERSION}+ (item_events/pr_commits are missing"
                    " below it)",
                    hint="re-run sync once against this database to migrate",
                )
            if version > CURRENT_USER_VERSION:
                raise ExportError(
                    "store_too_new",
                    f"store schema is v{version}; this build understands up to"
                    f" v{CURRENT_USER_VERSION} — exporting would silently"
                    " omit fields it does not know about",
                    hint="upgrade zaxbygraph to at least the store's schema",
                )
            state = _check_completeness(conn, repo, require_complete)

            counts = {
                "items": 0,
                "releases": 0,
                "edges": 0,
                "orphan_edges": 0,
                "comments": 0,
                "reviews": 0,
            }
            counts["edges"] = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE repo = ?", (repo,)
            ).fetchone()[0]
            counts["comments"] = conn.execute(
                "SELECT COUNT(*) FROM comments WHERE repo = ?", (repo,)
            ).fetchone()[0]
            counts["reviews"] = conn.execute(
                "SELECT COUNT(*) FROM reviews WHERE repo = ?", (repo,)
            ).fetchone()[0]

            # Items, releases, the optional revisions line, and the orphan
            # trailer: one shared byte-piece stream (issue #29 eval consumes
            # the same stream for its snapshot hash).
            for piece in iter_corpus_content_lines(
                conn, repo, include, routing=counts
            ):
                spool.write(piece)

            # The piecemeal lines above must hash exactly the bytes written;
            # simplest correct approach: hash the whole spool once.
            hasher = hashlib.sha256()
            spool.flush()
            with open(spool_path, "rb") as reread:
                while True:
                    chunk = reread.read(1 << 20)
                    if not chunk:
                        break
                    hasher.update(chunk)
            corpus_sha256 = hasher.hexdigest()

            sync_state_row = conn.execute(
                "SELECT * FROM sync_state WHERE repo = ?", (repo,)
            ).fetchone()
            meta = {
                r["key"]: r["value"]
                for r in conn.execute("SELECT key, value FROM meta ORDER BY key")
            }

            manifest = {
                "schema": SCHEMA_NAME,
                "repo": repo,
                "counts": counts,
                "corpus_sha256": corpus_sha256,
                "store": {
                    "complete": not bool(state["full_sync_pending"])
                    and state["last_error"] is None,
                    "full_sync_pending": int(state["full_sync_pending"]),
                    "last_error": state["last_error"],
                    "schema_version": version,
                    "issues_since": state["issues_since"],
                    "include_patches": bool(state["include_patches"]),
                },
                "provenance": {
                    "sync_state": (
                        {k: sync_state_row[k] for k in sync_state_row.keys()}
                        if sync_state_row
                        else None
                    ),
                    # Placeholder: the fetch_log array is streamed into the
                    # manifest line below, element-wise, so neither memory
                    # nor this dict ever holds the whole table.
                    "fetch_log": [],
                    "meta": meta,
                },
            }

            # The manifest line streams the fetch_log array element-wise:
            # canonical() of the manifest-with-empty-array produces the exact
            # bytes the full-line encoding would, with the empty array as the
            # insertion point (the marker cannot occur inside a string value
            # — JSON escaping escapes the double quotes it contains).
            manifest_head = canonical(manifest)
            marker = b'"provenance":{"fetch_log":[]'
            cut = manifest_head.find(marker)
            if cut < 0:  # pragma: no cover - structure is fixed by canonical()
                raise ExportError(
                    "runtime", "manifest serialization marker not found"
                )
            # Keep the array's opening bracket in the prefix; the suffix
            # already carries the array's closing bracket, so the streamed
            # elements replace only the empty interior.
            split = cut + len(marker) - 1
            with open(manifest_path, "wb") as mspool:
                mspool.write(manifest_head[:split])
                first = True
                for r in conn.execute(
                    "SELECT * FROM fetch_log WHERE repo = ? ORDER BY id",
                    (repo,),
                ):
                    if not first:
                        mspool.write(b",")
                    mspool.write(canonical({k: r[k] for k in r.keys()}))
                    first = False
                mspool.write(manifest_head[split:])
                mspool.write(b"\n")

        # All guards passed, the snapshot is released, and both spools are
        # complete: touch the destination only now, atomically for a file
        # target (temp file in the destination's own directory + os.replace,
        # so a mid-copy failure never truncates a previous good export).
        if out_path:
            _reject_database_alias(out_path, db_path)
            fd, dest_tmp = tempfile.mkstemp(
                prefix="zaxbygraph-out-", dir=os.path.dirname(out_path) or "."
            )
            os.close(fd)
            try:
                with open(dest_tmp, "wb") as out, open(
                    manifest_path, "rb"
                ) as mspool, open(spool_path, "rb") as spool:
                    while True:
                        chunk = mspool.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                    while True:
                        chunk = spool.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                os.replace(dest_tmp, out_path)
            except BaseException:
                try:
                    os.unlink(dest_tmp)
                except OSError:
                    pass
                raise
        elif target is not None:
            with open(manifest_path, "rb") as mspool, open(
                spool_path, "rb"
            ) as spool:
                while True:
                    chunk = mspool.read(1 << 20)
                    if not chunk:
                        break
                    target.write(chunk)
                while True:
                    chunk = spool.read(1 << 20)
                    if not chunk:
                        break
                    target.write(chunk)
        return manifest
    finally:
        for path in (spool_path, manifest_path):
            try:
                os.unlink(path)
            except OSError:
                pass
