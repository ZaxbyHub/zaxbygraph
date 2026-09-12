from __future__ import annotations

from pathlib import Path


def find_git_root(start: Path | None = None) -> Path | None:
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _gitignore_mentions_swarm(root: Path) -> bool:
    gi = root / ".gitignore"
    if not gi.is_file():
        return False
    try:
        text = gi.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.rstrip("/") == ".swarm" or line.startswith(".swarm/") or line == "**/.swarm/":
            return True
    return False


def default_db_path(cwd: Path | None = None) -> Path:
    """Resolve default history.db.

    git root of cwd (else cwd):
      .swarm/ exists as a dir OR .gitignore mentions .swarm
        → <root>/.swarm/zaxbygraph/history.db
      else → <root>/.zaxbygraph/history.db
    """
    start = (cwd or Path.cwd()).resolve()
    root = find_git_root(start) or start
    swarm_dir = root / ".swarm"
    if swarm_dir.is_dir() or _gitignore_mentions_swarm(root):
        return swarm_dir / "zaxbygraph" / "history.db"
    return root / ".zaxbygraph" / "history.db"


def default_jsonl_dir(db_path: Path) -> Path:
    return db_path.parent / "jsonl"
