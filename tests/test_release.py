"""Arm's-length acceptance checks for issue #73 (release hygiene: one
version everywhere, CHANGELOG, packaging metadata, init preflight + first
sync, the skill shipping with the package, and stale-skill reporting),
authored BEFORE any implementation.

Frozen check names (issue #73 acceptance table):

    C14 test_version_moves_with_schema_and_tools
    C15 test_one_version_everywhere
    C16 test_init_preflight_and_first_sync
    C17 test_packaging_metadata
    C18 test_skill_ships_with_the_package
    C19 test_stale_skill_copies_are_reported

Base facts these checks were authored against (verified at 5b41510):
pyproject version and __init__.__version__ are both 0.4.0 while
db.CURRENT_USER_VERSION is 12; there is no CHANGELOG.md and no git tags;
package-data carries only schema.sql; there are no init/skill
subcommands; doctor has no skill-staleness check; query.status() answers
{repos, counts, coverage, snapshot} with NO version of any kind;
mcp_server's initialize serverInfo already reads __version__ (that arm
agrees at base — the discriminating arms are the skill stamp already
agreeing, plus discover carrying no version and status carrying none);
skills/zaxbygraph/SKILL.md carries a version-only stamp
``<!-- zaxbygraph skill-version: 0.4.0 -->`` (no content hash).

Seam contracts the fix must honor (documented here because the surfaces
do not exist at base):

- C16: ``zaxbygraph init`` runs a gh presence/auth preflight through the
  SAME module-level ``zaxbygraph.claims.run_gh`` seam the claim commands
  use (tests patch the module attribute; the command late-binds it at
  call time), then builds the store through the CLI's own sync path —
  the shared quiet core ``zaxbygraph.cli._sync_core`` — so patching that
  name keeps the leg offline, then reports the MCP registration line in
  its envelope payload.
- C18: the skill ships INSIDE the package at
  ``src/zaxbygraph/skills/zaxbygraph/SKILL.md`` (package data), the
  ``zaxbygraph skill`` command prints it, and
  ``zaxbygraph skill --install DIR`` writes DIR/zaxbygraph/SKILL.md
  stamped ``<!-- zaxbygraph skill-version: <version> hash=<64-hex> -->``
  where the hash is the sha256 of the skill BODY (the file minus its
  stamp line).
- C19: ``zaxbygraph skill --check [--dir DIR]`` classifies each found
  copy by BODY hash against the packaged skill's body hash (stale iff
  the body hash differs; a stamp-only difference follows the version and
  is caught the same way), reports the refresh command naming
  ``--install``, and writes NOTHING without --install. Without ``--dir``
  the check scans the four documented default skill homes (~/.claude,
  ~/.agents, ~/.zcode, ~/.opencode — each ``<home>/.<tool>/skills/
  zaxbygraph/SKILL.md``), and ``doctor`` reports the same skill-copies
  staleness section (round 7).

Hermetic: FakeGitHubSource stores, temp dirs, in-process cli.main with
patched seams; no network, no gh, no writes outside temp roots. Stdlib
only; unittest (pytest runs the file).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from fixtures import REPO, FakeGitHubSource, issue

import zaxbygraph
from zaxbygraph import db as zg_db
from zaxbygraph.cli import main
from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo

REPO_ROOT = Path(__file__).resolve().parents[1]

#: sha256 over ','.join(sorted(tool names)) for the 20-tool registry that
#: ships with schema v12 (computed at 5b41510). Structured release pins
#: (plan-critic round 1): version -> (schema_version, toolset_digest) —
#: a tool-list change must ship with a version bump + CHANGELOG entry and
#: the new row recorded here.
RELEASE_PINS: dict[str, tuple[int, str]] = {
    "0.5.0": (
        12,
        "0eb4be5f40db5a88b8ffe9aaff1591a9063197a3361ded7c9ab44af544b8a9fc",
    ),
}
#: Back-compat alias for the digest-only membership check.
TOOLSET_DIGESTS: dict[str, str] = {
    digest: version for version, (schema, digest) in RELEASE_PINS.items()
}

MCP_LINE = "claude mcp add zaxbygraph -- zaxbygraph mcp"

_STAMP_RE = re.compile(
    r"<!--\s*zaxbygraph skill-version:\s*(?P<version>[^\s]+)"
    r"(?:\s+hash=(?P<hash>[0-9a-fA-F]{64}))?\s*-->"
)


def _run_cli_inproc(argv: list[str]) -> tuple[int, str, str]:
    """In-process cli.main under redirected stdio (the test_claim_cmd
    _run_cli pattern): argparse usage failures surface as SystemExit(2),
    which the envelope contract turns into exit 2 with a machine-format
    envelope when --format json is in argv."""
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            if isinstance(exc.code, int):
                code = exc.code
            elif exc.code is None:
                code = 0
            else:
                code = 2
    return code, out.getvalue(), err.getvalue()


def _env_json(
    test: unittest.TestCase, out: str, code: int, err: str, tag: str
) -> dict:
    """Fail with `tag` unless stdout carries one JSON envelope object."""
    try:
        env = json.loads(out)
    except ValueError:
        test.fail(
            f"{tag}: the command must answer one JSON envelope on stdout;"
            f" got exit {code}, stdout {out[:160]!r}, stderr"
            f" {err.strip()[:160]!r}"
        )
    test.assertIsInstance(env, dict, f"{tag}: envelope must be an object")
    return env


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _changelog_section_for(version: str) -> str | None:
    """CHANGELOG.md text from the section heading naming `version` to the
    next version heading (or EOF); None when the version has no section."""
    path = REPO_ROOT / "CHANGELOG.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    headings = list(re.finditer(r"(?m)^#{1,3}\s*\[?v?(\d+\.\d+(?:\.\d+)?)", text))
    for i, match in enumerate(headings):
        if match.group(1) == version:
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            return text[match.start():end]
    return None


def _skill_body(text: str) -> str:
    """The skill text minus every zaxbygraph skill-version stamp line (the
    stamped copy's body is what the content hash covers)."""
    return "\n".join(
        ln for ln in text.splitlines() if not _STAMP_RE.search(ln)
    )


class _HermeticCliTest(unittest.TestCase):
    """Temp cwd per test (config-chain and git-origin hermeticity) plus a
    pinned ZAXBYGRAPH_HOME/ZAXBYGRAPH_DB so no leg can touch the operator's
    real store."""

    def setUp(self) -> None:
        self._prior_cwd = os.getcwd()
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._td.name)
        os.chdir(self.root)
        self._saved_env = {
            k: os.environ.get(k) for k in ("ZAXBYGRAPH_HOME", "ZAXBYGRAPH_DB")
        }
        os.environ["ZAXBYGRAPH_HOME"] = str(self.root / "zhome")

    def tearDown(self) -> None:
        os.chdir(self._prior_cwd)
        for key, val in self._saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self._td.cleanup()


