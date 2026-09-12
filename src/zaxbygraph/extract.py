from __future__ import annotations

import re
from collections.abc import Iterable

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
    """EXTRACTED edge tuples: (src_type, src_id, rel, dst_type, dst_id, evidence)."""
    number = int(raw["number"])
    item_id = str(number)
    edges: list[tuple] = []
    author = _login(raw)
    if author:
        edges.append(("actor", author, "authored", "item", item_id, "user.login"))
    for label in raw.get("labels") or []:
        name = _label_name(label)
        if name:
            edges.append(("item", item_id, "has_label", "label", name, "labels[]"))
    body = raw.get("body") or ""
    closed = parse_closing_numbers(body, repo)
    for n in closed:
        if n != number:
            edges.append(("item", item_id, "closes", "item", str(n), "body closing keyword"))
    for n in parse_mentioned_numbers(body, repo):
        if n != number and n not in closed:
            edges.append(("item", item_id, "mentions", "item", str(n), "body #N"))
    return edges


def edges_from_comment(repo: str, number: int, raw: dict) -> list[tuple]:
    edges: list[tuple] = []
    cid = str(raw.get("id") or raw.get("github_id") or "")
    author = _login(raw)
    if author:
        edges.append(("actor", author, "commented", "item", str(number), f"comment:{cid}"))
    body = raw.get("body") or ""
    closed = parse_closing_numbers(body, repo)
    for n in closed:
        if n != number:
            edges.append(
                ("item", str(number), "closes", "item", str(n), f"comment:{cid} closing keyword")
            )
    for n in parse_mentioned_numbers(body, repo):
        if n != number and n not in closed:
            edges.append(("item", str(number), "mentions", "item", str(n), f"comment:{cid} #N"))
    return edges


def edges_from_review(repo: str, number: int, raw: dict) -> list[tuple]:
    edges: list[tuple] = []
    rid = str(raw.get("id") or "")
    author = _login(raw)
    state = raw.get("state") or ""
    if author:
        edges.append(("actor", author, "reviewed", "item", str(number), f"review:{rid}:{state}"))
    body = raw.get("body") or ""
    closed = parse_closing_numbers(body, repo)
    for n in closed:
        if n != number:
            edges.append(
                ("item", str(number), "closes", "item", str(n), f"review:{rid} closing keyword")
            )
    for n in parse_mentioned_numbers(body, repo):
        if n != number and n not in closed:
            edges.append(("item", str(number), "mentions", "item", str(n), f"review:{rid} #N"))
    return edges


def edges_from_files(number: int, files: Iterable[dict]) -> list[tuple]:
    edges: list[tuple] = []
    for rec in files:
        path = rec.get("filename") or rec.get("path")
        if path:
            edges.append(("item", str(number), "touches", "file", str(path), "pulls.files"))
    return edges


def collapse_edges(edges: Iterable[tuple]) -> list[tuple]:
    """Last-write-wins on the unique key (repo-less). Actor rels collapse."""
    by_key: dict[tuple, tuple] = {}
    for edge in edges:
        src_type, src_id, rel, dst_type, dst_id, evidence = edge
        key = (src_type, src_id, rel, dst_type, dst_id)
        by_key[key] = edge
    return list(by_key.values())
