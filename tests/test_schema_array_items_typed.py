"""PR guardrail for the 92-93 output-contract work (plan round-5
resolution, critic round 6 APPROVE): every array-typed node in EVERY
emitted schema declares TYPED items.

The frozen sweeps (tests/test_output_contract_92_93_c.py) validate real
envelopes against the emitted `schema --output COMMAND` full-envelope
schemas, but a schema whose rows are typed objects can still leave an
ARRAY-valued property inside those rows untyped (the round-5 survivor:
removing `items` only from `item`'s
`data.items.properties.labels` passed C10+C11). This guardrail walks
EVERY emitted CLI `schema --output <command>` schema and EVERY MCP
outputSchema (built from the same registry) at ALL nesting levels and
requires:

- every node whose declared type includes "array" carries a non-empty
  `items` object whose own type is a recognized JSON-Schema name (the
  SQL positional-cell union ["string", "number", "boolean", "null"]
  qualifies; a description-only or absent declaration does not);
- a data schema that permits an array (a list payload or the `item`
  object|array|null union) declares its rows `type: object`;
- the walker actually visits the tree (a pinned floor on visited dict
  nodes — a mutation that disconnects the walker cannot pass vacuously).

It carries a mutation self-test: a deep copy of the emitted `item`
schema with `items` removed ONLY from `data.items.properties.labels`
must be rejected naming that path, proving the walker discriminates the
round-5 survivor shape.

Hermetic: FakeGitHubSource-seeded temp store, no network, no gh, no
writes outside temp roots. Stdlib only; unittest; Windows + Linux.
"""

from __future__ import annotations

import copy
import os
import json
import tempfile
import unittest
from pathlib import Path

import zaxbygraph

_WORKTREE = Path(__file__).resolve().parents[1]
if not Path(zaxbygraph.__file__).resolve().is_relative_to(_WORKTREE / "src"):
    raise AssertionError(
        f"SAIT-ENV: zaxbygraph resolved to {zaxbygraph.__file__}, not this"
        f" worktree's src ({_WORKTREE / 'src'}); set"
        f" PYTHONPATH=<worktree>/src{os.pathsep}<worktree>/tests"
    )

from fixtures import REPO  # noqa: E402
from test_mcp import pin_env_for, run_cli  # noqa: E402
from test_output_contract_92_93_e import seed_corpus  # noqa: E402
from zaxbygraph import mcp_server  # noqa: E402

_KNOWN_TYPES = frozenset(
    {"object", "array", "string", "integer", "boolean", "number", "null"}
)
#: The walker must visit at least this many dict nodes across the emitted
#: schemas (39 schemas emitted ~1.3k nodes when pinned; a disconnected or
#: vacuous walker stays far below).
_MIN_VISITED_NODES = 500
#: Node keys that never carry schema structure.
_NON_SCHEMA_KEYS = frozenset({"description", "$schema", "default", "examples"})


def _type_names(node) -> list:
    declared = node.get("type") if isinstance(node, dict) else None
    return declared if isinstance(declared, list) else ([declared] if declared else [])


def array_items_defects(schema, path: str, stats: dict) -> list:
    """Every reason `schema` leaves an array untyped: recursively, every
    array-typed node must declare a non-empty `items` object with a real
    type, and a data schema permitting an array must type its rows
    object. `stats["visited"]` counts the dict nodes the walker reached
    (the vacuity guard)."""
    defects: list = []
    if not isinstance(schema, dict):
        return [f"{path}: schema root is not an object"]
    # The list/union data-row rule: `data` itself (a list payload, or the
    # item object|array|null union) declares its rows as objects.
    data = (schema.get("properties") or {}).get("data")
    if isinstance(data, dict) and ("array" in _type_names(data) or "items" in data):
        items = data.get("items")
        if not isinstance(items, dict) or "object" not in _type_names(items):
            defects.append(
                f"{path}.data.items: a list/union data schema must declare"
                f" its rows type object; got {items!r}"
            )

    def walk(node, at: str, in_properties: bool = False) -> None:
        if isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{at}[{index}]", in_properties)
            return
        if not isinstance(node, dict):
            return
        stats["visited"] += 1
        names = _type_names(node)
        unknown = [name for name in names if name not in _KNOWN_TYPES]
        if unknown:
            defects.append(f"{at}: unrecognized schema type name(s) {unknown}")
        if "array" in names:
            items = node.get("items")
            if not isinstance(items, dict) or not items:
                defects.append(
                    f"{at}: an array-typed node must declare a non-empty"
                    f" items object; got {items!r}"
                )
            elif not _type_names(items) or any(
                name not in _KNOWN_TYPES for name in _type_names(items)
            ):
                defects.append(
                    f"{at}.items: items must declare a real type; got {items!r}"
                )
        for key, child in node.items():
            if key in _NON_SCHEMA_KEYS and not in_properties:
                # PR #98 review P11: a key with a non-schema NAME still
                # carries schema when its parent is a `properties` map —
                # a property may be named "description". Only skip these
                # names where they are actual annotations.
                continue
            walk(child, f"{at}.{key}" if at else key, in_properties=key == "properties")

    walk(schema, path)
    return defects