# ==== C14: the version moves with the schema and the tools ==================


class VersionPinTests(unittest.TestCase):
    def test_version_moves_with_schema_and_tools(self) -> None:
        """RED at base (no CHANGELOG.md), GREEN after the fix. The package
        version, the schema version and the MCP tool set are pinned
        TOGETHER: pyproject == __version__; CHANGELOG.md exists and its
        TOPMOST version section is the live version and mentions the
        current schema version; and the live tool-set digest is one this
        file recorded (so a tool-list change without a recorded digest —
        i.e. without a version bump — fails)."""
        version = zaxbygraph.__version__
        project = _pyproject().get("project", {})
        self.assertEqual(
            project.get("version"),
            version,
            "pyproject version and zaxbygraph.__version__ must agree",
        )

        changelog = REPO_ROOT / "CHANGELOG.md"
        self.assertTrue(
            changelog.is_file(),
            "CHANGELOG.md must exist at the repo root (issue #73: the"
            " version moves with a CHANGELOG entry)",
        )
        text = changelog.read_text(encoding="utf-8")
        headings = list(
            re.finditer(r"(?m)^#{1,3}\s*\[?v?(\d+\.\d+(?:\.\d+)?)", text)
        )
        self.assertTrue(headings, "CHANGELOG.md must carry version headings")
        self.assertEqual(
            headings[0].group(1),
            version,
            f"the TOPMOST CHANGELOG section must be the live version"
            f" {version} (found {headings[0].group(1)!r})",
        )

        # the section for the live version pins the schema it ships with
        section = _changelog_section_for(version)
        self.assertIsNotNone(section, f"CHANGELOG has no section for {version}")
        schema_v = str(zg_db.CURRENT_USER_VERSION)
        self.assertRegex(
            section,
            rf"schema v?{re.escape(schema_v)}\b",
            f"the CHANGELOG entry for {version} must record the schema"
            f" version it ships (v{schema_v})",
        )

        # the live MCP tool set is a recorded tool set, and the recorded
        # release pin (version -> schema_version + digest) matches BOTH
        # live values for the live version (plan-critic R1: the pin must
        # use what it maps, not only check membership).
        from zaxbygraph import mcp_server

        digest = hashlib.sha256(
            ",".join(sorted(mcp_server._TOOL_SHAPES)).encode("utf-8")
        ).hexdigest()
        pin = RELEASE_PINS.get(version)
        self.assertIsNotNone(
            pin,
            f"no RELEASE_PINS row for the live version {version!r} — a"
            " tool-list or schema change must ship with a version bump,"
            " CHANGELOG entry, and a new recorded row here",
        )
        live_schema = int(zg_db.CURRENT_USER_VERSION)
        self.assertEqual(
            pin,
            (live_schema, digest),
            f"the recorded release pin for {version!r} is {pin!r} but the"
            f" live store ships schema v{live_schema} with toolset digest"
            f" {digest[:12]}… — update the pin together with the version"
            " bump and the CHANGELOG entry",
        )


