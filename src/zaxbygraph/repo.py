from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class RepoError(ValueError):
    pass


def validate_slug(slug: str) -> str:
    slug = slug.strip().strip("/")
    if not REPO_SLUG_RE.match(slug):
        raise RepoError(f"invalid repo slug: {slug!r}")
    owner, name = slug.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."}:
        raise RepoError(f"invalid repo slug: {slug!r}")
    return f"{owner}/{name}"


def _parse_remote_url(url: str) -> str | None:
    url = url.strip()
    if not url:
        return None
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@"):
        # git@github.com:owner/repo
        _, _, rest = url.partition(":")
        rest = rest.strip("/")
        if REPO_SLUG_RE.match(rest):
            return rest
        return None
    # https://github.com/owner/repo
    for prefix in ("https://github.com/", "http://github.com/", "ssh://git@github.com/"):
        if url.startswith(prefix):
            rest = url[len(prefix) :].strip("/")
            if REPO_SLUG_RE.match(rest):
                return rest
    return None


def slug_from_git(cwd: Path | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RepoError("git not found; pass --repo OWNER/REPO") from exc
    if proc.returncode != 0:
        raise RepoError("no git origin remote; pass --repo OWNER/REPO")
    parsed = _parse_remote_url(proc.stdout)
    if not parsed:
        raise RepoError(f"could not parse origin remote: {proc.stdout.strip()!r}")
    return validate_slug(parsed)


def resolve_repo(explicit: str | None, cwd: Path | None = None) -> str:
    if explicit:
        return validate_slug(explicit)
    return slug_from_git(cwd)
