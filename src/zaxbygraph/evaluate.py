"""Evaluation harness (issue #29): measure the derived tier against
GitHub's own ground truth, under leakage controls.

Ground truth comes only from GitHub-reported facts in the store:
``duplicate_of`` edges (duplicate detection), the files a merged fix PR
touched (localization), and the issue sets single PRs closed (bundles).
Per target, the harness applies AC6's controls — candidates restricted to
items that existed at the target's creation, the target's closing PRs and
their edges excluded, PRs merged after the target's creation excluded,
comments after the fix PR's creation excluded (including their rolled-up
path references, via origin-timestamp joins), and the target–partner edges
themselves excluded — and labels any result whose controls could not be
applied ``with_leakage``. Signature components are suppressed in eval
scoring by construction: ``item_signatures`` rows carry no origin, so a
cutoff-visible signature set cannot be reconstructed (conservative
suppression beats a leaky signal). Every scored target is therefore labelled
``with_leakage: signature_origin_unknown`` — the disclosure half of the
plan's suppression contract.

Metrics and ``corpus_sha256`` are computed inside ONE read snapshot, so a
hash always identifies the corpus the metrics were computed on.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from zaxbygraph import export
from zaxbygraph.derive import (
    _parse_instant,
    similar as derive_similar,
    localize_files,
)

#: Modes and their contract metrics (AC6's positional list):
#: similar -> recall@5 + MRR; localize -> Acc@5; bundle -> pair
#: recall/precision. Extra metrics may be reported but do not replace these.
EVAL_MODES = ("similar", "localize", "bundle")


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


def _closed_issues(conn: sqlite3.Connection, repo: str, pr_number: int) -> list[int]:
    rows = conn.execute(
        "SELECT dst_id FROM edges WHERE repo = ? AND src_type = 'item'"
        " AND dst_type = 'item' AND src_id = ? AND rel = 'closes'",
        (repo, str(pr_number)),
    ).fetchall()
    out = []
    for row in rows:
        try:
            out.append(int(row["dst_id"]))
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
) -> bool:
    """True when the item's title/body has a revision valid from after the
    cutoff (best-effort: the revisions table is v8+; its absence simply
    means the control cannot be applied and the caller labels leakage)."""
    if cutoff_dt is None:
        return False
    try:
        rows = conn.execute(
            "SELECT kind, valid_from FROM revisions WHERE repo = ?"
            " AND number = ? AND entity = 'item' AND kind IN"
            " ('title', 'body', 'title_body')",
            (repo, number),
        ).fetchall()
    except sqlite3.OperationalError:
        return False
    for row in rows:
        ts = _parse_instant(row["valid_from"])
        if ts is not None and ts > cutoff_dt:
            return True
    return False


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
    the store predates the v10 export floor."""
    labels = []
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < export.MIN_EXPORT_VERSION:
        labels.append("store_too_old")
        return None, labels
    pieces = export.iter_corpus_content_lines(conn, repo, frozenset())
    return export.corpus_sha256_from_lines(pieces), labels