# ==== C15: one version everywhere ===========================================


class OneVersionEverywhereTests(_HermeticCliTest):
    def test_one_version_everywhere(self) -> None:
        """RED at base (status carries no version of any kind and
        server/discover names none), GREEN after the fix. Every read
        surface reports the SAME version: pyproject, __version__, the
        skill stamp, the MCP initialize serverInfo, the server/discover
        payload, and query.status() — which must also carry the SCHEMA
        version beside the tool version."""
        version = zaxbygraph.__version__
        self.assertEqual(
            _pyproject().get("project", {}).get("version"),
            version,
            "pyproject version must equal zaxbygraph.__version__",
        )

        # the skill stamp reports the same version
        skill = REPO_ROOT / "skills" / "zaxbygraph" / "SKILL.md"
        self.assertTrue(skill.is_file(), "skills/zaxbygraph/SKILL.md missing")
        stamps = _STAMP_RE.findall(skill.read_text(encoding="utf-8"))
        self.assertTrue(
            stamps, "SKILL.md must carry a zaxbygraph skill-version stamp"
        )
        for stamped, _hash in stamps:
            self.assertEqual(
                stamped,
                version,
                f"the skill stamp must report the package version"
                f" {version} (found {stamped!r})",
            )

        # MCP initialize serverInfo reports it (live handshake)
        from test_mcp import McpSession

        session = McpSession(self, cwd=str(self.root))
        try:
            response = session.initialize()
        finally:
            session.stop()
        result = response.get("result") or {}
        server_info = result.get("serverInfo") or {}
        self.assertEqual(
            server_info.get("version"),
            version,
            f"the MCP initialize serverInfo must report the package version"
            f" {version} (found {server_info.get('version')!r})",
        )

        # server/discover names it too
        from zaxbygraph import mcp_server

        discover = mcp_server._discover_result(False)
        self.assertIn(
            version,
            json.dumps(discover),
            "the server/discover payload must name the tool version",
        )

        # status reports the schema version BESIDE the tool version
        from zaxbygraph import query

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            conn = connect(Path(td) / "history.db")
            try:
                init_schema(conn)
                src = FakeGitHubSource()
                src.add_issue(issue(1, title="probe", body="b"))
                sync_repo(conn, src, REPO)
                status = query.status(conn, REPO)
            finally:
                conn.close()
        self.assertIsInstance(status, dict)

        def _find_key(payload: dict, needles: set[str], forbid: set[str]) -> str | None:
            for key in payload:
                low = key.lower()
                if all(n in low for n in needles) and not any(f in low for f in forbid):
                    return key
            return None

        schema_key = _find_key(status, {"schema", "version"}, set())
        self.assertIsNotNone(
            schema_key,
            f"query.status() must report the schema version (a"
            f" schema_version-shaped key); payload keys: {sorted(status)}",
        )
        self.assertEqual(
            str(status[schema_key]),
            str(zg_db.CURRENT_USER_VERSION),
            f"status.{schema_key} must equal the live schema version"
            f" {zg_db.CURRENT_USER_VERSION}",
        )
        tool_key = _find_key(status, {"version"}, {"schema", "protocol", "coverage"})
        self.assertIsNotNone(
            tool_key,
            f"query.status() must report the TOOL version beside the schema"
            f" version; payload keys: {sorted(status)}",
        )
        self.assertEqual(
            str(status[tool_key]),
            version,
            f"status.{tool_key} must equal the package version {version}",
        )


# ==== C16: init preflight and first sync ====================================


class _FakeGh:
    """Stand-in for zaxbygraph.claims.run_gh: (returncode, stdout, stderr)
    tuples chosen per arm. `installed`/`authed` model the preflight's two
    questions; every call is recorded."""

    def __init__(self, *, installed: bool = True, authed: bool = True) -> None:
        self.installed = installed
        self.authed = authed
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]):
        self.calls.append([str(a) for a in argv])
        if not self.installed:
            return (1, "", "gh could not run: [WinError 2] gh: The system"
                           " cannot find the file specified")
        if not self.authed:
            return (1, "", "You are not logged into any GitHub hosts."
                           " Run gh auth login to authenticate.")
        return (0, " Logged in to github.com\n", "")


