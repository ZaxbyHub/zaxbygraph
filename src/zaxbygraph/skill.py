"""The packaged zaxbygraph skill: print, install, and staleness scan
(issue #73).

The skill ships INSIDE the package (``zaxbygraph/skills/zaxbygraph/
SKILL.md`` package data, byte-identical to the repository's
``skills/zaxbygraph/SKILL.md`` — pinned by test), so an installed copy can
always print, refresh, and audit the skill copies scattered through the
user-level skill homes.

ONE scanner (``scan_skill_copies``) serves both ``zaxbygraph skill
--check`` and ``doctor``'s skill-copies section. It only ever READS: the
write path is ``skill --install`` alone.

Classification is by BODY hash — the skill text minus every stamp line —
so line-ending differences and stamp-only edits never report a copy stale,
and any content drift always does.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from zaxbygraph import __version__

#: The stamp every distributed copy carries. The hash member is the sha256
#: of the copy's BODY (the file minus its stamp line); the version-only
#: form is what pre-0.5.0 copies carry and still parses.
STAMP_RE = re.compile(
    r"<!--\s*zaxbygraph skill-version:\s*(?P<version>[^\s]+)"
    r"(?:\s+hash=(?P<hash>[0-9a-fA-F]{64}))?\s*-->"
)

#: The four documented default skill homes, as ``.<tool>`` directory names
#: under the user's home (issue #73 AC7): ``~/.claude/skills``,
#: ``~/.agents/skills``, ``~/.zcode/skills``, ``~/.opencode/skills``.
#: ``Path.home()`` is read at SCAN time so a patched home (tests, or a
#: profile redirect) is honored.
DEFAULT_SKILL_HOME_TOOLS: tuple[str, ...] = ("claude", "agents", "zcode", "opencode")


def packaged_skill_path() -> Path:
    """The skill file shipped inside this package."""
    return Path(__file__).resolve().parent / "skills" / "zaxbygraph" / "SKILL.md"


def packaged_skill_text() -> str:
    """The packaged skill's text (universal-newline read)."""
    return _read_text(packaged_skill_path())


def _read_text(path: Path) -> str:
    # Universal-newline text read: a copy installed with CRLF endings (a
    # plain write_text on Windows) must hash to the same BODY as the LF
    # packaged file, or every Windows-installed copy would read stale.
    return path.read_text(encoding="utf-8", errors="replace")


def skill_body(text: str) -> str:
    """The skill text minus every zaxbygraph skill-version stamp line —
    the bytes the content hash covers (the stamp travels WITH a copy, it
    is never part of its identity)."""
    return "\n".join(
        line for line in text.splitlines() if not STAMP_RE.search(line)
    )


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def stamp_line(version: str, digest: str) -> str:
    return f"<!-- zaxbygraph skill-version: {version} hash={digest} -->"


def stamped_skill_text(packaged_text: str) -> str:
    """The installable form: the packaged text with today's version+hash
    stamp. An existing stamp line is replaced IN PLACE (a leading comment
    ahead of the YAML frontmatter can defeat skill loaders that require
    ``---`` as the first line); a stamp-less text gains one directly after
    the frontmatter. The BODY is untouched, so the stamp's hash is exactly
    the sha256 of what is written around it."""
    body = skill_body(packaged_text)
    stamp = stamp_line(__version__, body_sha256(body))
    lines = packaged_text.splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if STAMP_RE.search(line):
            out.append(stamp)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if lines and lines[0].strip() == "---":
            end = next(
                (i for i in range(1, len(lines)) if lines[i].strip() == "---"),
                0,
            )
            out = lines[: end + 1] + [stamp] + lines[end + 1 :]
        else:
            out = [stamp, *lines]
    return "\n".join(out) + "\n"


def default_skill_homes() -> list[Path]:
    """The four documented default skill-home directories, in scan order."""
    home = Path.home()
    return [home / f".{tool}" / "skills" for tool in DEFAULT_SKILL_HOME_TOOLS]


def _classify(copy: Path, packaged_digest: str) -> dict:
    """One scanned copy's record: freshness by BODY hash against the
    packaged skill, the copy's own stamped version for context, and — for
    a stale copy — the one-line refresh command naming --install."""
    text = _read_text(copy)
    digest = body_sha256(skill_body(text))
    stamped = None
    for match in STAMP_RE.finditer(text):
        stamped = match.group("version")
    stale = digest != packaged_digest
    record: dict = {
        "path": str(copy),
        "status": "stale" if stale else "current",
        "stamped_version": stamped,
        "body_sha256": digest,
    }
    if stale:
        # --install takes the skills DIRECTORY (it writes
        # DIR/zaxbygraph/SKILL.md), which is the copy's grandparent.
        record["refresh"] = f"zaxbygraph skill --install {copy.parent.parent}"
    return record


def scan_skill_copies(
    extra_dirs: list[Path] | None = None,
    *,
    include_defaults: bool = True,
) -> dict:
    """Classify installed skill copies by body hash against the packaged
    skill — the ONE scanner ``skill --check`` and ``doctor`` share.
    READ-ONLY: nothing is written; ``skill --install`` is the only writer.

    ``include_defaults`` adds the four documented default homes; the
    ``extra_dirs`` are scanned beside (or instead of) them, so ``skill
    --check --dir DIR`` can narrow the scan to exactly the named homes
    while ``doctor`` scans the defaults plus its ``--scan`` dirs. A
    missing packaged skill is reported, never raised — doctor must keep
    answering when the installation is broken."""
    packaged = packaged_skill_path()
    present = packaged.is_file()
    packaged_text = _read_text(packaged) if present else ""
    packaged_digest = body_sha256(skill_body(packaged_text)) if present else None
    scanned_dirs: list[Path] = [*default_skill_homes()] if include_defaults else []
    for extra in extra_dirs or []:
        if extra not in scanned_dirs:
            scanned_dirs.append(Path(extra))
    copies: list[dict] = []
    for home in scanned_dirs:
        copy = home / "zaxbygraph" / "SKILL.md"
        if not copy.is_file():
            continue
        if present:
            copies.append(_classify(copy, packaged_digest))
        else:
            copies.append(
                {
                    "path": str(copy),
                    "status": "unknown",
                    "stamped_version": None,
                    "body_sha256": None,
                    "note": "the packaged skill is missing from this"
                    " installation; re-install the package to classify"
                    " copies",
                }
            )
    return {
        "skill": "zaxbygraph",
        "packaged_version": __version__ if present else None,
        "packaged_present": present,
        "packaged_body_sha256": packaged_digest,
        "scanned_dirs": [str(d) for d in scanned_dirs],
        "stale_copies": sum(1 for c in copies if c.get("status") == "stale"),
        "copies": sorted(copies, key=lambda c: c["path"]),
    }


def install_skill(target_root: Path) -> dict:
    """Write the stamped skill to ``target_root/zaxbygraph/SKILL.md``
    (parent directories created). Returns the record for the envelope."""
    packaged = packaged_skill_path()
    if not packaged.is_file():
        raise FileNotFoundError(
            f"the packaged skill is missing from this installation: "
            f"{packaged}"
        )
    text = stamped_skill_text(_read_text(packaged))
    target = Path(target_root) / "zaxbygraph" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="" pins LF endings regardless of platform so the written body
    # hashes to exactly the packaged body the stamp names.
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    digest = body_sha256(skill_body(text))
    return {
        "installed": str(target),
        "version": __version__,
        "body_sha256": digest,
    }
