from __future__ import annotations

import re
from collections.abc import Callable, Iterable

_CLOSE_KW = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def item_kind(raw: dict) -> str:
    if raw.get("pull_request") is not None:
        return "pr"
    if raw.get("merged_at") is not None:
        return "pr"
    if isinstance(raw.get("base"), dict) and isinstance(raw.get("head"), dict):
        return "pr"
    return "issue"


def _strip_foreign(text: str, repo: str) -> str:
    """Blank out other-repo URLs and owner/repo#N so bare #N cannot steal them."""

    def repl_url(match: re.Match[str]) -> str:
        slug = match.group(1)
        return match.group(0) if slug.lower() == repo.lower() else " "

    def repl_short(match: re.Match[str]) -> str:
        slug = match.group(1)
        return match.group(0) if slug.lower() == repo.lower() else " "

    text = re.sub(
        r"https?://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/\d+",
        repl_url,
        text,
        flags=re.I,
    )
    text = re.sub(r"([\w.-]+/[\w.-]+)#\d+", repl_short, text)
    return text


def _mask_foreign(text: str, repo: str) -> str:
    """Same-length mask of other-repo references (issue #26 AC5): every
    character of a foreign URL or `owner/repo#N` span becomes a space, so
    bare #N cannot steal them AND match offsets still index the ORIGINAL
    text. `_strip_foreign` collapses spans to one space, which shifts every
    later offset; position-aware extraction must use this instead."""

    def blank(match: re.Match[str]) -> str:
        slug = match.group(1)
        if slug.lower() == repo.lower():
            return match.group(0)
        return " " * len(match.group(0))

    text = re.sub(
        r"https?://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/\d+",
        blank,
        text,
        flags=re.I,
    )
    text = re.sub(r"([\w.-]+/[\w.-]+)#\d+", blank, text)
    return text


def _foreign_text_positions(text: str, repo: str, patterns: list[str]) -> list[int]:
    """Start offsets of same-repo references in text, foreign spans masked
    same-length so offsets index the original string."""
    masked = _mask_foreign(text, repo)
    return [
        match.start(pattern_index)
        for pattern_index, pattern in enumerate(patterns)
        for match in re.finditer(pattern, masked)
    ]


def _location(prefix: str, offset: int) -> str:
    """Evidence location for one reference (issue #26 AC5): a title has no
    offset (the literal 'title'); anything else carries the reference token's
    character offset in the original text."""
    return prefix if prefix == "title" else f"{prefix}@{offset}"


def _iter_reference_locations(
    text: str | None, repo: str, prefix: str, patterns: list[str]
) -> list[tuple[int, str]]:
    """(number, location) for every same-repo #N reference matched by
    `patterns` in text.

    Foreign references are masked SAME-LENGTH (see _mask_foreign), so every
    offset indexes the text as stored. The reference token's offset is the
    '#' when the matched form carries one, else the digits' start (URL
    forms); the lowest offset wins per number so per-payload collapse is
    deterministic.
    """
    if not text or not _SLUG_RE.match(repo):
        return []
    masked = _mask_foreign(text, repo)
    best: dict[int, tuple[int, str]] = {}
    for pattern in patterns:
        for match in re.finditer(pattern, masked):
            number = int(match.group(1))
            digits_start = match.start(1)
            head = match.group(0)[: digits_start - match.start()]
            token_start = digits_start - 1 if head.endswith("#") else digits_start
            location = _location(prefix, token_start)
            prior = best.get(number)
            if prior is None or token_start < prior[0]:
                best[number] = (token_start, location)
    return [(number, location) for number, (_, location) in sorted(best.items())]


def _mention_patterns(repo: str) -> list[str]:
    return [
        rf"(?i)https?://github\.com/{re.escape(repo)}/(?:issues|pull)/(\d+)\b",
        rf"(?i){re.escape(repo)}#(\d+)\b",
        r"(?<![A-Za-z0-9._-])#(\d+)\b",
    ]


def _closing_patterns(repo: str) -> list[str]:
    return [
        rf"(?i)\b{_CLOSE_KW}\s+(?:this\s+)?#(\d+)\b",
        rf"(?i)\b{_CLOSE_KW}\s+(?:this\s+)?{re.escape(repo)}#(\d+)\b",
        rf"(?i)\b{_CLOSE_KW}\s+(?:this\s+)?https?://github\.com/{re.escape(repo)}/(?:issues|pull)/(\d+)\b",
    ]