class InitPreflightTests(_HermeticCliTest):
    def _run_init(self, db: Path, gh, sync_core) -> tuple[int, dict]:
        """`zaxbygraph init --repo O/R` with the gh seam and the sync seam
        patched; asserts the seams exist for the fix to bind to."""
        import zaxbygraph.claims as claims_mod
        import zaxbygraph.cli as cli_mod

        self.assertTrue(
            hasattr(cli_mod, "_sync_core"),
            "the CLI sync path must expose the shared _sync_core seam"
            " (zaxbygraph.cli._sync_core) for init's first sync to route"
            " through",
        )
        argv = [
            "init", "--repo", REPO, "--db", str(db), "--format", "json",
        ]
        os.environ["ZAXBYGRAPH_DB"] = str(db)
        with mock.patch.object(claims_mod, "run_gh", gh), \
                mock.patch.object(cli_mod, "_sync_core", sync_core):
            code, out, err = _run_cli_inproc(argv)
        env = _env_json(self, out, code, err, "init")
        return code, env

    def test_init_preflight_and_first_sync(self) -> None:
        """RED at base (no init subcommand: argparse exit 2, no envelope),
        GREEN after the fix. ``zaxbygraph init --repo O/R``:
        (a) gh missing -> coded error{code, message, hint} naming the fix
            (install gh), no sync attempt;
        (b) gh present but not authenticated -> coded error with a hint
            naming ``gh auth login``, no sync attempt;
        (c) healthy -> the store is built through the CLI's sync path and
            the envelope payload reports the sync outcome plus the MCP
            registration line ``claude mcp add zaxbygraph -- zaxbygraph mcp``.
        """
        synced: list[str] = []

        def fake_sync_core(conn, slug, **kwargs):
            src = FakeGitHubSource()
            src.add_issue(issue(1, title="first issue", body="hello"))
            sync_repo(conn, src, slug)
            synced.append(slug)
            return {"ingested": 1, "items": 1}

        # ---- (a) gh missing ------------------------------------------------
        db_a = self.root / "init-a.db"
        code_a, env_a = self._run_init(db_a, _FakeGh(installed=False), fake_sync_core)
        self.assertFalse(
            env_a.get("ok", False),
            "a missing gh must fail the init preflight, not proceed",
        )
        error = env_a.get("error")
        self.assertIsInstance(error, dict, "failures answer error{code,message,hint}")
        for key in ("code", "message", "hint"):
            self.assertTrue(
                error.get(key),
                f"the coded error must carry a non-empty {key!r}"
                f" (got {error!r})",
            )
        blob = (str(error.get("message", "")) + str(error.get("hint", ""))).lower()
        self.assertIn("gh", blob, "the gh-missing error names gh")
        self.assertIn(
            "install", blob,
            f"the gh-missing hint names the fix (install gh): {error!r}",
        )
        self.assertEqual(
            synced, [], "a failed preflight must not attempt the first sync"
        )

        # ---- (b) gh present, auth failing ----------------------------------
        db_b = self.root / "init-b.db"
        code_b, env_b = self._run_init(db_b, _FakeGh(authed=False), fake_sync_core)
        self.assertFalse(env_b.get("ok", False), "an unauthenticated gh fails preflight")
        error = env_b.get("error")
        self.assertIsInstance(error, dict)
        for key in ("code", "message", "hint"):
            self.assertTrue(error.get(key), f"coded error carries {key!r}: {error!r}")
        blob = (str(error.get("message", "")) + str(error.get("hint", ""))).lower()
        self.assertIn(
            "auth", blob,
            f"the auth-failure hint names the fix (gh auth login): {error!r}",
        )
        self.assertIn(
            "auth login", blob,
            f"the auth-failure hint names 'gh auth login' verbatim: {error!r}",
        )
        self.assertEqual(synced, [], "auth failure must not attempt the sync")

        # distinct coded errors (plan-critic R1): the two preflight arms
        # must answer DIFFERENT error codes so callers can distinguish a
        # missing gh from an unauthenticated one.
        codes = {code_a, code_b}
        self.assertEqual(
            len(codes), 1,
            "sanity: both arms exit nonzero (process codes {code_a, code_b})",
        )
        err_a = env_a.get("error", {}).get("code")
        err_b = env_b.get("error", {}).get("code")
        self.assertTrue(err_a and err_b, "both arms answer coded errors")
        self.assertNotEqual(
            err_a, err_b,
            f"gh-missing and auth-failure must carry DISTINCT error codes"
            f" (got {err_a!r} for both)",
        )

        # ---- (c) healthy: preflight + sync + MCP line ----------------------
        db_c = self.root / "init-c.db"
        code, env = self._run_init(db_c, _FakeGh(), fake_sync_core)
        self.assertEqual(code, 0, f"healthy init exits 0: {env}")
        self.assertTrue(env.get("ok"), env)
        self.assertEqual(
            synced, [REPO], "init builds the store through the CLI sync path"
        )
        data = env.get("data")
        self.assertIsInstance(data, dict, "the init payload is an object")
        self.assertIn(
            MCP_LINE,
            json.dumps(data),
            f"the init envelope payload must carry the MCP registration"
            f" line {MCP_LINE!r} (payload keys: {sorted(data)})",
        )
        self.assertTrue(
            db_c.is_file(),
            "init builds the store at the resolved database path",
        )


# ==== C17: packaging metadata ===============================================


