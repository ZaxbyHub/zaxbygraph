from __future__ import annotations

import os
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

    A loopback git-proxy origin (`http://user@127.0.0.1:<port>/git/<o>/<r>`
    and its localhost/::1/[::1] spellings) resolves to the forge host the
    proxy fronts instead of the ephemeral loopback key (issue #31 AC3);
    ZAXBYGRAPH_PROXY_FORGE_HOST has zmem's four states (see
    _loopback_proxy_forge).
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
            authority = host_raw.rsplit("@", 1)[-1]
            forge = _loopback_proxy_forge(authority)
            if forge is not None and path[:4].lower() == "git/":
                slug = path[4:].strip("/")
                if REPO_SLUG_RE.match(slug):
                    host = _clean_host(forge)
                    if host is not None:
                        return (host, slug)
            host = _clean_host(host_raw)
            if host is None or not REPO_SLUG_RE.match(path):
                return None
            return (host, path)
    return None


#: The loopback spellings a git proxy uses as its host. An unbracketed
#: `::1:port` is recognized too: a trailing 1-5-digit port is stripped
#: before the match, matching zmem's documented rule (issue #31 AC3).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: ZAXBYGRAPH_PROXY_FORGE_HOST grammar (mirrors zmem's documented rule):
#: host[:port] — the optional port is 1-5 digits, and the host is one or
#: more dot-separated labels, each matching this pattern (hyphens only
#: inside labels, no empty labels; `a..b`, `-a.com`, `a-.com` are invalid).
_FORGE_LABEL_RE = re.compile(r"[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?", re.ASCII)

_PROXY_ENV_VAR = "ZAXBYGRAPH_PROXY_FORGE_HOST"


def _loopback_authority(authority: str) -> bool:
    """True when a URL authority (userinfo stripped) is a loopback proxy host.

    Recognizes 127.0.0.1[:port], localhost[:port], ::1, and [::1][:port];
    a trailing 1-5-digit port is stripped first, so the unbracketed
    `::1:41233` spelling resolves as well."""
    host = authority.strip().lower()
    if not host:
        return False
    if host in _LOOPBACK_HOSTS:
        return True
    if host.startswith("["):
        inner, sep, after = host[1:].partition("]")
        if not sep:
            return False
        if after and not re.fullmatch(r":\d{1,5}", after):
            return False
        return f"[{inner}]" in _LOOPBACK_HOSTS
    left, sep, right = host.rpartition(":")
    if sep and right.isdigit() and len(right) <= 5:
        return left in _LOOPBACK_HOSTS
    return False


def _valid_forge_host(value: str) -> bool:
    """True when `value` parses as host[:port] per the documented grammar."""
    host = value.strip().lower()
    if ":" in host:
        host, _, port = host.rpartition(":")
        if not port.isdigit() or len(port) > 5:
            return False
    if not host:
        return False
    return all(
        _FORGE_LABEL_RE.fullmatch(label) is not None for label in host.split(".")
    )


def _loopback_proxy_forge(authority: str) -> str | None:
    """Forge host for a loopback proxy authority, or None (no rewrite).

    ZAXBYGRAPH_PROXY_FORGE_HOST states (zmem's documented rule): unset ->
    DEFAULT_HOST (github.com; the CCR proxy fronts GitHub); set to a valid
    `host[:port]` -> that value verbatim (lowercased); set non-empty but
    unparseable, or empty/whitespace -> None, silently disabling the
    rewrite so the remote keeps its literal loopback key."""
    if not _loopback_authority(authority):
        return None
    override = os.environ.get(_PROXY_ENV_VAR)
    if override is None:
        return DEFAULT_HOST
    if not override.strip():
        return None
    if not _valid_forge_host(override):
        return None
    return override.strip().lower()


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