def iter_mention_locations(
    text: str | None, repo: str, prefix: str = "body"
) -> list[tuple[int, str]]:
    """Positions of same-repo #N mentions (issue #26 AC5). prefix 'title'
    yields the literal 'title'; 'body'/'comment:<id>'/'review:<id>' yield
    '<prefix>@<offset>'."""
    return _iter_reference_locations(text, repo, prefix, _mention_patterns(repo))


def iter_closing_locations(
    text: str | None, repo: str, prefix: str = "body"
) -> list[tuple[int, str]]:
    """Positions of same-repo closing-keyword references (#26 AC5) — the
    keyword forms ONLY (a closing reference is never also a mention; the
    per-payload suppression rule compares the two sets)."""
    return _iter_reference_locations(text, repo, prefix, _closing_patterns(repo))


_DUP_KW = r"(?:duplicates?|duplicated|dup)"


def iter_duplicate_locations(
    text: str | None, repo: str, prefix: str = "body"
) -> list[tuple[int, str]]:
    """Positions of same-repo 'duplicate of #N' text references (issue #27
    AC6): the duplicate_keyword stream. Foreign references never match
    (masked), and these never produce duplicate_of edges — that relation is
    GitHub-reported only."""
    dup_patterns = [
        rf"(?i)\b{_DUP_KW}\s+(?:of|by|for)?\s*#(\d+)\b",
        rf"(?i)\b{_DUP_KW}\s+(?:of|by|for)?\s*{re.escape(repo)}#(\d+)\b",
    ]
    return _iter_reference_locations(text, repo, prefix, dup_patterns)


def parse_closing_numbers(text: str | None, repo: str) -> set[int]:
    if not text or not _SLUG_RE.match(repo):
        return set()
    local = _strip_foreign(text, repo)
    nums: set[int] = set()
    patterns = [
        rf"(?i)\b{_CLOSE_KW}\s+(?:this\s+)?https?://github\.com/{re.escape(repo)}/(?:issues|pull)/(\d+)\b",
        rf"(?i)\b{_CLOSE_KW}\s+(?:this\s+)?{re.escape(repo)}#(\d+)\b",
        rf"(?i)\b{_CLOSE_KW}\s+(?:this\s+)?#(\d+)\b",
    ]
    for pat in patterns:
        for match in re.finditer(pat, local):
            nums.add(int(match.group(1)))
    return nums


def parse_mentioned_numbers(text: str | None, repo: str) -> set[int]:
    if not text or not _SLUG_RE.match(repo):
        return set()
    local = _strip_foreign(text, repo)
    nums: set[int] = set()
    for match in re.finditer(
        rf"(?i)https?://github\.com/{re.escape(repo)}/(?:issues|pull)/(\d+)\b",
        local,
    ):
        nums.add(int(match.group(1)))
    for match in re.finditer(rf"(?i){re.escape(repo)}#(\d+)\b", local):
        nums.add(int(match.group(1)))
    for match in re.finditer(r"(?<![A-Za-z0-9._-])#(\d+)\b", local):
        nums.add(int(match.group(1)))
    return nums


def _login(raw: dict) -> str | None:
    user = raw.get("user") or raw.get("author")
    if isinstance(user, dict):
        login = user.get("login")
        return str(login) if login else None
    return None


def _label_name(label: object) -> str | None:
    if isinstance(label, dict):
        name = label.get("name")
        return str(name) if name else None
    if isinstance(label, str) and label:
        return label
    return None


def edges_from_item(repo: str, raw: dict) -> list[tuple]:
    """EXTRACTED edge tuples: (src_type, src_id, rel, dst_type, dst_id, evidence, source).

    Reference evidence carries its location (issue #26 AC5): 'title' or
    'body@<offset>'; the lowest offset wins per number, title beats body, and
    the edge identity (the 7-column key) is untouched. A closes_keyword
    reference is not also a mentions reference (per-payload suppression);
    duplicate_keyword is independent of both (issue #27 AC6)."""
    number = int(raw["number"])
    item_id = str(number)
    edges: list[tuple] = []
    author = _login(raw)
    if author:
        edges.append(("actor", author, "authored", "item", item_id, "user.login", "payload"))
    for label in raw.get("labels") or []:
        name = _label_name(label)
        if name:
            edges.append(("item", item_id, "has_label", "label", name, "labels[]", "payload"))
    body = raw.get("body") or ""
    title = raw.get("title") or ""
    closed_locations = iter_closing_locations(body, repo)
    closed = {n for n, _ in closed_locations}
    for n, location in closed_locations:
        if n != number:
            edges.append(
                ("item", item_id, "closes_keyword", "item", str(n), f"{location} closing keyword", "keyword")
            )
    mentions: dict[int, str] = {}
    for n, location in iter_mention_locations(title, repo, "title"):
        mentions.setdefault(n, location)
    for n, location in iter_mention_locations(body, repo):
        mentions.setdefault(n, location)
    for n, location in sorted(mentions.items()):
        if n != number and n not in closed:
            edges.append(("item", item_id, "mentions", "item", str(n), location, "keyword"))
    for n, location in iter_duplicate_locations(title, repo, "title"):
        edges.append(("item", item_id, "duplicate_keyword", "item", str(n), location, "keyword"))
    for n, location in iter_duplicate_locations(body, repo):
        edges.append(("item", item_id, "duplicate_keyword", "item", str(n), location, "keyword"))
    return edges


