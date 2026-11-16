"""The claim lifecycle (issues #77 and #74): one vocabulary, two surfaces.

The live surface (``claim`` / ``release`` / ``pr-guard`` / ``claims --live``)
talks to GitHub through ``gh`` and never touches the store; the derived
surface (the ``claimed`` triage gate and the ``claims`` listing) reads
stored comments at query time and never writes anything. Both surfaces
share ONE parser and ONE classification so a claim posted by the live
command is read back identically by the next sync.

Everything here is mechanically derived from GitHub-reported text -- no
model output, no inferred purpose. Comment text is untrusted GitHub user
content and travels wrapped as ``{"untrusted_text": ...}`` wherever it
surfaces in a payload.

gh routing rules (the live seam): ``gh api`` carries explicit
``repos/O/R/...`` routes (never ``-R`` -- ``gh api`` rejects it); ``-R O/R``
appears ONLY on the ``gh pr list`` call. No window-bound ``since=`` appears
anywhere: every read is a FULL history read, because an expired claim must
still be listed and a safety read must be complete.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence

try:  # Python 3.11+ stdlib
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - the documented floor has it
    tomllib = None  # type: ignore[assignment]

#: Associations whose claims can hold an issue (Design 11): a claim by any
#: other association is LISTED with ``trusted: false`` and never gates.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

#: The built-in [claims] table: every value overridable per key through the
#: same 3-level chain the [triage]/[derive] tables use.
CLAIMS_DEFAULTS: dict = {
    "enabled": True,
    "patterns": ["Agent claim", "Working this issue"],
    "release_prefix": "**Claim released",
    "token_prefix": "session=",
    "window_hours": 72,
    "settle_seconds": 2.0,
}

_CLAIMS_KEYS = frozenset(CLAIMS_DEFAULTS)

#: The token charset (Design 1/8): ``session=<token>`` where <token> is
#: 16-64 chars of [A-Za-z0-9-]. The trailing boundary makes a 65-char run
#: NOT match (backtracking cannot shrink past the lookahead).
_TOKEN_CHARSET = r"[A-Za-z0-9-]{16,64}"

#: Reference digit cap (the keyword-extractor bound): a digit run past 9
#: digits is not a reference, so an oversized token matches nothing instead
#: of wedging ``int()``.
_MAX_REF_DIGITS = 9
#: ASCII-only digit classes (FB-005): Python's ``\d`` is Unicode-aware, so
#: ``#٥`` (Arabic-Indic five) would otherwise localize to issue 5. GitHub
#: issue references are ASCII digits.
_REF_RE = re.compile(r"#([0-9]{1,%d})(?!\w)" % _MAX_REF_DIGITS)

#: A cross-repo reference (``other/repo#5``): the ref is skipped, never
#: localized to the same-numbered local item.
_CROSS_REPO_BEFORE_RE = re.compile(r"/[\w.-]+$")

#: Subprocess budget for one gh call (env-overridable).
_GH_TIMEOUT_S_DEFAULT = 30.0

#: Safety-read page caps (round-2 rule): a read that gates a decision must
#: be COMPLETE -- hitting the page cap on a safety read fails closed instead
#: of proceeding on partial evidence. Only the repo-wide presentation feed
#: may cap, and only AFTER complete collection.
_SAFETY_MAX_PAGES = 40
#: The page size every paged gh read requests; the completeness check uses
#: the RAW page length against it (FB-018).
_PAGE_SIZE = 100
_PRESENTATION_MAX_ENTRIES = 500

#: The live view caps PR scanning the same way the CLI does.
_PR_LIST_LIMIT = 500

#: Random pre-retry wait after a lost race (Design 3): 1-5 s. Seeded via
#: ``ZAXBYGRAPH_CLAIM_RNG_SEED`` for deterministic tests; the module-level
#: ``_RNG`` and ``sleep`` seams are patchable.
_RETRY_WAIT_MIN_S = 1.0
_RETRY_WAIT_MAX_S = 5.0

_seed_text = os.environ.get("ZAXBYGRAPH_CLAIM_RNG_SEED")
_RNG = random.Random(int(_seed_text)) if _seed_text and _seed_text.isdigit() else random.Random()

sleep = time.sleep


class ConfigError(ValueError):
    """Raised for malformed or invalid ``[claims]`` configuration.

    Mirrors :class:`zaxbygraph.derive.ConfigError` (it is caught beside it
    at every CLI/MCP catch site) but is this module's OWN class: derive.py
    imports claims, never the reverse.
    """

    def __init__(self, message: str, *, path: str | None = None) -> None:
        self.path = path
        super().__init__(message)


# ---------------------------------------------------------------------------
# Configuration


def _validate_claims_value(key: str, value, path: str | None) -> Any:
    """Validate + normalize one [claims] value; ConfigError on a bad one."""
    if key == "enabled":
        if not isinstance(value, bool):
            raise ConfigError(f"[claims] enabled must be a boolean", path=path)
        return value
    if key == "patterns":
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(p, str) or not p for p in value)
        ):
            raise ConfigError(
                "[claims] patterns must be a non-empty list of non-empty strings",
                path=path,
            )
        return list(value)
    if key in ("release_prefix", "token_prefix"):
        if not isinstance(value, str) or not value:
            raise ConfigError(f"[claims] {key} must be a non-empty string", path=path)
        return value
    if key == "window_hours":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(
                "[claims] window_hours must be a positive integer", path=path
            )
        return int(value)
    if key == "settle_seconds":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
            or value > _MAX_SETTLE_SECONDS
        ):
            raise ConfigError(
                "[claims] settle_seconds must be a finite non-negative number"
                f" at most {_MAX_SETTLE_SECONDS:g}",
                path=path,
            )
        try:
            if not math.isfinite(value):
                raise ConfigError(
                    "[claims] settle_seconds must be finite", path=path
                )
        except OverflowError:
            # A TOML integer past the float range: coded, never a traceback.
            raise ConfigError(
                "[claims] settle_seconds is outside the representable range",
                path=path,
            ) from None
        return float(value)
    raise ConfigError(
        f"unknown [claims] key {key!r} (allowed: {sorted(_CLAIMS_KEYS)})", path=path
    )


#: The settle delay's upper bound (FB-001 closeout): a finite-but-absurd
#: value still raises OverflowError inside ``time.sleep`` AFTER the claim
#: wave is posted, which is the orphan path this validation exists to close.
#: Five minutes is already far past any useful settle window.
_MAX_SETTLE_SECONDS = 300.0


def _coerce_settle(value, source: str) -> float:
    """The ONE settle validation, applied BEFORE any post (FB-001): a settle
    value must be a finite non-negative number at or below
    ``_MAX_SETTLE_SECONDS``. ``time.sleep`` raises ValueError for
    negative/NaN and OverflowError for infinity AND for finite values past
    the platform range, and those exceptions are not ``_GhFailure`` -- without
    this check a bad override kills the command after the claim wave is
    already posted, orphaning it. Raises ``ConfigError`` (a coded envelope on
    every surface)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"[claims] settle_seconds must be a number (got {value!r} from {source})"
        )
    try:
        finite = math.isfinite(value)
    except (OverflowError, ValueError):
        # A TOML integer past the float range: tomllib accepts it, math does
        # not -- a coded config_error, never a traceback.
        raise ConfigError(
            f"[claims] settle_seconds is outside the representable range"
            f" (got a value from {source})"
        ) from None
    if not finite:
        raise ConfigError(
            f"[claims] settle_seconds must be finite (got {value!r} from {source})"
        )
    if value < 0:
        raise ConfigError(
            f"[claims] settle_seconds must be non-negative (got {value!r} from {source})"
        )
    if value > _MAX_SETTLE_SECONDS:
        raise ConfigError(
            f"[claims] settle_seconds must be at most {_MAX_SETTLE_SECONDS:g}"
            f" (got {value!r} from {source})"
        )
    return float(value)


