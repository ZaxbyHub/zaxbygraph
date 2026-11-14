"""Evaluation harness (issue #29): measure the derived tier against
GitHub's own ground truth, under leakage controls — rebuilt for issues
#68/#69 around the per-fix-PR backtest.

Ground truth enumeration (issue #68 AC2) unions two provenance streams per
MERGED PR: the GitHub-reported ``closes`` edges, and the admissible
``closes_keyword`` edges (source='keyword', evidence prefixed ``body@`` or
``commit `` — never a comment origin). Eligibility is mode-specific:
bundle needs >= 2 closed issues per fix PR (pairs need partners), triage
and localize admit >= 1. Per-case provenance is 'closes' when the
GitHub-reported stream alone meets the mode's threshold, else 'keyword';
the top-level ``ground_truth`` label is exclusive and threshold-relative:
'github' / 'keyword' / 'mixed'.

The bundle and triage modes replay HISTORY (issue #68 AC1/AC4): one
as-of snapshot per fix PR at that PR's ``created_at``, with the fix PR
itself excluded, built by :func:`prepare_snapshot` as connection-local
TEMP tables that shadow the real ones. The FULL production
``derive.bundle``/``derive.triage`` then runs on the prepared connection
WITHOUT ``as_of`` — the prepared view owns the snapshot (the user-facing
``--as-of`` flag rides the same prepare chain with ``exclude_pr=None``).
Headline metrics are MICRO aggregates over per-case numerators and
denominators (never unions), with macro per-fix-PR averages reported
alongside; every mode carries a baseline (issue #68 AC3) computed on the
same geometry.

A mode whose controls leave zero eligible ground truth raises
:class:`NoGroundTruthError` (a ValueError carrying ``code =
'no_ground_truth'``) instead of reporting empty metrics — the honest
refusal the CLI translates to exit 2.

Metrics and ``corpus_sha256`` are computed inside ONE read snapshot, so a
hash always identifies the corpus the metrics were computed on. The
snapshot view itself never writes the store: temp objects live on the
read-only connection (issue #29 C8's never-write pin).
"""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timezone

from zaxbygraph import export, extract
from zaxbygraph.derive import (
    _parse_instant,
    load_weights,
    similar as derive_similar,
    localize_files,
)
from zaxbygraph.derive import bundle as derive_bundle
from zaxbygraph.derive import triage as derive_triage

#: Modes and their contract metrics: similar -> recall@5 + MRR; localize ->
#: Acc@5 + precision@5; bundle -> pair recall/precision (per-fix-PR
#: backtest); triage -> top1/top3 next-fix rates (per-fix-PR backtest).
EVAL_MODES = ("similar", "localize", "bundle", "triage")

#: Baseline RNG seed (issue #68 AC3): the random baselines are part of the
#: measured contract, so they must be reproducible run over run.
_BASELINE_SEED = 0


