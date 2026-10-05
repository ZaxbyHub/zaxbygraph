"""Phase 4.2 guardrail (issue #6): the relation vocabulary cannot drift.

Every relation any extractor emits (extract.RELATIONS) must be either
structural for the `path` BFS (query.STRUCTURAL) or on the documented
exclusion list (query.PATH_EXCLUDED_RELATIONS), and every structural relation
must appear in the docs relationships table. Mutation probe: removing
`closes_keyword` from query.STRUCTURAL fails this module; so does adding a
relation to extract without documenting or classifying it.

Review-round 2 hardening (PRR-021): the emitted-relation set is also DERIVED
by driving every edges_from_* emitter with branch-covering payloads, so a
relation emitted from an emitter without being added to extract.RELATIONS
now fails this module instead of shipping unguarded. The docs check scans
only the Relationships table section, not every table's first column.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from zaxbygraph import extract
from zaxbygraph.extract import RELATIONS
from zaxbygraph.query import PATH_EXCLUDED_RELATIONS, STRUCTURAL

_DOCS = Path(__file__).resolve().parent.parent / "docs" / "schema.md"

REPO = "zaxbyhub/zaxbygraph"


def _derived_relations() -> set[str]:
    """Drive every edges_from_* emitter with payloads that reach all of its
    branches and collect the produced relation names. A NEW relation emitted
    by any branch appears here without anyone editing a registry."""
    out: set[str] = set()
    item = {
        "number": 1,
        "user": {"login": "alice"},
        "labels": [{"name": "bug"}],
        "body": "Fixes #2, see #3",
    }
    out |= {e[2] for e in extract.edges_from_item(REPO, item)}
    comment = {"id": 7, "user": {"login": "bob"}, "body": "fixes #2"}
    out |= {e[2] for e in extract.edges_from_comment(REPO, 1, comment)}
    review = {"id": 8, "user": {"login": "bob"}, "state": "APPROVED", "body": "closes #2"}
    out |= {e[2] for e in extract.edges_from_review(REPO, 1, review)}
    out |= {e[2] for e in extract.edges_from_files(1, [{"filename": "src/a.py"}])}

    def lookup_sha(sha: str):
        return 7 if sha == "abc1234" else None

    timeline = [
        {  # commit closer: closed_by_commit + closes via lookup
            "type": "closed",
            "created_at": "2026-01-01T00:00:00Z",
            "commit_id": "abc1234",
            "closer_type": None,
            "closer_number": None,
        },
        {  # PR closer: closes
            "type": "closed",
            "created_at": "2026-01-01T00:01:00Z",
            "closer_type": "pull_request",
            "closer_number": 9,
        },
        {  # same-repo cross-reference
            "type": "cross_referenced",
            "source_typename": "Issue",
            "source_number": 3,
            "source_repo": "ZaxbyHub/zaxbygraph",
        },
        {  # foreign cross-reference
            "type": "cross_referenced",
            "source_typename": "Issue",
            "source_number": 42,
            "source_repo": "Other/Repo",
        },
    ]
    out |= {
        e[2] for e in extract.edges_from_timeline(REPO, 1, timeline, lookup_sha)
    }

    pr = dict(item)
    pr["title"] = 'Revert "x"'
    pr["merged_at"] = "2026-01-01T00:00:00Z"
    pr["merged_by"] = {"login": "carol"}
    pr["merge_commit_sha"] = "deadbee"
    pr["pull_request"] = {"url": "x"}
    pr["closing_issues_references"] = [{"number": 2, "repo": "ZaxbyHub/zaxbygraph"}]
    pr["body"] = "This reverts commit 24bbcf41c382f429d3cd8ac98de79a83c6deaa3a"
    out |= {e[2] for e in extract.edges_from_pr_state(REPO, pr)}

    pr_title_only = dict(item)
    pr_title_only["title"] = 'Revert "quoted target"'
    pr_title_only["merged_at"] = None
    pr_title_only.pop("body", None)
    pr_title_only["pull_request"] = {"url": "x"}
    out |= {
        e[2]
        for e in extract.edges_from_pr_state(
            REPO, pr_title_only, lambda span: [5]
        )
    }
    return out


class RelationRegistryTests(unittest.TestCase):
    def test_every_emitted_relation_is_classified(self) -> None:
        self.assertEqual(
            RELATIONS,
            STRUCTURAL | PATH_EXCLUDED_RELATIONS,
            "a relation was emitted by extract without a path classification "
            "(add it to query.STRUCTURAL or query.PATH_EXCLUDED_RELATIONS) "
            "or a classified relation is no longer emitted (prune it)",
        )

    def test_derived_relations_are_covered_by_the_registry(self) -> None:
        """PRR-021: drive the emitters and prove every produced relation is a
        registry member — a new relation emitted from any driven branch
        without updating extract.RELATIONS fails here."""
        derived = _derived_relations()
        self.assertGreaterEqual(
            len(derived),
            10,
            "emitter drive must reach every branch to be a real census",
        )
        self.assertEqual(
            derived - RELATIONS,
            set(),
            "an emitter produced a relation missing from extract.RELATIONS — "
            "add it to the frozenset (and to query.STRUCTURAL or "
            "query.PATH_EXCLUDED_RELATIONS, and to the docs table)",
        )

    def test_structural_and_excluded_are_disjoint(self) -> None:
        self.assertFalse(
            STRUCTURAL & PATH_EXCLUDED_RELATIONS,
            "a relation cannot be both structural and excluded",
        )

    def test_docs_relationships_table_covers_every_relation(self) -> None:
        text = _DOCS.read_text(encoding="utf-8")
        section = text.split("### Relationships", 1)[-1]
        section = section.split("\n### ", 1)[0]
        documented = set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", section, re.M))
        missing = sorted(RELATIONS - documented)
        self.assertEqual(
            missing,
            [],
            "relations missing from the docs/schema.md Relationships table",
        )


if __name__ == "__main__":
    unittest.main()