def load_claims_config(
    store_config=None,
    cwd_config=None,
    explicit_config=None,
) -> dict:
    """Resolve the effective ``[claims]`` table: built-ins, then the store
    config (``<store>/config.toml``), then ``.zaxbygraph.toml`` in the cwd,
    then an explicit file -- later sources win PER KEY (merge, not replace).

    Each layer parameter accepts a filesystem PATH (a TOML file whose
    ``[claims]`` section is read) or an already-unwrapped DICT of claim
    values (the direct-call shape the acceptance checks pin). Raises
    :class:`ConfigError` naming the offending file for malformed TOML,
    unknown keys, or invalid value types.
    """
    cfg = dict(CLAIMS_DEFAULTS)
    supplied: str | None = None
    sources = [
        ("store", store_config),
        ("cwd", cwd_config),
        ("explicit", explicit_config),
    ]
    for label, source in sources:
        if source is None:
            continue
        if isinstance(source, dict):
            table = source
            path = None
        else:
            path = str(source)
            if tomllib is None:  # pragma: no cover - the floor has tomllib
                raise ConfigError(
                    "cannot read claims config: tomllib unavailable", path=path
                )
            try:
                with open(path, "rb") as fh:
                    data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(
                    f"malformed TOML in {label} claims config: {exc}", path=path
                ) from exc
            except (OSError, UnicodeDecodeError) as exc:
                # UnicodeDecodeError is what tomllib raises for a non-UTF-8
                # file (Windows PowerShell writes UTF-16LE by default) --
                # FB-010: it must be a coded config_error, not a traceback.
                raise ConfigError(
                    f"cannot read {label} claims config: {exc}", path=path
                ) from exc
            table = data.get("claims", {})
            if not isinstance(table, dict):
                raise ConfigError("[claims] must be a TOML table", path=path)
        for key, value in table.items():
            cfg[key] = _validate_claims_value(key, value, path)
            supplied = label
    _validate_claims_vocabulary(cfg, supplied)
    cfg["_config_source"] = supplied or "built-ins"
    return cfg


def _validate_claims_vocabulary(cfg: dict, source: str | None) -> None:
    """Reject a claim pattern that is contained in the release prefix
    (FB-004): the release form is ANCHORED and the claim form is a
    substring test, so such an overlap makes every release parse as a
    fresh claim -- the release could never end the claim it targets."""
    release = str(cfg.get("release_prefix") or "").lower()
    if not release:
        return
    for pattern in cfg.get("patterns") or []:
        if str(pattern).lower() in release:
            raise ConfigError(
                "[claims] patterns entry "
                f"{pattern!r} is contained in release_prefix "
                f"{cfg.get('release_prefix')!r}; a release body matching a claim"
                " pattern could never end its claim",
                path=None,
            )


def claims_config_echo(cfg: dict) -> dict:
    """The payload echo: the six values in effect plus their config_source."""
    return {k: v for k, v in cfg.items() if k in _CLAIMS_KEYS} | {
        "config_source": cfg.get("_config_source") or "built-ins"
    }


# ---------------------------------------------------------------------------
# Untrusted-text wrapping (this module's own copy; derive.py imports claims,
# never the reverse)

_UNTRUSTED_NOTE = "untrusted: GitHub user content"


def _untrusted(value: str) -> dict:
    return {"untrusted_text": value, "content_trust": _UNTRUSTED_NOTE}


# ---------------------------------------------------------------------------
# Parsing


def _eligible_lines(body: str) -> list[str]:
    """The claim-scannable lines of a comment body: code-fence interior and
    quoted lines (a leading ``>``) are stripped before any pattern match,
    so a fenced example or a quoted claim never holds an issue."""
    out: list[str] = []
    in_fence = False
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith(">"):
            continue
        out.append(line)
    return out


def _token_re(token_prefix: str) -> re.Pattern:
    return re.compile(re.escape(token_prefix) + "(" + _TOKEN_CHARSET + r")(?![A-Za-z0-9-])")


def _ref_numbers(text: str) -> set[int]:
    """Local issue numbers referenced by ``#N`` in one line: bounded (a
    digit run past the cap is not a reference, ``#5abc`` is not a
    reference), and cross-repo ``owner/repo#N`` refs are skipped, never
    localized to the same-numbered local item."""
    out: set[int] = set()
    for match in _REF_RE.finditer(text):
        if _CROSS_REPO_BEFORE_RE.search(text[: match.start()]):
            continue
        out.add(int(match.group(1)))
    return out


def parse_claim(body: str | None, cfg: dict) -> dict | None:
    """Parse one comment body as a claim under the values in effect.

    Returns ``{token, named_issues, matched_text}`` -- ``token`` is ``None``
    for the recorded-usage tokenless form; ``named_issues`` is the SORTED
    union of the ``issues=`` segment refs and the eligible-text ``#N`` refs
    on the matched line; ``matched_text`` is the matched LINE verbatim.
    Returns ``None`` when no eligible line matches any configured pattern.
    """
    if not body:
        return None
    patterns = [str(p).lower() for p in (cfg.get("patterns") or [])]
    if not patterns:
        return None
    for line in _eligible_lines(body):
        lowered = line.lower()
        if not any(p in lowered for p in patterns):
            continue
        token: str | None = None
        token_match = _token_re(str(cfg.get("token_prefix", "session="))).search(line)
        if token_match:
            token = token_match.group(1)
        named: set[int] = set()
        seg = re.search(r"(?:^|\s)issues=(\S+)", line)
        if seg:
            for piece in seg.group(1).split(","):
                piece = piece.strip()
                if not piece:
                    continue
                if "/" in piece:
                    continue  # cross-repo ref: dropped, never localized
                if piece.startswith("#"):
                    piece = piece[1:]
                if re.fullmatch(r"[0-9]{1,%d}" % _MAX_REF_DIGITS, piece):
                    named.add(int(piece))
        named |= _ref_numbers(line)
        return {
            "token": token,
            "named_issues": sorted(named),
            "matched_text": line,
        }
    return None


def parse_release(body: str | None, cfg: dict) -> dict | None:
    """Parse one comment body as a release: the body must START WITH the
    configured release prefix (anchored -- a trailing mention is not a
    release). Returns ``{token}`` (``None`` for the tokenless release) or
    ``None``."""
    if not body:
        return None
    prefix = str(cfg.get("release_prefix", "**Claim released"))
    if not body.startswith(prefix):
        return None
    token: str | None = None
    token_match = _token_re(str(cfg.get("token_prefix", "session="))).search(body)
    if token_match:
        token = token_match.group(1)
    return {"token": token}


# ---------------------------------------------------------------------------
# Time helpers


