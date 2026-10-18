"""The fake incident tools as a stdio MCP server, for the Claude Code demo.

Run with ``python mcp_server.py``. The tool list follows ``tools.current_tools()`` on every request, so the
switches apply live: environment variables at startup, or the JSON file named by ``INCIDENT_SWITCH_FILE``
(``{"rollback_outage": true, "schema_reason": false, "tools_updated": false}``) at any time.
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tools as incidents  # noqa: E402

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)


def _text(value: Any) -> TextContent:
    return TextContent(type="text", text=json.dumps(value))


class IncidentServer(MCPServer):
    """Every call goes through ``tools.execute_tool``; a ``ToolError`` becomes an ``is_error`` result."""

    async def list_tools(self) -> list[Tool]:
        return [
            Tool(name=tool["name"], description=tool["description"], input_schema=tool["input_schema"])
            for tool in incidents.current_tools()
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> CallToolResult:
        try:
            result = incidents.execute_tool(name, dict(arguments or {}))
        except incidents.ToolError as exc:
            return CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)
        except TypeError as exc:
            return CallToolResult(content=[TextContent(type="text", text=f"invalid arguments: {exc}")], is_error=True)
        structured = result if isinstance(result, dict) else None
        return CallToolResult(content=[_text(result)], structured_content=structured)


def main() -> None:
    server = IncidentServer("incidents", instructions="Fake alerting, deploy, logs, metrics, on-call and chat tools for the tooldistill demo.")
    anyio.run(server.run_stdio_async)


if __name__ == "__main__":
    main()
