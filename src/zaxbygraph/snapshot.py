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

import errno
import gzip
import io
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

from .db import CURRENT_USER_VERSION, _readonly_uri, connect
from .export import MIN_EXPORT_VERSION, canonical, iter_corpus_content_lines

#: snapshot manifest format stamp
MANIFEST_FORMAT = "zaxbygraph.snapshot.v1"

#: max gzip part size (bytes): 100 MB per the issue's splitting rule
PART_MAX_BYTES = 100 * 1024 * 1024

#: per-invocation git credential configuration: gh's helper is the only
#: sanctioned bridge from GH_TOKEN (or an interactive gh login) to git, and
#: it is passed with -c so no config file is ever written
_CREDENTIAL_CONFIG = (
    ("credential.helper", ""),
    ("credential.helper", "!gh auth git-credential"),
    ("credential.useHttpPath", "true"),
)

#: hard ceiling on the EXPANDED snapshot bytes a pull will allocate: a
#: small published part can expand ~1030x, so the pull path caps the
#: expansion before decompressing into memory (publish-side PART_MAX_BYTES
#: bounds the compressed side only)
PULL_MAX_DB_BYTES = 8 * 1024 * 1024 * 1024

#: the published ref (issue #34 AC6 default location)
SNAPSHOT_REF = "refs/zaxbygraph/snapshot"

#: the fallback branch (AC6 fallback when the custom ref is refused)
SNAPSHOT_BRANCH = "zaxbygraph-snapshot"

#: deterministic commit identity: fixed timestamps keep the orphan commit
#: byte-deterministic for identical content (plan v2 finding 8)
_FIXED_DATE = "1970-01-01T00:00:00Z"

#: a SQLite `-wal` file's own header, which every open writes even when the
#: connection makes no change. A sidecar of exactly this size carries no
#: frames and cannot revert anything, so the post-install stale-WAL refusal
#: compares the size against it rather than against zero.
_WAL_HEADER_BYTES = 32


class SnapshotError(Exception):
    """Snapshot failure with an envelope-shaped payload.

    ``exit_code`` is the process exit the CLI answers with. It defaults to 2
    (usage or guard rejection: ``README.md`` promises a 2 "will never succeed
    on retry unchanged"), so every existing raise site keeps its current exit
    code.

    A raise passes 1 only when what blocked it was a stat or open fault on the
    destination path itself -- an uninspectable, busy or I/O-faulted
    destination. That is a per-attempt condition, so it belongs to the class
    ``README.md`` says "sometimes will" succeed on retry. The criterion is the
    *cause*, never the hint: ``git_timeout``'s hint tells the user to retry,
    but what blocked it was a transfer past its ceiling, which retrying the
    identical command cannot clear, so it stays 2.
    """

    def __init__(self, code: str, message: str, hint: str | None = None,
                 *, exit_code: int = 2):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code


#: Result codes that mean "this is a readable file that is not a usable
#: zaxbygraph store", as opposed to "this file could not be inspected".
#:
#: ``SQLITE_NOTADB`` -- the bytes are not a database. ``SQLITE_CORRUPT`` -- a
#: database whose pages are damaged. ``SQLITE_ERROR`` -- the reachable outcomes
#: for the two fixed SELECT literals below are all permanent and readable: no
#: ``sync_state`` table, no ``repo`` / ``issues_since`` column, an unregistered
#: collation, or a ``sync_state`` VIEW whose body evaluates ``json_extract`` on
#: a truncated document. The whole code is preserved rather than a subset of
#: it, because narrowing it would refuse readable destinations the base
#: ``pull`` replaces (plan-critic R2-1). All three are permanent, readable
#: conditions, so ``pull`` is still the documented recovery path for them
#: (frozen AC3).
#:
#: The comparison is on the PRIMARY code, never the raw ``sqlite_errorcode``:
#: Python exposes SQLite's extended result codes through that attribute, and
#: exact comparison to 1 and 11 does not implement those families. Two real
#: destination schemas reproduce the wrong refusal -- a ``sync_state`` with a
#: ``COLLATE`` column reopened without that collation answers
#: ``SQLITE_ERROR_MISSING_COLLSEQ`` (257), and an FTS5 ``sync_state`` with a
#: dropped shadow table answers ``SQLITE_CORRUPT_VTAB`` (267) -- and both are
#: replaced successfully by the base ``pull``. Masking with ``& 0xFF`` is what
#: makes the classifier read 267 as primary 11 (``SQLITE_CORRUPT``) rather than
#: as an unknown code it would refuse; an exact comparison to 11 would misroute
#: it, and a comparison to 1 would misroute it on any host that reports the
#: extended form.
_NOT_A_STORE_CODES = frozenset({
    getattr(sqlite3, "SQLITE_NOTADB", 26),
    getattr(sqlite3, "SQLITE_CORRUPT", 11),
    getattr(sqlite3, "SQLITE_ERROR", 1),
})