def _parse_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sync_stamp(conn: sqlite3.Connection | None, repo: str) -> str | None:
    """The store's latest sync stamp for ``repo`` (the derived tier's asof
    anchor -- never the wall clock)."""
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT last_incr_sync_at, last_full_sync_at FROM sync_state"
            " WHERE repo = ?",
            (repo,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return row[0] or row[1]


# ---------------------------------------------------------------------------
# The canonical claim list (collect_claims)


def _github_id_from_url(url: str | None) -> int:
    """The terminal ``issuecomment-<id>`` of a comment URL; 0 (a stable
    sentinel that keeps ordering deterministic) when unrecoverable."""
    if not url:
        return 0
    match = re.search(r"issuecomment-(\d+)(?:$|[?#])", str(url))
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def _item_titles(conn: sqlite3.Connection | None, repo: str) -> dict[int, str]:
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT number, title FROM items WHERE repo = ?", (repo,)
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[int, str] = {}
    for row in rows:
        try:
            out[int(row[0])] = row[1] or ""
        except (TypeError, ValueError):
            continue
    return out


def _token_match_ends_claim(claim_token: str | None, release_token: str | None) -> bool:
    """A release ends a claim only when their token shapes agree: a
    tokenless release ends only tokenless claims; a token release never
    ends a tokenless claim (and never ends a DIFFERENT token's claim)."""
    if claim_token is None:
        return release_token is None
    return release_token == claim_token


def collect_claims(
    conn: sqlite3.Connection | None,
    repo: str,
    rows: Sequence[Sequence],
    cfg: dict,
    asof: str | None,
) -> list[dict]:
    """The FULL canonical claim list over comment rows, ascending by
    ``(created_at, github_id)`` (every listing order is derived from this).

    Each row is ``(number, author, author_association, created_at, body,
    html_url)`` -- the frozen direct shape -- optionally extended with
    ``github_id`` and ``title`` at indices 6/7 (the store/live callers,
    which carry comment ids and item titles without re-parsing URLs).

    A claim is ACTIVE iff its author association is trusted, it was created
    at/before ``asof`` with ``0 <= age < window_hours``, no LATER release on
    the same issue matches its token shape, and claims are enabled. Rows
    outside those rules are still LISTED (with ``trusted``/``active``
    flags); they simply gate nothing. Open-ness is the caller's row
    selection.
    """
    titles = _item_titles(conn, repo)
    asof_dt = _parse_instant(asof)
    window = float(cfg.get("window_hours", 72) or 0)
    enabled = bool(cfg.get("enabled", True))

    claims_by_issue: dict[int, list[dict]] = {}
    releases_by_issue: dict[int, list[dict]] = {}
    for row in rows:
        if len(row) < 6:
            continue
        number, author, association, created_at, body, html_url = row[:6]
        try:
            number = int(number)
        except (TypeError, ValueError):
            continue
        github_id = row[6] if len(row) > 6 else None
        if github_id is None:
            github_id = _github_id_from_url(html_url)
        try:
            github_id = int(github_id)
        except (TypeError, ValueError):
            github_id = 0
        title = row[7] if len(row) > 7 else titles.get(number, "")
        # Release-first (FB-004): the release form is ANCHORED (starts-with),
        # the claim form is an unanchored substring test, so a configured
        # claim pattern contained in the release prefix would otherwise
        # consume the release before parse_release ever sees it.
        parsed_release = parse_release(body, cfg)
        if parsed_release is not None:
            releases_by_issue.setdefault(number, []).append(
                {**parsed_release, "_key": (str(created_at or ""), github_id)}
            )
            continue
        parsed = parse_claim(body, cfg)
        if parsed is not None:
            claims_by_issue.setdefault(number, []).append(
                {
                    "number": number,
                    "title": title or "",
                    "comment_id": github_id,
                    "author": author,
                    "author_association": association,
                    "trusted": str(association or "").upper() in TRUSTED_ASSOCIATIONS,
                    "token": parsed["token"],
                    "created_at": created_at,
                    "named_issues": list(parsed["named_issues"]),
                    "url": html_url,
                    "text": _untrusted(parsed["matched_text"]),
                    "_key": (str(created_at or ""), github_id),
                }
            )
            continue

    out: list[dict] = []
    for number, claims in claims_by_issue.items():
        claims.sort(key=lambda c: c["_key"])
        releases = sorted(
            releases_by_issue.get(number, []), key=lambda r: r["_key"]
        )
        for claim in claims:
            ended = any(
                release["_key"] > claim["_key"]
                and _token_match_ends_claim(claim["token"], release["token"])
                for release in releases
            )
            created = _parse_instant(claim["created_at"])
            age_hours = (
                (asof_dt - created).total_seconds() / 3600.0
                if asof_dt is not None and created is not None
                else None
            )
            active = bool(
                enabled
                and claim["trusted"]
                and age_hours is not None
                and 0.0 <= age_hours < window
                and not ended
            )
            entry = {k: v for k, v in claim.items() if k != "_key"}
            entry["age_hours"] = (
                round(age_hours, 4) if age_hours is not None else None
            )
            entry["active"] = active
            entry["ended_by_release"] = ended
            out.append(entry)
    out.sort(key=lambda c: (str(c.get("created_at") or ""), c.get("comment_id") or 0))
    return out


def _store_claim_rows(conn: sqlite3.Connection, repo: str) -> list[tuple]:
    """Comment rows of the repo's OPEN ISSUES (the derived tier's caller
    selection): issue comments only -- review comments are not claims.

    The full set is materialized once (FB-021, disclosed boundary): the gate
    needs every candidate row before it can sort and classify, so incremental
    consumption is not available at this layer. The AGENTS.md
    "fetched incrementally" rule is stated for the ``sql`` escape hatch's
    user-supplied queries, not for this internal fixed read; the critic rated
    the materialization INFO (optional) and it is recorded as a boundary
    rather than claimed as a fix."""
    return list(
        conn.execute(
            "SELECT c.number, c.author, c.author_association, c.created_at,"
            " c.body, c.html_url, c.github_id"
            " FROM comments c JOIN items i ON i.repo = c.repo AND i.number = c.number"
            " WHERE c.repo = ? AND c.kind = 'issue_comment'"
            " AND i.kind = 'issue' AND i.state = 'open'",
            (repo,),
        )
    )


def claims_gate_by_number(
    conn: sqlite3.Connection,
    repo: str,
    cfg: dict | None = None,
    asof: str | None = None,
) -> dict[int, dict]:
    """The ``claimed`` gate map, DERIVED from the canonical claim list:
    for every gated issue number, the evidence of the EARLIEST active
    trusted claim covering it (a claim posted on the issue's own thread,
    or one naming it in its ``issues=``/prose references)."""
    if cfg is None:
        cfg = load_claims_config()
    if asof is None:
        asof = _sync_stamp(conn, repo)
    if not bool(cfg.get("enabled", True)):
        return {}
    rows = _store_claim_rows(conn, repo)
    full = collect_claims(conn, repo, rows, cfg, asof)
    open_numbers: set[int] = set()
    try:
        open_numbers = {
            int(r[0])
            for r in conn.execute(
                "SELECT number FROM items WHERE repo = ? AND kind = 'issue'"
                " AND state = 'open'",
                (repo,),
            )
        }
    except sqlite3.Error:
        pass
    gate: dict[int, dict] = {}
    for claim in full:  # ascending (created_at, github_id)
        if not claim.get("active") or not claim.get("trusted"):
            continue
        covered = {claim["number"]} | {
            n for n in (claim.get("named_issues") or []) if n in open_numbers
        }
        for number in sorted(covered):
            if number not in gate:
                gate[number] = claim
    return gate


def claims_listing(
    conn: sqlite3.Connection,
    repo: str,
    cfg: dict,
    asof: str | None = None,
) -> dict:
    """The derived ``claims`` listing payload: the canonical list NEWEST
    FIRST (the reverse of the (created_at, github_id) ordering), the
    window-expired subset under ``stale_claims`` with its ages, and the
    values-in-effect echo."""
    if asof is None:
        asof = _sync_stamp(conn, repo)
    rows = _store_claim_rows(conn, repo)
    full = collect_claims(conn, repo, rows, cfg, asof)
    newest_first = list(reversed(full))
    window = float(cfg.get("window_hours", 72) or 0)
    stale = [
        entry
        for entry in newest_first
        if entry.get("age_hours") is not None and entry["age_hours"] >= window
    ]
    return {
        "claims": newest_first,
        "stale_claims": stale,
        "derived": True,
        "live": False,
        "claims_config": claims_config_echo(cfg),
        "config_source": cfg.get("_config_source") or "built-ins",
    }


# ---------------------------------------------------------------------------
# The live gh seam


def run_gh(argv: list[str]) -> tuple[int, str, str]:
    """One ``gh`` subprocess call: argv list, no shell. The subprocess
    timeout rides ``ZAXBYGRAPH_CLAIM_GH_TIMEOUT_S`` (default 30 s; values
    BELOW 30 are clamped up to 30 -- the floor keeps a misconfigured small
    value from turning slow calls into false github_unreachable verdicts, so
    the knob can lengthen a call, not shorten it). Tests replace this module
    attribute to redirect the surface offline; the command functions
    late-bind it at CALL time.

    Subprocess faults never escape as tracebacks: a missing/unrunnable gh
    binary (FileNotFoundError/OSError) and a timed-out call both translate
    to a failed-result tuple so every caller's _GhFailure path
    (github_unreachable, with rollback when posts already landed) holds.
    """
    try:
        timeout = float(os.environ.get("ZAXBYGRAPH_CLAIM_GH_TIMEOUT_S", ""))
    except ValueError:
        timeout = _GH_TIMEOUT_S_DEFAULT
    budget = max(timeout, _GH_TIMEOUT_S_DEFAULT) if timeout else _GH_TIMEOUT_S_DEFAULT
    rendered = " ".join(str(a) for a in argv)
    try:
        proc = subprocess.run(
            [str(a) for a in argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=budget,
        )
    except subprocess.TimeoutExpired:
        return (124, "", f"gh timed out after {budget:g}s: {rendered[:200]}")
    except (FileNotFoundError, OSError) as exc:
        return (1, "", f"gh could not run: {exc}")
    return (proc.returncode, proc.stdout or "", proc.stderr or "")


def _runner(run_gh: Callable | None) -> Callable:
    """Late-bind the module-level gh runner at call time (the injectable
    seam the tests patch by assigning ``zaxbygraph.claims.run_gh``)."""
    if run_gh is not None:
        return run_gh
    return sys.modules[__name__].run_gh


class _GhFailure(Exception):
    """A gh call that failed before anything could be posted."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


#: Public alias: the CLI/MCP adapters catch this to translate a failed gh
#: read into the ``github_unreachable`` outcome envelope.
GhFailure = _GhFailure


def _check_rc(rc: int, stderr: str, what: str) -> None:
    if rc != 0:
        raise _GhFailure(f"gh {what} failed (rc={rc}): {stderr.strip()[:200]}")


def _json_call(runner: Callable, argv: list[str], what: str):
    rc, stdout, stderr = runner(argv)
    _check_rc(rc, stderr, what)
    try:
        return json.loads(stdout)
    except ValueError as exc:
        raise _GhFailure(f"gh {what} returned unparseable output: {exc}") from exc


# -- probe normalization (round 3) ------------------------------------------
#
# A membership-probe result is a HIT only on rc in {0, 204}; a MISS only on
# an HTTP-404-shaped result -- rc == 404, or rc == 1 whose stderr shows
# HTTP 404 / "not found" / the frozen fake's no-route shape. Any OTHER rc=1
# (network error, unexpected status) is a probe FAILURE and surfaces as
# github_unreachable -- never folded into a negative trust answer, and a
# corroborating viewerPermission payload can never mask it.

_MISS_SHAPES = ("404", "not found", "fake cannot route")


def _probe_result(rc: int, stdout: str, stderr: str) -> str:
    """'hit' | 'miss' | 'failure' for one membership probe."""
    if rc in (0, 204):
        return "hit"
    if rc == 404:
        return "miss"
    if rc == 1:
        text = f"{stderr} {stdout}".lower()
        if any(shape in text for shape in _MISS_SHAPES):
            return "miss"
    return "failure"


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


def _resolve_login_trust(
    repo: str, runner: Callable
) -> tuple[bool, dict]:
    """The Design-11 login-trust precheck. Trusted iff the session login is
    the repo owner, OR the org-membership endpoint hits (204), OR the
    repository-collaborator endpoint hits (includes read-only outside
    collaborators, matching the COLLABORATOR association definition). When
    both membership probes conclusively MISS, a viewerPermission of
    ADMIN/MAINTAIN/WRITE corroborates (the frozen contract's third leg);
    a probe FAILURE raises (github_unreachable) and is never masked."""
    owner, _name = _split_repo(repo)
    repo_rec = _json_call(runner, ["gh", "api", f"repos/{repo}"], f"repos/{repo}")
    if not isinstance(repo_rec, dict):
        raise _GhFailure(f"gh repos/{repo} returned an unexpected payload shape")
    owner_login = ((repo_rec.get("owner") or {}).get("login")) or ""
    user_rec = _json_call(runner, ["gh", "api", "user"], "user")
    if not isinstance(user_rec, dict) or not user_rec.get("login"):
        raise _GhFailure("gh user returned no login")
    login = str(user_rec["login"])
    probes: dict = {"login": login, "owner": owner_login}
    if owner_login and login == owner_login:
        probes["owner_match"] = True
        return True, probes
    probes["owner_match"] = False

    org_rc, org_out, org_err = runner(
        ["gh", "api", f"orgs/{owner}/members/{login}"]
    )
    org = _probe_result(org_rc, org_out, org_err)
    probes["org_membership"] = org
    if org == "failure":
        raise _GhFailure(
            f"org membership probe failed (rc={org_rc}): {org_err.strip()[:160]}"
        )
    if org == "hit":
        return True, probes

    coll_rc, coll_out, coll_err = runner(
        ["gh", "api", f"repos/{repo}/collaborators/{login}"]
    )
    collab = _probe_result(coll_rc, coll_out, coll_err)
    probes["collaborator"] = collab
    if collab == "failure":
        raise _GhFailure(
            f"collaborator probe failed (rc={coll_rc}): {coll_err.strip()[:160]}"
        )
    if collab == "hit":
        return True, probes

    # Both membership probes missed conclusively: the corroborating
    # viewerPermission fallback. A FAILED probe raises (never masked into a
    # negative trust verdict -- FB-002); only a conclusive answer counts.
    try:
        payload = _json_call(
            runner,
            ["gh", "api", "graphql", "-f",
             "query={ repository(owner: \"%s\", name: \"%s\") { viewerPermission } }"
             % (owner, _name)],
            "graphql viewerPermission",
        )
    except _GhFailure:
        raise
    permission = ""
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if errors:
            raise _GhFailure(
                "graphql viewerPermission returned errors: "
                + str(errors)[:160]
            )
        permission = str(
            ((payload.get("data") or {}).get("repository") or {}).get(
                "viewerPermission"
            )
            or payload.get("viewerPermission")
            or ""
        )
    probes["viewer_permission"] = permission
    if permission in ("ADMIN", "MAINTAIN", "WRITE"):
        return True, probes
    return False, probes


# -- paged FULL reads (never a window-bound since=) --------------------------


def _read_page_stream(
    runner: Callable,
    argv_for_page: Callable[[int], list[str]],
    *,
    max_pages: int,
    what: str,
) -> list[dict]:
    """A paged FULL read: every page joined until exhausted (a page shorter
    than the page size, or a page whose comment ids were all already seen).
    Reaching ``max_pages`` raises -- the caller decides fail-closed (a
    safety read) or truncated (a presentation read)."""
    out: list[dict] = []
    seen_ids: set = set()
    for page_no in range(1, max_pages + 1):
        rc, stdout, stderr = runner(argv_for_page(page_no))
        _check_rc(rc, stderr, f"{what} page {page_no}")
        try:
            payload = json.loads(stdout)
        except ValueError as exc:
            raise _GhFailure(f"gh {what} page {page_no} was unparseable: {exc}") from exc
        if not isinstance(payload, list):
            raise _GhFailure(f"gh {what} page {page_no} was not a list")
        malformed = [e for e in payload if not isinstance(e, dict)]
        if malformed:
            # A safety read is complete or it fails closed (FB-018): a
            # malformed entry's content is unknown, and a claim-bearing
            # comment hidden inside one must never be silently dropped from
            # a decision that can put two sessions on one issue.
            raise _GhFailure(
                f"gh {what} page {page_no} carried {len(malformed)} malformed"
                " entr" + ("y" if len(malformed) == 1 else "ies")
                + "; the read fails closed rather than dropping unknown content"
            )
        entries = list(payload)
        page_ids = {
            e["id"] for e in entries if isinstance(e.get("id"), int)
        }
        fresh = bool(page_ids - seen_ids) if page_ids else None
        seen_ids |= page_ids
        out.extend(entries)
        # Completeness is judged on the RAW page length (FB-018): counting
        # only well-formed entries would treat a full page carrying one
        # malformed element as the final page and silently truncate the
        # safety read.
        if len(payload) < _PAGE_SIZE:
            return out
        if fresh is False:
            # A page of only-already-seen comment ids: the listing is
            # exhausted (a server that ignores the page cursor).
            return out
    raise _GhFailure(
        f"gh {what} exceeded the page cap ({max_pages} pages) without exhausting"
        " the listing"
    )


def _issue_comments(runner: Callable, repo: str, number: int) -> list[dict]:
    """One issue's FULL comment history (a safety read: complete or the
    command fails closed)."""

    def _argv(page: int) -> list[str]:
        return [
            "gh", "api",
            f"repos/{repo}/issues/{number}/comments?page={page}&per_page={_PAGE_SIZE}",
        ]

    return _read_page_stream(
        runner, _argv, max_pages=_SAFETY_MAX_PAGES, what=f"issues/{number}/comments"
    )


def _issue_record(runner: Callable, repo: str, number: int) -> dict:
    rec = _json_call(runner, ["gh", "api", f"repos/{repo}/issues/{number}"],
                     f"issues/{number}")
    if not isinstance(rec, dict):
        raise _GhFailure(f"gh issues/{number} returned an unexpected payload shape")
    return rec


def _same_repo_ref(node: dict, repo: str) -> bool:
    """Whether one closingIssuesReferences element names an issue of THIS
    repo. Real gh answers a flat array whose elements carry a
    ``repository`` object and an ``html_url`` (captured 2026-11-15 on
    ZaxbyHub/zaxbygraph: ``{"id", "number", "repository": {"name", "owner":
    {"login"}}, "url": "https://github.com/O/R/issues/N"}``); a cross-repo
    ref there must never attach to a same-numbered local issue. Elements
    that carry NO repository information (the offline fakes' bare
    ``{"number": N}``) are same-repo by construction."""
    slug = repo.lower()
    url = str(node.get("url") or node.get("html_url") or "").lower()
    if url:
        if f"github.com/{slug}/issues/" in url or f"/repos/{slug}/issues/" in url:
            return True
        if "/issues/" in url or "/pulls/" in url:
            return False  # an url naming a DIFFERENT repo
    repository = node.get("repository")
    if isinstance(repository, dict):
        owner = repository.get("owner")
        owner_login = (
            owner.get("login") if isinstance(owner, dict) else repository.get("login")
        )
        name = repository.get("name") or repository.get("full_name")
        if isinstance(name, str) and "/" in name:
            full = str(name)
        elif isinstance(owner_login, str) and isinstance(name, str):
            full = f"{owner_login}/{name}"
        else:
            full = str(repository.get("full_name") or "")
        if full:
            return full.lower() == slug
    return True  # no repo information: same-repo by construction


def _open_closing_prs(runner: Callable, repo: str) -> list[dict]:
    """The repo's open PRs with their closing references (a safety read for
    claim/pr-guard decisions). ``-R O/R`` appears ONLY here -- ``gh api``
    rejects it and every api route is explicit. The closing references are
    accepted in BOTH real shapes -- a FLAT array of element objects (the
    live gh shape, captured 2026-11-15) and a ``{"nodes": [...]}`` wrapper
    -- plus the offline fakes' bare int list; every element is filtered to
    THIS repo (cross-repo same-number refs are dropped, never localized).
    """
    argv = [
        "gh", "pr", "list", "-R", repo, "--state", "open",
        "--limit", str(_PR_LIST_LIMIT), "--json",
        "number,title,url,closingIssuesReferences",
    ]
    payload = _json_call(runner, argv, "pr list")
    if not isinstance(payload, list):
        raise _GhFailure("gh pr list returned an unexpected payload shape")
    if len(payload) >= _PR_LIST_LIMIT:
        # SAFETY-READ CAP RULE: a list that came back at the request cap
        # may be partial, and a partial closing-ref list can never certify
        # a clean guard -- fail closed instead of fabricating "no
        # collisions" from truncated evidence.
        raise _GhFailure(
            f"gh pr list returned the full --limit {_PR_LIST_LIMIT} cap;"
            " the closing-reference read may be partial"
        )
    out: list[dict] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("state") or "open").lower() != "open":
            continue
        closes: set[int] = set()
        refs = entry.get("closingIssueReferences")
        if refs is None:
            refs = entry.get("closingIssuesReferences")
        elements: list = []
        if isinstance(refs, list):
            elements = refs  # the real flat-array shape
        elif isinstance(refs, dict):
            nodes = refs.get("nodes")
            elements = nodes if isinstance(nodes, list) else []
        for node in elements:
            if isinstance(node, dict):
                try:
                    number = int(node.get("number"))
                except (TypeError, ValueError):
                    continue
                if _same_repo_ref(node, repo):
                    closes.add(number)
            else:
                # A bare INTEGER element (the shape the offline fakes use
                # for closing_issues, accepted in every refs container): a
                # same-repo issue number by construction -- never dropped.
                try:
                    closes.add(int(node))
                except (TypeError, ValueError):
                    continue
        raw = entry.get("closing_issues")
        if isinstance(raw, list):
            for value in raw:
                try:
                    closes.add(int(value))
                except (TypeError, ValueError):
                    continue
        record = {
            "number": entry.get("number"),
            "title": entry.get("title") or "",
            "url": entry.get("url") or "",
            "closes": sorted(n for n in closes if isinstance(n, int)),
        }
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# Live classification


def _live_claims_for_issue(
    comments: list[dict],
    number: int,
    cfg: dict,
    *,
    issue_open: bool,
    closing_prs: list[dict],
) -> list[dict]:
    """Classify one issue's comments into claim entries against the WALL
    CLOCK (the live view's asof), honouring the issue-state and
    open-closing-PR conditions read per-candidate -- never the comments
    feed alone."""
    rows = [
        (
            number,
            (c.get("user") or {}).get("login") if isinstance(c.get("user"), dict) else c.get("author"),
            c.get("author_association"),
            c.get("created_at"),
            c.get("body"),
            c.get("html_url"),
            c.get("id"),
        )
        for c in comments
        if isinstance(c, dict)
    ]
    entries = collect_claims(None, "", rows, cfg, _now().strftime("%Y-%m-%dT%H:%M:%SZ"))
    if issue_open and not closing_prs:
        return entries
    adjusted: list[dict] = []
    for entry in entries:
        if not issue_open:
            entry = dict(entry, active=False, closed_issue=True)
        elif closing_prs:
            entry = dict(entry, active=False, open_closing_pr=True)
        adjusted.append(entry)
    return adjusted


def list_claims_live(repo: str, cfg: dict, run_gh: Callable | None = None) -> dict:
    """The ``claims --live`` view: the repo-wide comments feed (paged, FULL
    -- no ``since=``, because expired claims must be listed), each candidate
    re-validated through its own issue read plus the open-PR list. The store
    is never opened for writing and never synced."""
    runner = _runner(run_gh)

    def _argv(page: int) -> list[str]:
        return [
            "gh", "api",
            f"repos/{repo}/issues/comments?page={page}&per_page={_PAGE_SIZE}",
        ]

    feed = _read_page_stream(
        runner, _argv, max_pages=_SAFETY_MAX_PAGES, what="issues/comments"
    )

    by_issue: dict[int, list[dict]] = {}
    for entry in feed:
        number = entry.get("issue_number")
        if number is None:
            url = str(entry.get("issue_url") or "")
            match = re.search(r"/issues/(\d+)$", url)
            number = int(match.group(1)) if match else None
        try:
            number = int(number)
        except (TypeError, ValueError):
            continue
        by_issue.setdefault(number, []).append(entry)

    prs = _open_closing_prs(runner, repo)
    closes_by_issue: dict[int, list[dict]] = {}
    for pr in prs:
        for n in pr["closes"]:
            closes_by_issue.setdefault(n, []).append(pr)

    claims: list[dict] = []
    for number in sorted(by_issue):
        comments = by_issue[number]
        # PR-conversation comments are not issue claims: the per-candidate
        # issue read tells issue from PR (a pull_request key) and supplies
        # the open/closed state. The read is a SAFETY read: a candidate
        # whose issue record cannot be fetched fails the WHOLE listing
        # (github_unreachable) -- a silently omitted claim would make a
        # partial list look complete.
        record = _issue_record(runner, repo, number)
        if isinstance(record.get("pull_request"), dict):
            continue  # a PR conversation thread, not an issue claim
        issue_open = str(record.get("state") or "").lower() == "open"
        rows = [
            (
                number,
                (c.get("user") or {}).get("login")
                if isinstance(c.get("user"), dict)
                else c.get("author"),
                c.get("author_association"),
                c.get("created_at"),
                c.get("body"),
                c.get("html_url"),
                c.get("id"),
                record.get("title") or "",
            )
            for c in comments
        ]
        entries = collect_claims(
            None, "", rows, cfg, _now().strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        for entry in entries:
            if issue_open and number not in closes_by_issue:
                claims.append(entry)
            else:
                reason = (
                    "closed_issue" if not issue_open else "open_closing_pr"
                )
                claims.append(dict(entry, active=False, inactive_reason=reason))

    claims.sort(
        key=lambda c: (str(c.get("created_at") or ""), c.get("comment_id") or 0)
    )
    newest_first = list(reversed(claims))
    truncated = len(newest_first) > _PRESENTATION_MAX_ENTRIES
    if truncated:
        newest_first = newest_first[:_PRESENTATION_MAX_ENTRIES]
    window = float(cfg.get("window_hours", 72) or 0)
    stale = [
        e
        for e in newest_first
        if e.get("age_hours") is not None and e["age_hours"] >= window
    ]
    return {
        "claims": newest_first,
        "stale_claims": stale,
        "derived": False,
        "live": True,
        "truncated": truncated,
        "claims_config": claims_config_echo(cfg),
        "config_source": cfg.get("_config_source") or "built-ins",
    }


# ---------------------------------------------------------------------------
# Live flows: take_claims / release_claims / pr_guard


def _mint_token() -> str:
    return str(uuid.uuid4())


#: The canonical Design-1 lead: the built-in patterns' first entry.
_CANONICAL_CLAIM_LEAD = "Agent claim"

#: The canonical Design-2 release prefix (the built-in [claims] default).
_CANONICAL_RELEASE_PREFIX = "**Claim released"


def _claim_body(token: str, numbers: list[int], cfg: dict) -> str:
    """The posted claim body, built from the CONFIGURED vocabulary: the
    lead is the effective patterns[0] and the token rides the effective
    token_prefix, so the tool's own parser recognizes its own posts under
    overrides. With the default table the body is byte-identical to the
    canonical Design-1 form. When the configured lead is NOT the built-in
    one, the canonical marker is appended after the configured-form body:
    the frozen C13 contract pins that a claim posted under a patterns
    override still carries the ``**Agent claim`` marker readers (and the
    offline fakes' post routing) look for."""
    patterns = [str(p) for p in (cfg.get("patterns") or [])]
    lead = patterns[0] if patterns else _CANONICAL_CLAIM_LEAD
    prefix = str(cfg.get("token_prefix", "session="))
    named = ",".join(f"#{n}" for n in numbers)
    canonical = (
        f"**{_CANONICAL_CLAIM_LEAD}:** {prefix}{token} issues={named}"
        " working this issue now."
    )
    if lead.lower() == _CANONICAL_CLAIM_LEAD.lower():
        return canonical
    configured = f"**{lead}:** {prefix}{token} issues={named} working this issue now."
    return f"{configured} {canonical}"


def _release_body(token: str | None, cfg: dict) -> str:
    """The posted release body from the CONFIGURED vocabulary: the
    effective release_prefix (anchored, so the configured form LEADS --
    the parser only matches bodies that START WITH the configured prefix)
    and token_prefix. With the default table the body is byte-identical to
    the Design-2 form. Under a release_prefix override the canonical
    ``**Claim released`` marker is appended after the configured form (the
    same frozen-contract compromise the claim body makes). The tokenless
    form carries NO token segment anywhere in the body."""
    release_prefix = str(cfg.get("release_prefix") or _CANONICAL_RELEASE_PREFIX)
    prefix = str(cfg.get("token_prefix", "session="))
    if token is None:
        configured = f"{release_prefix}:** scope handed back."
        canonical = f"{_CANONICAL_RELEASE_PREFIX}:** scope handed back."
    else:
        configured = f"{release_prefix}:** {prefix}{token} done."
        canonical = f"{_CANONICAL_RELEASE_PREFIX}:** {prefix}{token} done."
    if release_prefix == _CANONICAL_RELEASE_PREFIX:
        return canonical
    return f"{configured} {canonical}"


def _post_comment(
    runner: Callable, repo: str, number: int, body: str
) -> dict:
    rc, stdout, stderr = runner(
        ["gh", "api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}"]
    )
    _check_rc(rc, stderr, f"comment post on issues/{number}")
    try:
        created = json.loads(stdout)
    except ValueError as exc:
        raise _GhFailure(f"comment post on issues/{number} was unparseable: {exc}") from exc
    if not isinstance(created, dict):
        raise _GhFailure(f"comment post on issues/{number} was not an object")
    return created


def _sync_numbers_hint(numbers: Iterable[int]) -> str:
    joined = ",".join(str(int(n)) for n in sorted({int(n) for n in numbers}))
    return f"zaxbygraph sync --numbers {joined} refreshes the stored state for these items"


def _gh_unreachable(exc: _GhFailure) -> tuple[str, dict, str]:
    return (
        "github_unreachable",
        {"outcome": "github_unreachable", "details": {"reason": exc.message}},
        f"GitHub could not be reached: {exc.message}",
    )


def _entry_evidence(claim: dict) -> dict:
    """The refusal-facing projection of a holding claim (parsed fields; the
    comment text stays wrapped)."""
    return {
        "number": claim.get("number"),
        "author": claim.get("author"),
        "author_association": claim.get("author_association"),
        "trusted": claim.get("trusted"),
        "token": claim.get("token"),
        "created_at": claim.get("created_at"),
        "age_hours": claim.get("age_hours"),
        "named_issues": list(claim.get("named_issues") or []),
        "url": claim.get("url"),
        "comment_id": claim.get("comment_id"),
        "text": claim.get("text"),
    }


def _hold_snapshot(
    runner: Callable, repo: str, numbers: list[int], cfg: dict
) -> tuple[dict[int, list[dict]], list[dict], dict[int, str]]:
    """Pre-read every named issue's FULL history + the open-PR list once.
    Returns (active claims per issue, open closing PRs, issue problems)."""
    active: dict[int, list[dict]] = {}
    problems: dict[int, str] = {}
    prs: list[dict] = []
    for number in numbers:
        record = _issue_record(runner, repo, number)
        if isinstance(record.get("pull_request"), dict):
            problems[number] = "not an issue (pull request)"
            continue
        state = str(record.get("state") or "").lower()
        if state != "open":
            problems[number] = f"issue is {state or 'unknown state'}"
            continue
        comments = _issue_comments(runner, repo, number)
        entries = _live_claims_for_issue(
            comments, number, cfg, issue_open=True, closing_prs=[]
        )
        active[number] = [e for e in entries if e.get("active")]
    # DISCLOSED BOUNDARY (FB-006): the derived gate also holds every issue a
    # claim NAMES, wherever that claim was posted; the live pre-read reads
    # only the REQUESTED issues' own threads, so a claim posted solely on an
    # issue outside the request set that names a requested one is invisible
    # here (the derived gate still covers it once the store is synced). A
    # cross-read among the requested threads was tried and removed: under the
    # all-or-nothing refusal it is unobservable (the naming issue's own hold
    # already refuses the command), so it was dead code. Closing the gap for
    # real needs the repo-wide feed (which the 40x100 safety cap would make a
    # hard refusal on large repositories) or a store read (which trades the
    # live path's freshness guarantee) -- both recorded as boundaries.
    if not problems:
        prs = _open_closing_prs(runner, repo)
    return active, prs, problems


def _retry_wait_seconds() -> float:
    return _RNG.uniform(_RETRY_WAIT_MIN_S, _RETRY_WAIT_MAX_S)


def take_claims(
    repo: str,
    numbers: list[int],
    cfg: dict,
    run_gh: Callable | None = None,
    *,
    settle_seconds: float | None = None,
) -> tuple[str, dict, str | None]:
    """The live ``claim`` flow. Returns ``(outcome, data, message)`` where
    ``data`` is the envelope payload (outcome + details + claims_config +
    config_source echo) and ``message`` is the failure text for nonzero
    outcomes (None on success). Posts at most two waves (one retry), each
    wave released atomically when it loses."""
    runner = _runner(run_gh)
    ordered = sorted({int(n) for n in numbers})
    settle = _coerce_settle(cfg.get("settle_seconds", 2.0), "config")
    if settle_seconds is not None:
        settle = _coerce_settle(settle_seconds, "override")
    echo = claims_config_echo(cfg)
    base_data: dict = {"claims_config": echo, "config_source": echo["config_source"]}

    try:
        trusted, probes = _resolve_login_trust(repo, runner)
    except _GhFailure as exc:
        outcome, data, message = _gh_unreachable(exc)
        return outcome, {**base_data, **data}, message
    base_data["login_probes"] = probes
    if not trusted:
        return (
            "not_applicable",
            {
                **base_data,
                "outcome": "not_applicable",
                "reason": (
                    "the session login is not a trusted claim author"
                    " (not the repo owner, no org membership, no collaborator"
                    " access, viewerPermission below WRITE)"
                ),
            },
            None,
        )
    if not bool(cfg.get("enabled", True)):
        return (
            "not_applicable",
            {
                **base_data,
                "outcome": "not_applicable",
                "reason": "claims are disabled by the config chain ([claims] enabled = false)",
            },
            None,
        )

    posted: list[dict] = []  # this command's posts, for rollback
    token = _mint_token()

    def _release_wave(wave: list[dict]) -> list[dict]:
        """Release every comment the wave posted; returns unreleased ones."""
        unreleased: list[dict] = []
        for entry in wave:
            try:
                _post_comment(
                    runner, repo, entry["number"],
                    _release_body(token, cfg),
                )
            except _GhFailure:
                unreleased.append(entry)
        return unreleased

    def _outcome_taken(details: dict, message: str) -> tuple[str, dict, str]:
        return "taken", {**base_data, "outcome": "taken", "details": details}, message

    try:
        active, prs, problems = _hold_snapshot(runner, repo, ordered, cfg)
        if problems:
            return _outcome_taken(
                {
                    "invalid_issues": [
                        {"number": n, "problem": p} for n, p in sorted(problems.items())
                    ],
                    "remedy": _sync_numbers_hint(ordered),
                },
                "refused: " + "; ".join(
                    f"#{n}: {p}" for n, p in sorted(problems.items())
                ),
            )
        held = {n: claims for n, claims in active.items() if claims}
        if held:
            holders = {
                int(n): [_entry_evidence(c) for c in claims]
                for n, claims in sorted(held.items())
            }
            return _outcome_taken(
                {
                    "held_by_active_claim": holders,
                    "remedy": _sync_numbers_hint(ordered),
                },
                "refused: issues held by an active claim: "
                + ", ".join(f"#{n}" for n in sorted(held)),
            )
        collisions = [
            pr
            for pr in prs
            if any(int(n) in set(ordered) for n in pr["closes"])
        ]
        if collisions:
            return _outcome_taken(
                {
                    "open_closing_prs": collisions,
                    "remedy": _sync_numbers_hint(ordered),
                },
                "refused: an open pull request already closes "
                + ", ".join(
                    "#%d" % n
                    for pr in collisions
                    for n in pr["closes"]
                    if int(n) in set(ordered)
                ),
            )

        def _post_wave() -> tuple[str, dict, str | None]:
            wave: list[dict] = []
            for number in ordered:
                try:
                    created = _post_comment(
                        runner, repo, number, _claim_body(token, ordered, cfg)
                    )
                except _GhFailure as exc:
                    unreleased = _release_wave(wave)
                    details: dict = {
                        "failed_issue": number,
                        "failure": exc.message,
                        "released": [e["url"] for e in wave],
                    }
                    if unreleased:
                        details["unreleased_comments"] = [e["url"] for e in unreleased]
                    return (
                        "taken",
                        {
                            **base_data,
                            "outcome": "taken",
                            "token": token,
                            "details": details,
                        },
                        f"a comment post failed on issue #{number}: {exc.message}",
                    )
                wave.append(
                    {
                        "number": number,
                        "url": created.get("html_url")
                        or f"https://github.com/{repo}/issues/{number}",
                        "comment_id": created.get("id"),
                        "created_at": created.get("created_at"),
                    }
                )
            posted.extend(wave)
            return "", {}, None

        outcome, data, message = _post_wave()
        if outcome:
            return outcome, data, message

        # Settle (Design 2/AC13): give GitHub's read path a moment before
        # the post-settle verification read.
        sleep(settle)

        def _claim_key(entry: dict) -> tuple[str, int]:
            return (
                str(entry.get("created_at") or ""),
                int(entry.get("comment_id") or 0),
            )

        def _own_key(number: int, mine: dict | None) -> tuple[str, int]:
            """The session's own side of the arbitration: the posted
            comment's REAL id and creation time from the post response
            (falling back to the re-read entry when the response carried
            neither). The entry used is the CURRENT wave's post -- the LAST
            matching entry in posted order -- never a RELEASED earlier
            wave's post: a competitor that appeared between the first-wave
            release and the retry post must be arbitrated against the
            retry's key, or the retry would wrongly answer claimed."""
            meta = None
            for entry in posted:
                if entry["number"] == number:
                    meta = entry  # posted order is chronological: last = current wave
            created = (meta or {}).get("created_at") or (mine or {}).get(
                "created_at"
            )
            cid = (meta or {}).get("comment_id") or (mine or {}).get("comment_id")
            return (str(created or ""), int(cid or 0))

        def _settle_read() -> dict[int, dict]:
            """Re-read every named issue and ARBITRATE BY SESSION TOKEN
            (issue #77 claim 5: every session posts as one login, so
            authorship cannot separate sessions). A competing claim is any
            ACTIVE claim whose token differs from ours -- including
            tokenless foreign claims and same-login different-token
            claims. The earliest active claim per issue by (created_at,
            comment_id) wins; when the earliest is not our own post, the
            session loses that issue."""
            losers: dict[int, dict] = {}
            for number in ordered:
                comments = _issue_comments(runner, repo, number)
                entries = _live_claims_for_issue(
                    comments, number, cfg, issue_open=True, closing_prs=[]
                )
                active = [e for e in entries if e.get("active")]
                if not active:
                    continue
                mine = next(
                    (e for e in active if e.get("token") == token), None
                )
                competitors = [e for e in active if e.get("token") != token]
                if not competitors:
                    continue  # our own post is the only active claim
                own = _own_key(number, mine)
                earliest = min(competitors, key=_claim_key)
                if _claim_key(earliest) < own:
                    losers[number] = earliest
            return losers

        def _still_taken() -> dict[int, list[dict]]:
            taken: dict[int, list[dict]] = {}
            for number in ordered:
                comments = _issue_comments(runner, repo, number)
                entries = _live_claims_for_issue(
                    comments, number, cfg, issue_open=True, closing_prs=[]
                )
                live = [
                    e
                    for e in entries
                    if e.get("active") and e.get("token") != token
                ]
                if live:
                    taken[number] = live
            return taken

        losers = _settle_read()
        waves = 1
        wave_start = 0  # index into `posted` where the current wave begins
        while losers:
            winner = min(
                losers.values(),
                key=lambda e: (
                    str(e.get("created_at") or ""),
                    e.get("comment_id") or 0,
                ),
            )
            wave_slice = posted[wave_start:]
            unreleased = _release_wave(wave_slice)
            details: dict = {
                "winner": _entry_evidence(winner),
                "lost_issues": sorted(losers),
                "released": [e["url"] for e in wave_slice],
            }
            if unreleased:
                details["unreleased_comments"] = [e["url"] for e in unreleased]
            if waves >= 2:
                # The retry wave lost too: roll back and exit -- a third
                # posting wave never happens, so no retry wait either.
                return (
                    "lost_race",
                    {
                        **base_data,
                        "outcome": "lost_race",
                        "token": token,
                        "details": details,
                    },
                    "lost the claim race to an earlier claim held by "
                    + str(winner.get("author") or "another session"),
                )
            # Random pre-retry wait (Design 3): give the competitor a
            # moment to vanish before the fresh read that gates the ONE
            # retry wave.
            wait = _retry_wait_seconds()
            sleep(wait)
            details["retry_wait_seconds"] = round(wait, 3)
            still = _still_taken()
            if still:
                return (
                    "lost_race",
                    {
                        **base_data,
                        "outcome": "lost_race",
                        "token": token,
                        "details": details,
                    },
                    "lost the claim race to an earlier claim held by "
                    + str(winner.get("author") or "another session"),
                )
            # Clean fresh read: ONE retry wave, atomically rolled back if
            # it loses again.
            waves += 1
            wave_start = len(posted)
            outcome, data, message = _post_wave()
            if outcome:
                return outcome, data, message
            sleep(settle)
            losers = _settle_read()

        return (
            "claimed",
            {
                **base_data,
                "outcome": "claimed",
                "token": token,
                "posts": [
                    {"number": e["number"], "url": e["url"], "comment_id": e["comment_id"]}
                    for e in posted
                ],
            },
            None,
        )
    except _GhFailure as exc:
        rollback: dict = {}
        if posted:
            # A read failed after posts landed (timeout, transport): roll
            # back everything this command posted (never leave
            # unverifiable claims behind) and REPORT the rollback -- which
            # comments were released and which could not be.
            released_ok: list[str] = []
            unreleased: list[str] = []
            for entry in posted[:]:
                try:
                    _post_comment(
                        runner, repo, entry["number"], _release_body(token, cfg)
                    )
                    released_ok.append(entry["url"])
                except _GhFailure:
                    unreleased.append(entry["url"])
            rollback = {"released": released_ok}
            if unreleased:
                rollback["unreleased_comments"] = unreleased
        outcome, data, message = _gh_unreachable(exc)
        details = dict((data.get("details") or {}))
        details.update(rollback)
        data = {**data, "details": details}
        return outcome, {**base_data, **data}, message


def release_claims(
    repo: str,
    numbers: list[int],
    cfg: dict,
    token: str | None,
    run_gh: Callable | None = None,
) -> tuple[str, dict, str | None]:
    """The live ``release`` flow: post one Design-2 release on every named
    issue whose ACTIVE claim matches ``token`` (``None`` under the
    tokenless mode ends only tokenless claims); a token release never ends
    a tokenless claim. No matching issue -> ``taken`` (nonzero), nothing
    posted."""
    runner = _runner(run_gh)
    ordered = sorted({int(n) for n in numbers})
    echo = claims_config_echo(cfg)
    base_data: dict = {"claims_config": echo, "config_source": echo["config_source"]}
    posts: list[dict] = []
    try:
        matching: list[int] = []
        for number in ordered:
            record = _issue_record(runner, repo, number)
            if isinstance(record.get("pull_request"), dict):
                continue
            comments = _issue_comments(runner, repo, number)
            entries = _live_claims_for_issue(
                comments, number, cfg, issue_open=True, closing_prs=[]
            )
            for entry in entries:
                if not entry.get("active"):
                    continue
                if token is None:
                    if entry.get("token") is None:
                        matching.append(number)
                    break
                if entry.get("token") == token:
                    matching.append(number)
                    break
        if not matching:
            return (
                "taken",
                {
                    **base_data,
                    "outcome": "taken",
                    "details": {
                        "matching_issues": [],
                        "reason": (
                            "no named issue carries an active claim matching the"
                            " release token" + ("" if token else " (tokenless)")
                        ),
                    },
                },
                "refused: no active claim matches this release",
            )
        unreleased: list[dict] = []
        for number in matching:
            try:
                created = _post_comment(
                    runner, repo, number, _release_body(token, cfg)
                )
            except _GhFailure:
                unreleased.append({"number": number})
                continue
            posts.append(
                {
                    "number": number,
                    "url": created.get("html_url")
                    or f"https://github.com/{repo}/issues/{number}",
                }
            )
        if unreleased:
            return (
                "taken",
                {
                    **base_data,
                    "outcome": "taken",
                    "details": {
                        "released": posts,
                        "unreleased": [e["number"] for e in unreleased],
                    },
                },
                "a release post failed",
            )
        return (
            "claimed",
            {**base_data, "outcome": "claimed", "released": posts},
            None,
        )
    except _GhFailure as exc:
        outcome, data, message = _gh_unreachable(exc)
        return outcome, {**base_data, **data}, message


def pr_guard(
    repo: str,
    numbers: list[int],
    cfg: dict,
    run_gh: Callable | None = None,
) -> tuple[str, dict, str | None]:
    """The live ``pr-guard`` flow: list every open PR closing a named
    issue. Collisions -> ``taken`` (nonzero) naming each PR; a clean read
    exits 0. Posts nothing, ever."""
    runner = _runner(run_gh)
    ordered = sorted({int(n) for n in numbers})
    wanted = set(ordered)
    echo = claims_config_echo(cfg)
    base_data: dict = {"claims_config": echo, "config_source": echo["config_source"]}
    try:
        prs = _open_closing_prs(runner, repo)
    except _GhFailure as exc:
        outcome, data, message = _gh_unreachable(exc)
        return outcome, {**base_data, **data}, message
    collisions = [pr for pr in prs if wanted.intersection(pr["closes"])]
    if collisions:
        return (
            "taken",
            {
                **base_data,
                "outcome": "taken",
                "collisions": collisions,
                "remedy": _sync_numbers_hint(ordered),
            },
            "open pull requests already close "
            + ", ".join(f"#{pr['number']}" for pr in collisions),
        )
    return (
        "claimed",
        {
            **base_data,
            "outcome": "claimed",
            "collisions": [],
            "open_prs_checked": len(prs),
            "named": ordered,
        },
        None,
    )