def edges_from_comment(repo: str, number: int, raw: dict) -> list[tuple]:
    edges: list[tuple] = []
    cid = str(raw.get("id") or raw.get("github_id") or "")
    author = _login(raw)
    if author:
        edges.append(("actor", author, "commented", "item", str(number), f"comment:{cid}", "payload"))
    body = raw.get("body") or ""
    closed_locations = iter_closing_locations(body, repo, f"comment:{cid}")
    closed = {n for n, _ in closed_locations}
    for n, location in closed_locations:
        if n != number:
            edges.append(
                (
                    "item",
                    str(number),
                    "closes_keyword",
                    "item",
                    str(n),
                    f"{location} closing keyword",
                    "keyword",
                )
            )
    for n, location in iter_mention_locations(body, repo, f"comment:{cid}"):
        if n != number and n not in closed:
            edges.append(("item", str(number), "mentions", "item", str(n), location, "keyword"))
    for n, location in iter_duplicate_locations(body, repo, f"comment:{cid}"):
        edges.append(("item", str(number), "duplicate_keyword", "item", str(n), location, "keyword"))
    return edges


def edges_from_review(repo: str, number: int, raw: dict) -> list[tuple]:
    edges: list[tuple] = []
    rid = str(raw.get("id") or "")
    author = _login(raw)
    state = raw.get("state") or ""
    if author:
        edges.append(
            ("actor", author, "reviewed", "item", str(number), f"review:{rid}:{state}", "payload")
        )
    body = raw.get("body") or ""
    closed_locations = iter_closing_locations(body, repo, f"review:{rid}")
    closed = {n for n, _ in closed_locations}
    for n, location in closed_locations:
        if n != number:
            edges.append(
                (
                    "item",
                    str(number),
                    "closes_keyword",
                    "item",
                    str(n),
                    f"{location} closing keyword",
                    "keyword",
                )
            )
    for n, location in iter_mention_locations(body, repo, f"review:{rid}"):
        if n != number and n not in closed:
            edges.append(("item", str(number), "mentions", "item", str(n), location, "keyword"))
    for n, location in iter_duplicate_locations(body, repo, f"review:{rid}"):
        edges.append(("item", str(number), "duplicate_keyword", "item", str(n), location, "keyword"))
    return edges


def edges_from_files(number: int, files: Iterable[dict]) -> list[tuple]:
    edges: list[tuple] = []
    for rec in files:
        path = rec.get("filename") or rec.get("path")
        if path:
            edges.append(("item", str(number), "touches", "file", str(path), "pulls.files", "payload"))
    return edges


def collapse_edges(edges: Iterable[tuple]) -> list[tuple]:
    """Last-write-wins on the unique key (repo-less). Provenance source is
    part of the key: the same pair reported by two streams is two facts.

    ONE relation-scoped exception (plan revision 4): when one payload's
    emitters produce the SAME `closes_keyword` key — body text and a commit
    message both closing #N — their evidence MERGES instead of one
    provenance being silently dropped. Every other relation keeps strict
    last-write-wins: the pinned actor-collapse contracts (test_last_write_wins,
    test_two_comments_one_commented_edge, test_review_collapse_keeps_last_state)
    depend on the latest comment/review evidence winning, and full history
    already lives in the comments/reviews tables."""
    by_key: dict[tuple, tuple] = {}
    for edge in edges:
        src_type, src_id, rel, dst_type, dst_id, evidence, source = edge
        key = (src_type, src_id, rel, dst_type, dst_id, source)
        prior = by_key.get(key)
        if prior is not None and rel == "closes_keyword":
            by_key[key] = (*prior[:5], merge_evidence(prior[5], evidence), source)
        else:
            by_key[key] = edge
    return list(by_key.values())


