"""Verified store snapshot transport (issue #34, Workstream E).

``snapshot publish`` moves a verified snapshot of one repo's corpus as a
single orphan commit (manifest + gzip parts) to a target repository;
``snapshot pull`` verifies and installs one. Everything is verified before
anything is replaced: part shas, the gzip stream, the DB sha, ``quick_check``,
the schema version, the local-watermark rule, and the post-install corpus
digest. The one-writer rule lives in the README; ``gh`` stays the only
credential.

Design notes:
- Every manifest field is derived from ONE staged backup (plan v2 finding 9):
  completeness, watermark, schema version, and the corpus digest are all read
  from the staged file, never from the live store, so a concurrent sync can
  never split them.
- Install happens under the sync lock (sync.acquire_sync_lock) with the
  watermark re-checked under the lock, one atomic os.replace, and stale
  WAL/SHM sidecar cleanup; a dest-open failure refuses cleanly.
- Provenance is written into the STAGED db before install, so provenance and
  corpus land together (finding 5).
- Consolidation (doctor consolidate) DROPS snapshot provenance with disclosure:
  the merged corpus is not the pulled snapshot (plan v2 finding 6).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .db import CURRENT_USER_VERSION, connect
from .export import iter_corpus_content_lines

#: snapshot manifest format stamp
MANIFEST_FORMAT = "zaxbygraph.snapshot.v1"

#: max gzip part size (bytes): 100 MB per the issue's splitting rule
PART_MAX_BYTES = 100 * 1024 * 1024

#: the published ref (issue #34 AC6 default location)
SNAPSHOT_REF = "refs/zaxbygraph/snapshot"

#: the fallback branch (AC6 fallback when the custom ref is refused)
SNAPSHOT_BRANCH = "zaxbygraph-snapshot"

#: deterministic commit identity: fixed timestamps keep the orphan commit
#: byte-deterministic for identical content (plan v2 finding 8)
_FIXED_DATE = "1970-01-01T00:00:00Z"


class SnapshotError(Exception):
    """Snapshot failure with an envelope-shaped payload."""

    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def _run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """One git invocation with the environment stripped down so the snapshot
    commit is deterministic and nothing from the ambient repo (hooks,
    autocrlf, signing config) can leak in."""
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_DATE": _FIXED_DATE,
        "GIT_COMMITTER_DATE": _FIXED_DATE,
    })
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, env=env, timeout=300,
    )
    if check and proc.returncode != 0:
        raise SnapshotError(
            "git_failed",
            f"git {args[0]} failed: {_tail(proc.stderr)}",
            hint="snapshot transport drives git as a subprocess; "
                 "git must be on PATH",
        )
    return proc


def _tail(b: bytes) -> str:
    return b.decode("utf-8", "replace").strip().splitlines()[-1] if b.strip() else "(no output)"


def _check_completeness(conn: sqlite3.Connection, repo: str) -> None:
    """Refuse incomplete stores (issue #34 AC2). Completeness is derived on
    the staged backup (finding 9): full_sync_pending must be 0 and last_error
    must be NULL, both read from the STAGED connection."""
    row = conn.execute(
        "SELECT full_sync_pending, last_error FROM sync_state WHERE repo = ?",
        (repo,),
    ).fetchone()
    if row is None:
        raise SnapshotError(
            "repo_not_synced",
            f"no sync_state row for {repo}; run sync first",
            hint="snapshot publish moves a synced corpus",
        )
    if int(row["full_sync_pending"] or 0) != 0 or row["last_error"] is not None:
        raise SnapshotError(
            "store_incomplete",
            f"store for {repo} is incomplete (full_sync_pending="
            f"{int(row['full_sync_pending'] or 0)}, last_error={row['last_error']!r})",
            hint="snapshot publish refuses incomplete stores (issue #34 AC2)",
        )


def _parts_from_backup(staged: Path, tmp: Path) -> tuple[list[dict], bytes, int, int]:
    """gzip the staged db (mtime 0 for determinism) and split it into
    <=100 MB parts. Returns (parts, gz_bytes, db_size, user_version)."""
    raw = staged.read_bytes()
    buf = __import__("io").BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(raw)
    gz_bytes = buf.getvalue()
    parts = []
    for i in range(0, len(gz_bytes), PART_MAX_BYTES):
        piece = gz_bytes[i:i + PART_MAX_BYTES]
        name = f"snapshot-{i // PART_MAX_BYTES:04d}.gz"
        parts.append({
            "name": name,
            "sha256": hashlib.sha256(piece).hexdigest(),
            "bytes": len(piece),
        })
    uv = int(sqlite3.connect(str(staged)).execute(
        "PRAGMA user_version").fetchone()[0])
    return parts, gz_bytes, len(raw), uv


def _corpus_digest(staged: Path, repo: str) -> str:
    """The #28 canonical export digest, computed over the staged db through
    the same content-line stream the export-corpus command writes."""
    conn = connect(staged)
    conn.row_factory = sqlite3.Row
    try:
        hasher = hashlib.sha256()
        for piece in iter_corpus_content_lines(conn, repo, set()):
            hasher.update(piece)
        return hasher.hexdigest()
    finally:
        conn.close()
def _staged_backup(conn: sqlite3.Connection, staged: Path) -> None:
    """Connection.backup() into staged, then vacuum (finding 9: every
    manifest field is read from this staged file, never the live store)."""
    with sqlite3.connect(str(staged)) as dst:
        conn.backup(dst)
    c2 = sqlite3.connect(str(staged))
    try:
        c2.execute("VACUUM")
    finally:
        c2.close()


def publish(db_path: Path, target: str, publisher: str) -> dict:
    """snapshot publish (issue #34 AC1-AC2): verified orphan-commit
    transport under the sync lock; every manifest field derives from one
    staged backup."""
    db_path = Path(db_path).resolve()
    publisher = (publisher or "").strip() or "unknown"
    from .sync import acquire_sync_lock

    lock = acquire_sync_lock(db_path, wait=True)
    try:
        conn = connect(db_path)
        try:
            states = conn.execute("SELECT repo FROM sync_state").fetchall()
            if not states:
                raise SnapshotError(
                    "repo_not_synced",
                    "no sync_state rows; run sync first",
                    hint="snapshot publish moves a synced corpus",
                )
            if len(states) != 1:
                raise SnapshotError(
                    "multi_repo_store",
                    f"snapshot publish needs a single-repo store (found {len(states)})",
                    hint="publish from a store holding exactly one repo",
                )
            repo = states[0][0]
            tmp = Path(tempfile.mkdtemp(prefix="zaxbygraph-publish-"))
            staged = tmp / "staged.db"
            _staged_backup(conn, staged)
        finally:
            conn.close()
        try:
            return _publish_staged(staged, tmp, target, publisher, repo, db_path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        lock.release_owned()
def _publish_staged(staged: Path, tmp: Path, target: str,
                    publisher: str, repo: str, db_path: Path) -> dict:
    """Manifest assembly + push; every field derives from the staged file."""
    sc = sqlite3.connect(str(staged))
    sc.row_factory = sqlite3.Row
    try:
        _check_completeness(sc, repo)
        uv = int(sc.execute("PRAGMA user_version").fetchone()[0])
        watermark = sc.execute(
            "SELECT issues_since FROM sync_state WHERE repo = ?", (repo,)
        ).fetchone()["issues_since"]
    finally:
        sc.close()
    corpus_sha256 = _corpus_digest(staged, repo)
    parts, gz_bytes, _db_size, _uv2 = _parts_from_backup(staged, tmp)
    manifest = {
        "format": MANIFEST_FORMAT,
        "user_version": uv,
        "repo": repo,
        "host": _host_of(db_path),
        "watermark": watermark,
        "corpus_sha256": corpus_sha256,
        "gz_sha256": hashlib.sha256(gz_bytes).hexdigest(),
        "db_sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
        "parts": parts,
        "publisher": publisher,
    }
    commit_sha, pushed_ref = _git_publish(tmp, target, publisher, parts, manifest, gz_bytes)
    return {"manifest": manifest, "commit": commit_sha, "pushed_ref": pushed_ref}
def _host_of(db_path: Path) -> str:
    return "github.com"
def _git_publish(tmp: Path, target: str, publisher: str,
                 parts: list[dict], manifest: dict, gz_bytes: bytes) -> tuple[str, str]:
    """One orphan commit in a temp git repo, pushed to the AC6 default ref
    with the branch fallback. Deterministic identity; refusal = the push
    command's refusal (never a network/auth reinterpretation)."""
    wd = tmp / "work"
    wd.mkdir()
    _run_git(["init", "-b", "main"], wd)
    (wd / "manifest.json").write_bytes(
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"))
    offset = 0
    for part in parts:
        piece = gz_bytes[offset:offset + part["bytes"]]
        (wd / part["name"]).write_bytes(piece)
        offset += part["bytes"]
    _run_git(["add", "-A"], wd)
    tree = _run_git(["write-tree"], wd).stdout.decode().strip()
    ident = ["-c", "user.name=" + publisher,
             "-c", "user.email=publisher@snapshot.invalid"]
    commit = _run_git([*ident, "commit-tree", tree, "-m", "zaxbygraph snapshot"], wd).stdout.decode().strip()
    push = None
    for ref in (SNAPSHOT_REF, "refs/heads/" + SNAPSHOT_BRANCH):
        push = _run_git(["push", target, commit + ":" + ref, "--force"],
                        wd, check=False)
        if push.returncode == 0:
            return commit, ref
    raise SnapshotError(
        "push_refused",
        "target refused the snapshot ref and the fallback branch",
        hint=_tail(push.stderr),
    )
def pull(db_path: Path, source: str, force: bool = False) -> dict:
    """snapshot pull (issue #34 AC3-AC5): verify-then-install. Staged
    verification happens first (part shas, gzip stream, db sha, quick_check,
    schema version); provenance lands in the staged db; the sync lock is
    held for the authoritative re-check and the install; one atomic replace
    replaces the store; post-install the corpus digest is re-verified."""
    db_path = Path(db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    from .sync import acquire_sync_lock

    tmp = Path(tempfile.mkdtemp(prefix="zaxbygraph-pull-"))
    try:
        manifest, gz_bytes = _fetch_and_verify_parts(tmp, source)
        staged = tmp / "staged.db"
        _verify_staged(staged, gz_bytes, manifest)

        _check_staged_digest(staged, manifest)

        lock = acquire_sync_lock(db_path, wait=True)
        try:
            local_newer = _local_watermark(db_path)
            if (local_newer is not None
                    and manifest.get("watermark")
                    and local_newer > manifest["watermark"] and not force):
                raise SnapshotError(
                    "local_newer",
                    f"local store is newer ({local_newer}) than the snapshot"
                    f" ({manifest['watermark']}); pass --force to overwrite",
                    hint="pull keeps a newer local store (issue #34 AC4)",
                )
            _write_provenance(staged, manifest)
            _install(staged, db_path)
            result = _post_install_check(db_path, manifest)
            result["manifest"] = manifest
            return result
        finally:
            lock.release_owned()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
def _fetch_and_verify_parts(tmp: Path, source: str) -> tuple[dict, bytes]:
    """Fetch the snapshot ref (branch fallback), read the manifest, verify
    every part sha, return (manifest, gz_bytes). Refusal = the specific
    verification step's refusal."""
    wd = tmp / "work"
    wd.mkdir()
    _run_git(["init", "-b", "main"], wd)
    fetched = None
    for ref in (SNAPSHOT_REF, "refs/heads/" + SNAPSHOT_BRANCH):
        f = _run_git(["fetch", source, ref], wd, check=False)
        if f.returncode == 0:
            fetched = ref
            break
    if fetched is None:
        raise SnapshotError(
            "snapshot_absent",
            f"source has neither {SNAPSHOT_REF} nor the fallback branch",
            hint="publish first, or point pull at the right repo",
        )
    commit = _run_git(["rev-parse", "FETCH_HEAD"], wd).stdout.decode().strip()
    mbytes = _run_git(["cat-file", "blob", commit + ":manifest.json"], wd).stdout
    try:
        manifest = json.loads(mbytes.decode("utf-8"))
    except ValueError as exc:
        raise SnapshotError(
            "bad_manifest",
            f"snapshot manifest is not valid JSON: {exc}",
            hint="re-publish and re-pull",
        ) from exc
    missing = [k for k in ("format", "repo", "user_version", "watermark",
                           "db_sha256", "gz_sha256", "corpus_sha256",
                           "parts") if k not in manifest]
    if missing:
        raise SnapshotError(
            "bad_manifest",
            f"snapshot manifest is missing required keys: {missing}",
            hint="re-publish and re-pull",
        )
    parts = manifest.get("parts")
    shape_bad = (
        not isinstance(parts, list)
        or any(not isinstance(p, dict)
               or not all(k in p for k in ("name", "sha256", "bytes"))
               for p in parts)
    )
    if shape_bad:
        raise SnapshotError(
            "bad_manifest",
            "snapshot manifest parts must be a list of dicts with "
            "name/sha256/bytes",
            hint="re-publish and re-pull",
        )
    if manifest.get("format") != MANIFEST_FORMAT:
        raise SnapshotError(
            "bad_manifest",
            f"manifest format {manifest.get('format')!r} != {MANIFEST_FORMAT!r}",
            hint="not a zaxbygraph snapshot",
        )
    gz_parts = []
    for part in manifest.get("parts", []):
        blob = _run_git(["cat-file", "blob", commit + ":" + part["name"]], wd).stdout
        got = hashlib.sha256(blob).hexdigest()
        if got != part["sha256"]:
            raise SnapshotError(
                "part_sha_mismatch",
                f"part {part['name']} sha256 mismatch",
                hint="the snapshot content changed or was tampered with",
            )
        gz_parts.append(blob)
    return manifest, b"".join(gz_parts)
def _verify_staged(staged: Path, gz_bytes: bytes, manifest: dict) -> None:
    """gunzip -> db sha -> quick_check -> schema version. The staged file is
    the ONLY thing verified here; the local store is untouched on failure."""
    gz_sha = hashlib.sha256(gz_bytes).hexdigest()
    if manifest.get("gz_sha256") and gz_sha != manifest["gz_sha256"]:
        raise SnapshotError("gz_sha_mismatch", "gzip stream sha mismatch",
                            hint="re-publish or re-pull")
    try:
        raw = gzip.decompress(gz_bytes)
    except (OSError, EOFError) as exc:
        raise SnapshotError(
            "gz_bad",
            f"snapshot payload is not a readable gzip stream: {exc}",
            hint="the snapshot content is corrupt or tampered with",
        ) from exc
    if hashlib.sha256(raw).hexdigest() != manifest["db_sha256"]:
        raise SnapshotError("db_sha_mismatch", "db sha mismatch",
                            hint="re-publish or re-pull")
    staged.write_bytes(raw)
    sc = sqlite3.connect(str(staged))
    try:
        try:
            (check,) = sc.execute("PRAGMA quick_check").fetchone()
            uv = int(sc.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            raise SnapshotError(
                "quick_check_failed",
                f"staged db is not a readable sqlite database: {exc}",
                hint="the snapshot content is corrupt or tampered with",
            ) from exc
        if check != "ok":
            raise SnapshotError("quick_check_failed", f"quick_check: {check}",
                                hint="the staged db is corrupt")
    finally:
        sc.close()
    if uv > CURRENT_USER_VERSION:
        raise SnapshotError(
            "store_too_new",
            f"snapshot schema v{uv} newer than this build (v{CURRENT_USER_VERSION})",
            hint="upgrade zaxbygraph and re-pull",
        )


def _check_staged_digest(staged: Path, manifest: dict) -> None:
    """Staged-first corpus digest check (plan v4 round-2 finding 5): a
    manifest/bytes disagreement refuses BEFORE anything is replaced."""
    staged_digest = _corpus_digest(staged, manifest["repo"])
    if staged_digest != manifest["corpus_sha256"]:
        raise SnapshotError(
            "corpus_mismatch",
            "staged corpus digest differs from the manifest",
            hint="re-publish and re-pull; local store untouched",
        )


def _local_watermark(db_path: Path) -> str | None:
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT issues_since FROM sync_state LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None
def _write_provenance(staged: Path, manifest: dict) -> None:
    """Provenance lands in the STAGED db before install (critic round-1
    finding 5): provenance and corpus arrive together."""
    conn = sqlite3.connect(str(staged))
    try:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('snapshot.publisher', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (manifest.get("publisher", "unknown"),))
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('snapshot.manifest_sha256', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (hashlib.sha256(json.dumps(manifest, indent=2, sort_keys=True)
                            .encode("utf-8")).hexdigest(),))
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('snapshot.pulled_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_now_iso(),))
        conn.commit()
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _install(staged: Path, db_path: Path) -> None:
    """Quiesce dest (checkpoint its WAL), delete old sidecars BEFORE the
    replace, then ONE os.replace. The staged file is closed and
    self-contained; any quiescence/replace/open-handle failure refuses
    cleanly (coded SnapshotError) with the local store untouched."""
    try:
        dest = sqlite3.connect(str(db_path))
        try:
            busy = int(dest.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0])
        finally:
            dest.close()
        if busy:
            raise SnapshotError(
                "dest_open",
                "destination store is busy (a reader holds it); "
                f"checkpoint reported busy={busy}",
                hint="close readers holding the store open and re-pull",
            )
        for sidecar in (Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
            if sidecar.exists():
                sidecar.unlink()
        os.replace(staged, db_path)
    except OSError as exc:
        raise SnapshotError(
            "dest_open",
            f"destination store could not be quiesced and replaced: {exc}",
            hint="close readers holding the store open and re-pull",
        ) from exc


def _post_install_check(db_path: Path, manifest: dict) -> dict:
    """Reopen dest, recompute the corpus digest, assert the manifest value
    (AC5). Returns the result dict."""
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        digest = _corpus_digest_conn(conn, manifest["repo"])
    finally:
        conn.close()
    if digest != manifest["corpus_sha256"]:
        raise SnapshotError(
            "corpus_mismatch",
            "post-install corpus digest differs from the manifest",
            hint="the install did not land; re-pull",
        )
    return {"corpus_sha256": digest, "installed": True}


def _corpus_digest_conn(conn: sqlite3.Connection, repo: str) -> str:
    """Corpus digest from an open connection (the post-install check)."""
    hasher = hashlib.sha256()
    for piece in iter_corpus_content_lines(conn, repo, set()):
        hasher.update(piece)
    return hasher.hexdigest()