class NoGroundTruthError(ValueError):
    """Zero eligible ground truth after controls (issue #68 AC6).

    Carries ``code = 'no_ground_truth'`` so the CLI can translate it to a
    coded exit-2 envelope instead of the generic bad_request a plain
    ValueError would get."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "no_ground_truth"


# ==== as-of snapshot view (issue #68 AC7) ====================================
#
# prepare_snapshot builds connection-local TEMP tables that SHADOW the real
# ones for every reader on the connection (SQLite resolves unqualified
# names temp-first). The store file is never written: mode=ro connections
# host temp objects in the temp database. drop_snapshot tears them down
# explicitly (never rollback-reliant — rollback does not clean temp views).

_SNAPSHOT_TABLES = (
    "items",
    "comments",
    "reviews",
    "edges",
    "pr_files",
    "labels",
    "assignees",
    "issue_field_values",
    "item_events",
    "item_path_refs",
    "item_signatures",
    "sync_state",
)


def drop_snapshot(conn: sqlite3.Connection) -> None:
    """Drop the temp snapshot objects (idempotent)."""
    for name in _SNAPSHOT_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS temp.{name}")
    conn.execute("DROP TABLE IF EXISTS temp.asof_meta")


def _asof_text(
    revisions: list[sqlite3.Row], live_text: str | None, live_stamp: str | None,
    at: datetime,
) -> str | None:
    """As-of text for one entity slot (the query.py _versioned_text
    semantics): the covering revision (valid_from <= T < valid_to, highest
    id first), else the live text when its validity window started at or
    before T, else None — the slot cannot be certified at T."""
    best: sqlite3.Row | None = None
    for row in revisions:
        start = _parse_instant(row["valid_from"])
        end = _parse_instant(row["valid_to"])
        if start is None or end is None:
            continue
        if start <= at < end and (best is None or row["id"] > best["id"]):
            best = row
    if best is not None:
        return best["content"]
    ends = [
        instant
        for instant in (_parse_instant(row["valid_to"]) for row in revisions)
        if instant is not None
    ]
    start = max(ends) if ends else _parse_instant(live_stamp)
    if start is not None and at >= start:
        return live_text
    return None


def prepare_snapshot(
    conn: sqlite3.Connection,
    repo: str,
    as_of: str,
    exclude_pr: int | None = None,
    reopen: bool | set | frozenset | None = None,
) -> sqlite3.Connection:
    """Build the as-of snapshot view on ``conn`` and return the prepared
    connection (the same object — the temp shadows do the work).

    Per-table temporal rules (issue #68 AC7):

    - ``items``: created_at <= T; the excluded fix PR absent; issues closed
      after T (or by the excluded PR, or named in ``reopen``) re-opened
      (state 'open', closed_at/merged_at NULL); PRs created <= T but merged
      after T stay open with merged_at NULL. Title/body carry the AS-OF
      text (revision pre-image chain, else live text under its own start);
      a slot that cannot be certified at T is NULLed and disclosed
      (``text_as_of_unknown``). raw_json's undatable payload fields
      (reactions, milestone) stop contributing once updated_at > T.
    - ``comments``/``reviews``: created/submitted <= T.
    - ``edges``: both item endpoints visible; edges touching the excluded
      PR absent; origin resolved by ``edges.source`` (keyword evidence by
      its comment/commit/body origin, closes by the source PR's merge,
      current-state 'link' edges suppressed wholesale and disclosed).
    - ``pr_files``: only PRs merged at/before T (an open-at-T PR
      contributes no files — origin unknown, disclosed).
    - ``labels``/``assignees``/``issue_field_values``: EMPTY (undated
      current-state rows; disclosed as ``current_state_tables``) — a row
      whose arrival time the store cannot express must never change the
      as-of answer.
    - ``item_events``: occurred <= T.
    - ``item_path_refs``/``item_signatures``: comment/review-origin rows
      keep their visible origins; body-origin rows are RE-DERIVED from the
      selected text (the stored rows are current-state).
    - ``sync_state``: copied with BOTH sync stamps set to T — the hotspot
      clock anchor (``hotspot_asof`` echoes it).
    """
    at = _parse_instant(as_of)
    if at is None:
        raise ValueError(f"bad_request: unparseable as_of value: {as_of!r}")
    stamp = as_of
    disclosures: dict[str, list] = {}
    reopen_set: set[int] = set()
    if reopen is not None and not isinstance(reopen, bool):
        reopen_set = {int(n) for n in reopen}

    drop_snapshot(conn)

    # ---- real-table reads (before any shadow exists) ----------------------
    item_rows = conn.execute(
        "SELECT * FROM items WHERE repo = ? ORDER BY number", (repo,)
    ).fetchall()
    real_merged_at = {
        int(r["number"]): r["merged_at"] for r in item_rows if r["merged_at"]
    }
    # closes edges BY the excluded PR (their target issues reopen even when
    # the stored closed_at predates T — the closure is the PR's act).
    closed_by_excluded: set[int] = set()
    if exclude_pr is not None:
        for row in conn.execute(
            "SELECT dst_id FROM edges WHERE repo = ? AND src_type = 'item'"
            " AND dst_type = 'item' AND src_id = ?"
            " AND rel IN ('closes', 'closes_keyword')",
            (repo, str(exclude_pr)),
        ):
            try:
                closed_by_excluded.add(int(row["dst_id"]))
            except (TypeError, ValueError):
                continue

    revisions_by_item: dict[int, dict[str, list[sqlite3.Row]]] = {}
    try:
        for row in conn.execute(
            "SELECT id, number, entity, content, valid_from, valid_to"
            " FROM revisions WHERE repo = ?"
            " AND entity IN ('item_title', 'item_body') ORDER BY id",
            (repo,),
        ):
            revisions_by_item.setdefault(int(row["number"]), {}).setdefault(
                row["entity"], []
            ).append(row)
    except sqlite3.OperationalError:
        revisions_by_item = {}

    # ---- items shadow -------------------------------------------------------
    # The shadow carries one EXTRA column, asof_real_merged_at: the store's
    # real merge stamp for PRs whose merge postdates T (merged_at itself is
    # nulled per the temporal rules). The linked_pr keyword gate keys on
    # whether a closure ever MATERIALIZED — a PR the store knows merged
    # never gates again, in any view — so the replay must be able to see
    # it (C1's leak leg: the fix PR's own closes edges anchor its truth
    # pair unless EXCLUDED; they must not gate the pair out of the pool
    # first).
    cols = [d[0] for d in conn.execute("SELECT * FROM items LIMIT 0").description]
    conn.execute(
        f"CREATE TEMP TABLE items ({', '.join(_quote_ident(c) for c in cols)},"
        " asof_real_merged_at TEXT)"
    )
    visible: set[int] = set()
    text_unknown: set[int] = set()
    asof_bodies: dict[int, str] = {}
    insert_cols = ", ".join(_quote_ident(c) for c in cols)
    placeholders = ", ".join("?" for _ in cols) + ", ?"
    insert_sql = (
        f"INSERT INTO items ({insert_cols}, asof_real_merged_at)"
        f" VALUES ({placeholders})"
    )
    for row in item_rows:
        rec = dict(row)
        number = int(rec["number"])
        if number == exclude_pr:
            continue
        created = rec.get("created_at")
        if not created or (at and _parse_instant(created) > at):
            continue
        real_merge = rec.get("merged_at")
        visible.add(number)
        slots = revisions_by_item.get(number, {})
        title = _asof_text(
            slots.get("item_title", []), rec.get("title"), rec.get("updated_at"), at
        )
        body = _asof_text(
            slots.get("item_body", []), rec.get("body"), rec.get("updated_at"), at
        )
        if title is None or body is None:
            # A slot _asof_text cannot certify (no covering revision and
            # the live window starts after T) is disclosed and NULLed in
            # the shadow — regardless of whether revision rows exist:
            # the stored live title/body are post-T and must never reach
            # derive's lexical/title tokenizers (store text is never NULL,
            # so a None return is always the uncertifiable case).
            text_unknown.add(number)
        if title is None:
            rec["title"] = None
        else:
            rec["title"] = title
        if body is None:
            rec["body"] = None
        else:
            rec["body"] = body
            asof_bodies[number] = body
        # Temporal state: reopen issues closed after T / by the excluded
        # PR / by the forced reopen set; un-merge PRs merged after T.
        closed_at = rec.get("closed_at")
        closed_after = bool(
            closed_at
            and _parse_instant(closed_at)
            and _parse_instant(closed_at) > at
        )
        merged_at = rec.get("merged_at")
        merged_after = bool(
            merged_at
            and _parse_instant(merged_at)
            and _parse_instant(merged_at) > at
        )
        if rec.get("kind") == "pr":
            if merged_after or (not merged_at and closed_after):
                rec["state"] = "open"
                rec["merged_at"] = None
                rec["closed_at"] = None
        else:
            forced = (
                number in reopen_set
                or number in closed_by_excluded
                or closed_after
                or not closed_at
            )
            if rec.get("state") == "closed" and forced:
                rec["state"] = "open"
                rec["closed_at"] = None
                rec["merged_at"] = None
        # Undatable payload fields stop contributing once the item was
        # updated after T (reactions/sub-issue completion would otherwise
        # leak post-T state through raw_json).
        updated = rec.get("updated_at")
        if rec.get("raw_json") and updated and _parse_instant(updated) > at:
            try:
                raw = json.loads(rec["raw_json"])
            except (TypeError, ValueError):
                raw = None
            if isinstance(raw, dict):
                for key in ("reactions", "milestone", "sub_issues_summary"):
                    raw.pop(key, None)
                rec["raw_json"] = json.dumps(raw)
        conn.execute(insert_sql, (*tuple(rec[c] for c in cols), real_merge))

    if text_unknown:
        disclosures["text_as_of_unknown"] = sorted(text_unknown)

    # ---- comments / reviews -------------------------------------------------
    conn.execute(
        "CREATE TEMP TABLE comments AS SELECT * FROM main.comments WHERE 0"
    )
    conn.execute(
        "INSERT INTO comments SELECT * FROM main.comments WHERE repo = ?"
        " AND created_at IS NOT NULL AND created_at <= ?",
        (repo, stamp),
    )
    conn.execute(
        "CREATE TEMP TABLE reviews AS SELECT * FROM main.reviews WHERE 0"
    )
    conn.execute(
        "INSERT INTO reviews SELECT * FROM main.reviews WHERE repo = ?"
        " AND submitted_at IS NOT NULL AND submitted_at <= ?",
        (repo, stamp),
    )
    comment_ids = {
        (row["number"], row["kind"], row["github_id"])
        for row in conn.execute(
            "SELECT number, kind, github_id FROM comments WHERE repo = ?", (repo,)
        )
    }
    review_ids = {
        (row["number"], row["id"])
        for row in conn.execute(
            "SELECT number, id FROM reviews WHERE repo = ?", (repo,)
        )
    }
    comment_created = {
        row["github_id"]: row["created_at"]
        for row in conn.execute(
            "SELECT github_id, created_at FROM comments WHERE repo = ?", (repo,)
        )
    }

    # ---- edges: endpoint visibility + origin resolution --------------------
    conn.execute("CREATE TEMP TABLE edges AS SELECT * FROM main.edges WHERE 0")
    edge_insert_sql = (
        "INSERT INTO edges (repo, src_type, src_id, rel, dst_type, dst_id,"
        " confidence, evidence, source) VALUES (?,?,?,?,?,?,?,?,?)"
    )
    suppressed_by_source: dict[str, int] = {}
    item_updated = {
        int(r["number"]): r["updated_at"] for r in item_rows if r["updated_at"]
    }
    item_kind = {int(r["number"]): r["kind"] for r in item_rows}
    for row in conn.execute(
        "SELECT * FROM main.edges WHERE repo = ? AND src_type = 'item'"
        " AND dst_type = 'item'",
        (repo,),
    ):
        try:
            src = int(row["src_id"])
            dst = int(row["dst_id"])
        except (TypeError, ValueError):
            # Non-numeric item ids (cross-repo references) never resolve to
            # a local endpoint; keep the row only if BOTH endpoints are the
            # only kinds it can have — item-item numeric. Drop otherwise.
            continue
        if src not in visible or dst not in visible:
            continue
        if exclude_pr is not None and exclude_pr in (src, dst):
            continue
        source = row["source"] or ""
        rel = row["rel"]
        evidence = row["evidence"] or ""
        keep = True
        if source == "keyword":
            if evidence.startswith("comment:"):
                digits = evidence[len("comment:"):].split("@", 1)[0]
                digits = "".join(ch for ch in digits if ch.isdigit())
                ts = comment_created.get(int(digits)) if digits else None
                ts_dt = _parse_instant(ts)
                keep = ts_dt is not None and ts_dt <= at
            elif evidence.startswith("commit "):
                merged = real_merged_at.get(src)
                merged_dt = _parse_instant(merged)
                keep = merged_dt is not None and merged_dt <= at
            else:
                # body@/title origin: the src item's text state at T
                # governs — the stored text (updated_at <= T) or a covering
                # pre-image keeps the reference; an unresolvable state
                # suppresses it.
                updated = item_updated.get(src)
                if updated and _parse_instant(updated) <= at:
                    keep = src not in text_unknown
                else:
                    keep = bool(
                        revisions_by_item.get(src, {}).get("item_body")
                    ) and src not in text_unknown
        elif source == "closing_ref":
            # closingIssuesReferences ride the PR itself: the closure
            # DECLARATION exists from the PR's creation (the endpoint
            # visibility rule above already dropped PRs created after T).
            # This is the stream the fix-PR exclusion removes — without it
            # the excluded PR's own closes edges would leak its truth pair
            # into the replay (C1's leak leg).
            keep = True
        elif source == "timeline":
            if rel in ("closes", "closed_by_commit", "reverts"):
                # The event streams are merge-gated: the merge IS the
                # origin of a timeline closure/revert.
                merged = real_merged_at.get(src)
                merged_dt = _parse_instant(merged)
                keep = merged_dt is not None and merged_dt <= at
            else:
                # Event-stream origins we cannot date (cross-reference
                # mentions and the like): suppressed + disclosed.
                keep = False
        elif source == "link":
            # Current-state facts with no recorded history: suppressed in
            # as-of views + disclosed (the tracking gate's child_of branch
            # therefore cannot fire inside a replay — a disclosed
            # limitation).
            keep = False
        else:
            keep = False
        if keep:
            conn.execute(
                edge_insert_sql,
                (
                    row["repo"], row["src_type"], row["src_id"], row["rel"],
                    row["dst_type"], row["dst_id"], row["confidence"],
                    row["evidence"], row["source"],
                ),
            )
        else:
            suppressed_by_source[source] = suppressed_by_source.get(source, 0) + 1
    if suppressed_by_source:
        disclosures["suppressed_evidence"] = [
            f"edges_by_source.{name}:{count}"
            for name, count in sorted(suppressed_by_source.items())
        ]
    disclosures["current_state_tables"] = [
        "labels", "assignees", "issue_field_values",
    ]

    # comment_count is a derived current-state column: the as-of view
    # recomputes it from the VISIBLE comments, so post-T discussion cannot
    # leak into a replay's demand signal (the stored column counts every
    # comment ever, most of which postdate early snapshots).
    conn.execute(
        "UPDATE items SET comment_count = ("
        " SELECT COUNT(*) FROM comments c WHERE c.repo = items.repo"
        " AND c.number = items.number AND c.kind = 'issue_comment')"
        " WHERE repo = ?",
        (repo,),
    )

    # ---- pr_files: merged-at-T PRs only -------------------------------------
    conn.execute("CREATE TEMP TABLE pr_files AS SELECT * FROM main.pr_files WHERE 0")
    conn.execute(
        "INSERT INTO pr_files SELECT pf.* FROM main.pr_files pf"
        " JOIN items i ON i.repo = pf.repo AND i.number = pf.number"
        " WHERE pf.repo = ? AND i.merged_at IS NOT NULL",
        (repo,),
    )

    # ---- undated current-state tables: empty shadows ------------------------
    for name in ("labels", "assignees", "issue_field_values"):
        conn.execute(f"CREATE TEMP TABLE {name} AS SELECT * FROM main.{name} WHERE 0")

    # ---- item_events ---------------------------------------------------------
    conn.execute("CREATE TEMP TABLE item_events AS SELECT * FROM main.item_events WHERE 0")
    conn.execute(
        "INSERT INTO item_events SELECT * FROM main.item_events"
        " WHERE repo = ? AND created_at <= ?",
        (repo, stamp),
    )

    # ---- text-derived tables: re-derive the body half ------------------------
    conn.execute(
        "CREATE TEMP TABLE item_path_refs AS SELECT * FROM main.item_path_refs WHERE 0"
    )
    ref_insert_sql = (
        "INSERT INTO item_path_refs (repo, number, origin, path, backticked)"
        " VALUES (?,?,?,?,?)"
    )
    for row in conn.execute(
        "SELECT * FROM main.item_path_refs WHERE repo = ?", (repo,)
    ):
        number = int(row["number"])
        if number not in visible:
            continue
        origin = row["origin"] or ""
        if origin.startswith("issue_comment:") or origin.startswith("review_comment:"):
            digits = origin.split(":", 1)[1]
            digits = "".join(ch for ch in digits if ch.isdigit())
            if not digits or (number, "issue_comment" if origin.startswith("issue") else "review_comment", int(digits)) not in comment_ids:
                continue
        elif origin.startswith("review:"):
            digits = origin.split(":", 1)[1]
            digits = "".join(ch for ch in digits if ch.isdigit())
            if not digits or (number, int(digits)) not in review_ids:
                continue
        elif origin == "body":
            # Replaced by the re-derived rows below.
            continue
        else:
            continue
        conn.execute(
            ref_insert_sql,
            (repo, number, origin, row["path"], row["backticked"]),
        )
    for number, body in asof_bodies.items():
        for path, backticked in extract.parse_path_refs(body, repo):
            conn.execute(
                ref_insert_sql,
                (repo, number, "body", path, 1 if backticked else 0),
            )

    conn.execute(
        "CREATE TEMP TABLE item_signatures AS SELECT * FROM main.item_signatures WHERE 0"
    )
    sig_insert_sql = (
        "INSERT INTO item_signatures (repo, number, exception, error_line,"
        " frame_function) VALUES (?,?,?,?,?)"
    )
    sig_done: set[tuple] = set()

    def _signatures(number: int, text: str | None) -> None:
        if not text:
            return
        for exception, error_line, frame_function in extract.parse_error_signatures(
            text
        ):
            key = (number, exception, error_line)
            if key in sig_done:
                continue
            sig_done.add(key)
            conn.execute(
                sig_insert_sql,
                (repo, number, exception, error_line, frame_function),
            )

    for number in sorted(visible):
        if number in asof_bodies:
            _signatures(number, asof_bodies[number])
        elif number in text_unknown:
            continue
    for row in conn.execute(
        "SELECT number, kind, body FROM comments WHERE repo = ?", (repo,)
    ):
        _signatures(int(row["number"]), row["body"])
    for row in conn.execute(
        "SELECT number, body FROM reviews WHERE repo = ?", (repo,)
    ):
        _signatures(int(row["number"]), row["body"])

    # ---- sync_state: the hotspot clock anchors at T ---------------------------
    conn.execute("CREATE TEMP TABLE sync_state AS SELECT * FROM main.sync_state WHERE 0")
    conn.execute(
        "INSERT INTO sync_state SELECT * FROM main.sync_state WHERE repo = ?",
        (repo,),
    )
    conn.execute(
        "UPDATE sync_state SET last_incr_sync_at = ?, last_full_sync_at = ?,"
        " issues_since = ? WHERE repo = ?",
        (stamp, stamp, stamp, repo),
    )

    # ---- disclosures ----------------------------------------------------------
    conn.execute(
        "CREATE TEMP TABLE asof_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    meta = {
        "as_of": stamp,
        "exclude_pr": exclude_pr,
        "repo": repo,
        "text_as_of_unknown": sorted(text_unknown),
        "disclosures": disclosures,
    }
    conn.executemany(
        "INSERT INTO asof_meta (key, value) VALUES (?, ?)",
        [(k, json.dumps(v)) for k, v in meta.items()],
    )
    return conn


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _text_chain_reaches_before(
    conn: sqlite3.Connection, repo: str, number: int, at: datetime
) -> bool:
    """Whether the item's text at ``at`` is certifiable from the store: a
    recorded item_title/item_body pre-image chain reaching before the
    stamp, or a live text whose own validity window started before it (the
    never-edited item case)."""
    try:
        rows = conn.execute(
            "SELECT valid_from, valid_to FROM revisions WHERE repo = ?"
            " AND number = ? AND entity IN ('item_title', 'item_body')",
            (repo, number),
        ).fetchall()
    except sqlite3.OperationalError:
        # Pre-v8 store: no recorded history — only the live-start rule can
        # certify the text.
        rows = []
    if rows:
        for row in rows:
            start = _parse_instant(row["valid_from"])
            end = _parse_instant(row["valid_to"])
            if start is not None and start <= at:
                if end is None or end > at:
                    return True
        ends = [
            e for e in (_parse_instant(r["valid_to"]) for r in rows) if e is not None
        ]
        if ends and max(ends) <= at:
            return True
        return False
    row = conn.execute(
        "SELECT updated_at, created_at FROM items WHERE repo = ? AND number = ?",
        (repo, number),
    ).fetchone()
    if row is None:
        return False
    live_start = _parse_instant(row["updated_at"]) or _parse_instant(row["created_at"])
    return live_start is not None and live_start <= at


# ==== ground-truth enumeration (issue #68 AC2) =================================


def _fix_pr_cases(
    conn: sqlite3.Connection, repo: str, threshold: int
) -> list[dict]:
    """Merged fix PRs and their closed-issue sets under closes UNION
    admissible closes_keyword, with per-case provenance ('closes' when the
    GitHub-reported stream alone meets ``threshold``)."""
    gh: dict[int, set[int]] = {}
    for row in conn.execute(
        "SELECT CAST(e.src_id AS INTEGER) AS pr, CAST(e.dst_id AS INTEGER) AS n"
        " FROM edges e JOIN items i ON i.repo = e.repo"
        " AND i.number = CAST(e.src_id AS INTEGER)"
        " WHERE e.repo = ? AND e.src_type = 'item' AND e.dst_type = 'item'"
        " AND e.rel = 'closes' AND i.kind = 'pr' AND i.merged_at IS NOT NULL",
        (repo,),
    ):
        if row["pr"] is None or row["n"] is None:
            continue
        gh.setdefault(int(row["pr"]), set()).add(int(row["n"]))
    kw: dict[int, set[int]] = {}
    for row in conn.execute(
        "SELECT CAST(e.src_id AS INTEGER) AS pr, CAST(e.dst_id AS INTEGER) AS n"
        " FROM edges e JOIN items i ON i.repo = e.repo"
        " AND i.number = CAST(e.src_id AS INTEGER)"
        " WHERE e.repo = ? AND e.src_type = 'item' AND e.dst_type = 'item'"
        " AND e.rel = 'closes_keyword' AND e.source = 'keyword'"
        " AND i.kind = 'pr' AND i.merged_at IS NOT NULL"
        " AND (e.evidence LIKE 'body@%' OR e.evidence LIKE 'commit %')",
        (repo,),
    ):
        if row["pr"] is None or row["n"] is None:
            continue
        kw.setdefault(int(row["pr"]), set()).add(int(row["n"]))
    cases: list[dict] = []
    for pr in sorted(set(gh) | set(kw)):
        gh_set = gh.get(pr, set())
        closed = gh_set | kw.get(pr, set())
        if len(closed) < threshold:
            continue
        row = conn.execute(
            "SELECT created_at FROM items WHERE repo = ? AND number = ?",
            (repo, pr),
        ).fetchone()
        cases.append(
            {
                "pr": pr,
                "created_at": row["created_at"] if row else None,
                "gh": gh_set,
                "kw": kw.get(pr, set()),
                "closed": closed,
                "ground_truth": (
                    "closes" if len(gh_set) >= threshold else "keyword"
                ),
            }
        )
    return cases


def _top_level_ground_truth(cases: list[dict]) -> str:
    """Exclusive, threshold-relative label: 'github' when every case's
    GitHub-reported stream alone suffices, 'keyword' when every case needs
    the keyword stream, 'mixed' when both kinds occur."""
    kinds = {case["ground_truth"] for case in cases}
    if kinds == {"closes"}:
        return "github"
    if kinds == {"keyword"}:
        return "keyword"
    return "mixed"


def _pairs_of(numbers: set[int]) -> set[tuple[int, int]]:
    ordered = sorted(numbers)
    return {
        (a, b)
        for i, a in enumerate(ordered)
        for b in ordered[i + 1:]
    }


def _bundle_pair_numbers(snapshot: dict) -> set[tuple[int, int]]:
    """Every co-member pair of the snapshot's bundles (audit view)."""
    out: set[tuple[int, int]] = set()
    for b in snapshot.get("bundles", []):
        members = [
            int(m["number"]) if isinstance(m, dict) else int(m)
            for m in b["members"]
        ]
        out |= _pairs_of(set(members))
    return out


def _closing_prs(conn: sqlite3.Connection, repo: str, number: int) -> list[int]:
    """PRs that closed ``number`` (GitHub-reported ``closes`` edges, with the
    closer as the source endpoint and the closed item as the destination)."""
    rows = conn.execute(
        "SELECT e.src_id FROM edges e"
        " JOIN items i ON i.repo = e.repo AND i.number = e.src_id"
        " WHERE e.repo = ? AND e.src_type = 'item' AND e.dst_type = 'item'"
        " AND e.dst_id = ? AND e.rel = 'closes'"
        " AND i.kind = 'pr' AND i.merged_at IS NOT NULL",
        (repo, str(number)),
    ).fetchall()
    out = []
    for row in rows:
        try:
            out.append(int(row["src_id"]))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def _merged_pr_files(
    conn: sqlite3.Connection, repo: str, pr_number: int
) -> set[str]:
    rows = conn.execute(
        "SELECT pf.path, pf.status FROM pr_files pf"
        " JOIN items i ON i.repo = pf.repo AND i.number = pf.number"
        " WHERE pf.repo = ? AND pf.number = ? AND i.merged_at IS NOT NULL",
        (repo, pr_number),
    ).fetchall()
    out = set()
    for row in rows:
        if row["status"] and row["status"] == "removed":
            continue
        out.add(row["path"].replace("\\", "/"))
    return out


def _item_created(
    conn: sqlite3.Connection, repo: str, number: int
) -> str | None:
    row = conn.execute(
        "SELECT created_at FROM items WHERE repo = ? AND number = ?",
        (repo, number),
    ).fetchone()
    return row["created_at"] if row else None


def _revised_after(
    conn: sqlite3.Connection, repo: str, number: int, cutoff_dt: datetime | None
) -> tuple[bool, bool]:
    """``(revised_after_cutoff, control_applicable)``.

    Item revisions are stored per slot — ``entity='item_title'`` or
    ``'item_body'``, with ``kind`` NULL — and ``valid_from`` marks when a
    revision became current. The revisions table is v8+; on older stores
    the control cannot be applied and the caller discloses
    ``revisions_unavailable`` rather than silently passing.
    """
    if cutoff_dt is None:
        return False, False
    try:
        rows = conn.execute(
            "SELECT valid_from FROM revisions WHERE repo = ?"
            " AND number = ? AND entity IN ('item_title', 'item_body')",
            (repo, number),
        ).fetchall()
    except sqlite3.OperationalError:
        return False, False
    for row in rows:
        ts = _parse_instant(row["valid_from"])
        if ts is not None and ts > cutoff_dt:
            return True, True
    return False, True


def _comment_cutoff(
    conn: sqlite3.Connection, repo: str, closing: list[int], fallback: str | None
) -> str | None:
    """The #44 comment-scoping anchor for one eval target: the EARLIEST
    closing PR's creation when closers exist, else the target's own
    ``created_at`` (documented fallback — with no fix PR there is no later
    anchor to scoping the target's evidence)."""
    rows = [
        row
        for row in (
            conn.execute(
                "SELECT created_at FROM items WHERE repo = ? AND number = ?",
                (repo, pr),
            ).fetchone()
            for pr in closing
        )
    ]
    if len(rows) != len(closing) or any(
        row is None or not row["created_at"] for row in rows
    ):
        return fallback
    stamps = [row["created_at"] for row in rows]
    return min(stamps) if stamps else fallback


def _duplicate_pairs(conn: sqlite3.Connection, repo: str) -> dict[int, set[int]]:
    """Reciprocal duplicate_of edges folded into unordered target->partners."""
    pairs: dict[int, set[int]] = {}
    for row in conn.execute(
        "SELECT src_id, dst_id FROM edges WHERE repo = ?"
        " AND src_type = 'item' AND dst_type = 'item' AND rel = 'duplicate_of'",
        (repo,),
    ):
        try:
            src, dst = int(row["src_id"]), int(row["dst_id"])
        except (TypeError, ValueError):
            continue
        pairs.setdefault(src, set()).add(dst)
        pairs.setdefault(dst, set()).add(src)
    return pairs


def _snapshot_sha(conn: sqlite3.Connection, repo: str) -> tuple[str | None, list[str]]:
    """Corpus sha for the store's default include set, or None + label when
    the store predates the v10 export floor (``store_too_old``) or postdates
    this build's schema (``store_too_new``)."""
    labels = []
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < export.MIN_EXPORT_VERSION:
        labels.append("store_too_old")
        return None, labels
    if version > export.CURRENT_USER_VERSION:
        labels.append("store_too_new")
        return None, labels
    pieces = export.iter_corpus_content_lines(conn, repo, frozenset())
    return export.corpus_sha256_from_lines(pieces), labels


def run_eval(
    conn: sqlite3.Connection,
    repo: str,
    mode: str,
    weights: dict | None = None,
) -> dict:
    """Run one eval mode under its leakage controls.

    ``weights=None`` resolves the effective defaults here, so a direct
    caller's envelope carries the same weights the derivation scored with.
    Metrics and ``corpus_sha256`` share one read snapshot so the hash
    always identifies the corpus the metrics were computed on. Every
    result is ``derived: true`` and carries the weights used, so a metric
    is always tied to the ``[derive]`` configuration that produced it. A
    mode with zero eligible ground truth after controls RAISES
    :class:`NoGroundTruthError` (issue #68 AC6) instead of reporting
    empty metrics.
    """
    if mode not in EVAL_MODES:
        raise ValueError(f"unknown eval mode: {mode} (expected one of {EVAL_MODES})")
    if not repo:
        raise ValueError("repo_required: eval needs a resolved repository")

    leakage: dict[int, list[str]] = {}
    if weights is None:
        weights = load_weights()
    conn.execute("BEGIN DEFERRED")
    try:
        sha, labels = _snapshot_sha(conn, repo)
        if mode == "similar":
            result = _eval_similar(conn, repo, weights, leakage)
        elif mode == "localize":
            result = _eval_localize(conn, repo, weights, leakage)
        elif mode == "triage":
            result = _eval_triage(conn, repo, weights, leakage)
        else:
            result = _eval_bundle(conn, repo, weights, leakage)
    finally:
        drop_snapshot(conn)
        conn.rollback()

    result.update(
        {
            "mode": mode,
            "repo": repo,
            "corpus_sha256": sha,
            "with_leakage": {str(k): v for k, v in sorted(leakage.items())},
            "labels": labels,
            "weights": weights,
            "derived": True,
        }
    )
    return result


def _eval_similar(
    conn: sqlite3.Connection,
    repo: str,
    weights: dict | None,
    leakage: dict[int, list[str]],
) -> dict:
    """Duplicate detection: for each duplicate_of target, does the planted
    partner rank in the top 5 (recall@5) and at what reciprocal rank (MRR)?
    The candidate pool is untruncated before the visibility filter so a
    post-cutoff leader can never crowd a pre-cutoff partner out of the
    ranking; recall/MRR denominators count only partners visible at the
    cutoff. The ``random_recall_at_5`` baseline draws 5 random visible
    candidates per target (fixed seed) on the same geometry."""
    pairs = _duplicate_pairs(conn, repo)
    recalls: list[float] = []
    rr: list[float] = []
    eligible = 0
    baseline_recalls: list[float] = []
    baseline_rr: list[float] = []
    for target, partners in sorted(pairs.items()):
        cutoff = _item_created(conn, repo, target)
        if cutoff is None:
            leakage.setdefault(target, []).append("missing_created_at")
            continue
        cutoff_dt = _parse_instant(cutoff)
        if cutoff_dt is None:
            leakage.setdefault(target, []).append("created_at_unparseable")
            continue
        revised, revisions_ok = _revised_after(conn, repo, target, cutoff_dt)
        if not revisions_ok:
            leakage.setdefault(target, []).append("revisions_unavailable")
        elif revised:
            leakage.setdefault(target, []).append("item_revised_after_cutoff")
        closing = _closing_prs(conn, repo, target)
        leakage.setdefault(target, []).append("signature_origin_unknown")
        excluded_adjacency = {
            frozenset((target, partner)) for partner in partners
        } | {frozenset((target, pr)) for pr in closing}
        try:
            out = derive_similar(
                conn,
                repo,
                target,
                limit=None,
                weights=weights,
                exclude_adjacency=excluded_adjacency,
                filtered_sources={"keyword"},
                suppress_signals={"signature"},
                item_cutoff=cutoff,
                comment_cutoff=_comment_cutoff(conn, repo, closing, cutoff),
            )
        except LookupError:
            leakage.setdefault(target, []).append("target_missing")
            continue
        visible, unknown = _visible_candidates(out, cutoff)
        if unknown:
            leakage.setdefault(target, []).append("candidate_created_at_unknown")
        visible_numbers = [c["number"] for c in visible]
        in_scope = [p for p in partners if p in set(visible_numbers)]
        if not in_scope:
            continue
        ranked = visible_numbers[:5]
        eligible += 1
        recalls.append(sum(1 for p in in_scope if p in ranked) / len(in_scope))
        best = 0.0
        for partner in in_scope:
            if partner in ranked:
                best = max(best, 1.0 / (ranked.index(partner) + 1))
        rr.append(best)
        rng = random.Random(_BASELINE_SEED)
        pool = [c["number"] for c in visible if c["number"] not in partners]
        draws = pool[:5] if len(pool) <= 5 else rng.sample(pool, 5)
        baseline_recalls.append(
            sum(1 for p in in_scope if p in draws) / len(in_scope)
        )
        best = 0.0
        for partner in in_scope:
            if partner in draws:
                best = max(best, 1.0 / (draws.index(partner) + 1))
        baseline_rr.append(best)
    if not recalls:
        raise NoGroundTruthError(
            "no_ground_truth: no duplicate_of ground truth with a visible"
            " partner after controls in this store"
        )
    return {
        "metrics": {
            "recall_at_5": sum(recalls) / len(recalls),
            "mrr": sum(rr) / len(rr),
            "eligible_targets": eligible,
            "targets_total": len(pairs),
        },
        "baselines": {
            "random_recall_at_5": {
                "recall_at_5": (
                    sum(baseline_recalls) / len(baseline_recalls)
                    if baseline_recalls
                    else None
                ),
                "mrr": (
                    sum(baseline_rr) / len(baseline_rr) if baseline_rr else None
                ),
                **({} if baseline_recalls else {"reason": "no visible candidate pool"}),
            }
        },
    }


def _eval_localize(
    conn: sqlite3.Connection,
    repo: str,
    weights: dict | None,
    leakage: dict[int, list[str]],
) -> dict:
    """Localization: for each issue a merged PR closed (closes UNION
    admissible keyword evidence — issue #68 AC2), is one of the fix's files
    in the top 5 ranked by :func:`localize_files` (acc@5), and what share
    of the top 5 the truth files fill (precision@5)? The fix PR is excluded
    from the candidate pool and comment-derived references cut at its
    creation. The ``most_touched`` baseline ranks files by raw pr_files
    frequency over the same visible merged PRs."""
    closers_by_issue: dict[int, set[int]] = {}
    for case in _fix_pr_cases(conn, repo, 1):
        for n in case["closed"]:
            closers_by_issue.setdefault(n, set()).add(case["pr"])
    hits: list[float] = []
    precisions: list[float] = []
    base_hits: list[float] = []
    base_precisions: list[float] = []
    eligible = 0
    for target in sorted(closers_by_issue):
        closing = sorted(closers_by_issue[target])
        truth_files: set[str] = set()
        for pr in closing:
            truth_files |= _merged_pr_files(conn, repo, pr)
        if not truth_files:
            continue
        cutoff = _item_created(conn, repo, target)
        if cutoff is None:
            leakage.setdefault(target, []).append("missing_created_at")
            continue
        cutoff_dt = _parse_instant(cutoff)
        if cutoff_dt is None:
            leakage.setdefault(target, []).append("created_at_unparseable")
            continue
        revised, revisions_ok = _revised_after(conn, repo, target, cutoff_dt)
        if not revisions_ok:
            leakage.setdefault(target, []).append("revisions_unavailable")
        elif revised:
            leakage.setdefault(target, []).append("item_revised_after_cutoff")
        fix_created = [
            conn.execute(
                "SELECT created_at FROM items WHERE repo = ? AND number = ?",
                (repo, pr),
            ).fetchone()["created_at"]
            for pr in closing
        ]
        comment_cutoff = min((t for t in fix_created if t), default=None)
        if comment_cutoff is None:
            leakage.setdefault(target, []).append("fix_pr_created_at_unknown")
        out = localize_files(
            conn,
            repo,
            target,
            weights=weights,
            cutoff=cutoff,
            top=5,
            excluded_prs=set(closing),
            comment_cutoff=comment_cutoff,
        )
        for note in out.get("leakage_notes", []):
            leakage.setdefault(target, []).append(note)
        pool = _eval_pool(conn, repo, target, cutoff, closing)
        reachable_truth = truth_files & pool
        if not reachable_truth:
            continue
        eligible += 1
        top_paths = [f["path"] for f in out["files"][:5]]
        hits.append(1.0 if set(top_paths) & reachable_truth else 0.0)
        precisions.append(len(set(top_paths) & reachable_truth) / 5.0)
        # most_touched baseline: raw pr_files frequency over the same pool.
        freq: dict[str, int] = {}
        rows = conn.execute(
            "SELECT pf.number, pf.path, pf.status FROM pr_files pf"
            " JOIN items i ON i.repo = pf.repo AND i.number = pf.number"
            " WHERE pf.repo = ? AND i.merged_at IS NOT NULL",
            (repo,),
        ).fetchall()
        cutoff_dt_pool = _parse_instant(cutoff)
        for row in rows:
            if row["number"] in set(closing):
                continue
            if row["status"] and row["status"] == "removed":
                continue
            merged_dt = _parse_instant(
                conn.execute(
                    "SELECT merged_at FROM items WHERE repo = ? AND number = ?",
                    (repo, row["number"]),
                ).fetchone()["merged_at"]
            )
            if cutoff_dt_pool is not None and (
                merged_dt is None or merged_dt > cutoff_dt_pool
            ):
                continue
            path = row["path"].replace("\\", "/")
            if path in pool:
                freq[path] = freq.get(path, 0) + 1
        top_touched = [
            p
            for p, _ in sorted(freq.items(), key=lambda e: (-e[1], e[0]))[:5]
        ]
        base_hits.append(1.0 if set(top_touched) & reachable_truth else 0.0)
        base_precisions.append(len(set(top_touched) & reachable_truth) / 5.0)
    if not hits:
        raise NoGroundTruthError(
            "no_ground_truth: no issue closed by a merged PR (closes or"
            " admissible closes_keyword evidence) with reachable truth"
            " files after controls in this store"
        )
    provenance = [
        case["ground_truth"] for case in _fix_pr_cases(conn, repo, 1)
    ]
    return {
        "metrics": {
            "acc_at_5": sum(hits) / len(hits),
            "precision_at_5": sum(precisions) / len(precisions),
            "eligible_targets": eligible,
            "targets_total": len(closers_by_issue),
        },
        "ground_truth": _top_level_ground_truth(
            [{"ground_truth": p} for p in provenance]
        ),
        "baselines": {
            "most_touched": {
                "acc_at_5": (
                    sum(base_hits) / len(base_hits) if base_hits else None
                ),
                "precision_at_5": (
                    sum(base_precisions) / len(base_precisions)
                    if base_precisions
                    else None
                ),
                **(
                    {} if base_hits else {"reason": "no visible merged-PR pool"}
                ),
            }
        },
    }


def _eval_pool(
    conn: sqlite3.Connection,
    repo: str,
    target: int,
    cutoff: str,
    excluded_prs: list[int],
) -> set[str]:
    """The reachable candidate pool for one localize target (vocabulary from
    merged PRs at/before the cutoff, minus the excluded PRs)."""
    rows = conn.execute(
        "SELECT pf.number, pf.path, pf.status, i.merged_at FROM pr_files pf"
        " JOIN items i ON i.repo = pf.repo AND i.number = pf.number"
        " WHERE pf.repo = ? AND i.merged_at IS NOT NULL",
        (repo,),
    ).fetchall()
    cutoff_dt = _parse_instant(cutoff)
    pool: set[str] = set()
    for row in rows:
        if row["number"] in excluded_prs:
            continue
        if row["status"] and row["status"] == "removed":
            continue
        merged_dt = _parse_instant(row["merged_at"])
        if cutoff_dt is not None and (merged_dt is None or merged_dt > cutoff_dt):
            continue
        pool.add(row["path"].replace("\\", "/"))
    return pool


def _visible_candidates(
    out: dict, cutoff: str | None, kind: str | None = None
) -> tuple[list[dict], bool]:
    """Candidates visible at the target's item-visibility cutoff.

    Returns (visible, any_unknown_created_at): a candidate whose created_at
    cannot be parsed cannot be proven pre-cutoff, which flags leakage rather
    than silently counting it. ``kind`` optionally restricts the pool
    (bundles pair issues with issues).
    """
    cutoff_dt = _parse_instant(cutoff)
    visible: list[dict] = []
    unknown = False
    for c in out["candidates"]:
        if kind is not None and c.get("kind") != kind:
            continue
        ts = _parse_instant(c.get("created_at"))
        if ts is None:
            unknown = True
            continue
        if cutoff_dt is not None and ts > cutoff_dt:
            continue
        visible.append(c)
    return visible, unknown


# ==== per-fix-PR backtests (issues #68 AC1/AC4) ================================


def _scoreable_case(
    conn: sqlite3.Connection,
    repo: str,
    case: dict,
    leakage: dict[int, list[str]] | None = None,
) -> dict:
    """Visibility-filtered truth for one fix-PR case: pairs whose endpoints
    are visible at the case's stamp, with the suppressed endpoints
    disclosed. Per-endpoint control notes (missing/unparseable created_at,
    revised-after-stamp, revisions unavailable) ride ``leakage`` — the same
    disclosure surface the per-target modes carry."""
    stamp = case["created_at"]
    at = _parse_instant(stamp)
    excluded_endpoints: list[int] = []
    if at is None:
        excluded_endpoints = sorted(case["closed"])
    else:
        for n in case["closed"]:
            created = _item_created(conn, repo, n)
            if created is None:
                if leakage is not None:
                    leakage.setdefault(n, []).append("missing_created_at")
                excluded_endpoints.append(n)
                continue
            created_dt = _parse_instant(created)
            if created_dt is None:
                if leakage is not None:
                    leakage.setdefault(n, []).append("created_at_unparseable")
                excluded_endpoints.append(n)
                continue
            if created_dt > at:
                excluded_endpoints.append(n)
                continue
            if leakage is not None:
                revised, revisions_ok = _revised_after(conn, repo, n, at)
                if not revisions_ok:
                    leakage.setdefault(n, []).append("revisions_unavailable")
                elif revised:
                    leakage.setdefault(n, []).append("item_revised_after_cutoff")
    scoreable = case["closed"] - set(excluded_endpoints)
    return {
        "scoreable": scoreable,
        "excluded_endpoints": sorted(excluded_endpoints),
        "at": at,
    }


def _eval_bundle(
    conn: sqlite3.Connection,
    repo: str,
    weights: dict | None,
    leakage: dict[int, list[str]],
) -> dict:
    """Per-fix-PR bundle backtest (issue #68 AC1): replay the FULL
    production bundler on one prepared as-of snapshot per merged
    multi-issue fix PR (at the PR's created_at, the PR itself excluded) and
    score its proposed co-member pairs against the PR's closed-issue pairs.
    Headline metrics are MICRO aggregates (per-case numerators and
    denominators summed, never unioned); ``truth_pairs`` stays the AUDIT
    count of distinct closed-by-one-PR pairs."""
    cases = _fix_pr_cases(conn, repo, 2)
    if not cases:
        raise NoGroundTruthError(
            "no_ground_truth: no merged PR closes >= 2 issues (closes or"
            " admissible closes_keyword evidence) in this store"
        )
    per_fix: list[dict] = []
    proposed_union: set[tuple[int, int]] = set()
    truth_pairs_audit: set[tuple[int, int]] = set()
    hits_total = 0
    proposed_total = 0
    truth_total = 0
    eligible_endpoints: set[int] = set()
    precisions: list[float] = []
    recalls: list[float] = []
    base_hits_total = 0
    base_proposed_total = 0
    base_truth_total = 0
    base_precisions: list[float] = []
    base_recalls: list[float] = []
    base_exact: list[float] = []
    exact_sets: list[float] = []
    scoreable_cases = 0
    for case in cases:
        info = _scoreable_case(conn, repo, case, leakage)
        truth = _pairs_of(info["scoreable"])
        truth_pairs_audit |= _pairs_of(case["closed"])
        disclosures: list[str] = []
        for n in info["excluded_endpoints"]:
            disclosures.append(f"excluded_endpoint:{n}")
        stamp = case["created_at"]
        if info["at"] is not None:
            for n in sorted(case["closed"]):
                if not _text_chain_reaches_before(conn, repo, n, info["at"]):
                    disclosures.append("text_as_of_unknown")
                    break
        entry = {
            "pr": case["pr"],
            "created_at": stamp,
            "ground_truth": case["ground_truth"],
            "closed_issues": sorted(case["closed"]),
            "truth_pairs": len(truth),
            "excluded_endpoints": info["excluded_endpoints"],
            "disclosures": disclosures,
        }
        if not truth:
            entry.update(
                {
                    "proposed_pairs": [],
                    "hits": 0,
                    "precision": None,
                    "recall": None,
                    "exact_set": 0,
                    "scoreable": False,
                }
            )
            per_fix.append(entry)
            continue
        scoreable_cases += 1
        prepared = prepare_snapshot(
            conn, repo, as_of=stamp, exclude_pr=case["pr"], reopen=True
        )
        try:
            snapshot = derive_bundle(prepared, repo, weights=weights)
        finally:
            drop_snapshot(prepared)
        proposed = _bundle_pair_numbers(snapshot)
        entry["pool"] = list(snapshot.get("pool", []))
        entry["proposed_pairs"] = [list(p) for p in sorted(proposed)]
        hits = len(proposed & truth)
        entry["hits"] = hits
        entry["precision"] = hits / len(proposed) if proposed else None
        entry["recall"] = hits / len(truth)
        entry["scoreable"] = True
        members_sets = [
            {
                int(m["number"]) if isinstance(m, dict) else int(m)
                for m in b["members"]
            }
            for b in snapshot.get("bundles", [])
        ]
        entry["exact_set"] = 1 if any(
            ms == info["scoreable"] for ms in members_sets
        ) else 0
        per_fix.append(entry)
        proposed_union |= proposed
        hits_total += hits
        proposed_total += len(proposed)
        truth_total += len(truth)
        eligible_endpoints |= info["scoreable"]
        if proposed:
            precisions.append(hits / len(proposed))
        recalls.append(hits / len(truth))
        exact_sets.append(float(entry["exact_set"]))
        # random_same_size baseline on the same geometry (fixed seed).
        rng = random.Random(_BASELINE_SEED)
        pool = list(snapshot.get("pool", []))
        rng.shuffle(pool)
        sizes = [len(b["members"]) for b in snapshot.get("bundles", [])]
        base_groups: list[set[int]] = []
        index = 0
        for size in sizes:
            base_groups.append(set(pool[index : index + size]))
            index += size
        base_proposed: set[tuple[int, int]] = set()
        for group in base_groups:
            base_proposed |= _pairs_of(group)
        base_hits = len(base_proposed & truth)
        base_hits_total += base_hits
        base_proposed_total += len(base_proposed)
        base_truth_total += len(truth)
        if base_proposed:
            base_precisions.append(base_hits / len(base_proposed))
        base_recalls.append(base_hits / len(truth))
        base_exact.append(
            1.0 if any(g == info["scoreable"] for g in base_groups) else 0.0
        )
    if not scoreable_cases or not truth_total:
        raise NoGroundTruthError(
            "no_ground_truth: every fix PR's truth pairs lost their endpoints"
            " to the as-of controls in this store"
        )
    random_baseline = {
        "pair_precision": (
            base_hits_total / base_proposed_total if base_proposed_total else None
        ),
        "pair_recall": (
            base_hits_total / base_truth_total if base_truth_total else None
        ),
        "exact_set_rate": (
            sum(base_exact) / len(base_exact) if base_exact else None
        ),
    }
    if random_baseline["pair_precision"] is None:
        random_baseline["reason"] = "no baseline proposals on this geometry"
    return {
        "metrics": {
            "pair_recall": hits_total / truth_total,
            "pair_precision": (
                hits_total / proposed_total if proposed_total else None
            ),
            "proposed_case_total": proposed_total,
            "truth_case_total": truth_total,
            "truth_pairs": len(truth_pairs_audit),
            "eligible_targets": len(eligible_endpoints),
            "targets_total": len({n for c in cases for n in c["closed"]}),
            "exact_set_rate": (
                sum(exact_sets) / len(exact_sets) if exact_sets else None
            ),
        },
        "macro_precision": (
            sum(precisions) / len(precisions) if precisions else None
        ),
        "macro_recall": sum(recalls) / len(recalls) if recalls else None,
        "ground_truth": _top_level_ground_truth(cases),
        "per_fix_pr": per_fix,
        "proposed_pairs": [list(p) for p in sorted(proposed_union)],
        "baselines": {"random_same_size": random_baseline},
    }


def _eval_triage(
    conn: sqlite3.Connection,
    repo: str,
    weights: dict | None,
    leakage: dict[int, list[str]],
) -> dict:
    """Per-fix-PR triage backtest (issue #68 AC4): for each merged fix PR
    closing >= 1 issue (mode-specific eligibility), prepare the snapshot at
    the PR's created_at with the PR excluded, run the FULL production
    triage on the prepared connection (never with as_of — the prepared
    view owns the snapshot), and measure how often the next issues to be
    fixed (the PR's closed issues, reopened at T) sit at the top of the
    ranking. ``lowest_number_first`` is the baseline over the same pool."""
    cases = _fix_pr_cases(conn, repo, 1)
    if not cases:
        raise NoGroundTruthError(
            "no_ground_truth: no merged PR closes >= 1 issue (closes or"
            " admissible closes_keyword evidence) in this store"
        )
    top1: list[float] = []
    top3: list[float] = []
    base_top1: list[float] = []
    base_top3: list[float] = []
    per_fix: list[dict] = []
    # Pool cap (final-critic F3): each case's replayed ranking is cut at
    # ``pool_cap`` (1000) ranked rows — a bounded read, not the full pool
    # (paging the cursor is out of scope for this fix round). Two biases
    # follow and are disclosed, never hidden: a case whose truth target sits
    # past the cut leaves the pool intersection and reads scoreable: False
    # (it DROPS FROM THE DENOMINATOR rather than counting as a miss, biasing
    # hit rates upward), and the lowest_number_first baseline ranks the same
    # truncated pool. Per case the cap rides entry["disclosures"]
    # ("eval_triage_pool_cap"); the payload's top-level pool_cap block fires
    # whenever the cap bound at least one run.
    pool_cap = 1000
    cap_hit_cases: list[int] = []
    for case in cases:
        stamp = case["created_at"]
        info = _scoreable_case(conn, repo, case, leakage)
        prepared = prepare_snapshot(
            conn, repo, as_of=stamp, exclude_pr=case["pr"], reopen=True
        )
        try:
            out = derive_triage(
                prepared, repo, limit=pool_cap, include_gated=False,
                weights=weights,
            )
        finally:
            drop_snapshot(prepared)
        pool = [r["number"] for r in out["rows"] if r.get("ranked")]
        truth = {n for n in info["scoreable"] if n in set(pool)}
        disclosures = [
            f"excluded_endpoint:{n}" for n in info["excluded_endpoints"]
        ]
        if out.get("truncated"):
            cap_hit_cases.append(case["pr"])
            disclosures.append(
                f"eval_triage_pool_cap: the ranked pool was truncated at"
                f" {pool_cap} rows; a truth target past the cut reads"
                f" scoreable: False and leaves the denominator"
            )
        entry = {
            "pr": case["pr"],
            "created_at": stamp,
            "ground_truth": case["ground_truth"],
            "closed_issues": sorted(case["closed"]),
            "excluded_endpoints": info["excluded_endpoints"],
            "disclosures": disclosures,
        }
        if not truth:
            entry.update({"top1": None, "top3": None, "scoreable": False})
            per_fix.append(entry)
            continue
        ranked = pool
        entry["top1"] = 1 if ranked and ranked[0] in truth else 0
        entry["top3"] = 1 if any(n in truth for n in ranked[:3]) else 0
        entry["scoreable"] = True
        per_fix.append(entry)
        top1.append(float(entry["top1"]))
        top3.append(float(entry["top3"]))
        lowest_first = sorted(pool)
        base_top1.append(1.0 if lowest_first and lowest_first[0] in truth else 0.0)
        base_top3.append(
            1.0 if any(n in truth for n in lowest_first[:3]) else 0.0
        )
    if not top1:
        raise NoGroundTruthError(
            "no_ground_truth: no fix PR whose closed issues sit in the"
            " replayed pool after controls in this store"
        )
    payload = {
        "metrics": {
            "top1_next_fix_rate": sum(top1) / len(top1),
            "top3_next_fix_rate": sum(top3) / len(top3),
            "eligible_targets": len(top1),
            "targets_total": len(cases),
        },
        "ground_truth": _top_level_ground_truth(cases),
        "per_fix_pr": per_fix,
        "baselines": {
            "lowest_number_first": {
                "top1_next_fix_rate": (
                    sum(base_top1) / len(base_top1) if base_top1 else None
                ),
                "top3_next_fix_rate": (
                    sum(base_top3) / len(base_top3) if base_top3 else None
                ),
                **({} if base_top1 else {"reason": "no replayed pool"}),
            }
        },
    }
    if cap_hit_cases:
        # The cap actually bound at least one run — disclose it once at the
        # top level too, not only per case (final-critic F3).
        payload["pool_cap"] = {
            "limit": pool_cap,
            "cases": cap_hit_cases,
            "note": "the replayed ranked pool was truncated at the limit for"
                    " the listed cases; a truth target past the cut reads"
                    " scoreable: False and leaves the denominator (hit rates"
                    " bias upward), and lowest_number_first ranks the same"
                    " truncated pool",
        }
    return payload
