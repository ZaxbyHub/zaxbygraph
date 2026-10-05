"""Guardrail for the issue #7 defect class (Phase 4.2).

Class: a consumer surface gains a capability that has no counterpart on
the typed agent surface, so every agent session re-derives the capability
by hand. The guard is a two-directional mapping table between the CLI
read commands and the MCP tool/resource surface. Every CLI read command
must appear in a mapping cell — as a tool, as the schema resource, or as
an explicit documented gap — and every tool must map back to a CLI
command. Removing a mapping entry (or registering a read command without
adding a cell) turns this red, which is the demonstration that it bites.
"""

from __future__ import annotations

import unittest

from zaxbygraph.cli import build_parser
from zaxbygraph import mcp_server

#: {CLI read command -> cell}. "resource" means the capability is served
#: by the zaxbygraph://schema resource instead of a tool. Documented gaps
#: are surface capabilities that are meaningful only from a shell (or are
#: themselves about maintaining the DB), recorded here so the decision is
#: visible and revisitable rather than silent.
CLI_TO_SURFACE = {
    "status": "graph_status",
    "search": "search",
    "item": "get_item",
    "related": "related",
    "path": "path",
    "overlap": "pr_overlap",
    "file-history": "file_history",
    "what-closed": "what_closed",
    "open": "open_items",
    "sql": "sql",
    "schema": "resource",
    # Documented gaps: churn/export-graph are aggregation surfaces an
    # agent consumes through a tool result anyway (no arguments beyond a
    # limit); `where` and `doctor` are host-resolution/maintenance
    # commands for the human operating a shell.
    "churn": "documented-gap: aggregation view, read it via the sql tool (SELECT path, COUNT(*) FROM pr_files ...)",
    "export-graph": "documented-gap: bulk export for graph viewers, not an agent query",
    "where": "documented-gap: prints the resolution chain for a shell checkout",
    "doctor": "documented-gap: store maintenance for a human",
    "sync": "sync",
}

#: {tool -> CLI command}. Every typed tool must stay reachable from the
#: CLI so the skill's fallback story stays true.
TOOL_TO_CLI = {
    "graph_status": "status",
    "search": "search",
    "get_item": "item",
    "related": "related",
    "path": "path",
    "pr_overlap": "overlap",
    "file_history": "file-history",
    "what_closed": "what-closed",
    "open_items": "open",
    "sql": "sql",
    "sync": "sync",
}


def registered_read_commands() -> set[str]:
    parser = build_parser()
    sub_action = next(
        a
        for a in parser._actions
        if getattr(a, "dest", None) == "cmd" and getattr(a, "choices", None)
    )
    registered = set(sub_action.choices)
    # sync is a write, but it is mapped above on purpose (the MCP sync
    # tool starts the same locked background sync); mcp is the surface
    # itself, not a capability to mirror.
    registered.discard("mcp")
    return registered


class SurfaceMappingTests(unittest.TestCase):
    def test_every_cli_read_command_has_a_mapping_cell(self) -> None:
        registered = registered_read_commands()
        missing = registered - set(CLI_TO_SURFACE)
        self.assertFalse(
            missing,
            "CLI read commands with no MCP-surface mapping cell (add the "
            "tool, serve it as a resource, or record the gap): "
            + ", ".join(sorted(missing)),
        )
        stale = set(CLI_TO_SURFACE) - registered
        self.assertFalse(
            stale,
            "mapping table names commands build_parser no longer registers: "
            + ", ".join(sorted(stale)),
        )

    def test_every_tool_maps_to_a_cli_command(self) -> None:
        tools = {tool["name"] for tool in mcp_server._TOOLS}
        unmapped = tools - set(TOOL_TO_CLI)
        self.assertFalse(
            unmapped,
            "MCP tools with no CLI counterpart cell: " + ", ".join(sorted(unmapped)),
        )
        stale = set(TOOL_TO_CLI) - tools
        self.assertFalse(
            stale,
            "mapping table names tools the registry no longer serves: "
            + ", ".join(sorted(stale)),
        )

    def test_tool_to_cli_values_are_registered_commands(self) -> None:
        registered = registered_read_commands()
        for tool, command in TOOL_TO_CLI.items():
            self.assertIn(
                command,
                registered,
                f"TOOL_TO_CLI[{tool!r}] names {command!r}, which build_parser does not register",
            )

    def test_every_advertised_tool_has_a_handler(self) -> None:
        handlers = set(mcp_server._TOOL_HANDLERS)
        tools = {tool["name"] for tool in mcp_server._TOOLS}
        self.assertEqual(
            tools,
            handlers,
            "tools/list and the dispatch table disagree; one of them is a lie",
        )

    def test_mapped_tools_really_exist_in_the_registry(self) -> None:
        tools = {tool["name"] for tool in mcp_server._TOOLS}
        for command, tool in CLI_TO_SURFACE.items():
            if isinstance(tool, str) and tool not in ("resource",) and not tool.startswith("documented-gap:"):
                self.assertIn(
                    tool,
                    tools,
                    f"CLI command {command} maps to tool {tool}, which the registry does not serve",
                )

    def test_schema_capability_is_served_as_the_resource(self) -> None:
        self.assertEqual(
            mcp_server._SCHEMA_RESOURCE_URI,
            "zaxbygraph://schema",
            "the schema capability's resource URI moved; update CLI_TO_SURFACE",
        )


if __name__ == "__main__":
    unittest.main()