#: Evidence is a payload (AGENTS), so merged `closes` rows carry every
#: provenance string. The cap bounds one pathological store's growth.
#: Trimming protects the parts a consumer cannot afford to lose — the
#: arriving stream's marker and the closing_ref stream's canonical string —
#: and a lone over-cap part is HARD-TRUNCATED, never dropped: merged
#: evidence must never silently become NULL, because callers write it
#: straight into the survivor row (issue #19 feedback round).
EVIDENCE_SEP = "; "
EVIDENCE_CAP = 512
CLOSES_REF_MARKER = "closingIssuesReferences"


def merge_evidence(*parts: str | None, reserve: tuple[str, ...] = ()) -> str | None:
    """Union distinct provenance strings, first-seen first, '; '-joined.

    None/empty inputs contribute nothing; the result is None only when
    EVERY input is empty — content is never dropped on the floor (an
    over-cap part is truncated to keep the payload present and bounded).
    Splitting on the separator keeps `merge_evidence` idempotent on its
    own output. Trimming over the cap drops newest-unprotected parts
    first; a part is protected when it contains any `reserve` substring
    or is the most recently seen part, so the stream that just arrived
    and the reserved markers (e.g. the closing_ref stream's canonical
    string) survive even in a pathological full-cap store (issue #19
    feedback round).
    """
    seen: list[str] = []
    known: set[str] = set()
    for part in parts:
        for piece in (part or "").split(EVIDENCE_SEP):
            piece = piece.strip()
            if piece and piece not in known:
                known.add(piece)
                seen.append(piece)
    if not seen:
        return None

    def _length(items: list[str]) -> int:
        return sum(len(p) for p in items) + (len(items) - 1) * len(EVIDENCE_SEP)

    protected = {i for i, p in enumerate(seen) if any(r in p for r in reserve)}
    protected.add(len(seen) - 1)
    for i in range(len(seen) - 1, -1, -1):
        if _length(seen) <= EVIDENCE_CAP:
            break
        if i in protected:
            continue
        del seen[i]
    out = EVIDENCE_SEP.join(seen)
    if len(out) > EVIDENCE_CAP:
        # Last resort (protected parts alone exceed the cap): hard-truncate.
        # Bounded beats absent — callers persist this verbatim.
        out = out[:EVIDENCE_CAP]
    return out


#: Relations any edges_from_* emitter can produce. The Phase 4.2 guardrail
#: (tests/test_relation_registry.py) pins this set against query.py's
#: structural set and the documented relationships table, so a new relation
#: cannot ship undocumented or unhandled by `path`.
RELATIONS = frozenset(
    {
        "authored",
        "has_label",
        "commented",
        "reviewed",
        "touches",
        "closes_keyword",
        "mentions",
        "closes",
        "cross_referenced",
        "closed_by_commit",
        "merged_by",
        "merged_commit",
        "reverts",
        # v9 (issue #26): PR branch names linking to their one stored issue.
        "branch_ref",
        # v10 (issue #27): GitHub-reported links and the duplicate text stream.
        "blocked_by",
        "child_of",
        "duplicate_of",
        "linked_pr",
        "duplicate_keyword",
    }
)


# ==== issue #26 text-derived references (migration v9) ======================

_PY_FRAME_RE = re.compile(r'File "([^"\n]+)", line \d+(?:, in ([A-Za-z_][\w.]*))?')
_PATH_COL_RE = re.compile(r"\b((?:[\w.-]+/)+[\w.-]+):(\d+):(\d+)\b")
_BACKTICK_PATH_RE = re.compile(r"`([^`\n]+)`")
_TRAILING_PUNCT = ".,;:!?)>]}"


def _plausible_path(candidate: str) -> bool:
    return (
        bool(candidate)
        and "://" not in candidate
        and not candidate.startswith(("http", "mailto:"))
        and ("/" in candidate or "." in candidate)
    )