def _primary_code(exc: sqlite3.Error) -> int | None:
    """SQLite's primary result code for ``exc``, or None when unavailable."""
    raw = getattr(exc, "sqlite_errorcode", None)
    return None if raw is None else raw & 0xFF


def _is_not_a_store(exc: sqlite3.Error) -> bool:
    """True when ``exc`` means "a readable file that is not a usable store".

    Every ``SQLITE_ERROR`` outcome is in this set, not only the ones whose
    message names ``sync_state``. Narrowing it to a message match would refuse
    readable destinations whose ``sync_state`` exists but lacks the ``repo``
    or ``issues_since`` column -- a new refusal for a permanent, readable,
    schema-incompatible store, which the base ``pull`` replaces (plan-critic
    R2-1). Preserving the whole code keeps this change behaviour-preserving
    for every ``SQLITE_ERROR`` outcome: whatever the base did, this does too.
    """
    code = _primary_code(exc)
    return code is not None and code in _NOT_A_STORE_CODES


def _dest_uninspectable(db_path: Path) -> SnapshotError:
    """The refusal for a destination whose contents cannot be determined.

    Worded for both callers: `_check_destination_shape` asks whether the
    destination holds other repos, `_local_watermark` asks what its watermark
    is, and neither question is answerable when the file cannot be inspected."""
    return SnapshotError(
        "dest_uninspectable",
        f"the destination store {db_path} could not be inspected, so its"
        " contents cannot be determined",
        hint="close readers of the destination and re-pull, or check the"
             " path's permissions and the underlying storage",
        exit_code=1,
    )