class PackagingMetadataTests(unittest.TestCase):
    def test_packaging_metadata(self) -> None:
        """RED at base (license is a table, no classifiers, README has no
        one-line install), GREEN after the fix. pyproject carries an SPDX
        license STRING (PEP 639), Python version classifiers through the
        supported range, and README.md's Install section offers the
        one-line pipx/uvx install from the repository URL."""
        project = _pyproject().get("project", {})

        license_field = project.get("license")
        self.assertIsInstance(
            license_field, str,
            f"license must be an SPDX expression STRING (PEP 639),"
            f" not a table; got {license_field!r}",
        )
        self.assertTrue(
            license_field.strip(),
            "the SPDX license expression must be non-empty",
        )
        self.assertRegex(
            license_field,
            r"^[A-Za-z0-9.()+-]+$",
            f"{license_field!r} must look like an SPDX identifier"
            f" (no prose, no URL)",
        )

        classifiers = project.get("classifiers")
        self.assertIsInstance(
            classifiers, list, f"classifiers must be a list; got {classifiers!r}"
        )
        self.assertTrue(classifiers, "classifiers must be non-empty")
        self.assertIn(
            "Programming Language :: Python :: 3.11",
            classifiers,
            "the supported-Python floor (3.11) must be classified",
        )
        self.assertTrue(
            any(
                re.fullmatch(r"Programming Language :: Python :: 3\.1[2-4]", c)
                for c in classifiers
            ),
            f"classifiers must cover the supported range through 3.14:"
            f" {classifiers}",
        )
        # PEP 639 (post-publication CI failure on this PR): setuptools >= 77
        # REFUSES to build a project that carries BOTH an SPDX license
        # expression and a `License ::` classifier ("License classifiers have
        # been superseded by license expressions"). The SPDX expression IS the
        # license, so no license classifier may ride beside it — without this
        # pin the combination fails only inside `pip install -e .`, which the
        # suite never runs.
        license_classifiers = [c for c in classifiers if c.startswith("License ::")]
        self.assertEqual(
            license_classifiers,
            [],
            "a `License ::` classifier is superseded by the SPDX license"
            f" expression (PEP 639) and makes the build fail:"
            f" {license_classifiers}",
        )

        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "git+https://github.com/ZaxbyHub/zaxbygraph",
            readme,
            "README's Install section must offer the one-line install from"
            " the repository URL",
        )
        self.assertTrue(
            "pipx" in readme or "uvx" in readme,
            "the one-line install must be a pipx/uvx command (no editable"
            " pip install as the only path)",
        )


# ==== C18: the skill ships with the package =================================