def parse_path_refs(text: str | None, repo: str) -> list[tuple[str, int]]:
    """Raw path strings mentioned in text (issue #26 AC1): Python frames
    `File "<path>", line N`, `path:L:C` tokens (a `/` required, so URLs and
    clock times never match), same-repo `blob/<ref>/<path>` URLs (foreign
    repos produce nothing — the cross-repo rule), and backticked
    `seg/seg.ext` segments. Dedup per path with the backticked flag OR-ed;
    trailing punctuation trimmed. Paths stay RAW — read-time resolution is
    issue #29's lane."""
    if not text:
        return []
    flags: dict[str, int] = {}

    def record(candidate: str, backticked: int) -> None:
        candidate = candidate.replace("\\\\", "/").strip().rstrip(_TRAILING_PUNCT)
        if _plausible_path(candidate):
            flags[candidate] = flags.get(candidate, 0) | backticked

    for match in _PY_FRAME_RE.finditer(text):
        record(match.group(1), 0)
    for match in _PATH_COL_RE.finditer(text):
        record(match.group(1), 0)
    for match in re.finditer(
        rf"(?i)https?://github\.com/{re.escape(repo)}/blob/[^/\s)]+/([^\s)`]+)",
        text,
    ):
        record(match.group(1), 0)
    for match in _BACKTICK_PATH_RE.finditer(text):
        content = match.group(1)
        if "/" in content and "." in content and "://" not in content:
            record(content, 1)
    return sorted(flags.items())


def _mask_error_constants(line: str) -> str:
    """Replace run-varying constants with stable placeholders: 0x-hex
    addresses -> '<hex>', standalone digit runs -> '<n>' (issue #26 AC3)."""
    masked = re.sub(r"0[xX][0-9a-fA-F]+", "<hex>", line)
    masked = re.sub(r"(?<![A-Za-z0-9_])\d+(?![A-Za-z0-9_])", "<n>", masked)
    return masked.strip()

_SIGNATURE_LINE_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Interrupt|Exit|Warning|Failure|Fault))\b\s*:?\s*(.*)$"
)


def parse_error_signatures(text: str | None) -> list[tuple[str, str, str | None]]:
    """Error signatures from fenced blocks (issue #26 AC3): the exception
    name verbatim, the exception line with run-varying constants masked (so
    two runs differing only in hex addresses and line numbers produce the
    SAME signature), and the nearest preceding frame's function name when
    the block carries Python frames."""
    if not text:
        return []
    signatures: dict[tuple[str, str], str | None] = {}
    for block in re.findall(r"```.*?```", text, flags=re.S):
        last_function: str | None = None
        for line in block.splitlines():
            frame = _PY_FRAME_RE.match(line.strip())
            if frame:
                last_function = frame.group(2)
            signature = _SIGNATURE_LINE_RE.match(line)
            if signature:
                exception = signature.group(1)
                masked_line = _mask_error_constants(signature.group(0))
                key = (exception, masked_line)
                signatures.setdefault(key, last_function)
    return [(exc, line, fn) for (exc, line), fn in sorted(signatures.items())]


def branch_number_tokens(head_ref: str | None) -> list[int]:
    """Every maximal digit run in a branch name (issue #26 AC4):
    `fix/26-add-path-refs` -> [26], `issue-26` -> [26], `fix/26-27` ->
    [26, 27]. The branch_ref rule emits only for EXACTLY ONE token — a
    single token can never become ambiguous later (issue numbers are unique
    per repo), so no retraction path is ever needed."""
    if not head_ref:
        return []
    return [int(token) for token in re.findall(r"\d+", head_ref)]


# ==== issue #27 GitHub-reported links (migration v10) =======================

#: links-section connection key -> (relation, description) for the
#: source='link' stream. parent/duplicateOf are SINGULAR nodes (live schema
#: verified, plan-critic round 1); blockedBy/closedByPullRequestsReferences
#: are connections.
_LINK_CONNECTIONS = {
    "blocked_by": "blocked_by",
    "parent": "child_of",
    "duplicate_of": "duplicate_of",
    "open_prs": "linked_pr",
}


def _link_dst(repo: str, node: object, number: int) -> str | None:
    """One {number, repo} link node -> the stored endpoint id: bare number
    when same-repo, repo-qualified 'owner/repo#N' when foreign (the
    cross_referenced precedent); self-references and malformed nodes yield
    None."""
    if not isinstance(node, dict):
        return None
    dst_number = node.get("number")
    dst_repo = node.get("repo")
    if not isinstance(dst_number, int):
        return None
    if dst_number == number and (not isinstance(dst_repo, str) or dst_repo.lower() == repo.lower()):
        return None
    if isinstance(dst_repo, str) and dst_repo and dst_repo.lower() != repo.lower():
        return f"{dst_repo}#{dst_number}"
    return str(dst_number)


