from __future__ import annotations

import unittest

from zaxbygraph.extract import (
    collapse_edges,
    edges_from_item,
    edges_from_review,
    merge_evidence,
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
        self.assertIn(("closes_keyword", "1"), rels)
        self.assertNotIn(("mentions", "1"), rels)
        self.assertIn(("mentions", "2"), rels)


class ReviewBodyTests(unittest.TestCase):
    def test_review_body_closes(self) -> None:
        raw = {"id": 99, "user": {"login": "bob"}, "state": "APPROVED", "body": "closes #12"}
        edges = edges_from_review(REPO, 3, raw)
        rels = {(e[2], e[4]) for e in edges}
        self.assertIn(("closes_keyword", "12"), rels)
        self.assertNotIn(("mentions", "12"), rels)
        self.assertIn(("reviewed", "3"), {(e[2], e[4]) for e in edges})


class CollapseTests(unittest.TestCase):
    def test_last_write_wins(self) -> None:
        edges = [
            ("actor", "alice", "commented", "item", "1", "comment:1", "payload"),
            ("actor", "alice", "commented", "item", "1", "comment:2", "payload"),
            ("item", "1", "closes", "item", "2", "timeline: closed by pr", "timeline"),
            ("item", "1", "closes", "item", "2", "body closing keyword", "keyword"),
        ]
        out = collapse_edges(edges)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0][5], "comment:2")
        # source is part of the collapse key: the same (src, rel, dst) pair
        # under two different sources survives as two distinct edges
        close_sources = sorted(e[6] for e in out if e[2] == "closes")
        self.assertEqual(close_sources, ["keyword", "timeline"])


class MergeEvidenceTests(unittest.TestCase):
    def test_none_and_empty_contribute_nothing(self) -> None:
        self.assertIsNone(merge_evidence(None, None))
        self.assertIsNone(merge_evidence("", ""))
        self.assertEqual(merge_evidence(None, "a"), "a")
        self.assertEqual(merge_evidence("a", None), "a")

    def test_dedupes_first_seen_order(self) -> None:
        self.assertEqual(merge_evidence("a; b", "b; c"), "a; b; c")
        # idempotent on its own output
        self.assertEqual(merge_evidence("a; b; c", "b"), "a; b; c")
        # whitespace-only variants of a kept part do not duplicate it
        self.assertEqual(merge_evidence("a", " a "), "a")

    def test_cap_drops_middle_parts_keeps_oldest_and_newest(self) -> None:
        # trimming drops newest-unprotected parts first, but the arriving
        # (newest) part is always protected: for x/y/z over the cap, y goes
        # and the oldest (x) + newest (z) survive
        long_part = "x" * 200
        out = merge_evidence(long_part, "y" * 200, "z" * 200)
        self.assertLessEqual(len(out), 512)
        self.assertTrue(out.startswith(long_part))
        self.assertIn("z" * 200, out)
        self.assertNotIn("y" * 200, out)

    def test_never_returns_none_when_content_exists(self) -> None:
        # an over-cap part is hard-truncated, never dropped: callers persist
        # the result verbatim, so None would erase provenance entirely
        out = merge_evidence("x" * 600)
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 512)
        out = merge_evidence("x" * 600, "closingIssuesReferences")
        self.assertIsNotNone(out)
        self.assertIn("closingIssuesReferences", out)

    def test_reserve_protects_stream_markers_at_the_cap(self) -> None:
        filler = "timeline closed event " + "t" * 44
        parts = [f"{filler} {i:03d}" for i in range(10)]
        out = merge_evidence(*parts, "closingIssuesReferences", reserve=("closingIssuesReferences",))
        self.assertIn("closingIssuesReferences", out)
        self.assertLessEqual(len(out), 512)
        # the arriving (newest) part is always protected: over the cap, the
        # last part survives even without an explicit reserve
        newest = "n" * 200
        out = merge_evidence("a" * 200, "b" * 200, newest)
        self.assertIn(newest, out)
        self.assertNotIn("b" * 200, out)

    def test_parts_are_split_on_the_separator_only(self) -> None:
        # the payload format is the '; '-joined list: an existing merged
        # string re-splitting on the separator dedupes against itself
        self.assertEqual(merge_evidence("a; b", "a; b"), "a; b")


if __name__ == "__main__":
    unittest.main()
