"""The fake invoice tools as a stdio MCP server, for the Claude Code demo and the MCP adapter tests.

Run with ``python mcp_server.py``. The tool list follows ``tools.current_tools()`` on every request, so the
switches apply live: environment variables at startup, or the JSON file named by ``INVOICE_SWITCH_FILE``
(``{"import_outage": true, "schema_currency": false, "tools_updated": false}``) at any time.

Test-only switches: ``INVOICE_MCP_CRASH_ON=<tool>`` makes the process exit when that tool is called,
``INVOICE_MCP_SLEEP_MS=<n>`` delays every call.
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tools as invoice  # noqa: E402

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)


def _text(value: Any) -> TextContent:
    return TextContent(type="text", text=json.dumps(value))


class InvoiceServer(MCPServer):
    """Every call goes through ``tools.execute_tool``; a ``ToolError`` becomes an ``is_error`` result."""

    async def list_tools(self) -> list[Tool]:
        return [
            Tool(name=tool["name"], description=tool["description"], input_schema=tool["input_schema"])
            for tool in invoice.current_tools()
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> CallToolResult:
        crash_on = os.environ.get("INVOICE_MCP_CRASH_ON")
        if crash_on and name == crash_on:
            os._exit(1)
        delay = os.environ.get("INVOICE_MCP_SLEEP_MS")
        if delay:
            await anyio.sleep(int(delay) / 1000)
        try:
            result = invoice.execute_tool(name, dict(arguments or {}))
        except invoice.ToolError as exc:
            return CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)
        except TypeError as exc:
            return CallToolResult(content=[TextContent(type="text", text=f"invalid arguments: {exc}")], is_error=True)
        structured = result if isinstance(result, dict) else None
        return CallToolResult(content=[_text(result)], structured_content=structured)


def main() -> None:
    server = InvoiceServer("invoice", instructions="Fake Gmail and billing tools for the tooldistill demo.")
    anyio.run(server.run_stdio_async)


if __name__ == "__main__":
    main()