def edges_from_links(repo: str, number: int, links: object) -> list[tuple]:
    """Edges for the link connections a payload CARRIED (issue #27): items
    point at their blockers/parents/duplicates/open linked PRs with
    source='link'. Whether a connection's edges REPLACE or UNION is the
    caller's decision (per-connection completeness, plan revision 2); this
    function is pure emission so the relation-registry census drives it.
    Absent connection keys emit nothing; explicit-null singular nodes emit
    nothing (a cleared value)."""
    edges: list[tuple] = []
    if not isinstance(links, dict):
        return edges
    nid = str(number)
    for connection, relation in _LINK_CONNECTIONS.items():
        if connection not in links:
            continue
        value = links[connection]
        nodes = value if isinstance(value, list) else [value]
        for node in nodes:
            dst = _link_dst(repo, node, number)
            if dst is not None:
                edges.append(("item", nid, relation, "item", dst, f"links.{connection}", "link"))
    return edges


def normalize_incomplete_connections(links_incomplete: object) -> set[str]:
    """Normalize an overflow flag into connection names: the production
    emitter produces a LIST of overflowed connection keys; the legacy BOOLEAN
    form (any truthy value) means all four connections overflowed (plan
    revision 2, critic round 2)."""
    if isinstance(links_incomplete, (list, tuple, set, frozenset)):
        return {str(name) for name in links_incomplete}
    if links_incomplete:
        return set(_LINK_CONNECTIONS)
    return set()


def edges_from_commit_messages(
    repo: str, number: int, commits: object
) -> list[tuple]:
    """closes_keyword edges from PR commit messages (issue #27 AC5): a
    commit saying `fixes #N` carries commit-sha provenance in its evidence
    and NEVER produces the GitHub-reported `closes` relation (AGENTS reserve
    it for timeline/closing-reference streams). Foreign references never
    localize (same masking as every keyword stream)."""
    edges: list[tuple] = []
    if not isinstance(commits, list):
        return edges
    nid = str(number)
    for rec in commits:
        if not isinstance(rec, dict):
            continue
        sha = rec.get("sha")
        message = rec.get("message") or ""
        if not isinstance(sha, str) or not sha.strip():
            continue
        for n, _location in iter_closing_locations(message, repo):
            edges.append(
                (
                    "item",
                    nid,
                    "closes_keyword",
                    "item",
                    str(n),
                    f"commit {sha.strip()} closing keyword",
                    "keyword",
                )
            )
    return edges

_TIMELINE_EVIDENCE = {
    "closed": "timeline closed event",
    "cross_referenced": "timeline cross-referenced event",
}


def edges_from_timeline(
    repo: str,
    number: int,
    events: Iterable[dict],
    merge_commit_lookup: Callable[[str], int | None],
) -> list[tuple]:
    """EXTRACTED timeline edges (issue #6): closed and cross-referenced
    events, source='timeline'.

    `merge_commit_lookup(sha)` resolves a commit sha to a stored PR number
    (or None) so a closed event committed by a known merge commit also yields
    `item:<pr> closes item:<issue>`. Cross-references are stored as carried:
    the edge runs from the event's source to the ingested item, same-repo
    sources as bare numbers, foreign sources as repo-qualified ids that can
    never attach to a same-numbered local item.
    """
    nid = str(number)
    edges: list[tuple] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        evidence = _TIMELINE_EVIDENCE.get(etype, "timeline event")
        created = event.get("created_at")
        if created:
            evidence = f"{evidence} {created}"
        if etype == "closed":
            sha = event.get("commit_id")
            if isinstance(sha, str) and sha.strip():
                sha = sha.strip()
                edges.append(("item", nid, "closed_by_commit", "commit", sha, evidence, "timeline"))
                closer = merge_commit_lookup(sha)
                if isinstance(closer, int) and closer != number:
                    edges.append(
                        ("item", str(closer), "closes", "item", nid, evidence, "timeline")
                    )
            closer_type = event.get("closer_type")
            closer_number = event.get("closer_number")
            if closer_type == "pull_request" and isinstance(closer_number, int):
                if closer_number != number:
                    edges.append(
                        ("item", str(closer_number), "closes", "item", nid, evidence, "timeline")
                    )
        elif etype == "cross_referenced":
            src_number = event.get("source_number")
            src_repo = event.get("source_repo")
            if not isinstance(src_number, int) or not isinstance(src_repo, str) or not src_repo:
                continue
            if src_repo.lower() == repo.lower():
                if src_number != number:
                    edges.append(
                        ("item", str(src_number), "cross_referenced", "item", nid, evidence, "timeline")
                    )
            else:
                edges.append(
                    (
                        "item",
                        f"{src_repo}#{src_number}",
                        "cross_referenced",
                        "item",
                        nid,
                        evidence,
                        "timeline",
                    )
                )
    return edges