def run_eval(
    conn: sqlite3.Connection,
    repo: str,
    mode: str,
    weights: dict | None = None,
) -> dict:
    """Run one eval mode under AC6's leakage controls.

    Metrics and ``corpus_sha256`` share one ``BEGIN DEFERRED`` snapshot so
    the hash always identifies the corpus the metrics were computed on.
    Every result is ``derived: true``; targets whose controls could not be
    fully applied are reported under ``with_leakage`` and excluded from the
    strict denominators.
    """
    if mode not in EVAL_MODES:
        raise ValueError(f"unknown eval mode: {mode} (expected one of {EVAL_MODES})")
    if not repo:
        raise ValueError("repo_required: eval needs a resolved repository")

    leakage: dict[int, list[str]] = {}
    conn.execute("BEGIN DEFERRED")
    try:
        sha, labels = _snapshot_sha(conn, repo)
        if mode == "similar":
            result = _eval_similar(conn, repo, weights, leakage)
        elif mode == "localize":
            result = _eval_localize(conn, repo, weights, leakage)
        else:
            result = _eval_bundle(conn, repo, weights, leakage)
    finally:
        conn.rollback()

    result.update(
        {
            "mode": mode,
            "repo": repo,
            "corpus_sha256": sha,
            "with_leakage": {str(k): v for k, v in sorted(leakage.items())},
            "labels": labels,
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
    Controls: candidates filtered to items created at/before the target;
    the target-partner adjacency removed; keyword-derived edges filtered
    from adjacency; signature component suppressed (origin-less rows)."""
    pairs = _duplicate_pairs(conn, repo)
    recalls: list[float] = []
    rr: list[float] = []
    eligible = 0
    for target, partners in sorted(pairs.items()):
        cutoff = _item_created(conn, repo, target)
        if cutoff is None:
            leakage.setdefault(target, []).append("missing_created_at")
            continue
        if _revised_after(conn, repo, target, _parse_instant(cutoff)):
            leakage.setdefault(target, []).append("item_revised_after_cutoff")
        # the signature component is suppressed unconditionally (origin-less
        # rows); the plan's disclosure half records that on every target
        leakage.setdefault(target, []).append("signature_origin_unknown")
        cutoff_dt = _parse_instant(cutoff)
        excluded_adjacency = {
            frozenset((target, partner)) for partner in partners
        }
        try:
            out = derive_similar(
                conn,
                repo,
                target,
                limit=20,
                weights=weights,
                exclude_adjacency=excluded_adjacency,
                filtered_sources={"keyword"},
                suppress_signals={"signature"},
            )
        except LookupError:
            leakage.setdefault(target, []).append("target_missing")
            continue
        visible, unknown = _visible_candidates(out, cutoff)
        if unknown:
            leakage.setdefault(target, []).append("candidate_created_at_unknown")
        ranked = [c["number"] for c in visible[:5]]
        hits = [1 if p in ranked else 0 for p in partners]
        if not any(hits):
            # A partner that never ranks may simply not exist in the pool —
            # only count targets whose partners are visible candidates at all.
            if not (set(partners) & {c["number"] for c in visible}):
                continue
        eligible += 1
        recalls.append(sum(hits) / len(partners))
        best = 0.0
        for partner in partners:
            if partner in ranked:
                best = max(best, 1.0 / (ranked.index(partner) + 1))
        rr.append(best)
    return {
        "metrics": {
            "recall_at_5": (sum(recalls) / len(recalls)) if recalls else None,
            "mrr": (sum(rr) / len(rr)) if rr else None,
            "eligible_targets": eligible,
            "targets_total": len(pairs),
        },
    }


def _eval_localize(
    conn: sqlite3.Connection,
    repo: str,
    weights: dict | None,
    leakage: dict[int, list[str]],
) -> dict:
    """Localization: for each issue a merged PR closed, is one of the fix's
    files in the top 5 ranked by :func:`localize_files` — with the fix PR
    itself excluded from the candidate pool and comment-derived references
    cut at the fix PR's creation?"""
    targets: set[int] = set()
    for row in conn.execute(
        "SELECT DISTINCT dst_id FROM edges WHERE repo = ?"
        " AND src_type = 'item' AND dst_type = 'item' AND rel = 'closes'",
        (repo,),
    ):
        try:
            targets.add(int(row["dst_id"]))
        except (TypeError, ValueError):
            continue
    hits: list[float] = []
    eligible = 0
    for target in sorted(targets):
        closing = _closing_prs(conn, repo, target)
        if not closing:
            continue
        truth_files: set[str] = set()
        for pr in closing:
            truth_files |= _merged_pr_files(conn, repo, pr)
        if not truth_files:
            continue
        cutoff = _item_created(conn, repo, target)
        if cutoff is None:
            leakage.setdefault(target, []).append("missing_created_at")
            continue
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
        reachable_truth = truth_files & _eval_pool(
            conn, repo, target, cutoff, closing
        )
        if not reachable_truth:
            continue
        eligible += 1
        top_paths = {f["path"] for f in out["files"][:5]}
        hits.append(1.0 if top_paths & reachable_truth else 0.0)
    return {
        "metrics": {
            "acc_at_5": (sum(hits) / len(hits)) if hits else None,
            "eligible_targets": eligible,
            "targets_total": len(targets),
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
        "SELECT pf.number, pf.path, pf.status FROM pr_files pf"
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
        merged = conn.execute(
            "SELECT merged_at FROM items WHERE repo = ? AND number = ?",
            (repo, row["number"]),
        ).fetchone()
        merged_dt = _parse_instant(merged["merged_at"]) if merged else None
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


def _eval_bundle(
    conn: sqlite3.Connection,
    repo: str,
    weights: dict | None,
    leakage: dict[int, list[str]],
) -> dict:
    """Bundles: issue pairs a single merged PR closed (ground truth) versus
    the pairs the derived tier proposes. The closing PR is removed from each
    target's adjacency (it is the ground-truth source), candidates are cut
    at the target's creation, keyword edges are filtered, signatures
    suppressed."""
    truth_pairs: set[frozenset[int]] = set()
    closed_by: dict[int, set[int]] = {}
    for row in conn.execute(
        "SELECT DISTINCT src_id FROM edges WHERE repo = ?"
        " AND src_type = 'item' AND dst_type = 'item' AND rel = 'closes'",
        (repo,),
    ):
        try:
            pr = int(row["src_id"])
        except (TypeError, ValueError):
            continue
        issues = _closed_issues(conn, repo, pr)
        if len(issues) >= 2:
            closed_by[pr] = set(issues)
            for i, a in enumerate(sorted(issues)):
                for b in sorted(issues)[i + 1 :]:
                    truth_pairs.add(frozenset((a, b)))
    if not truth_pairs:
        return {
            "metrics": {
                "pair_recall": None,
                "pair_precision": None,
                "eligible_targets": 0,
                "targets_total": 0,
                "truth_pairs": 0,
            },
        }
    target_issues = sorted({n for pair in truth_pairs for n in pair})
    proposed: set[frozenset[int]] = set()
    eligible = 0
    for target in target_issues:
        cutoff = _item_created(conn, repo, target)
        if cutoff is None:
            leakage.setdefault(target, []).append("missing_created_at")
            continue
        closing = _closing_prs(conn, repo, target)
        leakage.setdefault(target, []).append("signature_origin_unknown")
        excluded_adjacency = {frozenset((target, pr)) for pr in closing}
        try:
            out = derive_similar(
                conn,
                repo,
                target,
                limit=5,
                weights=weights,
                exclude_adjacency=excluded_adjacency,
                filtered_sources={"keyword"},
                suppress_signals={"signature"},
            )
        except LookupError:
            leakage.setdefault(target, []).append("target_missing")
            continue
        cutoff_dt = _parse_instant(cutoff)
        visible, unknown = _visible_candidates(out, cutoff, kind="issue")
        if unknown:
            leakage.setdefault(target, []).append("candidate_created_at_unknown")
        if visible:
            eligible += 1
        for c in visible:
            proposed.add(frozenset((target, c["number"])))
    truth_hit = sum(1 for p in proposed if p in truth_pairs)
    return {
        "metrics": {
            "pair_recall": truth_hit / len(truth_pairs),
            "pair_precision": truth_hit / len(proposed) if proposed else None,
            "eligible_targets": eligible,
            "targets_total": len(target_issues),
            "truth_pairs": len(truth_pairs),
        },
    }
