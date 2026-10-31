"""Acceptance checks for issue-trace 32-33-mcp-typed-stateless (issue #33).

Authored at arm's length against the issue contract AC8-AC11: the checks
are RED at the base tree and turn GREEN once the 2026-07-28 stateless
surface is implemented. Every failure carries a distinctive single-line
marker (`stateless request rejected`, `stateless tools differ`,
`discover incomplete`, `no input-required result`, `roots/list observed`,
`no cache fields`, `order unstable`, `not sorted`) so a line-wise grep
can attribute each failure to exactly one contract point.

CONTRACT PINNED (issue #33), all over the suite's McpSession harness:

AC8 stateless requests: a request with NO prior initialize whose params
carry _meta {"protocolVersion": "2026-07-28", "capabilities": {}} is
answered with a result (never a -32601/-32602 error) that includes a
resultType key, and its tools list equals a post-initialize tools/list.

AC9 server/discover: answered with no prior initialize, carrying the
server's versions (a list including "2026-07-28" and "2025-06-18"),
capabilities (a dict including tools), and tools (a non-empty list whose
names include "search").

AC10 repo resolution without roots: a 2026-07-28 tools/call with no repo
argument and no server repo pin answers an input-required result, and
the server NEVER sends a roots/list request for it (2026-07-28 dropped
the roots round-trip).

AC11 tools/list is cacheable and ordered: the 2026-07-28 result carries
cache metadata (a result-level cache field, or per-tool cache-hint
annotations) and the tool order is deterministic (identical across two
calls) and sorted by name.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

# fixtures FIRST (suite convention), then the McpSession harness and the
# existing zaxbygraph surface, then the module under test LAST so a
# missing base module is a loud import failure, not a collection error.
from fixtures import REPO, FakeGitHubSource, issue, pr_file, pull
from test_mcp import McpSession, pin_env_for
from zaxbygraph.db import connect, init_schema
from zaxbygraph.sync import sync_repo

import zaxbygraph.mcp_server as mcp_server  # noqa: F401  (base-leg guard)

#: The params-level _meta block that selects the 2026-07-28 behavior:
#: the official 2026-07-28 RequestMetaObject namespaced keys
#: (modelcontextprotocol spec schema 2026-07-28, RequestMetaObject).
_META_2026 = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}

#: Huge staleness threshold: these checks must never kick a background
#: sync (they exercise the protocol surface, not freshness).
_STALE_AFTER_S = 10**9


def _seed_store(db_path: Path) -> None:
    """Offline corpus: two issues and two merged PRs touching two paths,
    so the pinned db has a corpus without touching network or gh."""
    src = FakeGitHubSource()
    src.add_issue(issue(1, title="seeded widget one", body="needle for search; see #3", state="open"))
    src.add_issue(issue(2, title="second issue", body="also mentions #1", state="closed"))
    src.add_pr(
        issue(3, title="touch a and b", body="fixes #2", kind="pr", state="closed"),
        pull(3, changed_files=2, merged=True),
        files=[pr_file("src/a.py"), pr_file("src/b.py")],
    )
    src.add_pr(
        issue(4, title="touch a again", body="", kind="pr", state="closed"),
        pull(4, changed_files=1, merged=True),
        files=[pr_file("src/a.py")],
    )
    conn = connect(db_path)
    init_schema(conn)
    sync_repo(conn, src, REPO)
    conn.close()


class TestMcp2026StatelessSurface(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db_path = self.root / "history.db"
        _seed_store(self.db_path)
        # Keep the user's real store/db out of resolution; sessions pin
        # repo/db explicitly on top of this.
        pin_env_for(self, self.root)

    def _session(self, **overrides) -> McpSession:
        kwargs: dict = {
            "cwd": str(self.root),
            "repo": REPO,
            "db": str(self.db_path),
            "stale_after_s": _STALE_AFTER_S,
        }
        kwargs.update(overrides)
        session = McpSession(self, **kwargs)
        self.addCleanup(session.stop)
        return session

    @staticmethod
    def _tool_names(tools) -> list:
        if not isinstance(tools, list):
            return []
        return [t.get("name") for t in tools if isinstance(t, dict)]

    def test_stateless_request_answered(self) -> None:
        """AC8: a _meta-tagged 2026-07-28 tools/list with no prior
        initialize is answered as a self-describing result whose tools
        equal a post-initialize tools/list.

        RED at base (the result carries no resultType); GREEN after the
        fix serves stateless 2026-07-28 requests."""
        session = self._session()
        req_id = session.request("tools/list", {"_meta": dict(_META_2026)})
        reply = session.wait_response(req_id)
        error = reply.get("error")
        result = reply.get("result")
        stateless_names = (
            self._tool_names(result.get("tools")) if isinstance(result, dict) else None
        )

        with self.subTest(part="answered-with-result"):
            self.assertIsNone(
                error,
                "AC8 stateless request rejected: a 2026-07-28 tools/list with no "
                f"prior initialize must be answered, got error {error!r}",
            )
            self.assertIsInstance(
                result,
                dict,
                "AC8 stateless request rejected: the stateless request must answer "
                "with a result object",
            )

        with self.subTest(part="resultType"):
            self.assertIsInstance(
                result,
                dict,
                "AC8 stateless request rejected: the stateless request must answer "
                "with a result object",
            )
            self.assertEqual(
                (result or {}).get("resultType"),
                "complete",
                "AC8 stateless request rejected: the stateless result must carry "
                f"resultType 'complete'; got {(result or {}).get('resultType')!r} "
                f"(keys {sorted(result or {})})",
            )

        with self.subTest(part="tools list present"):
            self.assertTrue(
                stateless_names,
                "AC8 stateless request rejected: the stateless result must still "
                "carry the tools list",
            )

        session.initialize()
        req_id = session.request("tools/list", {})
        result = session.wait_response(req_id).get("result")
        self.assertIsInstance(result, dict, "AC8: a post-initialize tools/list must answer with a result object")
        initialized_names = self._tool_names(result.get("tools"))

        with self.subTest(part="same-tools-as-initialized"):
            if stateless_names:
                self.assertEqual(
                    stateless_names,
                    initialized_names,
                    "AC8 stateless tools differ: the stateless tools list must equal "
                    "a post-initialize tools/list; "
                    f"stateless={stateless_names} initialized={initialized_names}",
                )
            else:
                self.fail(
                    "AC8 stateless tools differ: the stateless result carried no "
                    "tools list to compare against a post-initialize tools/list"
                )

        # Modern-era error contracts (issue #33: ping is removed; the
        # versioning page pins the wire errors).
        with self.subTest(part="modern ping removed"):
            req_id = session.request("ping", {"_meta": dict(_META_2026)})
            reply = session.wait_response(req_id)
            self.assertEqual(
                (reply.get("error") or {}).get("code"),
                -32601,
                "AC8 stateless request rejected: a modern-marked ping is removed in "
                "2026-07-28 and must answer method-not-found -32601; got "
                f"{reply.get('error')!r}",
            )

        with self.subTest(part="malformed modern metadata"):
            malformed = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
            req_id = session.request("tools/list", {"_meta": malformed})
            reply = session.wait_response(req_id)
            self.assertEqual(
                (reply.get("error") or {}).get("code"),
                -32602,
                "AC8 stateless request rejected: a modern-marked request missing the "
                "required clientCapabilities must answer invalid-params -32602; got "
                f"{reply.get('error')!r}",
            )

        with self.subTest(part="unsupported modern version"):
            unsupported = {
                "io.modelcontextprotocol/protocolVersion": "1900-01-01",
                "io.modelcontextprotocol/clientCapabilities": {},
            }
            req_id = session.request("tools/list", {"_meta": unsupported})
            reply = session.wait_response(req_id)
            err = reply.get("error") or {}
            self.assertEqual(
                err.get("code"),
                -32022,
                "AC8 stateless request rejected: an unsupported declared modern "
                "version must answer UnsupportedProtocolVersionError -32022; got "
                f"{err!r}",
            )
            self.assertIn(
                "2026-07-28",
                (err.get("data") or {}).get("supported", []),
                "AC8 stateless request rejected: the -32022 error data must list the "
                "supported versions including 2026-07-28",
            )
        session.stop()

    def test_server_discover(self) -> None:
        """AC9: server/discover (no prior initialize) reports versions
        including 2026-07-28 and 2025-06-18, capabilities including
        tools, and the tool list including search.

        RED at base (method not found); GREEN after the fix answers
        server/discover."""
        session = self._session()
        req_id = session.request("server/discover", {"_meta": dict(_META_2026)})
        reply = session.wait_response(req_id)

        with self.subTest(part="answered"):
            error = reply.get("error")
            self.assertIsNone(
                error,
                "AC9 discover incomplete: server/discover must be answered with no "
                f"prior initialize, got error {error!r}",
            )
        result = reply.get("result")

        with self.subTest(part="result object"):
            self.assertIsInstance(
                result,
                dict,
                "AC9 discover incomplete: server/discover must answer with a result object",
            )
        if not isinstance(result, dict):
            session.stop()
            return

        with self.subTest(part="versions"):
            # Official 2026-07-28 DiscoverResult field name.
            versions = result.get("supportedVersions")
            self.assertIsInstance(
                versions,
                list,
                "AC9 discover incomplete: the discover result must carry "
                f"supportedVersions as a list; got {sorted(result)}",
            )
            self.assertIn(
                "2026-07-28",
                versions,
                "AC9 discover incomplete: supportedVersions must include 2026-07-28",
            )
            self.assertIn(
                "2025-06-18",
                versions,
                "AC9 discover incomplete: supportedVersions must include 2025-06-18",
            )

        with self.subTest(part="cacheable result"):
            # DiscoverResult extends CacheableResult: ttlMs >= 0 and
            # cacheScope public|private are REQUIRED fields.
            ttl = result.get("ttlMs")
            self.assertIsInstance(
                ttl,
                int,
                "AC9 discover incomplete: the discover result must carry an integer "
                f"ttlMs; got {ttl!r}",
            )
            self.assertGreaterEqual(
                ttl,
                0,
                "AC9 discover incomplete: ttlMs must be >= 0",
            )
            self.assertIn(
                result.get("cacheScope"),
                ("public", "private"),
                "AC9 discover incomplete: cacheScope must be 'public' or 'private'; got "
                f"{result.get('cacheScope')!r}",
            )
            self.assertEqual(
                result.get("resultType"),
                "complete",
                "AC9 discover incomplete: the discover result must carry resultType "
                f"'complete'; got {result.get('resultType')!r}",
            )

        with self.subTest(part="capabilities"):
            capabilities = result.get("capabilities")
            self.assertIsInstance(
                capabilities,
                dict,
                "AC9 discover incomplete: the discover result must carry capabilities",
            )
            self.assertIn(
                "tools",
                capabilities,
                "AC9 discover incomplete: capabilities must include tools",
            )

        with self.subTest(part="tools"):
            tools = result.get("tools")
            self.assertIsInstance(
                tools,
                list,
                "AC9 discover incomplete: the discover result must carry the tools list",
            )
            self.assertTrue(
                tools,
                "AC9 discover incomplete: the discover tools list must not be empty",
            )
            self.assertIn(
                "search",
                self._tool_names(tools),
                "AC9 discover incomplete: the discover tools must include search",
            )
        session.stop()

    def test_repo_resolution_without_roots(self) -> None:
        """AC10: a repo-less, un-pinned 2026-07-28 get_item answers an
        input-required result and the session NEVER sees a roots/list
        request (the 2026-07-28 flow dropped the roots round-trip).

        RED at base (the call round-trips roots/list and answers a plain
        error envelope); GREEN after the fix answers InputRequiredResult."""
        session = self._session(repo=None)
        req_id = session.request(
            "tools/call",
            {
                "name": "get_item",
                "arguments": {"number": 1},
                "_meta": dict(_META_2026),
            },
        )
        reply = session.wait_response(req_id)
        result = reply.get("result")

        marked = False
        detail = f"reply keys {sorted(reply)}"
        if isinstance(result, dict):
            # Official 2026-07-28 InputRequiredResult shape: resultType
            # 'input_required', at least one of inputRequests/requestState
            # present, and the payload names repo (the issue's contract).
            text = ""
            content = result.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                text = str(content[0].get("text", ""))
            marked = (
                result.get("resultType") == "input_required"
                and isinstance(result.get("requestState"), str)
                and bool(result.get("requestState"))
                and "repo" in text.lower()
                and "required" in text.lower()
            )
            detail = (
                f"result keys {sorted(result)}, resultType "
                f"{result.get('resultType')!r}, requestState "
                f"{result.get('requestState')!r}, text head {text[:120]!r}"
            )

        with self.subTest(part="input-required result"):
            self.assertTrue(
                marked,
                "AC10 no input-required result: a 2026-07-28 tools/call with no repo "
                "argument and no server pin must answer an InputRequiredResult "
                f"({detail})",
            )

        with self.subTest(part="no roots round-trip"):
            offenders = [line for line in session.lines if "roots/list" in line]
            self.assertEqual(
                offenders,
                [],
                "AC10 roots/list observed: the 2026-07-28 flow must resolve without "
                f"a roots/list request; saw {offenders[:2]}",
            )
        session.stop()

    def test_tools_list_cacheable_and_ordered(self) -> None:
        """AC11: the 2026-07-28 tools/list carries cache metadata and a
        deterministic, sorted tool order.

        RED at base (no cache fields; documentation order is not sorted);
        GREEN after the fix tags and orders the list."""
        session = self._session()

        def list_once() -> dict:
            req_id = session.request("tools/list", {"_meta": dict(_META_2026)})
            result = session.wait_response(req_id).get("result")
            self.assertIsInstance(
                result, dict, "AC11: tools/list must answer with a result object"
            )
            return result

        first = list_once()
        second = list_once()
        names_first = self._tool_names(first.get("tools"))
        names_second = self._tool_names(second.get("tools"))

        with self.subTest(part="cache metadata"):
            # Official 2026-07-28 CacheableResult fields on the modern
            # tools/list result.
            ttl = first.get("ttlMs")
            self.assertIsInstance(
                ttl,
                int,
                "AC11 no cache fields: the 2026-07-28 tools/list result must carry "
                f"an integer ttlMs; got {ttl!r} (result keys {sorted(first)})",
            )
            self.assertGreaterEqual(
                ttl,
                0,
                "AC11 no cache fields: ttlMs must be >= 0",
            )
            self.assertIn(
                first.get("cacheScope"),
                ("public", "private"),
                "AC11 no cache fields: cacheScope must be 'public' or 'private'; got "
                f"{first.get('cacheScope')!r}",
            )
            self.assertEqual(
                first.get("resultType"),
                "complete",
                "AC11 no cache fields: the modern tools/list result must carry "
                f"resultType 'complete'; got {first.get('resultType')!r}",
            )

        with self.subTest(part="deterministic order"):
            self.assertEqual(
                names_first,
                names_second,
                "AC11 order unstable: two tools/list calls returned different tool "
                f"orders: {names_first} vs {names_second}",
            )

        with self.subTest(part="sorted order"):
            self.assertEqual(
                names_first,
                sorted(names_first),
                "AC11 not sorted: tools must be listed in sorted name order, got "
                f"{names_first}",
            )

        # The resource siblings are CacheableResult too on the modern path:
        # a complete resources/read carries resultType/ttlMs/cacheScope.
        with self.subTest(part="modern resources read"):
            req_id = session.request(
                "resources/read",
                {"uri": "zaxbygraph://schema", "_meta": dict(_META_2026)},
            )
            reply = session.wait_response(req_id)
            result = reply.get("result")
            self.assertIsInstance(
                result,
                dict,
                "AC11 no cache fields: a modern resources/read must answer a result "
                f"object; got error {reply.get('error')!r}",
            )
            self.assertEqual(
                result.get("resultType"),
                "complete",
                "AC11 no cache fields: the modern resources/read result must carry "
                f"resultType 'complete'; got {result.get('resultType')!r}",
            )
            ttl = result.get("ttlMs")
            self.assertIsInstance(
                ttl,
                int,
                "AC11 no cache fields: the modern resources/read result must carry an "
                f"integer ttlMs; got {ttl!r}",
            )
            self.assertGreaterEqual(ttl, 0, "AC11 no cache fields: ttlMs must be >= 0")
            self.assertEqual(
                result.get("cacheScope"),
                "private",
                "AC11 no cache fields: the modern resources/read result must carry "
                f"cacheScope 'private'; got {result.get('cacheScope')!r}",
            )

        # The no-roots input-required flow covers resources/read too: a
        # repo-less, un-pinned modern resources/read answers input_required
        # and never sends roots/list.
        unpinned = self._session(repo=None)
        req_id = unpinned.request(
            "resources/read",
            {"uri": "zaxbygraph://schema", "_meta": dict(_META_2026)},
        )
        reply = unpinned.wait_response(req_id)
        result = reply.get("result")
        with self.subTest(part="resources read input-required"):
            self.assertEqual(
                (result or {}).get("resultType"),
                "input_required",
                "AC11 no cache fields: a repo-less modern resources/read must answer "
                f"resultType 'input_required'; got {(result or {}).get('resultType')!r} "
                f"(keys {sorted(result or {})})",
            )
            offenders = [line for line in unpinned.lines if "roots/list" in line]
            self.assertEqual(
                offenders,
                [],
                "AC11 no cache fields: the modern resources/read flow must resolve "
                f"without a roots/list request; saw {offenders[:2]}",
            )
        session.stop()


if __name__ == "__main__":
    unittest.main()