class PackagedSkillTests(_HermeticCliTest):
    """The packaged skill is src/zaxbygraph/skills/zaxbygraph/SKILL.md
    (inside the package, shipped as package data); parity with the repo
    copy skills/zaxbygraph/SKILL.md is pinned so the two can never
    drift."""

    @property
    def packaged(self) -> Path:
        return (
            Path(zaxbygraph.__file__).resolve().parent
            / "skills" / "zaxbygraph" / "SKILL.md"
        )

    def test_skill_ships_with_the_package(self) -> None:
        """RED at base (no packaged skill, no package-data entry, no skill
        subcommand), GREEN after the fix. The package CONTAINS the skill;
        ``zaxbygraph skill`` prints it; ``zaxbygraph skill --install DIR``
        writes DIR/zaxbygraph/SKILL.md stamped with the package version
        and the skill body's sha256; the packaged copy is byte-identical
        to skills/zaxbygraph/SKILL.md."""
        # (a) the packaged file exists inside the package
        self.assertTrue(
            self.packaged.is_file(),
            f"the skill must ship inside the package at"
            f" {self.packaged}",
        )
        packaged_text = self.packaged.read_text(encoding="utf-8")

        # (b) pyproject package-data names it (a wheel carries it)
        pkg_data = (
            _pyproject()
            .get("tool", {})
            .get("setuptools", {})
            .get("package-data", {})
            .get("zaxbygraph", [])
        )
        self.assertTrue(
            any("SKILL.md" in str(entry) and "skills" in str(entry)
                for entry in pkg_data),
            f"pyproject package-data must include the packaged skill"
            f" (got {pkg_data!r})",
        )

        # (c) parity: packaged copy == the repo skill file, byte for byte
        repo_skill = REPO_ROOT / "skills" / "zaxbygraph" / "SKILL.md"
        self.assertTrue(repo_skill.is_file(), "skills/zaxbygraph/SKILL.md missing")
        self.assertEqual(
            self.packaged.read_bytes(),
            repo_skill.read_bytes(),
            "the packaged skill and skills/zaxbygraph/SKILL.md must not"
            " differ",
        )

        # (d) `zaxbygraph skill` prints it. Under piped stdio the CLI's
        # default is the JSON envelope, so accept either design: the raw
        # text on stdout, or the skill content riding inside an envelope
        # (probed via distinctive safe lines that survive JSON escaping).
        code, out, err = _run_cli_inproc(["skill"])
        self.assertEqual(
            code, 0, f"`zaxbygraph skill` must succeed (exit {code};"
                     f" stderr {err.strip()[:160]!r})"
        )
        probe_lines = [
            ln
            for ln in packaged_text.splitlines()
            if len(ln.strip()) > 40
            and not any(ch in ln for ch in "\"'\\")
        ]
        self.assertTrue(
            probe_lines, "fixture sanity: the packaged skill carries"
                         " distinctive prose lines"
        )
        printed_raw = packaged_text.strip() in out or probe_lines[0] in out
        printed_envelope = False
        try:
            env_print = json.loads(out)
            printed_envelope = isinstance(env_print, dict) and (
                probe_lines[0] in json.dumps(env_print)
            )
        except ValueError:
            printed_envelope = False
        self.assertTrue(
            printed_raw or printed_envelope,
            "`zaxbygraph skill` prints the packaged skill content (raw or"
            " inside its envelope)",
        )

        # (e) `skill --install DIR` writes the stamped copy
        target_dir = self.root / "claude-skills"
        code, out, err = _run_cli_inproc(["skill", "--install", str(target_dir)])
        self.assertEqual(
            code, 0,
            f"`zaxbygraph skill --install DIR` must succeed (exit {code};"
            f" stderr {err.strip()[:160]!r})",
        )
        written = target_dir / "zaxbygraph" / "SKILL.md"
        self.assertTrue(
            written.is_file(),
            f"--install must write DIR/zaxbygraph/SKILL.md (got exit 0 and"
            f" stdout {out[:200]!r})",
        )
        installed_text = written.read_text(encoding="utf-8")
        m = None
        for m in _STAMP_RE.finditer(installed_text):
            pass
        self.assertIsNotNone(
            m, f"the installed copy must carry the version+hash stamp"
               f" <!-- zaxbygraph skill-version: <version> hash=<64-hex> -->"
               f" (first 300 chars: {installed_text[:300]!r})",
        )
        self.assertEqual(
            m.group("version"), zaxbygraph.__version__,
            "the stamp names the package version",
        )
        stamped_hash = (m.group("hash") or "").lower()
        self.assertRegex(
            stamped_hash, r"^[0-9a-f]{64}$", "the stamp's hash is 64 hex chars"
        )
        # the hash covers the BODY (the file minus its stamp line); accept
        # the trailing-newline-preserving and the stripped form alike —
        # the pinned property is hash == sha256(body), not one byte-exact
        # body rendering.
        body = _skill_body(installed_text)
        body_candidates = {
            hashlib.sha256(b.encode("utf-8")).hexdigest()
            for b in (body, body.rstrip("\n") + "\n", body.rstrip("\n"))
        }
        self.assertIn(
            stamped_hash,
            body_candidates,
            "the stamp's hash must equal sha256 of the installed body"
            " (the file minus the stamp line)",
        )
        # and that body IS the packaged skill's body (hash equality is the
        # strongest form: same bytes)
        packaged_body = _skill_body(packaged_text)
        packaged_candidates = {
            hashlib.sha256(b.encode("utf-8")).hexdigest()
            for b in (packaged_body, packaged_body.rstrip("\n") + "\n",
                      packaged_body.rstrip("\n"))
        }
        self.assertTrue(
            stamped_hash in packaged_candidates
            or body_candidates & packaged_candidates,
            "the installed body must be the packaged skill's body",
        )


# ==== C19: stale skill copies are reported ==================================