class _SeededStoreCase(unittest.TestCase):
    """One seeded store per class (emitting every schema is the expensive
    part; the mutations run on deep copies)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls._td.name)
        cls.db = cls.root / "history.db"
        seed_corpus(cls.db)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._td.cleanup()

    def setUp(self) -> None:
        pin_env_for(self, self.root, db=self.db)
        self.base = ["--repo", REPO, "--db", str(self.db), "--format", "json"]

    def _emitted(self, command: str) -> dict:
        code, out, err = run_cli(["schema", "--output", command, *self.base])
        self.assertEqual(
            code, 0, f"SAIT: schema --output {command} must succeed: {err[:160]}"
        )
        schema = (json.loads(out).get("data") or {}).get("schema")
        self.assertIsInstance(
            schema,
            dict,
            f"SAIT: schema --output {command} must carry data['schema']",
        )
        return schema


class TestSchemaArrayItemsTyped(_SeededStoreCase):
    def test_every_emitted_array_declares_typed_items(self) -> None:
        # Every emitted CLI schema and every MCP outputSchema, walked at
        # every nesting level: an array-typed node without typed items is
        # a defect, and the walk must actually visit the trees.
        stats = {"visited": 0}
        problems: list = []
        schema_count = 0
        for command in sorted(mcp_server.CLI_OUTPUT_SHAPES):
            schema_count += 1
            problems += array_items_defects(
                self._emitted(command), f"CLI:{command}", stats
            )
        for tool in mcp_server._TOOLS:
            name = tool.get("name")
            schema_count += 1
            problems += array_items_defects(
                mcp_server._output_schema_for(name), f"MCP:{name}", stats
            )
        self.assertGreaterEqual(
            schema_count,
            len(mcp_server.CLI_OUTPUT_SHAPES) + len(mcp_server._TOOLS),
            "SAIT: fixture sanity — every registry schema must be swept",
        )
        self.assertGreaterEqual(
            stats["visited"],
            _MIN_VISITED_NODES,
            f"SAIT: vacuity guard — the walker visited only"
            f" {stats['visited']} nodes across {schema_count} schemas"
            f" (floor {_MIN_VISITED_NODES}); the sweep went vacuous",
        )
        self.assertFalse(
            problems,
            "SAIT: every array-typed node in every emitted schema must"
            " declare typed items (list/union data rows type object):\n- "
            + "\n- ".join(problems[:30]),
        )

    def test_item_row_labels_items_mutation_is_rejected(self) -> None:
        # The round-5 survivor, as a self-test: removing `items` ONLY from
        # the emitted item schema's data.items.properties.labels must be a
        # defect naming that path (a deep copy — production is untouched).
        schema = copy.deepcopy(self._emitted("item"))
        row_labels = (
            (schema.get("properties") or {}).get("data", {}).get("items", {})
            .get("properties", {}).get("labels")
        )
        self.assertIsInstance(
            row_labels, dict, "SAIT: fixture sanity — item row labels exist"
        )
        self.assertIn("items", row_labels)
        row_labels.pop("items")
        problems = array_items_defects(schema, "selftest:item", {"visited": 0})
        self.assertTrue(
            any("data.items.properties.labels" in p for p in problems),
            f"SAIT: SELF-TEST — the labels items mutation (the round-5"
            f" survivor) must be rejected naming the path; got {problems!r}",
        )

    def test_property_named_description_is_still_walked(self) -> None:
        # PR #98 review P11: `description` is a schema ANNOTATION name, but
        # a property NAMED description inside a properties map is a real
        # declaration — an array-typed one without items must be flagged.
        schema = {
            "type": "object",
            "properties": {
                "data": {
                    "type": "object",
                    "properties": {"description": {"type": "array"}},
                }
            },
        }
        problems = array_items_defects(schema, "selftest", {"visited": 0})
        self.assertTrue(
            any(
                "data.properties.description" in p and "items" in p
                for p in problems
            ),
            f"SAIT: SELF-TEST — an array property named 'description' with"
            f" no items must be rejected naming the path; got {problems!r}",
        )


if __name__ == "__main__":
    unittest.main()