_REVERT_SHA_RE = re.compile(r"(?i)\bThis reverts commit ([0-9a-f]{7,40})\b")
_REVERT_TITLE_PREFIX = 'Revert "'


def edges_from_pr_state(
    repo: str,
    raw: dict,
    title_lookup: Callable[[str], list[int]] | None = None,
    number_lookup: Callable[[list[int]], set[int]] | None = None,
) -> list[tuple]:
    """EXTRACTED PR-structural edges (issue #6): merged_by, merged_commit,
    closingIssueReferences closes edges, and reverts.

    `merged_by` accepts either a bare login string (the GraphQL mapping) or a
    `{"login": ...}` object (the raw REST pulls payload). Closing references
    for other repos are dropped, never localized. A title-only revert resolves
    its destination through `title_lookup` (stored item titles): exactly one
    match other than the PR itself becomes an item-targeted reverts edge;
    zero or multiple matches produce nothing, because a fabricated
    destination is worse than a missing one.

    branch_ref (issue #26 AC4) resolves through `number_lookup` (stored ISSUE
    numbers): a head_ref carrying EXACTLY ONE numeric token that names one
    stored issue other than the PR emits item->item branch_ref with
    source='keyword'. Multi-token branches never emit; a single token can
    never become ambiguous later, so no retraction path exists.
    """
    number = int(raw["number"])
    nid = str(number)
    edges: list[tuple] = []
    is_pr = raw.get("pull_request") is not None or raw.get("merged_at") is not None
    merged = bool(raw.get("merged_at") or raw.get("merged"))
    merged_by = raw.get("merged_by")
    login: str | None = None
    if isinstance(merged_by, str):
        login = merged_by or None
    elif isinstance(merged_by, dict):
        value = merged_by.get("login")
        login = str(value) if value else None
    if is_pr and login and merged:
        edges.append(("item", nid, "merged_by", "actor", login, "pull mergedBy", "payload"))
    merge_sha = raw.get("merge_commit_sha")
    # GitHub keeps a speculative test-merge oid in merge_commit_sha while a PR
    # is open (REST); a merged_commit edge to a commit that does not exist in
    # the repository would be a false fact, so gate on the merged state like
    # merged_by above.
    if is_pr and merged and isinstance(merge_sha, str) and merge_sha.strip():
        edges.append(
            ("item", nid, "merged_commit", "commit", merge_sha.strip(), "pull mergeCommit", "payload")
        )
    for ref in raw.get("closing_issues_references") or []:
        if not isinstance(ref, dict):
            continue
        ref_number = ref.get("number")
        ref_repo = ref.get("repo")
        if not isinstance(ref_number, int) or not isinstance(ref_repo, str) or not ref_repo:
            continue
        if ref_repo.lower() != repo.lower() or ref_number == number:
            continue
        edges.append(
            ("item", nid, "closes", "item", str(ref_number), "closingIssuesReferences", "closing_ref")
        )
    body = raw.get("body") or ""
    # Revert edges describe a PR reverting earlier work; an issue whose body
    # merely quotes "This reverts commit <sha>" (how-tos, templates) must not
    # fabricate one, so both revert branches gate on the item being a PR.
    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
    head_ref = head.get("ref")
    if is_pr and number_lookup is not None and isinstance(head_ref, str) and head_ref:
        tokens = branch_number_tokens(head_ref)
        if len(tokens) == 1:
            stored = number_lookup(tokens)
            if len(stored) == 1 and next(iter(stored)) != number:
                edges.append(
                    (
                        "item",
                        nid,
                        "branch_ref",
                        "item",
                        str(next(iter(stored))),
                        f"head_ref {head_ref}",
                        "keyword",
                    )
                )
    if is_pr:
        revert = _REVERT_SHA_RE.search(body)
        if revert:
            edges.append(
                ("item", nid, "reverts", "commit", revert.group(1), "body reverts commit sha", "keyword")
            )
        elif title_lookup is not None:
            title = raw.get("title") or ""
            if title.startswith(_REVERT_TITLE_PREFIX):
                span = title[len(_REVERT_TITLE_PREFIX) :]
                if span.endswith('"'):
                    span = span[:-1]
                span = span.strip()
                if span:
                    matches = {int(n) for n in title_lookup(span) if int(n) != number}
                    if len(matches) == 1:
                        edges.append(
                            (
                                "item",
                                nid,
                                "reverts",
                                "item",
                                str(matches.pop()),
                                "title reverts quoted item title",
                                "keyword",
                            )
                        )
    return edges