class StaleSkillReportTests(_HermeticCliTest):
    """Two --dir-scanned copies: a STALE one (body differs from the
    packaged skill) and a CURRENT one (the packaged body + a valid stamp,
    exactly what a fresh --install writes). Classification is by BODY
    hash, so both are constructed with stamps — only the stale copy's
    BODY differs."""

    def _packaged_text(self) -> str:
        packaged = (
            Path(zaxbygraph.__file__).resolve().parent
            / "skills" / "zaxbygraph" / "SKILL.md"
        )
        self.assertTrue(
            packaged.is_file(),
            "the packaged skill must exist (ships with the package)",
        )
        return packaged.read_text(encoding="utf-8")

    def _current_copy_text(self, packaged_text: str) -> str:
        body = _skill_body(packaged_text)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        stamp = (
            f"<!-- zaxbygraph skill-version: {zaxbygraph.__version__}"
            f" hash={digest} -->"
        )
        return stamp + "\n" + body

    def test_stale_skill_copies_are_reported(self) -> None:
        """RED at base (no skill subcommand), GREEN after the fix.
        ``zaxbygraph skill --check --dir DIR`` reports each found copy's
        freshness BY BODY HASH against the packaged skill: the stale copy
        is reported stale with a refresh command naming --install; the
        current copy is reported current; and --check writes NOTHING (a
        later --install is the write path). Without --dir the check scans
        the four documented DEFAULT skill homes (round 7: ~/.claude,
        ~/.agents, ~/.zcode, ~/.opencode under a patched Path.home —
        stale copies in two, current bodies in the others — all four
        reported with the right classification), and ``doctor`` reports
        the SAME skill-copies staleness section over the same dirs; the
        nothing-written contract holds for both."""
        packaged_text = self._packaged_text()

        stale_root = self.root / "scan-stale"
        current_root = self.root / "scan-current"
        stale_copy = stale_root / "zaxbygraph" / "SKILL.md"
        current_copy = current_root / "zaxbygraph" / "SKILL.md"
        stale_copy.parent.mkdir(parents=True, exist_ok=True)
        current_copy.parent.mkdir(parents=True, exist_ok=True)
        # stale: SAME stamp shape, DIFFERENT body (classification must be
        # by body hash, and body differences are what staleness means).
        stale_text = self._current_copy_text(packaged_text) + "\nstale drift line\n"
        stale_copy.write_text(stale_text, encoding="utf-8")
        current_copy.write_text(
            self._current_copy_text(packaged_text), encoding="utf-8"
        )
        stale_before = stale_copy.read_bytes()
        current_before = current_copy.read_bytes()

        def _check(dir_path: Path) -> object:
            """Run `skill --check --dir DIR`; return the payload object —
            the envelope's data when stdout is an envelope, else the raw
            text (both designs are accepted for the new command)."""
            code, out, err = _run_cli_inproc(
                ["skill", "--check", "--dir", str(dir_path)]
            )
            self.assertEqual(
                code, 0,
                f"`skill --check --dir` must succeed (exit {code}; stderr"
                f" {err.strip()[:160]!r})",
            )
            try:
                env = json.loads(out)
            except ValueError:
                self.fail(
                    f"skill --check must answer one JSON envelope (got"
                    f" exit {code}, stdout {out[:160]!r})"
                )
            self.assertTrue(env.get("ok"), env)
            return env.get("data") if isinstance(env.get("data"), dict) else env

        def _records(payload: object, root: Path) -> list[list[str]]:
            """Every per-copy RECORD naming a skill copy under `root`: each
            dict (or bare string) in the payload whose flattened string
            values contain the scanned root path contributes its full
            flattened string set (parsed strings — Windows path
            backslashes never match a raw JSON dump; and the record's
            sibling fields — status, refresh command — travel with it)."""
            needle = str(root)
            needle_alt = str(root).replace("\\", "/")
            records: list[list[str]] = []

            def _flat_strings(node) -> list[str]:
                if isinstance(node, str):
                    return [node]
                if isinstance(node, dict):
                    out: list[str] = []
                    for v in node.values():
                        out.extend(_flat_strings(v))
                    return out
                if isinstance(node, list):
                    out = []
                    for v in node:
                        out.extend(_flat_strings(v))
                    return out
                return []

            def _walk(node) -> None:
                if isinstance(node, dict):
                    strings = _flat_strings(node)
                    if any(needle in s or needle_alt in s for s in strings):
                        records.append(strings)
                    else:
                        for v in node.values():
                            _walk(v)
                elif isinstance(node, list):
                    for v in node:
                        _walk(v)
                elif isinstance(node, str) and (needle in node or needle_alt in node):
                    records.append([node])

            _walk(payload)
            return records

        # ---- the stale copy ------------------------------------------------
        payload = _check(stale_root)
        records = _records(payload, stale_root)
        self.assertTrue(
            records,
            f"the --dir copy must be reported in the check payload:"
            f" {json.dumps(payload)[:400]}",
        )
        self.assertTrue(
            any("stale" in " ".join(r).lower() for r in records),
            f"the body-hash-differing copy must be classified stale:"
            f" {records[:3]}",
        )
        self.assertTrue(
            any("--install" in " ".join(r) for r in records),
            f"the stale report carries the refresh command naming"
            f" --install: {records[:3]}",
        )

        # ---- the current copy ----------------------------------------------
        payload2 = _check(current_root)
        records2 = _records(payload2, current_root)
        self.assertTrue(
            records2,
            f"the current --dir copy must be reported: "
            f"{json.dumps(payload2)[:400]}",
        )
        self.assertTrue(
            any("current" in " ".join(r).lower() for r in records2),
            f"a copy whose body hash equals the packaged body must be"
            f" classified current: {records2[:3]}",
        )
        self.assertFalse(
            any(
                "stale" in " ".join(r).lower() and "current" not in
                " ".join(r).lower()
                for r in records2
            ),
            f"the current copy's record must not be reported stale:"
            f" {records2[:3]}",
        )

        # ---- --check writes NOTHING ----------------------------------------
        self.assertEqual(
            stale_copy.read_bytes(), stale_before,
            "skill --check must not rewrite the stale copy (only"
            " --install writes)",
        )
        self.assertEqual(
            current_copy.read_bytes(), current_before,
            "skill --check must not rewrite the current copy",
        )

        # ---- the four documented DEFAULT dirs (round 7) --------------------
        # `skill --check` with NO --dir scans the four documented default
        # skill homes — ~/.claude, ~/.agents, ~/.zcode, ~/.opencode (each
        # <home>/.<tool>/skills/zaxbygraph/SKILL.md) — and `doctor` reports
        # the SAME skill-copies staleness section. Hermetic: Path.home is
        # patched to a temp home (nothing real is scanned), and every copy's
        # bytes are snapshotted so the nothing-written contract is proved on
        # this surface too.
        tmp_home = self.root / "default-homes"
        default_copies: dict[str, Path] = {}
        for tool in ("claude", "agents", "zcode", "opencode"):
            copy_path = tmp_home / f".{tool}" / "skills" / "zaxbygraph" / "SKILL.md"
            copy_path.parent.mkdir(parents=True, exist_ok=True)
            default_copies[tool] = copy_path
        stale_tools = ("claude", "agents")
        current_tools = ("zcode", "opencode")
        for tool, copy_path in default_copies.items():
            text = self._current_copy_text(packaged_text)
            if tool in stale_tools:
                text += "\nstale drift line\n"
            copy_path.write_text(text, encoding="utf-8")
        default_before = {
            tool: path.read_bytes() for tool, path in default_copies.items()
        }

        with mock.patch.object(Path, "home", return_value=tmp_home):
            # `skill --check` WITHOUT --dir: all four default copies are
            # reported — the stale two classified stale, the current two
            # current.
            code_nd, out_nd, err_nd = _run_cli_inproc(["skill", "--check"])
            self.assertEqual(
                code_nd, 0,
                "`skill --check` (no --dir) must succeed"
                f" (exit {code_nd}; stderr {err_nd.strip()[:160]!r})",
            )
            env_nd = json.loads(out_nd)
            self.assertTrue(env_nd.get("ok"), env_nd)
            payload_nd = (
                env_nd.get("data")
                if isinstance(env_nd.get("data"), dict)
                else env_nd
            )
            for tool in stale_tools:
                records_nd = _records(payload_nd, default_copies[tool].parent)
                self.assertTrue(
                    records_nd,
                    f"the default {tool} copy must be reported by the"
                    f" default-dirs check: {json.dumps(payload_nd)[:400]}",
                )
                self.assertTrue(
                    any("stale" in " ".join(r).lower() for r in records_nd),
                    f"the stale default {tool} copy must be classified"
                    f" stale: {records_nd[:3]}",
                )
            for tool in current_tools:
                records_nd = _records(payload_nd, default_copies[tool].parent)
                self.assertTrue(
                    records_nd,
                    f"the current default {tool} copy must be reported by"
                    f" the default-dirs check:"
                    f" {json.dumps(payload_nd)[:400]}",
                )
                self.assertTrue(
                    any("current" in " ".join(r).lower() for r in records_nd),
                    f"the current default {tool} copy must be classified"
                    f" current: {records_nd[:3]}",
                )
                self.assertFalse(
                    any(
                        "stale" in " ".join(r).lower()
                        and "current" not in " ".join(r).lower()
                        for r in records_nd
                    ),
                    f"the current default {tool} copy must not be reported"
                    f" stale: {records_nd[:3]}",
                )

            # doctor reports the SAME skill-copies staleness section: the
            # packaged skill is present (asserted at the top of this
            # check), so doctor's payload must carry the section with the
            # same classifications over the same four default dirs.
            doctor_db = self.root / "doctor-scan.db"
            code_d, out_d, err_d = _run_cli_inproc(
                [
                    "doctor", "--repo", REPO, "--db", str(doctor_db),
                    "--format", "json",
                ]
            )
            self.assertEqual(
                code_d, 0,
                "doctor must succeed with the packaged skill present"
                f" (exit {code_d}; stderr {err_d.strip()[:160]!r})",
            )
            env_d = json.loads(out_d)
            self.assertTrue(env_d.get("ok"), env_d)
            payload_d = (
                env_d.get("data")
                if isinstance(env_d.get("data"), dict)
                else env_d
            )
            for tool in stale_tools:
                records_d = _records(payload_d, default_copies[tool].parent)
                self.assertTrue(
                    records_d,
                    f"doctor's payload must report the default {tool} skill"
                    " copy (the skill-copies staleness section):"
                    f" {json.dumps(payload_d)[:400]}",
                )
                self.assertTrue(
                    any("stale" in " ".join(r).lower() for r in records_d),
                    f"doctor must classify the stale default {tool} copy"
                    f" stale: {records_d[:3]}",
                )
            for tool in current_tools:
                records_d = _records(payload_d, default_copies[tool].parent)
                self.assertTrue(
                    records_d,
                    f"doctor's payload must report the current default"
                    f" {tool} skill copy: {json.dumps(payload_d)[:400]}",
                )
                self.assertTrue(
                    any("current" in " ".join(r).lower() for r in records_d),
                    f"doctor must classify the current default {tool} copy"
                    f" current: {records_d[:3]}",
                )

        # neither the default-dirs check nor doctor wrote anything: the
        # write path stays opt-in (--install only).
        for tool, path in default_copies.items():
            self.assertEqual(
                path.read_bytes(), default_before[tool],
                f"neither skill --check nor doctor may rewrite the {tool}"
                " default copy (only --install writes)",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