def _reject_unless_not_a_store(exc: sqlite3.Error, db_path: Path) -> None:
    """Raise ``dest_uninspectable`` unless ``exc`` means "not a store".

    The base code answered every ``sqlite3.Error`` from these legs with a bare
    ``return``, which read a busy or I/O-faulted destination as "no local
    corpus" and "no other repos". Those are inspection failures, not absences,
    and they fail open on every interpreter -- so a stat-first fix alone would
    leave them open (frozen AC7).
    """
    if not _is_not_a_store(exc):
        raise _dest_uninspectable(db_path) from exc


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
    # the temp-repo plumbing must not inherit an ambient checkout context
    # or an ambient identity (which would override the -c identity below
    # and make the orphan commit non-deterministic)
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
              "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
              "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
              "GIT_COMMITTER_DATE"):
        env.pop(k, None)
    env.update({
        "GIT_AUTHOR_DATE": _FIXED_DATE,
        "GIT_COMMITTER_DATE": _FIXED_DATE,
    })
    # gh is this repo's only credential (AGENTS.md), and the git subprocess
    # runs with the ambient git config neutralized for determinism — so the
    # credential helper is configured PER INVOCATION (never written to any
    # config file). That keeps the documented HTTPS push/fetch working with
    # GH_TOKEN or an interactive gh login, while a host without gh still
    # gets the clean GIT_TERMINAL_PROMPT=0 failure rather than a hang.
    argv = ["git"]
    for key, value in _CREDENTIAL_CONFIG:
        argv += ["-c", f"{key}={value}"]
    argv += args
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, env=env,
            timeout=300,
        )
    except FileNotFoundError as exc:
        raise SnapshotError(
            "git_missing",
            f"git executable not found: {exc}",
            hint="snapshot transport drives git as a subprocess; "
                 "git must be on PATH",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SnapshotError(
            "git_timeout",
            f"git {args[0]} timed out after 300s",
            hint="the snapshot transfer is too slow for the transport"
                 " timeout; retry or publish from a faster link",
        ) from exc
    except OSError as exc:
        raise SnapshotError(
            "git_failed",
            f"git {args[0]} could not run: {exc}",
            hint="check the git installation and the target path",
        ) from exc
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


def publish(db_path: Path, target: str, publisher: str,
            expected_repo: str | None = None) -> dict:
    """snapshot publish (issue #34 AC1-AC2): verified orphan-commit
    transport under the sync lock; every manifest field derives from one
    staged backup."""
    db_path = Path(db_path).resolve()
    publisher = (publisher or "").strip() or "unknown"
    from .sync import acquire_sync_lock

    # A store that does not exist (or is not a file) must refuse BEFORE
    # anything creates it: connect() would otherwise leave a schema-less
    # file at the canonical path that resolve_db prefers over the real
    # store, turning later reads into store_unreadable.
    if not db_path.is_file():
        raise SnapshotError(
            "no_corpus",
            f"no store at {db_path}; run sync first",
            hint="snapshot publish moves a synced corpus",
        )
    lock = acquire_sync_lock(db_path, wait=True)
    tmp: Path | None = None
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
            if expected_repo is not None and repo != expected_repo:
                # the manifest is written from sync_state; an envelope
                # labelled with a different --repo would report a corpus
                # this store does not hold (cubic second-round finding)
                raise SnapshotError(
                    "repo_mismatch",
                    f"store holds {repo!r} but the requested repo is"
                    f" {expected_repo!r}",
                    hint="publish from the matching repo's store, or pass"
                         " --repo for the store's repo",
                )
            tmp = Path(tempfile.mkdtemp(prefix="zaxbygraph-publish-"))
            staged = tmp / "staged.db"
            _staged_backup(conn, staged)
            uv = int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
        if uv < MIN_EXPORT_VERSION:
            # the corpus digest needs the v10 tables; refuse with the
            # same coded floor export-corpus already applies
            raise SnapshotError(
                "store_too_old",
                f"store schema v{uv} is below the export floor"
                f" (v{MIN_EXPORT_VERSION}); re-run sync once against this"
                " database to migrate",
                hint="snapshot transport needs a v10+ store",
            )
        return _publish_staged(staged, tmp, target, publisher, repo, db_path)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
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
        # The same guard `pull` applies at the manifest boundary, applied here
        # at the producing end. A store for a repository with zero issues and
        # PRs carries a NULL watermark; publishing it produces a snapshot that
        # every pull refuses, so the transport would advertise a snapshot it
        # can never deliver (issue #55 AC6).
        #
        # The code is `no_watermark`, distinct from both `bad_manifest` (which
        # callers read as a malformed RECEIVED manifest on the pull side) and
        # `repo_not_synced` (which the publish path already raises for "no
        # sync_state rows" and `_check_completeness` raises for "no row for
        # this repo"). One code spanning three conditions with two different
        # remedies sends a consumer keyed on error.code to the wrong fix: an
        # empty repository is already synced, so "run sync first" can never
        # clear it. The hint names the real precondition instead.
        if not isinstance(watermark, str) or not watermark:
            raise SnapshotError(
                "no_watermark",
                "store watermark is empty; publish needs a store that has"
                " ingested at least one item for this repository",
                hint="a repository with no issues or pull requests has no"
                     " watermark to publish; sync a repository that has items",
            )
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
    """The store's origin host: the capability-probe meta key carries it
    (the segment between the prefix and the next colon); the slug-keyed
    store path (<root>/<host>/<owner>/<repo>/) is the fallback; a bare
    --db path outside that layout derives 'unknown'."""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT key FROM meta WHERE key LIKE 'capability_probe:%'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        row = None
    if row:
        parts = row[0].split(":")
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    try:
        host_dir = Path(db_path).resolve().parent.parent.parent
        if "." in host_dir.name and host_dir.name not in ("", ".", ".."):
            return host_dir.name
    except OSError:
        pass
    return "unknown"
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
def pull(db_path: Path, source: str, force: bool = False,
         verify_sample: int = 0, source_factory=None,
         expected_repo: str | None = None,
         expected_host: str | None = None) -> dict:
    """snapshot pull (issue #34 AC3-AC5): verify-then-install. Staged
    verification happens first (part shas, gzip stream, db sha, quick_check,
    schema version); the destination identity is bound to the manifest
    BEFORE the lock is taken; provenance lands in the staged db; the sync
    lock is held for the authoritative re-check and the install; one atomic
    replace replaces the store; post-install the corpus digest is re-verified."""
    db_path = Path(db_path).resolve()
    # the lock file needs its directory; the store FILE itself is never
    # created here (a schema-less file at the canonical path would shadow
    # the real store in resolve_db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    from .sync import acquire_sync_lock

    tmp = Path(tempfile.mkdtemp(prefix="zaxbygraph-pull-"))
    try:
        manifest, gz_bytes = _fetch_and_verify_parts(tmp, source)
        _bind_destination(manifest, expected_repo, expected_host)
        staged = tmp / "staged.db"
        _verify_staged(staged, gz_bytes, manifest)

        _check_staged_digest(staged, manifest)

        lock = acquire_sync_lock(db_path, wait=True)
        try:
            local_newer = _local_watermark(db_path, manifest["repo"])
            _check_destination_shape(db_path, manifest, force)
            if (local_newer is not None
                    and local_newer > manifest["watermark"] and not force):
                raise SnapshotError(
                    "local_newer",
                    f"local store is newer ({local_newer}) than the snapshot"
                    f" ({manifest['watermark']}); pass --force to overwrite",
                    hint="pull keeps a newer local store (issue #34 AC4)",
                )
            # Sample verification runs on the STAGED store, BEFORE the
            # replace (cubic second-round finding): a gh/source fault must
            # refuse while the destination is still intact, because the
            # refusal envelope is a promise that nothing was replaced. A
            # drift REPORT is not a fault - the manifest digest already
            # proves store == snapshot, so the install proceeds and the
            # counts ride along in the result.
            sample = None
            if verify_sample > 0:
                sample = _verify_sample(
                    staged, manifest["repo"], verify_sample, source_factory)
            _write_provenance(staged, manifest)
            _install(staged, db_path)
            result = _post_install_check(db_path, manifest)
            if sample is not None:
                result["verify_sample"] = sample
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
    # fetch EVERY candidate ref and install the NEWEST manifest: a stale
    # custom ref left behind by a namespace refusal must not keep serving
    # the first snapshot forever (swarm-pr-review PRR-H)
    candidates = {}
    for ref in (SNAPSHOT_REF, "refs/heads/" + SNAPSHOT_BRANCH):
        f = _run_git(["fetch", source, ref], wd, check=False)
        if f.returncode != 0:
            continue
        commit = _run_git(["rev-parse", "FETCH_HEAD"], wd).stdout.decode().strip()
        mbytes = _run_git(["cat-file", "blob", commit + ":manifest.json"],
                          wd, check=False)
        if mbytes.returncode != 0:
            continue
        try:
            cand = json.loads(mbytes.stdout.decode("utf-8"))
        except ValueError:
            # unparseable: keep the candidate so the downstream decoder
            # produces the coded bad_manifest refusal
            cand = None
        if isinstance(cand, dict) and isinstance(cand.get("watermark"), str)                 and cand["watermark"]:
            wm = cand["watermark"]
        else:
            # malformed/absent watermark sorts lowest but is still served,
            # so the manifest validation below refuses it with its code
            wm = ""
        candidates[ref] = (wm, commit, mbytes.stdout)
    if not candidates:
        raise SnapshotError(
            "snapshot_absent",
            f"source has neither {SNAPSHOT_REF} nor the fallback branch",
            hint="publish first, or point pull at the right repo",
        )
    # newest watermark wins; ties break toward the custom ref so a stable
    # source keeps a deterministic answer
    fetched = max(candidates,
                  key=lambda r: (candidates[r][0], r == SNAPSHOT_REF))
    watermark, commit, mbytes = candidates[fetched]
    try:
        manifest = json.loads(mbytes.decode("utf-8"))
    except ValueError as exc:
        raise SnapshotError(
            "bad_manifest",
            f"snapshot manifest is not valid JSON: {exc}",
            hint="re-publish and re-pull",
        ) from exc
    if not isinstance(manifest, dict):
        # a remote manifest that decodes to null/scalar/list must refuse as
        # bad_manifest, not raise TypeError on the membership test below
        raise SnapshotError(
            "bad_manifest",
            f"snapshot manifest must be a JSON object, got"
            f" {type(manifest).__name__}",
            hint="re-publish and re-pull",
        )
    missing = [k for k in ("format", "repo", "user_version", "watermark",
                           "db_sha256", "gz_sha256", "corpus_sha256",
                           "parts") if k not in manifest]
    if missing:
        raise SnapshotError(
            "bad_manifest",
            f"snapshot manifest is missing required keys: {missing}",
            hint="re-publish and re-pull",
        )
    if not isinstance(manifest.get("watermark"), str) or not manifest["watermark"]:
        # a null-valued watermark must not bypass AC4's newer-local guard
        raise SnapshotError(
            "bad_manifest",
            "snapshot manifest watermark must be a non-empty string",
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
    for part in parts:
        # part names reach a git object path and local paths: require a
        # plain non-empty string without separators or NUL
        name = part["name"]
        if (not isinstance(name, str) or not name
                or "\x00" in name or "/" in name or "\\" in name
                or name in (".", "..")):
            raise SnapshotError(
                "bad_manifest",
                f"snapshot part name {name!r} is not a plain file name",
                hint="re-publish and re-pull",
            )
        if not isinstance(part["bytes"], int) or isinstance(part["bytes"], bool) \
                or part["bytes"] < 0:
            raise SnapshotError(
                "bad_manifest",
                f"snapshot part {name!r} bytes must be a non-negative int",
                hint="re-publish and re-pull",
            )
    if manifest.get("format") != MANIFEST_FORMAT:
        raise SnapshotError(
            "bad_manifest",
            f"manifest format {manifest.get('format')!r} != {MANIFEST_FORMAT!r}",
            hint="not a zaxbygraph snapshot",
        )
    gz_parts = []
    for part in parts:
        blob = _run_git(["cat-file", "blob", commit + ":" + part["name"]], wd).stdout
        if len(blob) != part["bytes"]:
            # the descriptor must match the bytes actually received
            raise SnapshotError(
                "part_size_mismatch",
                f"part {part['name']} is {len(blob)} bytes, manifest says"
                f" {part['bytes']}",
                hint="the snapshot content changed or was tampered with",
            )
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
        # bounded decompression: a small published part can expand ~1030x,
        # so the pull path caps the expanded size BEFORE allocating it -
        # the ceiling must bound the allocation, not inspect it after the
        # fact (cubic run 3). GzipFile.read(n) returns at most n bytes, so
        # the largest buffer this path can build is ceiling + 1.
        with gzip.GzipFile(fileobj=io.BytesIO(gz_bytes)) as gunzip:
            raw = gunzip.read(PULL_MAX_DB_BYTES + 1)
        if len(raw) > PULL_MAX_DB_BYTES:
            raise SnapshotError(
                "snapshot_too_large",
                f"snapshot expands to {len(raw)} bytes, over the"
                f" {PULL_MAX_DB_BYTES}-byte pull ceiling",
                hint="the snapshot is too large for this build to install",
            )
    except SnapshotError:
        raise
    except (OSError, EOFError, zlib.error, MemoryError) as exc:
        # zlib.error is the corrupt-DEFLATE-stream case: the gzip header, the
        # CRC32 trailer and the ISIZE trailer are all intact, so gzip raises
        # zlib.error rather than OSError/EOFError and the tuple below it used
        # to let it escape as a traceback (issue #55 AC5). It derives from
        # Exception, not OSError, so it has to be named explicitly.
        #
        # MemoryError is the resource-exhaustion sibling: the read is bounded
        # at ceiling+1 bytes, but on a machine with less than that free the
        # allocation can still fail, and MemoryError is not an OSError. The
        # AGENTS.md contract is that a storage fault is a result object, never
        # a traceback (PR #57 swarm review, reliability-performance lane).
        if isinstance(exc, MemoryError):
            raise SnapshotError(
                "snapshot_too_large",
                "snapshot could not be decompressed within available memory",
                hint="the snapshot expands beyond what this machine can hold",
            ) from exc
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
    if uv < MIN_EXPORT_VERSION:
        # the corpus digest below needs the v10 tables; refuse with the
        # same coded floor export-corpus already applies
        raise SnapshotError(
            "store_too_old",
            f"snapshot schema v{uv} is below the export floor"
            f" (v{MIN_EXPORT_VERSION}); re-run sync once against this"
            " database to migrate",
            hint="snapshot transport needs a v10+ store",
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


def _bind_destination(manifest: dict, expected_repo: str | None,
                      expected_host: str | None) -> None:
    """Bind the snapshot to the destination BEFORE anything is replaced
    (swarm-pr-review PRR-C): a snapshot for another repo must never install
    into this store, and a manifest whose host namespace differs from the
    destination's is refused too. The manifest repo/host are written by
    publish; this is the read side that was missing."""
    snap_repo = manifest.get("repo")
    if not isinstance(snap_repo, str) or not snap_repo:
        raise SnapshotError(
            "bad_manifest",
            "snapshot manifest repo must be a non-empty string",
            hint="re-publish and re-pull",
        )
    if expected_repo is not None and snap_repo != expected_repo:
        raise SnapshotError(
            "repo_mismatch",
            f"snapshot holds {snap_repo!r} but the destination is"
            f" {expected_repo!r}",
            hint="pull the snapshot into the matching repo's store, or"
                 " pass --repo for the snapshot's repo",
        )
    if expected_host is not None:
        snap_host = manifest.get("host")
        if snap_host not in (None, "unknown") and snap_host != expected_host:
            raise SnapshotError(
                "host_mismatch",
                f"snapshot was taken on host {snap_host!r} but the"
                f" destination is {expected_host!r}",
                hint="snapshots do not move between origin hosts",
            )


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a store read-only WITHOUT ever writing to it. The URI must be
    built by `db._readonly_uri`, which percent-encodes the path: a raw
    f-string leaves a literal '?', '#' or '%' in the path to terminate or
    corrupt the URI, sqlite3.connect then raises, and the caller's except
    branch would silently skip the guard it is protecting (cubic run 3 - a
    fail-open on the multi-repo and newer-local checks). The helper also
    guarantees an absolute path, which the URI form requires, and keeps the
    backslash spelling SQLite accepts for a UNC destination (issue #55).
    `pull` resolves its destination first, so the relative branch is
    unreachable here.

    The import is at module scope: `snapshot` already imports `.db` for
    `CURRENT_USER_VERSION` and `connect`, and `db` imports nothing from
    `snapshot`, so there is no cycle for a deferred import to break."""
    return sqlite3.connect(_readonly_uri(db_path), uri=True)


def _check_destination_shape(db_path: Path, manifest: dict,
                             force: bool) -> None:
    """A whole-file replace destroys every other repo in the destination,
    so a multi-repo destination is refused unless --force is passed. The
    publish side already refuses a multi-repo SOURCE (multi_repo_store);
    this is the symmetric destination guard (PRR-C).

    The guard's own worst case is a destination it cannot inspect, so the
    existence question is answered by ``stat`` and only ``FileNotFoundError``
    means "absent". ``is_file()`` is not a stable oracle: on 3.14 it delegates
    to ``os.path.isfile``, which swallows every ``OSError``, and on 3.11-3.13
    it re-raises only outside pathlib's own swallow set. Either way an
    uninspectable destination read as an absent one, and the early return that
    exists for the absent case then skipped the guard immediately before
    ``_install``'s whole-file ``os.replace`` (issue #61).

    ``ValueError`` is caught alongside ``OSError`` because ``stat`` raises it
    for a path carrying an embedded null character, and this guard's contract
    is a coded refusal rather than a traceback -- the same pair the sibling
    guard in ``export._reject_database_alias`` catches."""
    try:
        st = db_path.stat()
    except FileNotFoundError:
        return                       # genuinely absent: nothing to refuse
    except (OSError, ValueError) as exc:
        raise _dest_uninspectable(db_path) from exc
    if not stat.S_ISREG(st.st_mode):
        # Legacy behaviour preserved, NOT a safety claim: a directory fails
        # later at `os.replace` (Windows -> PermissionError -> dest_open;
        # POSIX rename -> EISDIR), and POSIX rename atomically replaces an
        # existing non-directory node, including FIFO and device nodes.
        # Recorded as a boundary in the PR body rather than given a new
        # refusal here.
        return
    try:
        conn = _open_readonly(db_path)
    except sqlite3.Error as exc:
        _reject_unless_not_a_store(exc, db_path)
        return
    try:
        repos = [r[0] for r in conn.execute("SELECT repo FROM sync_state")]
    except sqlite3.Error as exc:
        _reject_unless_not_a_store(exc, db_path)
        return
    finally:
        conn.close()
    others = [r for r in repos if r != manifest["repo"]]
    if others and not force:
        raise SnapshotError(
            "multi_repo_destination",
            f"destination store also holds {', '.join(sorted(others))};"
            " replacing it would drop those corpora",
            hint="pull into a single-repo store, or pass --force to"
                 " overwrite the whole store",
        )


def _local_watermark(db_path: Path, repo: str) -> str | None:
    """The destination's watermark FOR THE SNAPSHOT'S REPO.

    An ABSENT store reads as None (no local corpus) and must never be created
    just to ask -- connect() would leave a schema-less file that resolve_db
    prefers over the real store (PRR-F3). An UNINSPECTABLE store is a
    different thing and raises: reading it as None silently overwrote a newer
    local store, because the `local_newer` refusal it feeds never fired
    (issue #61, frozen AC4). ``ValueError`` is caught with ``OSError`` for the
    same reason as in ``_check_destination_shape``: a null-byte path is
    "cannot inspect", not "absent"."""
    try:
        st = db_path.stat()
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise _dest_uninspectable(db_path) from exc
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        conn = _open_readonly(db_path)
        try:
            row = conn.execute(
                "SELECT issues_since FROM sync_state WHERE repo = ?",
                (repo,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _reject_unless_not_a_store(exc, db_path)
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
    """Quiesce the destination, ONE os.replace, then drop the OLD sidecars.
    Order matters (swarm-pr-review PRR-F1): the sidecars are only deleted
    AFTER a successful replace, so a failed replace can never discard
    committed WAL frames — the pre-replace file and its sidecars survive
    untouched. The quiesce is best-effort and unguarded-by-verdict (any
    failure falls through — pull is the recovery path for a corrupt
    destination); the COMMITTED refusals are the guarded replace below,
    classified by cause: a busy reader, a cross-volume move, or a storage
    fault each get their own code and hint.

    Once the replace has committed, dropping the OLD sidecars is best-effort
    housekeeping: a failure there must NOT claim the store was not replaced
    (issue #56, AC6 — the pre-fix code folded the cleanup's OSError into the
    replace's refusal, so the envelope lied about a committed mutation and
    the post-install check never ran). `pull` always reaches
    `_post_install_check` after this returns, and that check's corpus-digest
    verdict is the truthful outcome: a stale sidecar that reverts the pulled
    corpus is exactly what `corpus_mismatch` exists to catch, and its hint
    already discloses that the store was already replaced.
    """
    # best-effort WAL quiesce: back-fill pending frames into the main db so
    # the replaced file never coexists with stale sidecar data. Skipped
    # when the destination does not exist yet — sqlite3.connect CREATES a
    # missing file, and a pull that then refuses would leave a 0-byte store
    # behind that `resolve_db` prefers over a legacy in-repo DB holding the
    # corpus (swarm-pr-review N-08).
    if db_path.exists():
        try:
            dest = sqlite3.connect(str(db_path))
            try:
                dest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                dest.close()
        except (OSError, sqlite3.Error):
            # pull is the recovery path for a corrupt destination (README
            # recipes): a quiesce failure on unreadable bytes falls through
            # to the guarded install, which replaces them
            pass
    # ONE guarded replace: a same-file open reader, a cross-volume move, and
    # a storage fault are different faults with different remedies.
    try:
        os.replace(staged, db_path)
    except OSError as exc:
        winerr = getattr(exc, "winerror", None)
        if winerr == 17 or exc.errno == errno.EXDEV:
            raise SnapshotError(
                "dest_cross_volume",
                "destination store is on a different volume than the"
                " staging area; set TMPDIR onto the store's volume",
                hint="os.replace cannot move a file across volumes",
            ) from exc
        if winerr in (5, 32) or exc.errno in (errno.EACCES, errno.EBUSY,
                                              errno.EPERM, errno.ETXTBSY):
            # EACCES and EPERM are ambiguous: a reader holding the file open,
            # or a permissions problem on the destination. Name the cause so
            # the user is not told to close readers when the real fix is a
            # permission change (cubic review, PR #58).
            cause = (f"{exc.__class__.__name__}: {exc}"
                     if exc.errno in (errno.EACCES, errno.EPERM) else "")
            raise SnapshotError(
                "dest_open",
                "destination store could not be replaced: held open by"
                " another process, or not writable"
                + (f" ({cause})" if cause else ""),
                hint="close readers holding the store open, or check the"
                " destination path's permissions, and re-pull",
            ) from exc
        raise SnapshotError(
            "dest_open",
            f"destination store could not be replaced: {exc}",
            hint="check the destination path and its permissions",
        ) from exc
    # The replace has COMMITTED. Only now are the OLD sidecars stale, and
    # dropping them is best-effort: a failure here (a locked or read-only
    # sidecar) leaves the store REPLACED, so it is swallowed rather than
    # reported as a failed replace. The post-install check that follows is
    # the verdict on what the destination now serves.
    for sidecar in (Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm"),
                    Path(str(db_path) + "-journal")):
        # Each unlink gets its own guard: one locked sidecar must not stop
        # the others being dropped (cubic review, PR #58).
        try:
            if sidecar.exists():
                sidecar.unlink()
        except OSError:
            pass


def _post_install_check(db_path: Path, manifest: dict) -> dict:
    """Reopen dest, recompute the corpus digest, assert the manifest value
    (AC5). Returns the result dict.

    The reopen is READ-ONLY (issue #56, AC6): a read-write open of a
    destination whose stale sidecar survived the install's best-effort
    cleanup would recover that sidecar into the replaced main on close, so
    the check's own close would bake the very revert it exists to report —
    and the replaced bytes would no longer be provable. A read-only reopen
    observes exactly what the destination now serves (the stale sidecar's
    content, when one survived) without mutating it: the digest verdict is
    the truth, and the install's evidence survives the check.

    A surviving `-wal` with anything in it beyond its 32-byte header is
    refused BEFORE the digest (swarm-pr-review N-07): the corpus digest
    covers items, their children, releases and orphan edges, so a stale WAL
    whose frames touch only `revisions`, `sync_state`, `fetch_log` or
    `actors` leaves the digest matching while the destination serves the OLD
    watermark, the OLD edit history and the OLD actor URLs — and every later
    read-write open recovers those frames into the replaced main, so the
    corruption outlives the pull. After `os.replace` any surviving WAL
    belongs to the pre-replace main by construction, so its presence beyond
    the header SQLite itself writes on any open is itself the defect. The
    header alone (exactly 32 bytes) carries no frames and is inert, so it is
    NOT refused; a surviving `-shm` without a `-wal` is likewise inert.
    A sidecar that CANNOT be inspected is refused too, never assumed empty:
    an OSError on `stat` would otherwise fail open and let the digest be the
    only verdict, which is the exact hole this guard closes (cubic review,
    PR #58). `stat` is called FIRST and only `FileNotFoundError` means
    "absent", because `Path.exists()` is not a stable oracle: on 3.14 it
    delegates to `os.path.exists`, which swallows EACCES and answers False,
    so an exists()-first shape would fail open on exactly the interpreters
    this repo supports (final-critic round 3).
    """
    stale_wal = Path(str(db_path) + "-wal")
    try:
        wal_size = stale_wal.stat().st_size
    except FileNotFoundError:
        wal_size = 0  # no sidecar survived the install: nothing to refuse
    except OSError as exc:
        # Fail CLOSED: an unreadable sidecar is not evidence of an absent
        # one. The digest covers only part of the store, so "cannot inspect"
        # must refuse rather than defer to a verdict that cannot see this
        # class of corruption.
        raise SnapshotError(
            "dest_sidecar_stale",
            "the destination's -wal could not be inspected after the install,"
            " so whether it carries pre-replace frames is unknown",
            hint="the store was already replaced; close every reader of it and"
                 " re-pull, or restore a known-good store",
        ) from exc
    stale_frames = wal_size > _WAL_HEADER_BYTES
    if stale_frames:
        raise SnapshotError(
            "dest_sidecar_stale",
            "a stale -wal survived the install beside the replaced store, so"
            " the destination can serve pre-replace frames on its next open",
            hint="the store was already replaced; close every reader of it and"
                 " re-pull, or restore a known-good store",
        )
    conn = _open_readonly(db_path)
    conn.row_factory = sqlite3.Row
    try:
        digest = _corpus_digest_conn(conn, manifest["repo"])
    finally:
        conn.close()
    if digest != manifest["corpus_sha256"]:
        raise SnapshotError(
            "corpus_mismatch",
            "post-install corpus digest differs from the manifest",
            hint="the store was already replaced; re-pull or restore a"
                 " known-good store",
        )
    return {"corpus_sha256": digest, "installed": True}


def _payload_projection(payload: dict) -> bytes:
    """The comparable core of one issue-list payload: exactly the fields
    the REST listing endpoint (the same family the watermark contract
    documents as identity-preserving) faithfully carries — title, body,
    state, timestamps, author, label names, and the pull_request marker.
    Children and GraphQL-only sections are deliberately excluded: a
    re-fetch cannot faithfully reproduce them, so they never enter the
    comparison (they can never false-drift)."""
    core = {
        "title": payload.get("title"),
        "body": payload.get("body"),
        "state": payload.get("state"),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
        "user": (payload.get("user") or {}).get("login"),
        "labels": sorted(l.get("name") for l in payload.get("labels") or []),
        "is_pr": bool(payload.get("pull_request")),
    }
    return canonical(core)


def _store_projection(conn: sqlite3.Connection, repo: str, number: int) -> bytes:
    """The same projection read from the installed store's row + labels."""
    row = conn.execute(
        "SELECT title, body, state, created_at, updated_at, author"
        " FROM items WHERE repo = ? AND number = ?",
        (repo, number)).fetchone()
    labels = sorted(r[0] for r in conn.execute(
        "SELECT name FROM labels WHERE repo = ? AND number = ? ORDER BY name",
        (repo, number)))
    core = {
        "title": row["title"],
        "body": row["body"],
        "state": row["state"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "user": row["author"],
        "labels": labels,
        "is_pr": bool(conn.execute(
            "SELECT 1 FROM items WHERE repo = ? AND number = ?"
            " AND kind = 'pr'", (repo, number)).fetchone()),
    }
    return canonical(core)


def _verify_sample(db_path: Path, repo: str, n: int, source_factory=None) -> dict:
    """Issue #34's --verify-sample N: re-fetch N random items from the
    live source and compare the payload-level projection (title, body,
    state, timestamps, author, labels, pull marker) against the store at
    db_path - the STAGED copy, so a source fault refuses before the
    destination is replaced. A mismatch means GitHub moved on after the snapshot was taken
    (drift, reported - not corruption: the manifest digest already proves
    store == snapshot). Payload-level only: children and GraphQL-only
    sections are never sampled (a re-fetch cannot faithfully reproduce
    them, so they can never false-drift). Uses the real ``gh`` REST
    source unless ``source_factory`` injects one (tests). Source faults
    raise the coded ``verify_failed``, never a raw traceback."""
    import random

    try:
        if source_factory is None:
            from zaxbygraph.github import GhApiSource

            owner, _, name = repo.rpartition("/")
            if not owner:
                raise SnapshotError(
                    "verify_failed",
                    f"cannot derive owner/name from repo {repo!r}",
                    hint="sample verification needs an owner/repo slug",
                )
            src = GhApiSource(owner, name)
        else:
            src = source_factory()
        conn = connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            numbers = [r["number"] for r in conn.execute(
                "SELECT number FROM items WHERE repo = ?", (repo,))]
            if not numbers:
                return {"requested": n, "checked": 0, "matched": 0,
                        "changed": 0, "skipped": 0}
            sample = random.sample(numbers, min(n, len(numbers)))
            try:
                payloads = src.get_issues(sample)
            except SnapshotError:
                raise
            except Exception as exc:
                raise SnapshotError(
                    "verify_failed",
                    f"sample re-fetch failed: {exc}",
                    hint="gh must be authenticated for --verify-sample",
                ) from exc
            checked = matched = changed = skipped = 0
            for number in sample:
                payload, _status = payloads.get(number, (None, None))
                if payload is None:
                    skipped += 1
                    continue
                try:
                    live = _payload_projection(payload)
                    local = _store_projection(conn, repo, number)
                except Exception as exc:
                    raise SnapshotError(
                        "verify_failed",
                        f"sample comparison failed for #{number}: {exc}",
                        hint="report this as a bug",
                    ) from exc
                checked += 1
                if live == local:
                    matched += 1
                else:
                    changed += 1
            return {"requested": n, "checked": checked, "matched": matched,
                    "changed": changed, "skipped": skipped}
        finally:
            conn.close()
    except SnapshotError:
        raise
    except Exception as exc:
        raise SnapshotError(
            "verify_failed",
            f"sample verification failed: {exc}",
            hint="gh must be authenticated for --verify-sample",
        ) from exc


def _corpus_digest_conn(conn: sqlite3.Connection, repo: str) -> str:
    """Corpus digest from an open connection (the post-install check)."""
    hasher = hashlib.sha256()
    for piece in iter_corpus_content_lines(conn, repo, set()):
        hasher.update(piece)
    return hasher.hexdigest()
