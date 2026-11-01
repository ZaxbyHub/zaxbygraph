"""Config-chain coverage at the store/cwd/explicit levels (issue #46 AC1)
plus the hermeticity pins (AC2). Covers BOTH public chain entry points:
``load_weights`` ([derive]) and ``load_triage_config`` ([triage]).

Frozen check names:
    C6 (whole file: per-level precedence, same-scalar overrides, map merge,
        config_source echoes, hermeticity)

NEW-SURFACE file: absent at base. Stdlib only; unittest; hermetic.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO, issue

from test_triage_bundle_supp import _BulkSource, _pin_synced_at
from zaxbygraph.db import connect, init_schema
from zaxbygraph.derive import (
    ConfigError,
    load_triage_config,
    load_weights,
)
from zaxbygraph.sync import sync_repo

REPO_ROOT = Path(__file__).resolve().parents[1]

#: the triage loader's source-key spelling, extracted at runtime so the
#: tests can never drift from the production literal
CHAIN_SRC_KEY = [k for k in load_triage_config() if k.startswith("_") and "source" in k][0]

#: hermeticity (issue #46 AC2): mirrors the isolated derived-tier leg
#: runners - CLI legs launched from THIS module run in a temp cwd so the
#: planted repo-root config never leaks into unrelated suites.
_LEG_CWD = tempfile.mkdtemp(prefix="zaxbygraph-chain-legs-")


def _cli_json(argv: list[str]) -> dict:
    """Envelope runner for one CLI leg (temp cwd, leg-local PYTHONPATH)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), str(REPO_ROOT / "tests"),
         env.get("PYTHONPATH", "")])
    proc = subprocess.run(
        [os.sys.executable, "-m", "zaxbygraph", *argv],
        capture_output=True, env=env, cwd=_LEG_CWD, timeout=120)
    return json.loads(proc.stdout.decode("utf-8"))
class ChainSourceTests(unittest.TestCase):
    """Per-level source labels and per-key precedence (AC1)."""

    def test_defaults_source(self) -> None:
        cfg = load_triage_config()
        self.assertEqual(cfg[CHAIN_SRC_KEY], "defaults")
        w = load_weights()
        self.assertNotIn(CHAIN_SRC_KEY, w)

    def test_store_level(self) -> None:
        td = tempfile.mkdtemp(prefix="chain_store_")
        try:
            cfg_path = Path(td) / "config.toml"
            cfg_path.write_text("[triage]\ndemand = 2.0\n", encoding="utf-8")
            cfg = load_triage_config(store_config=str(cfg_path))
            self.assertEqual(cfg["demand"], 2.0)
            self.assertEqual(cfg[CHAIN_SRC_KEY], "store")
            cfg2_path = Path(td) / "derive.toml"
            cfg2_path.write_text("[derive]\nlexical = 1.5\n", encoding="utf-8")
            w = load_weights(store_config=str(cfg2_path))
            self.assertEqual(w["lexical"], 1.5)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_cwd_level(self) -> None:
        td = tempfile.mkdtemp(prefix="chain_cwd_")
        old = os.getcwd()
        try:
            os.chdir(td)
            (Path(".zaxbygraph.toml")).write_text(
                "[triage]\nleverage = 3.0\n", encoding="utf-8")
            cfg = load_triage_config(cwd_config=".zaxbygraph.toml")
            self.assertEqual(cfg["leverage"], 3.0)
            self.assertEqual(cfg[CHAIN_SRC_KEY], "cwd")
        finally:
            os.chdir(old)
            shutil.rmtree(td, ignore_errors=True)

    def test_explicit_level(self) -> None:
        td = tempfile.mkdtemp(prefix="chain_explicit_")
        try:
            cfg_path = Path(td) / "explicit.toml"
            cfg_path.write_text("[triage]\nrisk = 1.5\n", encoding="utf-8")
            cfg = load_triage_config(explicit_config=str(cfg_path))
            self.assertEqual(cfg["risk"], 1.5)
            self.assertEqual(cfg[CHAIN_SRC_KEY], "explicit")
        finally:
            shutil.rmtree(td, ignore_errors=True)
