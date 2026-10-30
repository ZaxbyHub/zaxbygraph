"""Derived read tier (issue #29): similarity, coupling, and localization
computed at query time, never written to the store — extended (issue #30)
with the work-selection layer: :func:`triage`, :func:`bundle` and
:func:`brief` rank open issues, group them into work bundles, and emit one
issue's work packet. All of it is candidates, not facts: heuristics over
stored evidence that no code path ever writes back.

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

import fnmatch
import hashlib
import json
import math
import re
import sqlite3
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

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

#: Weights are relative, so nothing legitimate needs a magnitude anywhere
#: near this bound — but an unbounded weight makes ``total_score`` overflow
#: to ``inf``, which ``json.dump`` emits as bare ``Infinity``: not valid
#: RFC 8259 (pr43 review PRR-013).
_MAX_WEIGHT = 1e9

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
    """Normalize a stored path to forward slashes (Windows-first reads).

    Only a literal ``./`` prefix is stripped: ``lstrip("./")`` would take a
    character SET and rename every dotfile (``.github/x`` -> ``github/x``),
    colliding with real non-dot paths (pr43 review PRR-001).
    """
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def load_weights(
    store_config: str | None = None,
    cwd_config: str | None = None,
    explicit_config: str | None = None,
) -> dict:
    """Resolve ``[derive]`` weights: defaults, then store, then cwd, then an
    explicit ``--config`` path — later sources win per key (merge, not
    replace). Raises :class:`ConfigError` naming the offending file for
    malformed TOML, unknown keys, non-finite/negative/over-magnitude
    weights, or out-of-range hub parameters.
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
            # math.isfinite raises OverflowError (not ValueError) on ints too
            # large for float, so the magnitude check needs its own guard
            # (pr43 review PRR-012); the upper bound keeps total_score inside
            # what json can emit — an unbounded weight turns a score into a
            # bare `Infinity`, which is not valid RFC 8259 (PRR-013).
            try:
                finite = math.isfinite(value)
            except OverflowError:
                raise ConfigError(
                    f"[derive] {key} magnitude is out of range", path=path
                ) from None
            if not finite or value < 0 or value > _MAX_WEIGHT:
                raise ConfigError(
                    f"[derive] {key} must be finite, non-negative, and <= {_MAX_WEIGHT}",
                    path=path,
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
    """Deterministic vocabulary resolution: exact, then unique suffix in
    either direction — the ref is a component-boundary tail of a vocab path
    (the common case, ``query.py`` -> ``src/zaxbygraph/query.py``; that is
    exactly what ``by_suffix`` indexes), or a vocab path is a tail of the
    ref (absolute or checkout-prefixed mentions, ``/repo/src/a.py`` ->
    ``src/a.py``).

    Ambiguous or unresolved references resolve to None and can never anchor
    a pair.
    """
    if ref in vocab:
        return ref
    matches = by_suffix.get(ref)
    if matches and len(matches) == 1:
        return matches[0]
    # Reverse direction: a vocab path that is a tail of the longer ref.
    heads = [p for p in vocab if p != ref and ref.endswith("/" + p)]
    if len(heads) == 1:
        return heads[0]
    return None


def build_context(
    conn: sqlite3.Connection,
    repo: str,
    caps: _Capabilities | None = None,
    ls_files: list[str] | None = None,
    exclude_adjacency: set[frozenset[int]] | None = None,
    filtered_sources: set[str] | None = None,
    item_cutoff: str | None = None,
    comment_cutoff: str | None = None,
) -> dict:
    """Prefetch everything the signals need in one pass per table.

    The path vocabulary comes ONLY from merged PRs' ``pr_files`` minus
    ``removed`` statuses. Raw ``item_path_refs`` strings are then RESOLVED
    against that vocabulary (exact match, then unique-suffix match in either
    direction); unresolved references never enter a resolved path set and
    can never anchor a pair. ``ls_files`` (opt-in, from ``git ls-files`` of
    the invoking checkout) intersects AFTER resolution — suffix ambiguity is
    decided by the full merged-PR vocabulary, never by the checkout filter,
    so ``similar --ls-files`` and plain ``similar`` share resolution
    semantics (pr43 review PRR-017). Hub statistics are likewise computed
    over the full vocabulary before the filter thins it.

    Eval-only scoping knobs (issue #44, both default ``None`` = current
    state): ``item_cutoff`` (an ISO instant) restricts the vocabulary to
    merged PRs with ``merged_at <= cutoff`` (ISO strings compare), and
    ``comment_cutoff`` filters stored path references BY ORIGIN TIMESTAMP
    for EVERY item in the context, not just a designated target — a
    reference riding a comment/review created after the cutoff is dropped,
    an origin whose source row cannot be found is suppressed (the
    :func:`origin_timestamp` joins behind ``localize_files``), and ``body``
    origins always survive (cutoff governance for the item's own text is
    the caller's revisions check).
    """
    caps = caps or _Capabilities(conn)
    items = _load_items(conn, repo, caps)

    vocab_files: dict[str, set[int]] = {}
    if caps.has_pr_files:
        vocab_sql = (
            "SELECT pf.number, pf.path FROM pr_files pf"
            " JOIN items i ON i.repo = pf.repo AND i.number = pf.number"
            " WHERE pf.repo = ? AND i.merged_at IS NOT NULL"
            " AND (pf.status IS NULL OR pf.status != 'removed')"
        )
        vocab_params: list[object] = [repo]
        if item_cutoff is not None:
            vocab_sql += " AND i.merged_at <= ?"
            vocab_params.append(item_cutoff)
        for row in conn.execute(vocab_sql, vocab_params):
            path = _norm_path(row["path"])
            if path:
                vocab_files.setdefault(path, set()).add(row["number"])

    # Resolution: exact match wins; otherwise a unique suffix match in
    # either direction (ref is a tail of a vocab path, or a vocab path is a
    # tail of the ref). Ambiguous suffixes resolve to nothing.
    by_suffix: dict[str, list[str]] = {}
    for path in vocab_files:
        parts = PurePosixPath(path).parts
        for i in range(len(parts)):
            by_suffix.setdefault("/".join(parts[i:]), []).append(path)

    comment_cutoff_dt = (
        _parse_instant(comment_cutoff) if comment_cutoff is not None else None
    )
    if caps.has_path_refs:
        # comment_cutoff: bulk-load the origin timestamps once (the
        # origin_timestamp per-ref joins, hoisted out of the row loop), so
        # per-item reference filtering costs one pass instead of one query
        # per stored ref.
        comment_ts: dict[tuple[int, str, int], str | None] = {}
        review_ts: dict[tuple[int, int], str | None] = {}
        if comment_cutoff_dt is not None:
            for row in conn.execute(
                "SELECT number, kind, github_id, created_at FROM comments"
                " WHERE repo = ?",
                (repo,),
            ):
                comment_ts[(row["number"], row["kind"], row["github_id"])] = row[
                    "created_at"
                ]
            for row in conn.execute(
                "SELECT number, id, submitted_at FROM reviews WHERE repo = ?",
                (repo,),
            ):
                review_ts[(row["number"], row["id"])] = row["submitted_at"]

        def _ref_visible(number: int, origin: str) -> bool:
            """The localize_files origin rule: body refs always visible;
            comment/review refs visible at/before the cutoff; an origin
            whose source row is missing is suppressed (conservative)."""
            if comment_cutoff_dt is None:
                return True
            prefix, _, id_part = origin.partition(":")
            if prefix == "body":
                return True
            if not id_part or not id_part.isdigit():
                return False
            if prefix in ("issue_comment", "review_comment"):
                ts = comment_ts.get((number, prefix, int(id_part)), None)
            elif prefix == "review":
                ts = review_ts.get((number, int(id_part)), None)
            else:
                return False
            if ts is None:
                return False
            ts_dt = _parse_instant(ts)
            return ts_dt is None or ts_dt <= comment_cutoff_dt

        for row in conn.execute(
            "SELECT number, path, origin FROM item_path_refs WHERE repo = ?",
            (repo,),
        ):
            data = items.get(row["number"])
            if data is None:
                continue
            ref = _norm_path(row["path"])
            if not ref:
                continue
            if not _ref_visible(row["number"], row["origin"]):
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

    # Hub statistics over the FULL resolved vocabulary: a path touching a
    # large share of the merged-PR population carries less discriminating
    # evidence. The denominator counts EVERY merged PR in scope — including
    # zero-file merges (issue #44 AC2: dividing by the item count let the
    # knob fire only on stores with few non-PR items). Computed before the
    # ls-files filter so the checkout never changes evidence weight, only
    # which paths can score.
    merged_population = 0
    population_sql = (
        "SELECT COUNT(*) FROM items WHERE repo = ? AND merged_at IS NOT NULL"
    )
    population_params: list[object] = [repo]
    if item_cutoff is not None:
        population_sql += " AND merged_at <= ?"
        population_params.append(item_cutoff)
    row = conn.execute(population_sql, population_params).fetchone()
    if row is not None:
        merged_population = int(row[0])
    hub = {
        p: len(nums) / max(1, merged_population) for p, nums in vocab_files.items()
    }

    # The ls-files intersection is applied LAST — after suffix resolution
    # and hub statistics, so the checkout filter never changes what a
    # reference resolves to or how much evidence a path carries; it only
    # thins which paths can score (pr43 review PRR-017).
    if ls_files is not None:
        allowed = {_norm_path(p) for p in ls_files}
        vocab_files = {p: nums for p, nums in vocab_files.items() if p in allowed}
        for data in items.values():
            if data.paths - allowed:
                data.paths &= allowed

    return {
        "repo": repo,
        "items": items,
        "vocab": set(vocab_files),
        "hub_share_of_population": hub,
        "caps": caps,
    }


def _file_weight(path: str, ctx: dict, weights: dict) -> float:
    share = ctx["hub_share_of_population"].get(path, 0.0)
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

    Score is the maximum single-pair symmetric confidence,
    ``support(A,B) / max(support(A), support(B))`` — commutative by
    construction, so affinity stays symmetric even when the two files'
    support counts differ (a directional ``support(A,B)/support(A)``
    score was order-dependent and could break AC2; cubic run-2 thread).
    Evidence carries the contributing pairs with their support.
    """
    support = ctx.get("coupling_support")
    if support is None:
        return 0.0, []
    file_support = ctx.get("coupling_filesupport") or {}
    best = 0.0
    pairs = []
    for fa in sorted(a_paths):
        row = support.get(fa)
        if not row:
            continue
        for fb, _conf, sup in row:
            if fb in b_paths:
                sym_conf = sup / max(
                    file_support.get(fa, sup), file_support.get(fb, sup)
                )
                pairs.append(
                    {"a": fa, "b": fb, "support": sup, "confidence": sym_conf}
                )
                best = max(best, sym_conf)
    pairs.sort(key=lambda p: (-p["support"], p["a"], p["b"]))
    return best, pairs[:6]


def _apply_cochange(a: _ItemData, b: _ItemData, ctx: dict, components: dict) -> None:
    score, pairs = _cochange_between(a.paths, b.paths, ctx)
    components["cochange"] = {"score": score, "evidence": {"pairs": pairs}}


def _total_score(components: dict, weights: dict) -> float:
    return sum(
        weights[name] * comp["score"] for name, comp in components.items()
    )


def _anchor_fired(components: dict, exclude: frozenset[str] = frozenset()) -> str | None:
    for name in sorted(ANCHOR_SIGNALS - exclude):
        comp = components.get(name)
        if comp and comp["score"] > 0:
            return name
    return None


def similar(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    limit: int | None = 20,
    weights: dict | None = None,
    ls_files: list[str] | None = None,
    exclude_adjacency: set[frozenset[int]] | None = None,
    filtered_sources: set[str] | None = None,
    suppress_signals: set[str] | None = None,
    item_cutoff: str | None = None,
    comment_cutoff: str | None = None,
) -> dict:
    """Rank items related to ``number`` with per-signal evidence.

    Raises LookupError when the item does not exist in ``repo``. The target
    itself is never a candidate. A candidate pair is admitted only when an
    anchor signal fired (AC3's guard); raisers then lift its score.
    ``limit=None`` returns the complete ranked pool — the eval harness uses
    it to apply its own visibility filters BEFORE taking a top-N, so a
    post-cutoff leader can never crowd a pre-cutoff partner out of the
    window (pr43 review PRR-006).
    ``exclude_adjacency`` (eval) removes specific item pairs from the
    structural adjacency; ``filtered_sources`` (eval) drops keyword-derived
    edges from adjacency so comment-cutoff controls stay honest;
    ``suppress_signals`` (eval) zeroes named components — the harness
    suppresses ``signature`` because ``item_signatures`` rows carry no
    origin and a cutoff-visible subset cannot be reconstructed.
    ``item_cutoff``/``comment_cutoff`` (eval-only, default ``None`` =
    current state) scope the path evidence (issue #44): the vocabulary and
    the co-change population keep only merged PRs from before the item
    cutoff, and every scored item's comment/review-derived path references
    are cut at the comment cutoff. Under scoping the cochange signal also
    loses its pair-CREATING vote (it still scores, over the cutoff-scoped
    population, and raises an already-anchored pair): ``pr_files`` rows
    carry no per-file origin, so a cutoff-visible co-change subset cannot
    be reconstructed — the same conservative anchor suppression the eval
    applies to origin-less ``item_signatures`` rows.
    """
    weights = weights or load_weights()
    caps = _Capabilities(conn)
    ctx = build_context(
        conn,
        repo,
        caps,
        ls_files=ls_files,
        exclude_adjacency=exclude_adjacency,
        filtered_sources=filtered_sources,
        item_cutoff=item_cutoff,
        comment_cutoff=comment_cutoff,
    )
    index, file_support, _population = _coupling_index(
        conn, repo, caps, item_cutoff=item_cutoff
    )
    ctx["coupling_support"] = index
    ctx["coupling_filesupport"] = file_support
    items = ctx["items"]
    target = items.get(number)
    if target is None:
        raise LookupError(f"item not found: #{number}")

    suppressed = suppress_signals or frozenset()
    scoped = item_cutoff is not None or comment_cutoff is not None
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
        if anchor == "cochange" and scoped:
            # Scoped read: cochange may only raise, never create (see
            # docstring) — fall through to the next anchor, if any.
            anchor = _anchor_fired(components, exclude=frozenset({"cochange"}))
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
    if limit is not None:
        candidates = candidates[: _clamp_limit(limit)]
    return {
        "target": number,
        "repo": repo,
        "candidates": candidates,
        "resolved_path_count": len(ctx["vocab"]),
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
            "resolved_path_count": len(ctx["vocab"]),
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
        "resolved_path_count": len(ctx["vocab"]),
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
    conn: sqlite3.Connection,
    repo: str,
    caps: _Capabilities,
    item_cutoff: str | None = None,
) -> tuple[dict[str, list[tuple[str, float, int]]], dict[str, int], int]:
    """Co-change index over merged PRs touching 2-50 distinct files.

    Population N = merged PRs whose distinct stored file count is within
    [2, 50]. support(A,B) = number of those PRs containing both;
    confidence(B|A) = support(A,B) / support(A); the index stores, per file
    A, the pairs (B, confidence(B|A), support(A,B)) with support >= 1, and
    the per-file support(A) counts are returned alongside (they cannot be
    recovered from the pair index — averaging pair supports is not file
    support). Returns ``(index, file_support, population_n)``. Unmerged PRs
    are excluded by construction. ``item_cutoff`` (eval-only, default
    ``None``) keeps only PRs merged at/before the cutoff, so post-cutoff
    co-change support can never anchor a pair (issue #44 AC1).
    """
    if not caps.has_pr_files:
        return {}, {}, 0
    pr_paths: dict[int, set[str]] = {}
    coupling_sql = (
        "SELECT pf.number, pf.path FROM pr_files pf"
        " JOIN items i ON i.repo = pf.repo AND i.number = pf.number"
        " WHERE pf.repo = ? AND i.merged_at IS NOT NULL"
        " AND (pf.status IS NULL OR pf.status != 'removed')"
    )
    coupling_params: list[object] = [repo]
    if item_cutoff is not None:
        coupling_sql += " AND i.merged_at <= ?"
        coupling_params.append(item_cutoff)
    for row in conn.execute(coupling_sql, coupling_params):
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

    ``weights`` is accepted for call-site symmetry with :func:`similar` and
    :func:`explain` but is unused today: file ranking is weight-free, so no
    ``[derive]`` configuration is loaded here (an invalid weights file must
    not abort a weight-free command — pr43 review PRR-025).
    """
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
        if caps.has_path_refs:
            # Pre-v9 stores have no item_path_refs table: there are no stored
            # refs to resolve, so target_paths stays empty instead of
            # crashing (pr43 review PRR-011).
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


# ==== issue #30: triage / bundle / brief (the work-selection layer) =========

#: Default ``[triage]`` table (AC9's blessed values, issue 30 comment
#: 6510715064). Overrides live in the same TOML chain as ``[derive]``:
#: ``<store>/config.toml`` -> ``.zaxbygraph.toml`` (cwd) -> ``--config``.
TRIAGE_DEFAULTS = {
    "demand": 1.0,
    "leverage": 1.0,
    "risk": 1.0,
    "priority": 1.0,
    "stage2_weight": 0.25,
    "top_k": 50,
    "affinity_floor": 3.0,
    "max_files": 12,
    "test_globs": ["tests/*"],
    "formula_version": "triage.v1",
}

#: [triage] map tables (exact name -> weight; the weight clamps to [0, 1]
#: AT SCORING TIME so the payload echo keeps the configured magnitude).
_TRIAGE_MAP_KEYS = ("labels", "types", "fields")

_TRIAGE_REQUIRES = {
    "reactions_plus1": "requires: reactions object absent from the stored payload",
    "milestone_due": "requires: milestone due_on absent from the stored payload",
    "sub_issue_gap": "requires: sub_issues_summary absent from the stored payload",
    "reopens": "requires: item_events table absent (pre-v10 store)",
    "issue_type": "requires: item_type absent or [triage.types] map not configured",
    "label_weight": "requires: no labels on the item or [triage.labels] map not configured",
    "field_weight": "requires: no issue field values or [triage.fields] map not configured",
}


def _triage_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_triage_config(
    store_config: str | None = None,
    cwd_config: str | None = None,
    explicit_config: str | None = None,
) -> dict:
    """Resolve the effective ``[triage]`` table: defaults, then store, then
    cwd, then an explicit ``--config`` path — scalars merge per key and the
    label/type/field maps merge PER NAME (later sources win per name, never
    replacing the whole map). Raises :class:`ConfigError` naming the file
    for malformed TOML, unknown keys, or invalid value types.

    Returns the effective table, always carrying every default key plus the
    three maps (empty dicts when unconfigured) — this dict is the payload
    ``weights`` echo and the input to bundle_id's config hash, and its
    ``_config_source`` key names which level supplied the table
    (``explicit``/``cwd``/``store``/``defaults``).
    """
    cfg = dict(TRIAGE_DEFAULTS)
    maps = {key: {} for key in _TRIAGE_MAP_KEYS}
    cfg.update(maps)
    supplied: str | None = None
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
            raise ConfigError(
                f"malformed TOML in {label} config: {exc}", path=path
            ) from exc
        except OSError as exc:
            raise ConfigError(f"cannot read {label} config: {exc}", path=path) from exc
        table = data.get("triage", {})
        if not isinstance(table, dict):
            raise ConfigError("[triage] must be a TOML table", path=path)
        if table:
            supplied = label
        for key, value in table.items():
            if key in _TRIAGE_MAP_KEYS:
                if not isinstance(value, dict):
                    raise ConfigError(f"[triage] {key} must be a TOML table", path=path)
                for name, weight in value.items():
                    if (
                        not _triage_number(weight)
                        or not math.isfinite(weight)
                        or weight < 0
                    ):
                        raise ConfigError(
                            f"[triage] {key}.{name} must be a finite non-negative number",
                            path=path,
                        )
                    maps[key][name] = float(weight)
            elif key in ("demand", "leverage", "risk", "priority", "stage2_weight",
                         "affinity_floor"):
                if not _triage_number(value) or not math.isfinite(value) or value < 0:
                    raise ConfigError(
                        f"[triage] {key} must be a finite non-negative number",
                        path=path,
                    )
                cfg[key] = float(value)
            elif key in ("top_k", "max_files"):
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise ConfigError(
                        f"[triage] {key} must be a positive integer", path=path
                    )
                cfg[key] = int(value)
            elif key == "test_globs":
                if not isinstance(value, list) or not all(
                    isinstance(g, str) for g in value
                ):
                    raise ConfigError(
                        "[triage] test_globs must be a list of fnmatch patterns",
                        path=path,
                    )
                cfg[key] = list(value)
            elif key == "formula_version":
                if not isinstance(value, str) or not value:
                    raise ConfigError(
                        "[triage] formula_version must be a non-empty string",
                        path=path,
                    )
                cfg[key] = value
            else:
                raise ConfigError(
                    f"unknown [triage] key {key!r}"
                    f" (allowed: {sorted(set(TRIAGE_DEFAULTS) | set(_TRIAGE_MAP_KEYS))})",
                    path=path,
                )
    cfg.update(maps)
    cfg["_config_source"] = supplied or "defaults"
    return cfg


def _triage_config_chain(
    conn: sqlite3.Connection, explicit: str | None
) -> tuple[str | None, str | None, str | None]:
    """The load_triage_config source chain discovered from a read
    connection: ``<store>/config.toml`` (via PRAGMA database_list — the
    derived functions never receive the db path), ``.zaxbygraph.toml`` in
    the cwd, then the explicit ``--config`` path."""
    store_cfg: str | None = None
    try:
        for _seq, name, file in conn.execute("PRAGMA database_list"):
            if name == "main" and file:
                candidate = Path(file).parent / "config.toml"
                if candidate.is_file():
                    store_cfg = str(candidate)
                break
    except sqlite3.Error:
        store_cfg = None
    cwd_cfg = None
    candidate = Path(".zaxbygraph.toml")
    if candidate.is_file():
        cwd_cfg = str(candidate)
    return store_cfg, cwd_cfg, explicit


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _item_state_map(conn: sqlite3.Connection, repo: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for row in conn.execute(
        "SELECT number, kind, state FROM items WHERE repo = ?", (repo,)
    ):
        out[row["number"]] = {"kind": row["kind"], "state": row["state"]}
    return out


def _synced_at(conn: sqlite3.Connection, repo: str) -> str | None:
    """The store's latest sync stamp: last_incr_sync_at falling back to
    last_full_sync_at — the triage/hotspot as-of anchor, so ranking is
    wall-clock independent and deterministic per store state."""
    row = conn.execute(
        "SELECT last_incr_sync_at, last_full_sync_at FROM sync_state WHERE repo = ?",
        (repo,),
    ).fetchone()
    if row is None:
        return None
    return row["last_incr_sync_at"] or row["last_full_sync_at"]


def _triage_features(conn: sqlite3.Connection, repo: str) -> dict:
    """Stage-1 raw features and gates for EVERY open issue: demand
    (reactions/participants/comments/duplicates/mentions), leverage
    (blocking/sub-issue gap/milestone), risk (reopens/file reverts) and the
    gate shapes (blocked/assigned/linked_pr/locked). Reactions and the
    milestone due date parse Python-side from ``raw_json`` — no JSON1."""
    has_events = _table_exists(conn, "item_events")
    has_field_values = _table_exists(conn, "issue_field_values")

    rows: dict[int, dict] = {}
    for row in conn.execute(
        "SELECT number, title, author, comment_count, locked, item_type,"
        " sub_issues_total, sub_issues_completed, dependencies_blocked_by,"
        " dependencies_blocking, raw_json FROM items"
        " WHERE repo = ? AND kind = 'issue' AND state = 'open'",
        (repo,),
    ):
        rows[row["number"]] = dict(row)

    comment_authors: dict[int, set[str]] = {}
    for row in conn.execute(
        "SELECT number, author FROM comments WHERE repo = ? AND author IS NOT NULL",
        (repo,),
    ):
        comment_authors.setdefault(row["number"], set()).add(row["author"])
    review_authors: dict[int, set[str]] = {}
    for row in conn.execute(
        "SELECT number, author FROM reviews WHERE repo = ? AND author IS NOT NULL",
        (repo,),
    ):
        review_authors.setdefault(row["number"], set()).add(row["author"])

    inbound_dups: dict[int, int] = {}
    for row in conn.execute(
        "SELECT dst_id, COUNT(DISTINCT src_id) AS n FROM edges"
        " WHERE repo = ? AND rel IN ('duplicate_of', 'duplicate_keyword')"
        " AND src_type = 'item' AND dst_type = 'item' GROUP BY dst_id",
        (repo,),
    ):
        try:
            inbound_dups[int(row["dst_id"])] = int(row["n"])
        except (TypeError, ValueError):
            continue
    inbound_mentions: dict[int, int] = {}
    for row in conn.execute(
        "SELECT dst_id, COUNT(DISTINCT src_id) AS n FROM edges"
        " WHERE repo = ? AND rel = 'mentions'"
        " AND src_type = 'item' AND dst_type = 'item' GROUP BY dst_id",
        (repo,),
    ):
        try:
            inbound_mentions[int(row["dst_id"])] = int(row["n"])
        except (TypeError, ValueError):
            continue

    blocked_by_edges: list[tuple[int, int]] = []
    any_blocked_by = False
    for row in conn.execute(
        "SELECT src_id, dst_id FROM edges WHERE repo = ? AND rel = 'blocked_by'"
        " AND src_type = 'item' AND dst_type = 'item'",
        (repo,),
    ):
        any_blocked_by = True
        try:
            blocked_by_edges.append((int(row["src_id"]), int(row["dst_id"])))
        except (TypeError, ValueError):
            continue
    states = _item_state_map(conn, repo)

    reverted_prs: set[int] = set()
    for row in conn.execute(
        "SELECT DISTINCT dst_id FROM edges WHERE repo = ? AND rel = 'reverts'"
        " AND src_type = 'item' AND dst_type = 'item'",
        (repo,),
    ):
        try:
            reverted_prs.add(int(row["dst_id"]))
        except (TypeError, ValueError):
            continue

    reopens: dict[int, int] = {}
    if has_events:
        for row in conn.execute(
            "SELECT number, COUNT(*) AS n FROM item_events"
            " WHERE repo = ? AND type = 'reopened' GROUP BY number",
            (repo,),
        ):
            reopens[row["number"]] = int(row["n"])

    assignees: set[int] = set()
    for row in conn.execute(
        "SELECT DISTINCT number FROM assignees WHERE repo = ?", (repo,)
    ):
        assignees.add(row["number"])

    field_values: dict[int, list[str]] = {}
    if has_field_values:
        for row in conn.execute(
            "SELECT number, value FROM issue_field_values WHERE repo = ?", (repo,)
        ):
            if row["value"] is not None:
                field_values.setdefault(row["number"], []).append(row["value"])

    # Per-item blocking: the edge stream wins when it carries any rows,
    # else the dependencies_blocking column (echoed as blocking_source).
    # blocking_open counts open AND UNGATED blocked items — rankable work N
    # unblocks. Counting a gated blocked item would float a blocker of
    # already-excluded work above every other row without unblocking
    # anything the ranking could act on.
    blocking_open_edges: dict[int, int] = {}
    blocked_by_out: dict[int, set[int]] = {}
    for src, dst in blocked_by_edges:
        blocked_by_out.setdefault(src, set()).add(dst)

    linked_pr_dsts: dict[int, set[int]] = {}
    for row in conn.execute(
        "SELECT src_id, dst_id FROM edges WHERE repo = ? AND rel = 'linked_pr'"
        " AND src_type = 'item' AND dst_type = 'item'",
        (repo,),
    ):
        try:
            linked_pr_dsts.setdefault(int(row["src_id"]), set()).add(
                int(row["dst_id"])
            )
        except (TypeError, ValueError):
            continue
    # Gates first (blocking_open needs to know which blocked items are
    # themselves ungated).
    gates_by: dict[int, list[str]] = {}
    for number, row in rows.items():
        gates: list[str] = []
        if any_blocked_by:
            is_blocked = any(
                states.get(dst, {}).get("state") == "open"
                for dst in blocked_by_out.get(number, set())
            )
        else:
            dep_blocked = row["dependencies_blocked_by"]
            is_blocked = bool(dep_blocked and int(dep_blocked) > 0)
        if is_blocked:
            gates.append("blocked")
        if number in assignees:
            gates.append("assigned")
        direct_linked = any(
            states.get(dst, {}).get("state") == "open"
            and states.get(dst, {}).get("kind") == "pr"
            for dst in linked_pr_dsts.get(number, set())
        )
        keyword_linked = bool(_open_pr_closers(conn, repo, number))
        if direct_linked:
            gates.append("linked_pr")
        elif keyword_linked:
            gates.append("linked_pr (append-only evidence; may be stale)")
        if row["locked"]:
            gates.append("locked")
        gates_by[number] = gates

    for src, dst in blocked_by_edges:
        src_state = states.get(src)
        if (
            src_state
            and src_state["state"] == "open"
            and src_state.get("kind") == "issue"
            and not gates_by.get(src)
        ):
            blocking_open_edges[dst] = blocking_open_edges.get(dst, 0) + 1

    out: dict[int, dict] = {}
    for number, row in rows.items():
        try:
            raw = json.loads(row["raw_json"])
        except (TypeError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}

        reactions = raw.get("reactions")
        if isinstance(reactions, dict):
            plus1 = reactions.get("+1", 0)
            reactions_raw: int | None = plus1 if isinstance(plus1, int) else 0
        else:
            reactions_raw = None

        milestone = raw.get("milestone")
        if isinstance(milestone, dict) and milestone.get("due_on"):
            milestone_due: str | None = str(milestone["due_on"])
        else:
            milestone_due = None

        participants = set(comment_authors.get(number, set()))
        participants |= review_authors.get(number, set())
        if row["author"]:
            participants.add(row["author"])

        blocking_open: int | None
        if any_blocked_by:
            blocking_open = blocking_open_edges.get(number, 0)
        elif row["dependencies_blocking"] is not None:
            blocking_open = int(row["dependencies_blocking"])
        else:
            blocking_open = 0

        if row["sub_issues_total"] is None:
            gap: int | None = None
        else:
            gap = max(
                0, int(row["sub_issues_total"]) - int(row["sub_issues_completed"] or 0)
            )

        gates = gates_by[number]

        out[number] = {
            "number": number,
            "title": row["title"],
            "item_type": row["item_type"],
            "reactions_plus1": reactions_raw,
            "milestone_due": milestone_due,
            "participants": len(participants),
            "comments": int(row["comment_count"] or 0),
            "inbound_duplicates": inbound_dups.get(number, 0),
            "inbound_mentions": inbound_mentions.get(number, 0),
            "blocking_open": blocking_open,
            "sub_issue_gap": gap,
            "reopens": reopens.get(number, 0) if has_events else None,
            "field_values": field_values.get(number, []),
            "reverted_prs": reverted_prs,
            "gates": gates,
        }
    return {
        "features": out,
        "blocking_source": "edges" if any_blocked_by else "dependencies_blocking",
        "blocked_by_out": blocked_by_out,
    }


def _open_pr_closers(conn: sqlite3.Connection, repo: str, number: int) -> set[int]:
    """OPEN PRs with a closes/closes_keyword edge pointing at ``number``
    (the append-only linked_pr fallback)."""
    out: set[int] = set()
    for row in conn.execute(
        "SELECT CAST(e.src_id AS INTEGER) AS pr FROM edges e JOIN items i"
        " ON i.repo = e.repo AND i.number = CAST(e.src_id AS INTEGER)"
        " WHERE e.repo = ? AND e.src_type = 'item' AND e.dst_type = 'item'"
        " AND e.rel IN ('closes', 'closes_keyword') AND e.dst_id = ?"
        " AND i.kind = 'pr' AND i.state = 'open'",
        (repo, str(number)),
    ):
        try:
            out.add(int(row["pr"]))
        except (TypeError, ValueError):
            continue
    return out


def _triage_normalize(name: str, raw, asof_dt: datetime | None):
    """One feature's normalized [0, 1] value; None when the feature is
    missing (04-root-cause.md section 2's documented calibrations:
    reactions /3, participants /3, comments /5, duplicates 0/1,
    mentions /10, blocking /3, gap /3, milestone 14-day window vs
    triage_asof, reopens /1, file_reverts /1, priority weights clamped)."""
    if raw is None:
        return None
    if name in ("reactions_plus1", "participants"):
        return min(1.0, raw / 3.0)
    if name == "comments":
        return min(1.0, raw / 5.0)
    if name == "inbound_duplicates":
        return 1.0 if raw >= 1 else 0.0
    if name == "inbound_mentions":
        return min(1.0, raw / 10.0)
    if name in ("blocking_open", "sub_issue_gap"):
        return min(1.0, raw / 3.0)
    if name == "milestone_due":
        if asof_dt is None:
            return None
        due = _parse_instant(raw)
        if due is None:
            return 0.0
        if due < asof_dt or due <= asof_dt + timedelta(days=14):
            return 1.0
        return 0.0
    if name in ("reopens", "file_reverts"):
        return min(1.0, raw / 1.0)
    if name in ("issue_type", "label_weight", "field_weight"):
        return _clamp01(raw)
    raise ValueError(f"unknown triage feature {name!r}")


_TRIAGE_GROUPS = {
    "demand": ("reactions_plus1", "participants", "comments",
               "inbound_duplicates", "inbound_mentions"),
    "leverage": ("blocking_open", "sub_issue_gap", "milestone_due"),
    "risk": ("reopens", "file_reverts"),
    "priority": ("issue_type", "label_weight", "field_weight"),
}


def _triage_row_scores(feat: dict, cfg: dict, asof_dt: datetime | None) -> dict:
    """Group means over PRESENT features (missing excluded from the mean;
    an all-missing group contributes 0) and the stage-1 sum. The raw echo
    carries the SOURCE data (the item_type string; the matched map weights
    for label/field); normalization reads the configured weights."""
    raw = {
        "reactions_plus1": feat["reactions_plus1"],
        "participants": feat["participants"],
        "comments": feat["comments"],
        "inbound_duplicates": feat["inbound_duplicates"],
        "inbound_mentions": feat["inbound_mentions"],
        "blocking_open": feat["blocking_open"],
        "sub_issue_gap": feat["sub_issue_gap"],
        "milestone_due": feat["milestone_due"],
        "reopens": feat["reopens"],
        "file_reverts": feat["file_reverts"],
        "issue_type": feat["item_type"],
        "label_weight": feat["label_weight"],
        "field_weight": feat["field_weight"],
    }
    scoring_source = dict(raw)
    scoring_source["issue_type"] = feat["issue_type_weight"]
    normalized = {
        name: _triage_normalize(name, scoring_source[name], asof_dt)
        for name in raw
    }
    groups = {}
    for group, names in _TRIAGE_GROUPS.items():
        present = [normalized[n] for n in names if normalized[n] is not None]
        groups[group] = (
            cfg[group] * (sum(present) / len(present)) if present else 0.0
        )
    missing = {
        name: _TRIAGE_REQUIRES[name]
        for name, value in normalized.items()
        if value is None and name in _TRIAGE_REQUIRES
    }
    if asof_dt is None:
        # Without an as-of stamp the milestone window cannot fire even when
        # a due date exists; the reason must name the actual cause.
        missing["milestone_due"] = "requires: triage_asof (sync stamp) unknown"
    return {
        "stage1": sum(groups.values()),
        "groups": groups,
        "features_raw": raw,
        "features_normalized": normalized,
        "features_missing": missing,
    }


def _analog_for(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    reverted_prs: set[int],
) -> tuple[dict | None, int]:
    """The issue's best PR analog: the top ``similar`` candidate of kind
    'pr' under default [derive] weights (capped scan). Returns
    ``(analog_evidence, regression_flag)`` — regression fires when the
    analog PR has an inbound 'reverts' edge (a reverted fix)."""
    out = similar(conn, repo, number, limit=20, weights=load_weights())
    for cand in out["candidates"]:
        if cand.get("kind") == "pr":
            analog = {"number": cand["number"], "anchor": cand["anchor"]}
            return analog, 1 if cand["number"] in reverted_prs else 0
    return None, 0


def _triage_compute(
    conn: sqlite3.Connection, repo: str, cfg: dict, top_k: int
) -> dict:
    """The full two-stage triage computation shared by triage/bundle/brief:
    stage-1 features + gates for every open issue, stage-2 (hotspot,
    regression, analog) for the top-K UNGATED rows only (gated rows never
    consume stage-2 slots)."""
    caps = _Capabilities(conn)
    ctx = build_context(conn, repo, caps)
    asof = _synced_at(conn, repo)
    asof_dt = _parse_instant(asof)

    # path -> merged PR numbers (the vocabulary map build_context keeps
    # internally; rebuilt here for the hotspot arithmetic) + PR merge stamps.
    path_prs: dict[str, set[int]] = {}
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
                path_prs.setdefault(path, set()).add(row["number"])
    merged_at: dict[int, str | None] = {}
    for row in conn.execute(
        "SELECT number, merged_at FROM items"
        " WHERE repo = ? AND merged_at IS NOT NULL",
        (repo,),
    ):
        merged_at[row["number"]] = row["merged_at"]

    base = _triage_features(conn, repo)
    features = base["features"]
    items = ctx["items"]

    rows: list[dict] = []
    for number, feat in features.items():
        item = items.get(number)
        labels = set(item.labels) if item is not None else set()
        # Priority features — the blessed missing-feature rule: source data
        # absent -> missing; configured map with no match -> present 0.0;
        # map weights clamp [0, 1] at scoring, raw values echo unclamped.
        if feat["item_type"] is None or not cfg["types"]:
            feat["issue_type_weight"] = None
        else:
            feat["issue_type_weight"] = cfg["types"].get(feat["item_type"], 0.0)
        if not labels or not cfg["labels"]:
            feat["label_weight"] = None
        else:
            feat["label_weight"] = max(
                (cfg["labels"].get(name, 0.0) for name in labels), default=0.0
            )
        if not feat["field_values"] or not cfg["fields"]:
            feat["field_weight"] = None
        else:
            feat["field_weight"] = max(
                (cfg["fields"].get(value, 0.0) for value in feat["field_values"]),
                default=0.0,
            )
        resolved = set(item.paths) if item is not None else set()
        feat["file_reverts"] = len(
            {
                pr
                for path in resolved
                for pr in path_prs.get(path, set())
                if pr in feat["reverted_prs"]
            }
        )
        scores = _triage_row_scores(feat, cfg, asof_dt)
        rows.append(
            {
                "number": number,
                "title": feat["title"],
                "gates": list(feat["gates"]),
                "ranked": not feat["gates"],
                "scores": scores,
                "resolved_paths": sorted(resolved),
                "stage1": scores["stage1"],
            }
        )
    rows.sort(key=lambda r: (-r["stage1"], r["number"]))
    ungated = [r for r in rows if r["ranked"]]

    # Stage 2 for the top-K UNGATED rows only.
    for row in ungated[: max(1, top_k)]:
        number = row["number"]
        hotspot = 0.0
        if asof_dt is not None:
            seen_prs = {
                pr
                for path in row["resolved_paths"]
                for pr in path_prs.get(path, set())
            }
            recency = 0.0
            for pr in seen_prs:
                merged = _parse_instant(merged_at.get(pr))
                if merged is None:
                    continue
                recency += 1.0 if (asof_dt - merged) <= timedelta(days=90) else 0.5
            hotspot = min(1.0, recency / 10.0)
        analog, regression = _analog_for(
            conn, repo, number, features[number]["reverted_prs"]
        )
        bonus = cfg["stage2_weight"] * (hotspot + regression) / 2.0
        row["hotspot"] = hotspot
        row["regression"] = regression
        row["analog"] = analog
        row["stage2"] = bonus
        row["total_score"] = row["stage1"] + bonus
    for row in ungated[max(1, top_k):]:
        row["hotspot"] = None
        row["regression"] = None
        row["analog"] = None
        row["stage2"] = None
        row["note"] = "not_computed (outside top_k)"
        row["total_score"] = row["stage1"]
    for row in rows:
        if not row["ranked"]:
            row["hotspot"] = None
            row["regression"] = None
            row["analog"] = None
            row["stage2"] = None
            row["note"] = "not_computed (gated)"
            row["total_score"] = row["stage1"]

    return {
        "rows": rows,
        "ungated": ungated,
        "asof": asof,
        "blocking_source": base["blocking_source"],
        "blocked_by_out": base["blocked_by_out"],
        "ctx": ctx,
        "caps": caps,
        "features": features,
        "path_prs": path_prs,
        "merged_at": merged_at,
    }


def _triage_row_payload(row: dict) -> dict:
    return {
        "number": row["number"],
        "title": row["title"],
        "gates": list(row["gates"]),
        "ranked": row["ranked"],
        "scores": {
            "stage1": row["scores"]["stage1"],
            "groups": row["scores"]["groups"],
            "features_raw": row["scores"]["features_raw"],
            "features_normalized": row["scores"]["features_normalized"],
            "features_missing": row["scores"]["features_missing"],
        },
        "total_score": row["total_score"],
        "stage2": row["stage2"],
        "hotspot": row["hotspot"],
        "regression": row["regression"],
        "analog": row["analog"],
        "resolved_paths": row["resolved_paths"],
        **({"note": row["note"]} if "note" in row else {}),
    }


def _cursor_key(total: float, number: int) -> str:
    return f"{round(total, 9):.9f}:{number}"


def triage(
    conn: sqlite3.Connection,
    repo: str,
    limit: int = 20,
    cursor: str | None = None,
    top_k: int | None = None,
    include_gated: bool = False,
    config: str | None = None,
) -> dict:
    """Two-stage triage of every open issue (issue #30, 04-root-cause.md
    section 2). Read-only; ``config`` is an explicit ``[triage]`` TOML path
    (highest precedence over the store/cwd chain). Gated rows (blocked by
    an open item, assigned, linked to an open PR, locked) are excluded from
    the default ranking — the observable default-exclusion contract — and
    return with ``ranked: false`` under ``include_gated``. ORDER:
    total_score DESC, number ASC."""
    cfg = load_triage_config(*_triage_config_chain(conn, config))
    effective_top_k = top_k if top_k is not None else cfg["top_k"]
    if not isinstance(effective_top_k, int) or effective_top_k < 1:
        raise ValueError(f"top_k must be a positive integer, not {effective_top_k!r}")
    compute = _triage_compute(conn, repo, cfg, effective_top_k)

    def _sort_key(row: dict) -> tuple[float, int]:
        return (-row["total_score"], row["number"])

    if include_gated:
        ordered = sorted(compute["rows"], key=_sort_key)
    else:
        ordered = sorted((r for r in compute["rows"] if r["ranked"]), key=_sort_key)

    start = 0
    if cursor:
        _total_text, _, number_text = cursor.partition(":")
        try:
            cursor_number = int(number_text)
        except ValueError:
            raise ValueError(f"invalid cursor: {cursor!r}") from None
        for index, row in enumerate(ordered):
            if row["number"] == cursor_number:
                start = index + 1
                break

    page_limit = _clamp_limit(limit)
    page = ordered[start : start + page_limit]
    truncated = start + page_limit < len(ordered)
    next_cursor = (
        _cursor_key(page[-1]["total_score"], page[-1]["number"])
        if truncated and page
        else None
    )
    return {
        "repo": repo,
        "rows": [_triage_row_payload(row) for row in page],
        "weights": {k: v for k, v in cfg.items() if k != "_config_source"},
        "config_source": cfg["_config_source"],
        "top_k": effective_top_k,
        "stage2_weight": cfg["stage2_weight"],
        "formula_version": cfg["formula_version"],
        "triage_asof": compute["asof"],
        "hotspot_asof": compute["asof"],
        "blocking_source": compute["blocking_source"],
        "cursor": next_cursor,
        "truncated": truncated,
        "derived": True,
    }


def _parse_size(size: str) -> tuple[int, int]:
    """Bundle size range: ``"2-3"`` -> (2, 3), ``"3"`` -> (3, 3)."""
    try:
        if "-" in size:
            lo_text, hi_text = size.split("-", 1)
            lo, hi = int(lo_text), int(hi_text)
        else:
            lo = hi = int(size)
    except ValueError:
        raise ValueError(f"invalid bundle size {size!r} (expected N or N-M)") from None
    if lo < 2 or hi < lo:
        raise ValueError(
            f"invalid bundle size {size!r} (min 2 members, max >= min)"
        ) from None
    return lo, hi


def _bundle_config_hash(cfg: dict) -> str:
    echo = {k: v for k, v in cfg.items() if k != "_config_source"}
    canonical = json.dumps(echo, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


def _open_pr_files(conn: sqlite3.Connection, repo: str) -> dict[str, set[int]]:
    """Open PRs' file sets — the collision surface."""
    out: dict[str, set[int]] = {}
    for row in conn.execute(
        "SELECT pf.number, pf.path FROM pr_files pf"
        " JOIN items i ON i.repo = pf.repo AND i.number = pf.number"
        " WHERE pf.repo = ? AND i.kind = 'pr' AND i.state = 'open'",
        (repo,),
    ):
        path = _norm_path(row["path"])
        if path:
            out.setdefault(path, set()).add(row["number"])
    return out


def _pair_evidence(info: dict) -> dict:
    return {
        "components": {
            name: {"score": round(comp["score"], 6)}
            for name, comp in info["components"].items()
        },
        "evidence": {
            name: comp["evidence"] for name, comp in info["components"].items()
        },
        "anchor": info["anchor"],
        "title": {
            "similarity": round(info["title_sim"], 6),
            "metric": "overlap coefficient over lowercase word tokens",
        },
    }


def _has_dup_edge(conn: sqlite3.Connection, repo: str, a: int, b: int, rel: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM edges WHERE repo = ? AND rel = ?"
        " AND src_type = 'item' AND dst_type = 'item'"
        " AND ((src_id = ? AND dst_id = ?) OR (src_id = ? AND dst_id = ?))",
        (repo, rel, str(a), str(b), str(b), str(a)),
    ).fetchone()
    return row is not None


def bundle(
    conn: sqlite3.Connection,
    repo: str,
    size: str = "2-3",
    count: int = 5,
    pool: list[int] | None = None,
    exclude: list[int] | None = None,
    allow_shared_files: bool = False,
    max_files: int | None = None,
    config: str | None = None,
    budget_tokens: int | None = None,
) -> dict:
    """Greedy work bundles over the triage pool (issue #30, 04-root-cause.md
    section 3). Pool = the top-K UNGATED triage rows (or the named
    ``pool``) minus ``exclude``, with in-pool blocker relaxation
    (blocked-only items whose blockers are all in the pool stay,
    ``gate_relaxed: true``); ``--exclude`` wins over pool membership and the
    relaxation alike. Pool members absent from the ranked pool are dropped
    silently — the echoed ``pool`` array is the disclosure. Pair affinity
    runs the exact pipeline ``pair_components -> _apply_cochange ->
    _total_score`` (coupling index built ONCE per call, default [derive]
    weights); an edge requires affinity >= ``affinity_floor`` AND a fired
    anchor; duplicate pairs (edges or the >= 0.9 title-overlap
    possible_duplicate predicate) collapse to ``merge_candidates`` and are
    never co-bundled; anchor-failing floor-passing text pairs land in
    ``related_not_bundleable``. Selection is greedy: highest unused
    triage total seeds (ties number ASC), then the member maximizing
    triage_total x mean pair affinity, subject to the size range, the
    file-set cap and cross-bundle file disjointness (unless
    ``allow_shared_files``). Read-only. ``budget_tokens`` (chars/4) caps
    the WHOLE serialized payload: bundles are dropped first, then the
    tails of ``merge_candidates``/``related_not_bundleable``, then the
    ``pool`` echo; ``truncated: true`` marks any payload that needed it,
    including one still over budget only because of its mandatory echoes."""
    cfg = load_triage_config(*_triage_config_chain(conn, config))
    compute = _triage_compute(conn, repo, cfg, cfg["top_k"])
    size_min, size_max = _parse_size(size)
    file_cap = max_files if max_files is not None else cfg["max_files"]
    excluded = sorted({int(n) for n in (exclude or [])})

    ranked_map = {r["number"]: r for r in compute["ungated"]}
    if pool is not None:
        members = {int(n) for n in pool if int(n) in ranked_map}
    else:
        members = {r["number"] for r in compute["ungated"][: cfg["top_k"]]}
    members -= set(excluded)

    # In-pool blocker relaxation (fixpoint: a relaxed member can unblock the
    # next hop). Excluded blockers are already gone, so excluding the
    # blocker collapses the relaxation behind it.
    relaxed: set[int] = set()
    blocked_by_out = compute["blocked_by_out"]
    all_rows = {r["number"]: r for r in compute["rows"]}
    changed = True
    while changed:
        changed = False
        for number, row in all_rows.items():
            if number in members or number in excluded or row["ranked"]:
                continue
            if list(row["gates"]) != ["blocked"]:
                continue
            blockers = blocked_by_out.get(number, set())
            if blockers and blockers <= members:
                members.add(number)
                relaxed.add(number)
                changed = True

    ctx = compute["ctx"]
    index, file_support, _population = _coupling_index(conn, repo, compute["caps"])
    ctx["coupling_support"] = index
    ctx["coupling_filesupport"] = file_support
    affinity_weights = load_weights()
    items = ctx["items"]

    dup_edge_pairs: set[frozenset[int]] = set()
    for row in conn.execute(
        "SELECT src_id, dst_id FROM edges WHERE repo = ?"
        " AND rel IN ('duplicate_of', 'duplicate_keyword')"
        " AND src_type = 'item' AND dst_type = 'item'",
        (repo,),
    ):
        try:
            dup_edge_pairs.add(frozenset((int(row["src_id"]), int(row["dst_id"]))))
        except (TypeError, ValueError):
            continue

    def _pool_total(number: int) -> float:
        row = ranked_map.get(number) or all_rows[number]
        return row["total_score"]

    order = sorted(members, key=lambda n: (-_pool_total(n), n))

    pair_cache: dict[frozenset[int], dict] = {}
    merge_candidates: list[dict] = []
    related_not_bundleable: list[dict] = []
    edge_weight: dict[frozenset[int], float] = {}

    member_list = sorted(members)
    for i, a in enumerate(member_list):
        for b in member_list[i + 1:]:
            key = frozenset((a, b))
            components = pair_components(items[a], items[b], ctx, affinity_weights)
            _apply_cochange(items[a], items[b], ctx, components)
            total = _total_score(components, affinity_weights)
            anchor = _anchor_fired(components)
            title_sim = components["title"]["score"]
            info = {
                "a": a,
                "b": b,
                "components": components,
                "total": total,
                "anchor": anchor,
                "title_sim": title_sim,
            }
            pair_cache[key] = info
            if key in dup_edge_pairs:
                rel = (
                    "duplicate_of"
                    if _has_dup_edge(conn, repo, a, b, "duplicate_of")
                    else "duplicate_keyword"
                )
                merge_candidates.append(
                    {
                        "numbers": sorted((a, b)),
                        "kind": rel,
                        "affinity": round(total, 6),
                        "evidence": _pair_evidence(info),
                    }
                )
            elif anchor is None and title_sim >= 0.9:
                # possible_duplicate: near-identical titles with NO anchor
                # signal — text look-alikes, offered as merge candidates
                # instead of a bundle edge. An anchored pair with an
                # identical title (a whole family crash-named the same)
                # still bundles through its anchor evidence.
                merge_candidates.append(
                    {
                        "numbers": sorted((a, b)),
                        "kind": "possible_duplicate",
                        "affinity": round(total, 6),
                        "evidence": _pair_evidence(info),
                    }
                )
            elif total >= cfg["affinity_floor"] and anchor is not None:
                edge_weight[key] = total
            elif total >= cfg["affinity_floor"] and title_sim < 0.9:
                related_not_bundleable.append(
                    {
                        "numbers": sorted((a, b)),
                        "affinity": round(total, 6),
                        "title_similarity": round(title_sim, 6),
                        "scores": {
                            name: round(comp["score"], 6)
                            for name, comp in components.items()
                        },
                        "evidence": _pair_evidence(info),
                    }
                )

    open_prs = _open_pr_files(conn, repo)

    def _direct_paths(number: int) -> set[str]:
        item = items.get(number)
        return set(item.paths) if item is not None else set()

    used: set[int] = set()
    skipped: set[int] = set()
    used_files: set[str] = set()
    bundles_payload: list[dict] = []

    while (
        len(bundles_payload) < max(0, count)
        and len(used) + len(skipped) < len(members)
    ):
        available = [n for n in order if n not in used and n not in skipped]
        if not available:
            break
        current_seed = available[0]

        group = [current_seed]
        group_files = _direct_paths(current_seed)
        why = "pool exhausted"
        while len(group) < size_max:
            candidates = [n for n in available if n not in group]
            best = None
            best_key = None
            blocked_by_shared = False
            blocked_by_cap = False
            for cand in candidates:
                affinities = [
                    edge_weight.get(frozenset((member, cand)), 0.0)
                    for member in group
                ]
                mean_affinity = (
                    sum(affinities) / len(affinities) if affinities else 0.0
                )
                if mean_affinity <= 0:
                    continue
                cand_files = _direct_paths(cand)
                if len(group_files | cand_files) > file_cap:
                    blocked_by_cap = True
                    continue
                if not allow_shared_files and (cand_files & used_files):
                    blocked_by_shared = True
                    continue
                collisions = bool(cand_files & set(open_prs))
                score = _pool_total(cand) * mean_affinity
                key_tuple = (-round(score, 9), collisions, cand)
                if best_key is None or key_tuple < best_key:
                    best_key = key_tuple
                    best = (cand, cand_files)
            if best is None:
                if blocked_by_shared:
                    why = "shared-file conflict"
                elif blocked_by_cap:
                    why = f"file cap (max_files={file_cap})"
                elif any(
                    frozenset((member, cand)) in dup_edge_pairs
                    or pair_cache.get(frozenset((member, cand)), {}).get(
                        "title_sim", 0.0
                    )
                    >= 0.9
                    for member in group
                    for cand in candidates
                ):
                    why = "duplicate collapse"
                else:
                    why = "affinity floor"
                break
            cand, cand_files = best
            group.append(cand)
            group_files |= cand_files
        if len(group) >= size_min:
            bundles_payload.append(
                _bundle_payload(
                    conn, repo, group, compute, pair_cache, edge_weight, cfg,
                    open_prs, relaxed, why, ctx,
                )
            )
            used |= set(group)
            used_files |= group_files
        else:
            skipped.add(current_seed)

    payload = {
        "repo": repo,
        "bundles": bundles_payload,
        "merge_candidates": merge_candidates,
        "related_not_bundleable": related_not_bundleable,
        "excluded": excluded,
        "pool": sorted(members),
        "weights": {k: v for k, v in cfg.items() if k != "_config_source"},
        "config_source": cfg["_config_source"],
        "formula_version": cfg["formula_version"],
        "derived": True,
    }
    if budget_tokens is not None and budget_tokens > 0:
        budget = budget_tokens * 4

        def _size() -> int:
            return len(
                json.dumps(
                    payload, sort_keys=True, ensure_ascii=True,
                    separators=(",", ":"),
                )
            )

        if _size() > budget:
            # The cap binds the WHOLE payload, not just the bundles list:
            # whole bundles drop first, then the tails of merge_candidates
            # and related_not_bundleable, and only as the last resort the
            # pool echo (a convenience copy, not evidence). The flag is set
            # up front so a payload that stays over budget purely through
            # its mandatory echoes (weights, excluded) is still marked.
            payload["truncated"] = True
            for section in ("bundles", "merge_candidates",
                            "related_not_bundleable"):
                while payload[section] and _size() > budget:
                    payload[section].pop()
            if _size() > budget:
                payload["pool"] = []
    return payload


def _bundle_payload(
    conn: sqlite3.Connection,
    repo: str,
    group: list[int],
    compute: dict,
    pair_cache: dict,
    edge_weight: dict,
    cfg: dict,
    open_prs: dict[str, set[int]],
    relaxed: set[int],
    why: str,
    ctx: dict,
) -> dict:
    all_rows = {r["number"]: r for r in compute["rows"]}
    items = ctx["items"]

    members = []
    for number in group:
        row = all_rows[number]
        member = {
            "number": number,
            "title": row["title"],
            "triage_total": row["total_score"],
            "stage1": row["stage1"],
            "gates": list(row["gates"]),
        }
        if number in relaxed:
            member["gate_relaxed"] = True
        members.append(member)

    direct = sorted(
        {p for n in group for p in (items[n].paths if n in items else set())}
    )

    linked_items: set[int] = set()
    for n in group:
        linked_items |= items[n].neighbors if n in items else set()
    linked_items -= set(group)
    linked = sorted(
        {p for n in linked_items if n in items for p in items[n].paths} - set(direct)
    )

    analog_files: list[dict] = []
    seen_analog: set[str] = set()
    for n in group:
        analog = all_rows[n].get("analog")
        if not analog:
            continue
        pr = analog["number"]
        for row in conn.execute(
            "SELECT path FROM pr_files WHERE repo = ? AND number = ?"
            " AND (status IS NULL OR status != 'removed')",
            (repo, pr),
        ):
            path = _norm_path(row["path"])
            if path and path not in direct and path not in seen_analog:
                seen_analog.add(path)
                analog_files.append({"path": path, "via": f"#{pr}"})
    analog_files.sort(key=lambda e: e["path"])

    file_set = set(direct) | set(linked) | {e["path"] for e in analog_files}
    index = ctx.get("coupling_support") or {}
    cochanged: set[str] = set()
    for path in file_set:
        for other, _conf, _sup in index.get(path, []):
            if any(fnmatch.fnmatchcase(other, g) for g in cfg["test_globs"]):
                cochanged.add(other)

    collisions = [
        {"path": path, "prs": sorted(open_prs[path])}
        for path in sorted(file_set & set(open_prs))
    ]

    # In-pool blocked_by topological order (ties number ASC): a member
    # blocked by another member sorts after its blocker.
    members_set = set(group)
    blockers_in = {
        n: {b for b in compute["blocked_by_out"].get(n, set()) if b in members_set}
        for n in group
    }
    remaining = sorted(group)
    placed: list[int] = []
    placed_set: set[int] = set()
    while remaining:
        progressed = False
        for n in list(remaining):
            if blockers_in[n] <= placed_set:
                placed.append(n)
                placed_set.add(n)
                remaining.remove(n)
                progressed = True
        if not progressed:  # cycle: fall back to number order
            placed.extend(sorted(remaining))
            break

    pair_scores = []
    affinities = []
    for i, a in enumerate(sorted(group)):
        for b in sorted(group)[i + 1:]:
            key = frozenset((a, b))
            info = pair_cache.get(key)
            if info is None:
                continue
            weight = edge_weight.get(key, 0.0)
            affinities.append(weight)
            pair_scores.append(
                {
                    "a": a,
                    "b": b,
                    "affinity": round(weight, 6),
                    "anchor": info["anchor"],
                    "components": {
                        name: {"score": round(comp["score"], 6)}
                        for name, comp in info["components"].items()
                    },
                    "evidence": {
                        name: comp["evidence"]
                        for name, comp in info["components"].items()
                    },
                }
            )
    cohesion = sum(affinities) / len(affinities) if affinities else 0.0

    risk_features = {
        name: max(
            (all_rows[n]["scores"]["features_raw"].get(name) or 0) for n in group
        )
        for name in ("reopens", "file_reverts")
    }
    risk_summary = {
        "max_risk_features": risk_features,
        "hotspot": max(all_rows[n].get("hotspot") or 0.0 for n in group),
        "regression": max(all_rows[n].get("regression") or 0 for n in group),
        "open_pr_collisions": len(collisions),
    }

    bundle_id = hashlib.sha1(
        (
            "zaxbygraph.bundle.v1|"
            + ",".join(str(n) for n in sorted(group))
            + "|"
            + _bundle_config_hash(cfg)
        ).encode("utf-8")
    ).hexdigest()

    return {
        "bundle_id": bundle_id,
        "members": members,
        "pairs": pair_scores,
        "files": {"direct": direct, "linked": linked, "analog": analog_files},
        "cochanged_tests": sorted(cochanged),
        "open_pr_collisions": collisions,
        "order": placed,
        "risk_summary": risk_summary,
        "cohesion": round(cohesion, 6),
        "why_not_bigger": why,
    }


_UNTRUSTED_NOTE = "untrusted: GitHub user content"


def _untrusted(value: str) -> dict:
    """The AC6 wrapper: every GitHub-authored string in a brief packet
    travels inside an ``untrusted_text`` object."""
    return {"untrusted_text": value}


def _association_rank(value: str | None) -> int:
    """Comment ordering rank: MEMBER/OWNER/COLLABORATOR first, then every
    other known association, then NONE (and NULL)."""
    if value in ("MEMBER", "OWNER", "COLLABORATOR"):
        return 0
    if value is None or value == "NONE":
        return 2
    return 1


#: Truncation marker for the brief body's character-level cap of last
#: resort (the same "…" the README documents for the packet budget).
_ELLIPSIS = "…"


def _hard_cap(text: str, allowance: int, marker: str = _ELLIPSIS) -> str:
    """Character-level cap of last resort: ``text`` plus a trailing
    ellipsis marker, cut so the JSON serialization fits ``allowance``
    chars. The cut is computed in SERIALIZED units — under ensure_ascii a
    multi-byte-dense payload serializes to six chars per character, so a
    char-arithmetic cut overshoots to a negative prefix and empties the
    result. Serialized length is strictly increasing in the prefix length
    (every character serializes to at least one char), so a binary search
    finds the largest prefix p >= 1 with ser(text[:p] + marker) <=
    allowance: non-empty and marker-terminated whenever the allowance
    covers one serialized character plus the marker; only a sub-floor
    allowance returns the empty string (documented degenerate case)."""

    def _fits(chars: int) -> bool:
        return len(json.dumps(text[:chars] + marker)) - 2 <= allowance

    if not _fits(1):
        return ""
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + marker


def _fit_paragraphs(body: str, allowance: int) -> str:
    """Truncate ``body`` so its JSON serialization fits ``allowance`` chars
    — unconditionally. Whole-paragraph (blank-line boundary) cutting is the
    PREFERRED shape: paragraphs are kept while they fit, and the surviving
    tail is a run of complete paragraphs. But that preference is
    subordinate to the bound: a body with no paragraph boundaries (or whose
    first paragraph alone exceeds the allowance) is hard-truncated at the
    character level with a trailing ``…`` marker rather than returned whole
    or emptied (comments/file evidence absorb the rest of the packet)."""
    if allowance <= 0:
        return ""
    if len(json.dumps(body)) - 2 <= allowance:
        return body
    if "\n\n" in body:
        parts = body.split("\n\n")
        kept: list[str] = []
        for part in parts:
            candidate = "\n\n".join(kept + [part])
            if len(json.dumps(candidate)) - 2 > allowance:
                break
            kept.append(part)
        fitted = "\n\n".join(kept)
        if fitted:
            return fitted
    return _hard_cap(body, allowance)


def _wrap_live_entry(entry: dict, keep: tuple[str, ...]) -> dict:
    out = {}
    for key in keep:
        value = entry.get(key)
        if isinstance(value, str):
            out[key] = _untrusted(value)
        elif value is not None:
            out[key] = value
    return out


#: Captured gh stdout is capped at this many bytes BEFORE parsing: time is
#: already bounded (timeout=30) and this bounds the bytes — a runaway gh
#: response must not flow into an unbounded packet. A response past the
#: cap no longer parses and degrades the live block like any gh failure.
_GH_STDOUT_BYTE_CAP = 262144


def _brief_live(
    repo: str, pr_numbers: list[int], gh_path: str
) -> tuple[dict | None, str | None]:
    """gh-backed live CI/review reads for the linked PRs (argv list, no
    shell, results never stored). One cheap reachability probe runs first,
    so a broken gh degrades the block even when there are no linked PRs to
    read. Any failure degrades the whole live block to ``(None, error
    note)`` — the packet still returns. Each call is bounded in time
    (timeout=30) and in bytes: stdout is truncated to
    ``_GH_STDOUT_BYTE_CAP`` (256 KiB) before parsing, so an oversized
    response degrades rather than bloats the packet.

    The CI leg costs TWO gh calls per PR (accepted cost, documented here):
    the PR commit list — whose LAST element names the head commit CI ran
    against — and that sha's combined status, the real CI state surface.
    ``live_ci`` therefore reports real fields only: ``{"sha", "state",
    "contexts"}``, where contexts are the combined status's per-check
    entries reduced to ``{"state", "context"}`` and capped at the first
    10. The reviews leg is one more call per PR (real review objects
    carry ``state``/``body``)."""
    import subprocess

    def _gh(api_path: str):
        proc = subprocess.run(
            [gh_path, "api", api_path],
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"gh api {api_path} exited {proc.returncode}")
        capped = proc.stdout[:_GH_STDOUT_BYTE_CAP]
        return json.loads(capped.decode("utf-8", "replace"))

    try:
        _gh(f"repos/{repo}")
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
        return None, f"gh probe failed: {exc}"

    prs: list[dict] = []
    for pr in pr_numbers:
        entry: dict = {"number": pr}
        commits_path = f"repos/{repo}/pulls/{pr}/commits"
        try:
            commits = _gh(commits_path)
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            return None, f"gh api {commits_path} failed: {exc}"
        if not isinstance(commits, list):
            return None, f"gh api {commits_path}: unexpected payload shape"
        head: str | None = None
        for element in reversed(commits):
            if isinstance(element, dict) and isinstance(element.get("sha"), str):
                head = element["sha"]
                break
        if head is None:
            # a PR with no commits has no CI surface to report
            entry["live_ci"] = None
        else:
            status_path = f"repos/{repo}/commits/{head}/status"
            try:
                combined = _gh(status_path)
            except (
                OSError,
                subprocess.SubprocessError,
                ValueError,
                RuntimeError,
            ) as exc:
                return None, f"gh api {status_path} failed: {exc}"
            if not isinstance(combined, dict):
                return None, f"gh api {status_path}: unexpected payload shape"
            raw_statuses = combined.get("statuses")
            entry["live_ci"] = {
                "sha": _untrusted(head),
                "state": _wrap_live_entry(combined, ("state",)).get("state"),
                "contexts": [
                    _wrap_live_entry(s, ("state", "context"))
                    for s in (
                        raw_statuses if isinstance(raw_statuses, list) else []
                    )[:10]
                    if isinstance(s, dict)
                ],
            }
        reviews_path = f"repos/{repo}/pulls/{pr}/reviews"
        try:
            reviews = _gh(reviews_path)
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            return None, f"gh api {reviews_path} failed: {exc}"
        if not isinstance(reviews, list):
            return None, f"gh api {reviews_path}: unexpected payload shape"
        entry["live_review"] = [
            _wrap_live_entry(e, ("state", "body"))
            for e in reviews
            if isinstance(e, dict)
        ]
        prs.append(entry)
    return {"prs": prs}, None


def brief(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    budget_tokens: int = 4000,
    config: str | None = None,
    live: bool = False,
    gh_path: str = "gh",
) -> dict:
    """The work packet for one issue (issue #30, 04-root-cause.md section
    4): the triage feature row, the body truncated at paragraph boundaries,
    top comments (max 5, ordered by author_association rank then recency
    DESC), implicated files with resolution evidence, the closest PR analog
    with its fix diffstat, and linked PRs — plus the AC9 echo every derived
    payload carries (the effective ``weights``, ``config_source``, and
    ``formula_version``). Every GitHub-authored string (title, body,
    comment bodies, live gh text) is wrapped as
    ``{"untrusted_text": ...}`` and the packet carries ``content_trust``.
    Budget: chars/4 of the serialized packet <= ``budget_tokens`` whenever
    the budget is at or above the packet's fixed skeleton floor (~480
    tokens for a bare issue with the default [triage] echo; smaller
    budgets return the skeleton with ``truncated: true``), with the
    mandatory ``truncated``/``derived``
    echoes counted inside the envelope (their width is reserved before the
    body allowance is computed). The envelope covers the complete packet:
    the ``--live`` block attaches before enforcement, and a live block
    that cannot fit inside the envelope even over an empty body degrades
    to ``live: null`` + a ``live_error`` budget note before the allowance
    is computed. Truncation order: comments capped at the top-5 first,
    then the body cut at paragraph boundaries (a body that cannot fit any
    whole paragraph under the allowance — including a single-paragraph
    body — is hard-truncated at the character level with a trailing
    ``…``), then comments drop from the tail, then file evidence.
    ``truncated`` is set whenever any of that fired.
    ``live`` shells out to ``gh`` (argv list, no shell) for linked-PR CI
    state/reviews; any failure degrades to ``live: null`` +
    ``live_error``. Read-only."""
    cfg = load_triage_config(*_triage_config_chain(conn, config))
    compute = _triage_compute(conn, repo, cfg, cfg["top_k"])
    all_rows = {r["number"]: r for r in compute["rows"]}
    row = all_rows.get(number)
    if row is None:
        raise LookupError(f"item not found: #{number}")

    item_row = conn.execute(
        "SELECT title, body, author, created_at FROM items"
        " WHERE repo = ? AND kind = 'issue' AND number = ?",
        (repo, number),
    ).fetchone()
    if item_row is None:  # pragma: no cover - triage rows are open issues
        raise LookupError(f"item not found: #{number}")

    comment_rows = [
        {
            "author": crow["author"],
            "author_association": crow["author_association"],
            "created_at": crow["created_at"],
            "body": crow["body"] or "",
        }
        for crow in conn.execute(
            "SELECT author, author_association, created_at, body FROM comments"
            " WHERE repo = ? AND number = ? AND kind = 'issue_comment'",
            (repo, number),
        )
    ]
    ranked_comments: list[dict] = []
    for rank in (0, 1, 2):
        bucket = [
            c
            for c in comment_rows
            if _association_rank(c["author_association"]) == rank
        ]
        bucket.sort(key=lambda c: c["created_at"] or "", reverse=True)
        ranked_comments.extend(bucket)
    kept_comments = ranked_comments[:5]

    resolved = row["resolved_paths"]
    unresolved_count = 0
    item_data = compute["ctx"]["items"].get(number)
    if item_data is not None:
        unresolved_count = len(item_data.unresolved_refs)

    analog, _regression = _analog_for(
        conn, repo, number, compute["features"][number]["reverted_prs"]
    )
    analog_block = None
    if analog:
        arow = conn.execute(
            "SELECT number, additions, deletions FROM items"
            " WHERE repo = ? AND number = ?",
            (repo, analog["number"]),
        ).fetchone()
        if arow is not None:
            analog_block = {
                "number": arow["number"],
                "anchor": analog["anchor"],
                "additions": arow["additions"],
                "deletions": arow["deletions"],
            }

    linked: list[dict] = []
    seen_prs: set[int] = set()
    # linked_pr: the issue (src) points at its linked PR (dst); the closes
    # fallback: the PR (src) points at the issue (dst).
    for erow in conn.execute(
        "SELECT CAST(e.dst_id AS INTEGER) AS pr, i.title AS pr_title,"
        " i.state AS pr_state FROM edges e JOIN items i"
        " ON i.repo = e.repo AND i.number = CAST(e.dst_id AS INTEGER)"
        " WHERE e.repo = ? AND e.src_type = 'item' AND e.dst_type = 'item'"
        " AND e.rel = 'linked_pr' AND e.src_id = ? AND i.kind = 'pr'",
        (repo, str(number)),
    ):
        pr = erow["pr"]
        if pr is None or pr in seen_prs:
            continue
        seen_prs.add(pr)
        linked.append(
            {
                "number": pr,
                "state": erow["pr_state"],
                "title": _untrusted(erow["pr_title"] or ""),
            }
        )
    for erow in conn.execute(
        "SELECT CAST(e.src_id AS INTEGER) AS pr, i.title AS pr_title,"
        " i.state AS pr_state FROM edges e JOIN items i"
        " ON i.repo = e.repo AND i.number = CAST(e.src_id AS INTEGER)"
        " WHERE e.repo = ? AND e.src_type = 'item' AND e.dst_type = 'item'"
        " AND e.rel IN ('closes', 'closes_keyword') AND e.dst_id = ?"
        " AND i.kind = 'pr'",
        (repo, str(number)),
    ):
        pr = erow["pr"]
        if pr is None or pr in seen_prs:
            continue
        seen_prs.add(pr)
        linked.append(
            {
                "number": pr,
                "state": erow["pr_state"],
                "title": _untrusted(erow["pr_title"] or ""),
            }
        )
    linked.sort(key=lambda e: e["number"])

    features = _triage_row_payload(row)
    features["title"] = _untrusted(features["title"])

    packet: dict = {
        "repo": repo,
        "number": number,
        "features": features,
        "content_trust": _UNTRUSTED_NOTE,
        "comments": [
            {
                "author": c["author"],
                "author_association": c["author_association"],
                "created_at": c["created_at"],
                "body": _untrusted(c["body"]),
            }
            for c in kept_comments
        ],
        "body": _untrusted(item_row["body"] or ""),
        "files": {
            "resolved": list(resolved),
            "unresolved_count": unresolved_count,
        },
        "analog": analog_block,
        "linked_prs": linked,
        "weights": {k: v for k, v in cfg.items() if k != "_config_source"},
        "config_source": cfg["_config_source"],
        "formula_version": cfg["formula_version"],
    }

    # The live block attaches BEFORE the budget enforcement so the envelope
    # covers the complete packet, --live content included.
    if live:
        live_block, live_error = _brief_live(repo, sorted(seen_prs), gh_path)
        packet["live"] = live_block
        if live_error is not None:
            packet["live_error"] = live_error

    if budget_tokens and budget_tokens > 0:
        budget_chars = budget_tokens * 4

        def _size(pkt: dict) -> int:
            return len(
                json.dumps(
                    pkt, sort_keys=True, ensure_ascii=True, separators=(",", ":")
                )
            )

        if packet.get("live"):
            # A live block that cannot fit inside the envelope even over an
            # empty body can never ship: degrade it up front to the
            # documented live:null + note shape, so the body allowance is
            # measured against a skeleton that actually can.
            probe_live = dict(packet)
            probe_live["body"] = _untrusted("")
            probe_live["derived"] = True
            probe_live["truncated"] = True
            if _size(probe_live) > budget_chars:
                packet["live"] = None
                packet["live_error"] = "live block dropped: packet budget"
                packet["truncated"] = True

        probe = dict(packet)
        probe["body"] = _untrusted("")
        # The mandatory echoes ride inside the envelope, not after it:
        # `derived` serializes onto every packet and `truncated` onto any
        # packet a trim touched, so both widths are reserved BEFORE the
        # body allowance is computed — with zero comments nothing absorbs
        # them, and a body filled to exactly the bound overflows it once
        # the flags serialize in.
        probe["derived"] = True
        probe["truncated"] = True
        allowance = budget_chars - _size(probe)
        body_text = packet["body"]["untrusted_text"]
        fitted = _fit_paragraphs(body_text, allowance)
        if fitted != body_text:
            packet["truncated"] = True
        packet["body"] = _untrusted(fitted)
        # derived is stamped before the backstop loops so their size
        # checks measure the FINAL packet shape, flags included.
        packet["derived"] = True
        while _size(packet) > budget_chars and packet["comments"]:
            packet["comments"].pop()
            packet["truncated"] = True
        if _size(packet) > budget_chars:
            packet["files"] = {"resolved": packet["files"]["resolved"][:5]}
            packet["truncated"] = True

    packet["derived"] = True
    return packet
