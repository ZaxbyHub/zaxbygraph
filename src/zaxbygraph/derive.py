"""Derived read tier (issue #29): similarity, coupling, and localization
computed at query time, never written to the store.

Everything in this module answers "what is related to what, and why" from
GitHub-reported facts already in the graph. Every result carries
``derived: true`` — these are computed opinions about relatedness, not
extracted evidence, and they must never be confused with the ``edges``
table's EXTRACTED facts (AGENTS.md invariants). The module only ever reads:
callers pass a ``mode=ro`` connection (``db.open_existing``), temp tables
aside, and a store file hash before/after any call is byte-identical.

Signal vocabulary (each returns a component with a score in [0, 1] plus
evidence): ``lexical`` (title+body term overlap, symmetric average),
``title``, ``signature`` (shared masked error signatures), ``files_direct``
(shared resolved paths), ``files_analog`` (same directory/basename family),
``cochange`` (co-change support between direct files), ``structural``
(shared item-item adjacency), ``context`` (author/milestone/labels/date
proximity — raise-only).

Anchor guard: only ``signature``, ``files_direct``, ``structural`` and
``cochange`` may CREATE a candidate pair. ``lexical``, ``title``,
``files_analog`` and ``context`` only RAISE an existing pair's score.
Affinity is symmetric by construction: every pairwise signal is a
symmetric set operation and lexical averages both directed coverages.
"""

from __future__ import annotations

import math
import re
import sqlite3
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from zaxbygraph.query import _clamp_limit

#: Default per-signal weights. Overrides live in ``[derive]`` tables of
#: ``<store>/config.toml``, ``.zaxbygraph.toml`` (cwd), or an explicit
#: ``--config`` file — later sources win per key.
DEFAULT_WEIGHTS = {
    "lexical": 1.0,
    "title": 2.0,
    "signature": 4.0,
    "files_direct": 4.0,
    "files_analog": 1.0,
    "cochange": 3.0,
    "structural": 2.0,
    "context": 1.0,
}

#: Signals allowed to CREATE a pair (the anchor guard, AC3). Everything else
#: only raises an anchored pair's score.
ANCHOR_SIGNALS = frozenset({"signature", "files_direct", "structural", "cochange"})

RAISE_SIGNALS = frozenset({"lexical", "title", "files_analog", "context"})

DEFAULT_HUB_SHARE = 0.5
DEFAULT_HUB_FLOOR = 0.25

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_TOKEN_MIN_LEN = 3
_TOKEN_STOP = frozenset(
    "the and for with this that from into have has not are was were but you your "
    "their there then when what which will would can could should may might "
    "issue pull request merge merged close closed fix fixed bug error file "
    "test tests".split()
)

_DAYS_PROXIMITY = 30
_COUPLING_MIN_FILES = 2
_COUPLING_MAX_FILES = 50


class ConfigError(ValueError):
    """Raised for malformed or invalid ``[derive]`` weight configuration."""

    def __init__(self, message: str, *, path: str | None = None) -> None:
        self.path = path
        super().__init__(message)


def tokenize(text: str | None) -> set[str]:
    """Lowercase word tokens, minus stopwords and very short fragments.

    These are the terms the ``lexical`` and ``title`` signals overlap. They
    are derived from the same text ``items_fts`` indexes, so an FTS-backed
    reader and this tokenizer see the same vocabulary; no FTS match query is
    needed because the candidate pass below is anchor-driven, not lexical.
    """
    if not text:
        return set()
    return {
        t
        for t in _TOKEN_RE.findall(text.lower())
        if len(t) >= _TOKEN_MIN_LEN and t not in _TOKEN_STOP
    }


def _norm_path(path: str) -> str:
    """Normalize a stored path to forward slashes (Windows-first reads)."""
    return path.replace("\\", "/").lstrip("./") if path else path