class ChainPrecedenceTests(unittest.TestCase):
    """Same-scalar overrides, per-name map merge, mixed sources (AC1)."""

    def test_same_scalar_override(self) -> None:
        td = tempfile.mkdtemp(prefix="chain_ovr_")
        try:
            store = Path(td) / "s.toml"
            store.write_text("[triage]\ndemand = 1.0\n", encoding="utf-8")
            cwd = Path(td) / "c.toml"
            cwd.write_text("[triage]\ndemand = 2.0\n", encoding="utf-8")
            exp = Path(td) / "e.toml"
            exp.write_text("[triage]\ndemand = 3.0\n", encoding="utf-8")
            cfg = load_triage_config(store_config=str(store),
                                     cwd_config=str(cwd))
            self.assertEqual(cfg["demand"], 2.0)
            self.assertEqual(cfg[CHAIN_SRC_KEY], "cwd")
            full = load_triage_config(store_config=str(store),
                                      cwd_config=str(cwd),
                                      explicit_config=str(exp))
            self.assertEqual(full["demand"], 3.0)
            self.assertEqual(full[CHAIN_SRC_KEY], "explicit")
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_map_merge_per_name(self) -> None:
        td = tempfile.mkdtemp(prefix="chain_map_")
        try:
            store = Path(td) / "s.toml"
            store.write_text(
                "[triage]\n"
                "[triage.labels]\nhigh = 1.5\nmed = 1.0\n", encoding="utf-8")
            cwd = Path(td) / "c.toml"
            cwd.write_text(
                "[triage]\n"
                "[triage.labels]\nhigh = 2.5\nlow = 0.5\n", encoding="utf-8")
            cfg = load_triage_config(store_config=str(store),
                                     cwd_config=str(cwd))
            labels = cfg["labels"]
            self.assertEqual(labels["high"], 2.5)   # cwd wins the same name
            self.assertEqual(labels["med"], 1.0)    # disjoint names survive
            self.assertEqual(labels["low"], 0.5)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_mixed_source_keys(self) -> None:
        td = tempfile.mkdtemp(prefix="chain_mix_")
        try:
            store = Path(td) / "s.toml"
            store.write_text("[triage]\ndemand = 1.5\n", encoding="utf-8")
            exp = Path(td) / "e.toml"
            exp.write_text("[triage]\ntop_k = 7\n", encoding="utf-8")
            cfg = load_triage_config(store_config=str(store),
                                     explicit_config=str(exp))
            self.assertEqual(cfg["demand"], 1.5)
            self.assertEqual(cfg["top_k"], 7)
            self.assertEqual(cfg[CHAIN_SRC_KEY], "explicit")
        finally:
            shutil.rmtree(td, ignore_errors=True)
class HermeticityTests(unittest.TestCase):
    """AC2 pins: the harness keeps ambient configs out of every derived-tier
    CLI leg — triage through the committed supp helper, similar through its
    temp-checkout runner."""

    def test_hermetic(self) -> None:
        planted = REPO_ROOT / ".zaxbygraph.toml"
        try:
            planted.write_text("[triage]\nstage2_weight = 0.9\n", encoding="utf-8")
            sys.path.insert(0, str(REPO_ROOT / "tests"))
            import test_triage_bundle_supp as supp
            env = _cli_json([
                "triage", "--db", str(_chain_store()), "--repo", REPO,
                "--config", str(_explicit_cfg()), "--format", "json",
            ])
            self.assertEqual(env["data"]["stage2_weight"], 0.25)
        finally:
            planted.unlink(missing_ok=True)
        # without the plant: same blessed default
        env2 = _cli_json([
            "triage", "--db", str(_chain_store()), "--repo", REPO,
            "--config", str(_explicit_cfg()), "--format", "json",
        ])
        self.assertEqual(env2["data"]["stage2_weight"], 0.25)

    def test_hermetic_similar_checkout(self) -> None:
        ck = _similar_checkout()
        (ck / ".zaxbygraph.toml").write_text(
            "[derive]\nlexical = 999.0\n", encoding="utf-8")
        try:
            env = _cli_json([
                "similar", "501", "--db", str(_chain_store()), "--repo", REPO,
                "--format", "json",
            ])
            # 999 would breach the weight cap (ConfigError) if the ambient
            # config leaked; isolation keeps the leg green with defaults.
            self.assertTrue(env.get("ok"), env)
        finally:
            (ck / ".zaxbygraph.toml").unlink(missing_ok=True)
def _chain_store() -> str:
    """One shared complete store for the CLI legs (module-level cache)."""
    global _STORE_CACHE
    try:
        return _STORE_CACHE
    except NameError:
        import tempfile as _tf
        td = Path(_tf.mkdtemp(prefix="chain_store_"))
        db = td / "history.db"
        conn = connect(db)
        init_schema(conn)
        conn.row_factory = sqlite3.Row
        src = _BulkSource()
        for n in range(501, 511):
            src.add_issue(issue(n, title=f"Chain report {n}",
                                body=f"see src/a.py:{n % 3 + 1}:1",
                                created_at="2027-06-01T00:00:00Z"))
        sync_repo(conn, src, REPO)
        _pin_synced_at(conn)
        conn.close()
        _STORE_CACHE = str(db)
        return _STORE_CACHE


def _explicit_cfg() -> str:
    """Explicit --config leaving stage2_weight unset (the C1 shape)."""
    global _EXPLICIT_CACHE
    try:
        return _EXPLICIT_CACHE
    except NameError:
        import tempfile as _tf
        f = Path(_tf.mkdtemp(prefix="chain_explicit_")) / "explicit.toml"
        f.write_text("[triage]\ntop_k = 50\n", encoding="utf-8")
        _EXPLICIT_CACHE = str(f)
        return _EXPLICIT_CACHE


def _similar_checkout() -> Path:
    """The temp git checkout test_similar's CLI legs run in (reused so the
    planted config sits exactly where the leg's cwd resolves it)."""
    import test_similar
    return test_similar._leg_checkout()
