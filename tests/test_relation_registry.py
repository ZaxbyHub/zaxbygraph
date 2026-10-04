"""Phase 4.2 guardrail (issue #6): the relation vocabulary cannot drift.

Every relation any extractor emits (extract.RELATIONS) must be either
structural for the `path` BFS (query.STRUCTURAL) or on the documented
exclusion list (query.PATH_EXCLUDED_RELATIONS), and every structural relation
must appear in the docs relationships table. Mutation probe: removing
`closes_keyword` from query.STRUCTURAL fails this module; so does adding a
relation to extract without documenting or classifying it.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from zaxbygraph.extract import RELATIONS
from zaxbygraph.query import PATH_EXCLUDED_RELATIONS, STRUCTURAL

_DOCS = Path(__file__).resolve().parent.parent / "docs" / "schema.md"


class RelationRegistryTests(unittest.TestCase):
    def test_every_emitted_relation_is_classified(self) -> None:
        self.assertEqual(
            RELATIONS,
            STRUCTURAL | PATH_EXCLUDED_RELATIONS,
            "a relation was emitted by extract without a path classification "
            "(add it to query.STRUCTURAL or query.PATH_EXCLUDED_RELATIONS) "
            "or a classified relation is no longer emitted (prune it)",
        )

    def test_structural_and_excluded_are_disjoint(self) -> None:
        self.assertFalse(
            STRUCTURAL & PATH_EXCLUDED_RELATIONS,
            "a relation cannot be both structural and excluded",
        )

    def test_docs_relationships_table_covers_every_relation(self) -> None:
        text = _DOCS.read_text(encoding="utf-8")
        documented = set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", text, re.M))
        missing = sorted(RELATIONS - documented)
        self.assertEqual(
            missing,
            [],
            "relations missing from the docs/schema.md relationships table",
        )


if __name__ == "__main__":
    unittest.main()