def load_weights(
    store_config: str | None = None,
    cwd_config: str | None = None,
    explicit_config: str | None = None,
) -> dict:
    """Resolve ``[derive]`` weights: defaults, then store, then cwd, then an
    explicit ``--config`` path — later sources win per key (merge, not
    replace). Raises :class:`ConfigError` naming the offending file for
    malformed TOML, unknown keys, non-finite/negative weights, or
    out-of-range hub parameters.
    """
    weights = dict(DEFAULT_WEIGHTS)
    hub = {"hub_share": DEFAULT_HUB_SHARE, "hub_floor": DEFAULT_HUB_FLOOR}
    allowed = set(DEFAULT_WEIGHTS) | set(hub)
    sources = [
        ("store", store_config),
        ("cwd", cwd_config),
        ("explicit", explicit_config),
    ]
    for label, path in sources:
        if not path:
            continue
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"malformed TOML in {label} config: {exc}", path=path) from exc
        except OSError as exc:
            raise ConfigError(f"cannot read {label} config: {exc}", path=path) from exc
        table = data.get("derive", {})
        if not isinstance(table, dict):
            raise ConfigError("[derive] must be a TOML table", path=path)
        for key, value in table.items():
            if key not in allowed:
                raise ConfigError(
                    f"unknown [derive] key {key!r} (allowed: {sorted(allowed)})",
                    path=path,
                )
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ConfigError(f"[derive] {key} must be a number", path=path)
            if not math.isfinite(value) or value < 0:
                raise ConfigError(
                    f"[derive] {key} must be finite and non-negative", path=path
                )
            if key in hub:
                hub[key] = float(value)
            else:
                weights[key] = float(value)
    if not (0 < hub["hub_share"] <= 1):
        raise ConfigError("[derive] hub_share must be in (0, 1]")
    if not (0 <= hub["hub_floor"] < 1):
        raise ConfigError("[derive] hub_floor must be in [0, 1)")
    weights.update(hub)
    return weights


