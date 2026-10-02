from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

#: Store-safe host charset (lowercased before this check): hostname
#: characters — letters, digits, hyphens, dots, underscore. `_clean_host`
#: strips userinfo (`user:pass@`), folds a trailing `:port` into a
#: path-safe `host_port` suffix (distinct ports keep distinct stores), and
#: rejects path separators including Windows backslashes and any `..`
#: sequence (issue #10 review PRR-001: the host becomes ONE path segment
#: under the store root, so `..` or `\` would escape it and a raw `:`
#: would create an unusable directory).
HOST_RE = re.compile(r"^[a-z0-9_](?:[a-z0-9._-]*[a-z0-9_])?$")

DEFAULT_HOST = "github.com"


class RepoError(ValueError):
    pass


def validate_slug(slug: str) -> str:
    slug = slug.strip().strip("/")
    if not REPO_SLUG_RE.match(slug):
        raise RepoError(f"invalid repo slug: {slug!r}")
    owner, name = slug.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."}:
        raise RepoError(f"invalid repo slug: {slug!r}")
    # Canonical storage is lowercase: GitHub slugs are case-insensitive, and
    # every table keys on `repo`, so preserving caller casing would store one
    # repo twice. Display case survives in the raw payloads, never in keys.
    return f"{owner.lower()}/{name.lower()}"


def _clean_host(raw_host: str) -> str | None:
    """Canonicalize a URL authority to a store-safe host, or None.

    Strips credentials (`user:pass@`), a trailing `:port`, and surrounding
    dots; rejects anything left that is not a plain hostname (including
    empty, `..`, and Windows separators) — PRR-001.
    """
    host = raw_host.strip().strip("/").strip(".")
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if ":" in host:  # port: keep it as a DISTINCT, path-safe suffix
        # (PRR-001 round 2: two GHE instances on one hostname at different
        # ports are different stores; merging them would silently collide.
        # ':' itself cannot be a path segment on Windows, hence _port.)
        host, _, port = host.rpartition(":")
        if not port.isdigit():
            return None
        host = f"{host}_{port}"
    host = host.strip(".").lower()
    if not host or ".." in host or "\\" in host or not HOST_RE.match(host):
        return None
    return host


def _parse_remote_info(url: str) -> tuple[str, str] | None:
    """Parse an origin URL into (host, slug) for any git host.

    A slug-only flag (`--repo OWNER/REPO`) carries no host; callers default
    it to `github.com` (README documents the collision boundary for other
    hosts). Hosts are lowercased and charset-validated by _clean_host; the
    slug keeps REPO_SLUG_RE's charset and is case-folded later by
    validate_slug.
    """
    url = url.strip()
    if not url:
        return None
    if url.endswith(".git"):
        url = url[:-4]
    # scp-like form: git@host:owner/repo (the host MUST survive — issue #2
    # keys the store by <host>/<owner>/<repo>).
    if url.startswith("git@"):
        _, _, rest = url.partition(":")
        rest = rest.strip("/")
        if REPO_SLUG_RE.match(rest):
            host = _clean_host(url[4:].split(":", 1)[0])
            if host is None:
                return None
            return (host, rest)
        return None
    for scheme in ("https://", "http://", "ssh://"):
        if url.startswith(scheme):
            rest = url[len(scheme) :]
            if rest.startswith("git@"):
                rest = rest[4:]
            host_raw, _, path = rest.partition("/")
            path = path.strip("/")
            host = _clean_host(host_raw)
            if host is None or not REPO_SLUG_RE.match(path):
                return None
            return (host, path)
    return None


def _parse_remote_url(url: str) -> str | None:
    """Slug-only view of _parse_remote_info (legacy helper)."""
    info = _parse_remote_info(url)
    return None if info is None else info[1]


def _redact_url(url: str) -> str:
    """Mask userinfo credentials before echoing an origin URL anywhere.

    Greedy through the last `@` before the first `/`: `user:p@ss@host`
    would otherwise leak the credential tail past a single-@ mask
    (final-critic round on PR #10)."""
    return re.sub(r"//[^/]*@", "//***@", url)


def slug_from_git(cwd: Path | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RepoError("git not found; pass --repo OWNER/REPO") from exc
    if proc.returncode != 0:
        raise RepoError("no git origin remote; pass --repo OWNER/REPO")
    parsed = _parse_remote_url(proc.stdout)
    if not parsed:
        raise RepoError(f"could not parse origin remote: {_redact_url(proc.stdout.strip())!r}")
    return validate_slug(parsed)


def remote_info(cwd: Path | None = None) -> tuple[str, str]:
    """(host, slug) for the origin of cwd; host lowercased, slug case-folded."""
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RepoError("git not found; pass --repo OWNER/REPO") from exc
    if proc.returncode != 0:
        raise RepoError("no git origin remote; pass --repo OWNER/REPO")
    info = _parse_remote_info(proc.stdout)
    if info is None:
        raise RepoError(f"could not parse origin remote: {_redact_url(proc.stdout.strip())!r}")
    host, slug = info
    return host, validate_slug(slug)


def resolve_repo(explicit: str | None, cwd: Path | None = None) -> str:
    if explicit:
        return validate_slug(explicit)
    return slug_from_git(cwd)
