from __future__ import annotations

import os
import subprocess
from pathlib import Path

from zaxbygraph.repo import DEFAULT_HOST, RepoError, validate_slug


def git_common_root(cwd: Path | None = None) -> Path | None:
    """The MAIN worktree's checkout root (parent of the common .git dir).

    `--path-format=absolute` needs git >= 2.31; older git falls back to the
    possibly-relative form, absolutized against cwd. None outside a repo.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            return None
        gitdir = Path(proc.stdout.strip())
        if not gitdir.is_absolute():
            base = (cwd or Path.cwd()).resolve()
            gitdir = (base / gitdir).resolve()
    except (OSError, subprocess.SubprocessError):
        return None
    if gitdir.name != ".git":
        # Bare or unusual layouts have no checkout root to offer.
        return None
    return gitdir.parent


def _legacy_candidates(root: Path) -> list[Path]:
    """The two in-repo layouts this tool has ever used (issue #2 repro)."""
    return [root / ".zaxbygraph" / "history.db", root / ".swarm" / "zaxbygraph" / "history.db"]


def legacy_db_paths(cwd: Path | None = None) -> list[Path]:
    """Existing per-checkout DBs under the MAIN worktree (and cwd when it is
    a different checkout of the same repo). Resolution reads them for
    migration continuity; `doctor` reports and consolidates them."""
    found: list[Path] = []
    roots: list[Path] = []
    common = git_common_root(cwd)
    if common is not None:
        roots.append(common)
    here = (cwd or Path.cwd()).resolve()
    if common is None or here != common:
        roots.append(here)
    for root in roots:
        for candidate in _legacy_candidates(root):
            if candidate.exists() and candidate not in found:
                found.append(candidate)
    return found


def store_root() -> Path:
    """User-level store root (issue #2 AC8).

    `ZAXBYGRAPH_HOME` override, else %LOCALAPPDATA%\\zaxbygraph on Windows,
    else ${XDG_DATA_HOME:-~/.local/share}/zaxbygraph.
    """
    home = os.environ.get("ZAXBYGRAPH_HOME")
    if home:
        return Path(home)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "zaxbygraph"
        return Path.home() / "AppData" / "Local" / "zaxbygraph"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "zaxbygraph"
    return Path.home() / ".local" / "share" / "zaxbygraph"


def store_db_path(host: str, repo: str) -> Path:
    """`<store-root>/<host>/<owner>/<repo>/history.db` (slug case-folded)."""
    if not repo:
        raise RepoError("cannot resolve a store without a repo")
    repo = validate_slug(repo)
    owner, name = repo.split("/", 1)
    return store_root() / (host or DEFAULT_HOST).lower() / owner / name / "history.db"


def resolve_db(
    repo: str | None,
    cwd: Path | None = None,
    explicit: str | os.PathLike[str] | None = None,
    host: str | None = None,
) -> tuple[Path, list[str]]:
    """Where this repo's graph lives, with the reason trail.

    Order: `--db` (or the caller's explicit path) > `ZAXBYGRAPH_DB` > the
    user-level store for the slug IF it exists > a legacy in-repo DB under
    the main worktree (migration continuity) > the would-be store path
    (which `sync` creates and the no-corpus message names). The store wins
    over legacy once it exists, so `doctor --consolidate` adoption sticks.
    `host`: pass the origin host when the slug came from the origin; a
    `--repo`-supplied slug carries no host and defaults to github.com.
    """
    chain: list[str] = []
    if explicit:
        return Path(explicit), ["--db"]
    env = os.environ.get("ZAXBYGRAPH_DB")
    if env:
        return Path(env), ["ZAXBYGRAPH_DB"]
    if repo is None:
        # Explicit no-filter without a db: nothing to resolve.
        raise RepoError("cannot resolve a database without --db or a repo")
    if host is None:
        host = _cwd_origin_host(cwd) or DEFAULT_HOST
    store = store_db_path(host, repo)
    if store.exists():
        chain.append(f"store exists: {store}")
        return store, chain
    chain.append(f"store missing: {store}")
    for legacy in legacy_db_paths(cwd):
        chain.append(f"legacy: {legacy}")
        return legacy, chain
    chain.append("nothing exists; the store path is what sync would create")
    return store, chain


def default_jsonl_dir(db_path: Path) -> Path:
    return db_path.parent / "jsonl"


def _cwd_origin_host(cwd: Path | None) -> str | None:
    from zaxbygraph.repo import remote_info

    try:
        host, _slug = remote_info(cwd)
        return host
    except RepoError:
        return None