class _Capabilities:
    """Runtime store capability probe (per-connection, computed once).

    Slot-gated surfaces degrade by absence rather than crashing: milestone
    needs the v7 column, item_path_refs/item_signatures need v9 tables.
    Runtime introspection keeps old stores readable without version
    coupling — the contract is the same one search's ``index_stale``
    degradation serves.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        item_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(items)")
        }
        self.has_path_refs = "item_path_refs" in tables
        self.has_signatures = "item_signatures" in tables
        self.has_pr_files = "pr_files" in tables
        self.has_labels = "labels" in tables
        self.has_milestone = "milestone" in item_cols
        self.notes: list[str] = []
        if not self.has_path_refs:
            self.notes.append("item_path_refs absent (pre-v9 store): path signals degraded")
        if not self.has_signatures:
            self.notes.append("item_signatures absent (pre-v9 store): signature signal omitted")
        if not self.has_milestone:
            self.notes.append("items.milestone absent (pre-v7 store): context signal degraded")


class _ItemData:
    __slots__ = (
        "number",
        "kind",
        "state",
        "title",
        "body",
        "author",
        "milestone",
        "created_at",
        "tokens",
        "title_tokens",
        "paths",
        "unresolved_refs",
        "signatures",
        "labels",
        "neighbors",
    )

    def __init__(self, row: sqlite3.Row) -> None:
        self.number = row["number"]
        self.kind = row["kind"]
        self.state = row["state"] if "state" in row.keys() else None
        self.title = row["title"] or ""
        self.body = row["body"]
        self.author = row["author"]
        self.milestone = row["milestone"] if "milestone" in row.keys() else None
        self.created_at = row["created_at"]
        self.tokens = tokenize(f"{self.title}\n{self.body or ''}")
        self.title_tokens = tokenize(self.title)
        self.paths: set[str] = set()
        self.unresolved_refs: list[str] = []
        self.signatures: set[tuple[str, str]] = set()
        self.labels: set[str] = set()
        self.neighbors: set[int] = set()


def _load_items(conn: sqlite3.Connection, repo: str, caps: _Capabilities) -> dict[int, _ItemData]:
    cols = "repo, number, kind, state, title, body, author, created_at"
    if caps.has_milestone:
        cols += ", milestone"
    items: dict[int, _ItemData] = {}
    for row in conn.execute(
        f"SELECT {cols} FROM items WHERE repo = ? ORDER BY number", (repo,)
    ):
        data = _ItemData(row)
        items[data.number] = data
    return items


def _parse_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _resolve_ref(ref: str, vocab: set[str], by_suffix: dict[str, list[str]]) -> str | None:
    """Deterministic vocabulary resolution: exact, then unique suffix.

    Ambiguous or unresolved references resolve to None and can never anchor
    a pair.
    """
    if ref in vocab:
        return ref
    matches = by_suffix.get(ref)
    if matches and len(matches) == 1:
        return matches[0]
    tails = [p for p in vocab if p.endswith("/" + ref) or ("/" + p).endswith("/" + ref)]
    if len(tails) == 1:
        return tails[0]
    return None


def build_context(
    conn: sqlite3.Connection,
    repo: str,
    caps: _Capabilities | None = None,
    ls_files: list[str] | None = None,
    exclude_adjacency: set[frozenset[int]] | None = None,
    filtered_sources: set[str] | None = None,
) -> dict:
    """Prefetch everything the signals need in one pass per table.

    The path vocabulary comes ONLY from merged PRs' ``pr_files`` minus
    ``removed`` statuses. Raw ``item_path_refs`` strings are then RESOLVED
    against that vocabulary (exact match, then unique-suffix match in either
    direction); unresolved references never enter a resolved path set and
    can never anchor a pair — they only feed ``files_analog`` evidence.
    ``ls_files`` (opt-in, from ``git ls-files`` of the invoking checkout)
    intersects the vocabulary LAST.
    """
    caps = caps or _Capabilities(conn)
    items = _load_items(conn, repo, caps)

    vocab_files: dict[str, set[int]] = {}
    if caps.has_pr_files:
        for row in conn.execute(
            "SELECT pf.number, pf.path FROM pr_files pf"
            " JOIN items i ON i.repo = pf.repo AND i.number = pf.number"
            " WHERE pf.repo = ? AND i.merged_at IS NOT NULL"
            " AND (pf.status IS NULL OR pf.status != 'removed')",
            (repo,),
        ):
            path = _norm_path(row["path"])
            if path:
                vocab_files.setdefault(path, set()).add(row["number"])

    if ls_files is not None:
        allowed = {_norm_path(p) for p in ls_files}
        vocab_files = {p: nums for p, nums in vocab_files.items() if p in allowed}

    # Resolution: exact match wins; otherwise a unique suffix match in
    # either direction (ref is a tail of a vocab path, or a vocab path is a
    # tail of the ref). Ambiguous suffixes resolve to nothing.
    by_suffix: dict[str, list[str]] = {}
    for path in vocab_files:
        parts = PurePosixPath(path).parts
        for i in range(len(parts)):
            by_suffix.setdefault("/".join(parts[i:]), []).append(path)

    if caps.has_path_refs:
        for row in conn.execute(
            "SELECT number, path FROM item_path_refs WHERE repo = ?",
            (repo,),
        ):
            data = items.get(row["number"])
            if data is None:
                continue
            ref = _norm_path(row["path"])
            if not ref:
                continue
            resolved = _resolve_ref(ref, vocab_files, by_suffix)
            if resolved is not None:
                data.paths.add(resolved)
            else:
                data.unresolved_refs.append(ref)

    if caps.has_signatures:
        for row in conn.execute(
            "SELECT number, exception, error_line FROM item_signatures WHERE repo = ?",
            (repo,),
        ):
            data = items.get(row["number"])
            if data is not None:
                data.signatures.add((row["exception"], row["error_line"]))

    if caps.has_labels:
        for row in conn.execute(
            "SELECT number, name FROM labels WHERE repo = ?", (repo,)
        ):
            data = items.get(row["number"])
            if data is not None:
                data.labels.add(row["name"])

    # Structural adjacency: item-item edges only. Labels, actors, files and
    # commits are endpoints of OTHER relations and never count here (labels
    # raise via context; they must never anchor — AC3). Keyword-stream edges
    # stay in direct reads; the eval harness (which filters keyword sources
    # to honor its comment cutoffs) passes filtered_sources={"keyword"}.
    item_types = ("item",)
    excluded = exclude_adjacency or set()
    for row in conn.execute(
        "SELECT src_type, src_id, dst_type, dst_id, source FROM edges"
        " WHERE repo = ? AND rel NOT IN"
        " ('closes_keyword', 'duplicate_keyword')",
        (repo,),
    ):
        if row["src_type"] in item_types and row["dst_type"] in item_types:
            if filtered_sources and row["source"] in filtered_sources:
                continue
            try:
                src = int(row["src_id"])
                dst = int(row["dst_id"])
            except ValueError:
                continue
            if frozenset((src, dst)) in excluded:
                continue
            if src in items and dst in items:
                items[src].neighbors.add(dst)
                items[dst].neighbors.add(src)

    # Hub statistics over the resolved vocabulary: a path touching a large
    # share of items carries less discriminating evidence.
    hub = {
        p: len(nums) / max(1, len(items)) for p, nums in vocab_files.items()
    }
    return {
        "repo": repo,
        "items": items,
        "vocab": set(vocab_files),
        "hub_share_of_items": hub,
        "caps": caps,
    }


def _file_weight(path: str, ctx: dict, weights: dict) -> float:
    share = ctx["hub_share_of_items"].get(path, 0.0)
    if share and share >= weights["hub_share"]:
        return weights["hub_floor"]
    return 1.0


def _overlap_score(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def pair_components(a: _ItemData, b: _ItemData, ctx: dict, weights: dict) -> dict:
    """Compute every signal component for one unordered pair.

    Symmetric by construction: ``pair_components(a, b) ==
    pair_components(b, a)`` — all set operations, and the lexical score is
    the average of the two directed coverages.
    """
    components: dict[str, dict] = {}

    shared_terms_set = a.tokens & b.tokens
    cov_ab = len(shared_terms_set) / len(a.tokens) if a.tokens else 0.0
    cov_ba = len(shared_terms_set) / len(b.tokens) if b.tokens else 0.0
    shared_terms = sorted(shared_terms_set)[:12]
    components["lexical"] = {
        "score": (cov_ab + cov_ba) / 2,
        "evidence": {"shared_terms": shared_terms},
    }

    title_overlap = a.title_tokens & b.title_tokens
    components["title"] = {
        "score": _overlap_score(a.title_tokens, b.title_tokens),
        "evidence": {"shared_terms": sorted(title_overlap)[:12]},
    }

    shared_sigs = a.signatures & b.signatures
    components["signature"] = {
        "score": _overlap_score(a.signatures, b.signatures) if shared_sigs else 0.0,
        "evidence": {
            "shared": [
                {"exception": e, "error_line": line} for e, line in sorted(shared_sigs)[:5]
            ]
        },
    }

    direct_a, direct_b = a.paths, b.paths
    shared_paths = direct_a & direct_b
    if shared_paths:
        wa = sum(_file_weight(p, ctx, weights) for p in direct_a)
        wb = sum(_file_weight(p, ctx, weights) for p in direct_b)
        wi = sum(_file_weight(p, ctx, weights) for p in shared_paths)
        score = wi / (wa + wb - wi) if (wa + wb - wi) else 0.0
    else:
        score = 0.0
    components["files_direct"] = {
        "score": score,
        "evidence": {"shared_paths": sorted(shared_paths)[:12]},
    }

    analog_pairs = []
    by_dir: dict[str, set[str]] = {}
    by_base: dict[str, set[str]] = {}
    for p in direct_a | direct_b:
        parts = PurePosixPath(p)
        parent = str(parts.parent) if str(parts.parent) not in (".", "") else ""
        by_dir.setdefault(parent, set()).add(p)
        by_base.setdefault(parts.name, set()).add(p)
    for group in list(by_dir.values()) + list(by_base.values()):
        in_a = sorted(group & direct_a)
        in_b = sorted(group & direct_b)
        if in_a and in_b and not (set(in_a) & set(in_b)):
            analog_pairs.append((in_a[0], in_b[0]))
    analog_pairs = sorted(set(analog_pairs))
    denom = min(len(direct_a), len(direct_b)) or 1
    components["files_analog"] = {
        "score": min(1.0, len(analog_pairs) / denom) if analog_pairs else 0.0,
        "evidence": {"pairs": [list(p) for p in analog_pairs[:8]]},
    }

    shared_neighbors = a.neighbors & b.neighbors
    union = a.neighbors | b.neighbors
    components["structural"] = {
        "score": (len(shared_neighbors) / len(union)) if union else 0.0,
        "evidence": {"shared_adjacent_items": sorted(shared_neighbors)[:12]},
    }

    shared_labels = a.labels & b.labels
    same_author = bool(a.author) and a.author == b.author
    same_milestone = (
        ctx["caps"].has_milestone
        and bool(a.milestone)
        and a.milestone == b.milestone
    )
    da, db_ = _parse_instant(a.created_at), _parse_instant(b.created_at)
    close_days = False
    if da and db_:
        close_days = abs((da - db_).total_seconds()) <= _DAYS_PROXIMITY * 86400
    facets = sum(1 for f in (shared_labels, same_author, same_milestone, close_days) if f)
    components["context"] = {
        "score": facets / 4,
        "evidence": {
            "shared_labels": sorted(shared_labels)[:8],
            "same_author": same_author,
            "same_milestone": bool(same_milestone),
            "created_within_30d": close_days,
        },
    }

    return components


def _cochange_between(
    a_paths: set[str], b_paths: set[str], ctx: dict
) -> tuple[float, list[dict]]:
    """Co-change signal between two resolved path sets (direct files only).

    Score is the maximum single-pair confidence; evidence carries the
    contributing pairs with their support.
    """
    support = ctx.get("coupling_support")
    if support is None:
        return 0.0, []
    best = 0.0
    pairs = []
    for fa in sorted(a_paths):
        row = support.get(fa)
        if not row:
            continue
        for fb, conf, sup in row:
            if fb in b_paths:
                pairs.append({"a": fa, "b": fb, "support": sup, "confidence": conf})
                best = max(best, conf)
    pairs.sort(key=lambda p: (-p["support"], p["a"], p["b"]))
    return best, pairs[:6]


def _apply_cochange(a: _ItemData, b: _ItemData, ctx: dict, components: dict) -> None:
    score, pairs = _cochange_between(a.paths, b.paths, ctx)
    components["cochange"] = {"score": score, "evidence": {"pairs": pairs}}


def _total_score(components: dict, weights: dict) -> float:
    return sum(
        weights[name] * comp["score"] for name, comp in components.items()
    )


def _anchor_fired(components: dict) -> str | None:
    for name in sorted(ANCHOR_SIGNALS):
        comp = components.get(name)
        if comp and comp["score"] > 0:
            return name
    return None


def similar(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    limit: int = 20,
    weights: dict | None = None,
    ls_files: list[str] | None = None,
    exclude_adjacency: set[frozenset[int]] | None = None,
    filtered_sources: set[str] | None = None,
    suppress_signals: set[str] | None = None,
) -> dict:
    """Rank items related to ``number`` with per-signal evidence.

    Raises LookupError when the item does not exist in ``repo``. The target
    itself is never a candidate. A candidate pair is admitted only when an
    anchor signal fired (AC3's guard); raisers then lift its score.
    ``exclude_adjacency`` (eval) removes specific item pairs from the
    structural adjacency; ``filtered_sources`` (eval) drops keyword-derived
    edges from adjacency so comment-cutoff controls stay honest;
    ``suppress_signals`` (eval) zeroes named components — the harness
    suppresses ``signature`` because ``item_signatures`` rows carry no
    origin and a cutoff-visible subset cannot be reconstructed.
    """
    weights = weights or load_weights()
    limit = _clamp_limit(limit)
    caps = _Capabilities(conn)
    ctx = build_context(
        conn,
        repo,
        caps,
        ls_files=ls_files,
        exclude_adjacency=exclude_adjacency,
        filtered_sources=filtered_sources,
    )
    index, file_support, _population = _coupling_index(conn, repo, caps)
    ctx["coupling_support"] = index
    ctx["coupling_filesupport"] = file_support
    items = ctx["items"]
    target = items.get(number)
    if target is None:
        raise LookupError(f"item not found: #{number}")

    suppressed = suppress_signals or frozenset()
    candidates = []
    for other in items.values():
        if other.number == number:
            continue
        components = pair_components(target, other, ctx, weights)
        _apply_cochange(target, other, ctx, components)
        for name in suppressed:
            if name in components:
                components[name] = {"score": 0.0, "evidence": {"suppressed": True}}
        anchor = _anchor_fired(components)
        if anchor is None:
            continue
        total = _total_score(components, weights)
        candidates.append(
            {
                "number": other.number,
                "kind": other.kind,
                "state": other.state,
                "title": other.title,
                "created_at": other.created_at,
                "anchor": anchor,
                "total_score": round(total, 6),
                "components": {
                    name: {
                        "score": round(comp["score"], 6),
                        "evidence": comp["evidence"],
                    }
                    for name, comp in components.items()
                },
            }
        )
    candidates.sort(key=lambda c: (-c["total_score"], c["number"]))
    candidates = candidates[:limit]
    return {
        "target": number,
        "repo": repo,
        "candidates": candidates,
        "weights": weights,
        "capabilities": caps.notes,
        "derived": True,
    }


def explain(
    conn: sqlite3.Connection,
    repo: str,
    a: int,
    b: int,
    weights: dict | None = None,
    ls_files: list[str] | None = None,
) -> dict:
    """Full per-signal breakdown for exactly one pair.

    ``similar``'s candidate entries are slices of the same
    :func:`pair_components` output this returns, so the two commands cannot
    diverge (AC4) — including under identical ``--ls-files`` input.
    """
    weights = weights or load_weights()
    caps = _Capabilities(conn)
    ctx = build_context(conn, repo, caps, ls_files=ls_files)
    index, file_support, _population = _coupling_index(conn, repo, caps)
    ctx["coupling_support"] = index
    ctx["coupling_filesupport"] = file_support
    items = ctx["items"]
    if a not in items:
        raise LookupError(f"item not found: #{a}")
    if b not in items:
        raise LookupError(f"item not found: #{b}")
    if a == b:
        return {
            "a": a,
            "b": b,
            "repo": repo,
            "components": {
                name: {"score": 0.0, "evidence": comp["evidence"]}
                for name, comp in pair_components(items[a], items[b], ctx, weights).items()
            }
            | {
                "cochange": {"score": 0.0, "evidence": {"pairs": []}},
            },
            "total_score": 0.0,
            "anchor": None,
            "weights": weights,
            "capabilities": caps.notes,
            "derived": True,
        }
    ia, ib = items[a], items[b]
    components = pair_components(ia, ib, ctx, weights)
    _apply_cochange(ia, ib, ctx, components)
    return {
        "a": a,
        "b": b,
        "repo": repo,
        "components": {
            name: {"score": round(comp["score"], 6), "evidence": comp["evidence"]}
            for name, comp in components.items()
        },
        "total_score": round(_total_score(components, weights), 6),
        "anchor": _anchor_fired(components),
        "weights": weights,
        "capabilities": caps.notes,
        "derived": True,
    }


def _merged_pr_paths(
    conn: sqlite3.Connection, repo: str, caps: _Capabilities
) -> set[str]:
    """Every path in any merged PR's files (removed excluded), regardless of
    the 2-50 population. Distinguishes "unknown path" from "exists but its
    PRs fall outside the coupling population"."""
    if not caps.has_pr_files:
        return set()
    paths: set[str] = set()
    for row in conn.execute(
        "SELECT pf.path FROM pr_files pf"
        " JOIN items i ON i.repo = pf.repo AND i.number = pf.number"
        " WHERE pf.repo = ? AND i.merged_at IS NOT NULL"
        " AND (pf.status IS NULL OR pf.status != 'removed')",
        (repo,),
    ):
        path = _norm_path(row["path"])
        if path:
            paths.add(path)
    return paths


def _coupling_index(
    conn: sqlite3.Connection, repo: str, caps: _Capabilities
) -> tuple[dict[str, list[tuple[str, float, int]]], dict[str, int], int]:
    """Co-change index over merged PRs touching 2-50 distinct files.

    Population N = merged PRs whose distinct stored file count is within
    [2, 50]. support(A,B) = number of those PRs containing both;
    confidence(B|A) = support(A,B) / support(A); the index stores, per file
    A, the pairs (B, confidence(B|A), support(A,B)) with support >= 1, and
    the per-file support(A) counts are returned alongside (they cannot be
    recovered from the pair index — averaging pair supports is not file
    support). Returns ``(index, file_support, population_n)``. Unmerged PRs
    are excluded by construction.
    """
    if not caps.has_pr_files:
        return {}, {}, 0
    pr_paths: dict[int, set[str]] = {}
    for row in conn.execute(
        "SELECT pf.number, pf.path FROM pr_files pf"
        " JOIN items i ON i.repo = pf.repo AND i.number = pf.number"
        " WHERE pf.repo = ? AND i.merged_at IS NOT NULL"
        " AND (pf.status IS NULL OR pf.status != 'removed')",
        (repo,),
    ):
        path = _norm_path(row["path"])
        if path:
            pr_paths.setdefault(row["number"], set()).add(path)
    population = [paths for paths in pr_paths.values() if 2 <= len(paths) <= 50]
    pair_support: dict[tuple[str, str], int] = {}
    file_support: dict[str, int] = {}
    for paths in population:
        ordered = sorted(paths)
        for i, fa in enumerate(ordered):
            file_support[fa] = file_support.get(fa, 0) + 1
            for fb in ordered[i + 1 :]:
                key = (fa, fb)
                pair_support[key] = pair_support.get(key, 0) + 1
    index: dict[str, list[tuple[str, float, int]]] = {}
    for (fa, fb), sup in pair_support.items():
        conf_b_given_a = sup / file_support[fa]
        conf_a_given_b = sup / file_support[fb]
        index.setdefault(fa, []).append((fb, conf_b_given_a, sup))
        index.setdefault(fb, []).append((fa, conf_a_given_b, sup))
    for entries in index.values():
        entries.sort(key=lambda e: (-e[2], e[0]))
    return index, file_support, len(population)


def coupling(
    conn: sqlite3.Connection,
    repo: str,
    path: str,
    limit: int = 20,
) -> dict:
    """Files that change together with ``path``, with support/confidence/lift.

    lift = N * support(A,B) / (support(A) * support(B)); it is ``null`` —
    never NaN or infinite — when either file never appears alone in the
    population. Zero-support queries return an explicit empty result with a
    reason instead of silent zeros.
    """
    limit = _clamp_limit(limit)
    caps = _Capabilities(conn)
    normalized = _norm_path(path)
    index, file_support, population_n = _coupling_index(conn, repo, caps)
    merged_paths = _merged_pr_paths(conn, repo, caps)
    entries = index.get(normalized, [])
    results = []
    for fb, conf, sup in entries[: max(0, limit)]:
        sup_b = file_support.get(fb, 0)
        sup_a = file_support.get(normalized, 0)
        lift = None
        if sup_b and sup_a:
            lift = population_n * sup / (sup_a * sup_b)
        results.append(
            {
                "path": fb,
                "support": sup,
                "confidence": round(conf, 6),
                "lift": round(lift, 6) if lift is not None else None,
            }
        )
    reason = None
    if not results:
        if not population_n:
            reason = "no_merged_prs"
        elif normalized not in file_support:
            # The path exists in merged PRs but its PRs fall outside the
            # 2-50 population (single-file / 51-file): exists-but-excluded.
            # A path no merged PR ever touched is genuinely unknown.
            if normalized in merged_paths:
                reason = "excluded_from_population"
            else:
                reason = "unknown_path"
        else:
            reason = "excluded_from_population"
    return {
        "path": normalized,
        "repo": repo,
        "population_prs": population_n,
        "results": results,
        "reason": reason,
        "derived": True,
    }


def origin_timestamp(
    conn: sqlite3.Connection, repo: str, number: int, origin: str
) -> tuple[str | None, bool]:
    """Resolve one ``item_path_refs`` origin to its source timestamp.

    Returns ``(timestamp, resolved)``. Per-prefix joins (each scoped by repo
    and owning number): ``issue_comment:<id>`` / ``review_comment:<id>`` →
    the ``comments`` row of that kind and github id → ``created_at``;
    ``review:<id>`` → the ``reviews`` row → ``submitted_at`` (reviews carry
    submitted_at, not created_at); ``body`` → ``(None, True)`` (the item's
    own body — cutoff governance is the caller's revisions check). A missing
    or ambiguous source row returns ``(None, False)`` — the caller must
    treat that row conservatively (suppress + flag leakage).
    """
    prefix, _, id_part = origin.partition(":")
    if prefix == "body":
        return None, True
    if not id_part or not id_part.isdigit():
        return None, False
    if prefix in ("issue_comment", "review_comment"):
        row = conn.execute(
            "SELECT created_at FROM comments WHERE repo = ? AND number = ?"
            " AND kind = ? AND github_id = ?",
            (repo, number, prefix, int(id_part)),
        ).fetchone()
        if row is None or row["created_at"] is None:
            return None, False
        return row["created_at"], True
    if prefix == "review":
        row = conn.execute(
            "SELECT submitted_at FROM reviews WHERE repo = ? AND number = ?"
            " AND id = ?",
            (repo, number, int(id_part)),
        ).fetchone()
        if row is None or row["submitted_at"] is None:
            return None, False
        return row["submitted_at"], True
    return None, False


def localize_files(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    weights: dict | None = None,
    cutoff: str | None = None,
    top: int = 5,
    excluded_prs: set[int] | None = None,
    comment_cutoff: str | None = None,
) -> dict:
    """Rank candidate files for an issue (the ranked input eval scores).

    Candidate pool: the path vocabulary from PRs merged at or before the
    item-visibility cutoff (``cutoff``), minus ``excluded_prs`` (eval removes
    the target's closing PRs so their files cannot leak into the pool).
    Co-visibility (defined): item X is co-visible with the target when it
    shares at least one resolved path with it, or a coupling edge with
    support >= 2 connects one of X's resolved files to one of the target's —
    all within the cutoff. When ``comment_cutoff`` is set (the ground-truth
    fix PR's creation), the target's own path references are filtered by
    their origin timestamps: refs from comments/reviews after that moment
    are excluded, an origin that fails to resolve suppresses the ref and
    flags the result. Deterministic order: score descending, then path
    ascending.
    """
    weights = weights or load_weights()
    caps = _Capabilities(conn)
    ctx = build_context(conn, repo, caps)
    items = ctx["items"]
    target = items.get(number)
    if target is None:
        raise LookupError(f"item not found: #{number}")
    cutoff_dt = _parse_instant(cutoff or target.created_at)
    comment_cutoff_dt = _parse_instant(comment_cutoff)
    leakage_notes: list[str] = []

    # The target's own resolved refs, filtered by the comment cutoff when the
    # eval harness supplies one.
    target_paths: set[str]
    if comment_cutoff_dt is not None:
        pool_vocab = ctx["vocab"]
        by_suffix: dict[str, list[str]] = {}
        for path in pool_vocab:
            parts = PurePosixPath(path).parts
            for i in range(len(parts)):
                by_suffix.setdefault("/".join(parts[i:]), []).append(path)
        target_paths = set()
        for row in conn.execute(
            "SELECT path, origin FROM item_path_refs WHERE repo = ? AND number = ?",
            (repo, number),
        ):
            ref = _norm_path(row["path"])
            if not ref:
                continue
            ts, resolved = origin_timestamp(conn, repo, number, row["origin"])
            if not resolved:
                leakage_notes.append(f"origin_unresolved:{row['origin']}")
                continue
            ts_dt = _parse_instant(ts)
            if row["origin"] != "body" and ts_dt is not None and ts_dt > comment_cutoff_dt:
                continue
            resolved_path = _resolve_ref(ref, pool_vocab, by_suffix)
            if resolved_path is not None:
                target_paths.add(resolved_path)
    else:
        target_paths = set(target.paths)

    merged_at: dict[int, str | None] = {}
    if caps.has_pr_files:
        for row in conn.execute(
            "SELECT number, merged_at FROM items WHERE repo = ?"
            " AND kind = 'pr' AND merged_at IS NOT NULL",
            (repo,),
        ):
            merged_at[row["number"]] = row["merged_at"]

    def _within_cutoff(pr_number: int) -> bool:
        if excluded_prs and pr_number in excluded_prs:
            return False
        if cutoff_dt is None:
            return True
        merged = _parse_instant(merged_at.get(pr_number))
        return merged is not None and merged <= cutoff_dt

    # Cutoff-restricted vocabulary and per-item eligible path sets.
    eligible: dict[int, set[str]] = {n: set() for n in items}
    pool: set[str] = set()
    if caps.has_pr_files:
        for row in conn.execute(
            "SELECT pf.number, pf.path FROM pr_files pf"
            " JOIN items i ON i.repo = pf.repo AND i.number = pf.number"
            " WHERE pf.repo = ? AND i.merged_at IS NOT NULL"
            " AND (pf.status IS NULL OR pf.status != 'removed')",
            (repo,),
        ):
            if _within_cutoff(row["number"]):
                path = _norm_path(row["path"])
                if path:
                    eligible.setdefault(row["number"], set()).add(path)
                    pool.add(path)
    target_paths = {p for p in target_paths if p in pool}

    # Co-change within the cutoff window only.
    pr_paths: dict[int, set[str]] = {}
    for n, paths in eligible.items():
        if n in merged_at and paths:
            pr_paths[n] = paths
    population = [paths for paths in pr_paths.values() if 2 <= len(paths) <= 50]
    pair_support: dict[tuple[str, str], int] = {}
    file_support: dict[str, int] = {}
    for paths in population:
        ordered = sorted(paths)
        for i, fa in enumerate(ordered):
            file_support[fa] = file_support.get(fa, 0) + 1
            for fb in ordered[i + 1 :]:
                key = (fa, fb)
                pair_support[key] = pair_support.get(key, 0) + 1

    def _pair_conf(fa: str, fb: str) -> float:
        if fa > fb:
            fa, fb = fb, fa
        sup = pair_support.get((fa, fb), 0)
        denom = file_support.get(fa, 0)
        return sup / denom if denom else 0.0

    # Co-visible items: share a resolved path with the target, or carry a
    # coupling edge (support >= 2) to one of the target's paths.
    covisible_files: dict[str, float] = {}

    def _offer(path: str, score: float) -> None:
        if score > covisible_files.get(path, 0.0):
            covisible_files[path] = score

    for path in target_paths:
        _offer(path, 1.0)
    if target_paths:
        for other_num, other_paths in eligible.items():
            if other_num == number or not other_paths:
                continue
            shares = target_paths & other_paths
            linked = bool(shares) or any(
                pair_support.get((min(fa, fb), max(fa, fb)), 0) >= 2
                for fa in other_paths
                for fb in target_paths
            )
            if not linked:
                continue
            other_score = 0.0
            for fa in other_paths:
                for fb in target_paths:
                    other_score = max(other_score, _pair_conf(fa, fb))
            for path in other_paths:
                # Only the shared paths themselves carry the direct 1.0
                # evidence; the co-visible item's other paths carry the
                # pair confidence, never the shared-path score.
                _offer(path, 1.0 if path in shares else other_score)
    ranked = sorted(
        ((p, s) for p, s in covisible_files.items() if p in pool),
        key=lambda e: (-e[1], e[0]),
    )[: max(0, top)]
    return {
        "target": number,
        "repo": repo,
        "cutoff": cutoff or target.created_at,
        "files": [
            {"path": p, "score": round(s, 6)} for p, s in ranked
        ],
        "pool_size": len(pool),
        "leakage_notes": sorted(set(leakage_notes)),
        "derived": True,
    }
