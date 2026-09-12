from __future__ import annotations

import unittest

from zaxbygraph.extract import (
    collapse_edges,
    edges_from_item,
    edges_from_review,
    parse_closing_numbers,
    parse_mentioned_numbers,
)

REPO = "acme/forgegate"


class ClosingTests(unittest.TestCase):
    def test_bare_hash(self) -> None:
        self.assertEqual(parse_closing_numbers("fixes #12", REPO), {12})
        self.assertEqual(parse_closing_numbers("Closes #3 and resolves #4", REPO), {3, 4})

    def test_same_repo_url(self) -> None:
        text = "close https://github.com/acme/forgegate/issues/9"
        self.assertEqual(parse_closing_numbers(text, REPO), {9})
        text = "fixed https://github.com/acme/forgegate/pull/11"
        self.assertEqual(parse_closing_numbers(text, REPO), {11})

    def test_owner_repo_shorthand(self) -> None:
        self.assertEqual(parse_closing_numbers("fixes acme/forgegate#8", REPO), {8})

    def test_cross_repo_url_does_not_attach_locally(self) -> None:
        text = "closes https://github.com/OTHER/repo/issues/5"
        self.assertEqual(parse_closing_numbers(text, REPO), set())

    def test_cross_repo_shorthand_ignored(self) -> None:
        self.assertEqual(parse_closing_numbers("fixes other/repo#5", REPO), set())

    def test_cross_repo_does_not_poison_bare_hash(self) -> None:
        text = "closes https://github.com/OTHER/repo/issues/5 and also fixes #7"
        self.assertEqual(parse_closing_numbers(text, REPO), {7})


class MentionTests(unittest.TestCase):
    def test_bare_and_url(self) -> None:
        text = "see #2 and https://github.com/acme/forgegate/issues/3"
        self.assertEqual(parse_mentioned_numbers(text, REPO), {2, 3})

    def test_foreign_url_not_mentioned(self) -> None:
        text = "see https://github.com/OTHER/repo/issues/5 and #9"
        self.assertEqual(parse_mentioned_numbers(text, REPO), {9})

    def test_self_mention_dropped_by_edge_builder(self) -> None:
        raw = {
            "number": 4,
            "user": {"login": "alice"},
            "body": "see #4 and #5",
            "labels": [],
        }
        rels = {(e[2], e[4]) for e in edges_from_item(REPO, raw)}
        self.assertNotIn(("mentions", "4"), rels)
        self.assertIn(("mentions", "5"), rels)
        self.assertIn(("authored", "4"), {(e[2], e[4]) for e in edges_from_item(REPO, raw)})

    def test_closes_does_not_also_mention(self) -> None:
        raw = {
            "number": 3,
            "user": {"login": "alice"},
            "body": "Closes #1. See also #2.",
            "labels": [],
        }
        rels = {(e[2], e[4]) for e in edges_from_item(REPO, raw)}
        self.assertIn(("closes", "1"), rels)
        self.assertNotIn(("mentions", "1"), rels)
        self.assertIn(("mentions", "2"), rels)


class ReviewBodyTests(unittest.TestCase):
    def test_review_body_closes(self) -> None:
        raw = {"id": 99, "user": {"login": "bob"}, "state": "APPROVED", "body": "closes #12"}
        edges = edges_from_review(REPO, 3, raw)
        rels = {(e[2], e[4]) for e in edges}
        self.assertIn(("closes", "12"), rels)
        self.assertNotIn(("mentions", "12"), rels)
        self.assertIn(("reviewed", "3"), {(e[2], e[4]) for e in edges})


class CollapseTests(unittest.TestCase):
    def test_last_write_wins(self) -> None:
        edges = [
            ("actor", "alice", "commented", "item", "1", "comment:1"),
            ("actor", "alice", "commented", "item", "1", "comment:2"),
        ]
        out = collapse_edges(edges)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][5], "comment:2")


if __name__ == "__main__":
    unittest.main()