def revert_title_variants(target_title: str) -> tuple[str, str]:
    """The two revert-PR titles that the title-only revert rule maps to
    `target_title`: with and without the closing quote (a hand-edited title
    may drop it). Used by the store's reverse pass so a title-only revert
    edge is order-independent, mirroring the merge-close symmetry."""
    quoted = f'{_REVERT_TITLE_PREFIX}{target_title}"'
    return quoted, f"{_REVERT_TITLE_PREFIX}{target_title}"


# ==== issue #24 payload-signal extraction ===================================
#
# The v7 columns are source-owned: the listing-owned signals extract from the
# /issues listing payload only, the pull-owned ones from the /pulls payload
# only. Extraction distinguishes ABSENT KEY (the source does not carry the
# field -- a later re-ingest must preserve the stored value) from a key
# present with JSON null (the source says the value is cleared).


class _Absent:
    """Marker for 'this payload does not carry the key' (never a value)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT = _Absent()


def _object_field(value: object, field: str):
    return value.get(field) if isinstance(value, dict) else None


def _login_of(value: object):
    if isinstance(value, dict):
        return value.get("login")
    if isinstance(value, str):
        return value
    return None


def _merge_commit_of(value: object):
    if isinstance(value, str):
        return value.strip() or None
    return None


#: (column, payload key, optional value transform) for the listing-owned v7
#: columns. Keyed by the live /issues listing payload shapes (02 P2 probe).
LISTING_SIGNAL_EXTRACTIONS = (
    ("reactions_total", "reactions", lambda v: _object_field(v, "total_count")),
    ("milestone", "milestone", lambda v: _object_field(v, "title")),
    ("item_type", "type", lambda v: _object_field(v, "name")),
    ("sub_issues_total", "sub_issues_summary", lambda v: _object_field(v, "total")),
    (
        "sub_issues_completed",
        "sub_issues_summary",
        lambda v: _object_field(v, "completed"),
    ),
    (
        "dependencies_blocked_by",
        "issue_dependencies_summary",
        lambda v: _object_field(v, "blocked_by"),
    ),
    (
        "dependencies_blocking",
        "issue_dependencies_summary",
        lambda v: _object_field(v, "blocking"),
    ),
    (
        "dependencies_total_blocked_by",
        "issue_dependencies_summary",
        lambda v: _object_field(v, "total_blocked_by"),
    ),
    (
        "dependencies_total_blocking",
        "issue_dependencies_summary",
        lambda v: _object_field(v, "total_blocking"),
    ),
    ("author_association", "author_association", None),
)

#: Pull-owned v7 columns (issue #24 AC3: the reproduced defect was a listing
#: re-ingest nulling pull-only values; merge_commit/merged_at join merged_by).
PULL_OWNED_EXTRACTIONS = (
    ("merged_by", "merged_by", _login_of),
    ("merge_commit", "merge_commit_sha", _merge_commit_of),
    ("merged_at", "merged_at", None),
)


def _signal(raw: dict, key: str, transform):
    if key not in raw:
        return ABSENT
    value = raw[key]
    if value is None:
        return None
    if transform is not None:
        return transform(value)
    return value


def extract_listing_signals(raw: dict) -> dict:
    """Listing-owned v7 column values; ABSENT when the payload lacks the key."""
    return {
        column: _signal(raw, key, transform)
        for column, key, transform in LISTING_SIGNAL_EXTRACTIONS
    }


def extract_pull_owned(raw: dict) -> dict:
    """Pull-owned v7 column values; ABSENT when the payload lacks the key."""
    return {
        column: _signal(raw, key, transform)
        for column, key, transform in PULL_OWNED_EXTRACTIONS
    }


def merge_absent(extracted: dict, existing: dict) -> dict:
    """Resolve ABSENT entries against the stored row (first sight -> None).

    A key present in the payload wins even when its JSON value is null
    (clearing); a key the payload does not carry keeps the stored value so a
    narrower sibling source cannot null it (AC3 / claim 5)."""
    resolved = {}
    for column, value in extracted.items():
        if value is ABSENT:
            resolved[column] = existing.get(column) if existing else None
        else:
            resolved[column] = value
    return resolved
