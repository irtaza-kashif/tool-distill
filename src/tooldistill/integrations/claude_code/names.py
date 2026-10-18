"""Name mapping between Claude Code's tool names and the library's base tool names.

Claude Code reports an MCP tool as ``mcp__<server>__<tool>`` (``mcp__plugin_<plugin>_<server>__<tool>`` for
plugin servers). The library names the same tool ``<server>__<tool>`` through ``base_tool_name``, the function
the MCP adapter uses, so hook-recorded names and adapter names agree. Claude's built-in tools become
``builtin__<Name>``: recorded, never eligible for a distilled tool.
"""

import os

from tooldistill.integrations.mcp import base_tool_name, sanitize_name_part

__all__ = [
    "BUILTIN_PREFIX",
    "DEFAULT_OWN_PREFIX",
    "MCP_PREFIX",
    "is_builtin",
    "is_own_tool",
    "own_prefix",
    "own_tool_name",
    "split_mcp_name",
    "to_base_tool_name",
]

MCP_PREFIX = "mcp__"
BUILTIN_PREFIX = "builtin__"
DEFAULT_OWN_PREFIX = "mcp__plugin_tooldistill_tooldistill__"


def own_prefix() -> str:
    """The prefix of the plugin's own MCP tools as Claude Code names them (override: TOOLDISTILL_MCP_PREFIX)."""
    return os.environ.get("TOOLDISTILL_MCP_PREFIX") or DEFAULT_OWN_PREFIX


def is_own_tool(claude_name: str) -> bool:
    return claude_name.startswith(own_prefix())


def own_tool_name(claude_name: str) -> str:
    """``mcp__plugin_tooldistill_tooldistill__propose_distilled_tool`` -> ``propose_distilled_tool``."""
    prefix = own_prefix()
    return claude_name[len(prefix):] if claude_name.startswith(prefix) else claude_name


def split_mcp_name(claude_name: str, server_hint: str | None = None) -> tuple[str, str] | None:
    """``(server, tool)`` for an MCP tool name, None for anything else.

    ``server_hint`` is the ``mcp_server.name`` field of a hook payload when present; it resolves the ambiguity
    of server names that themselves contain a double underscore.
    """
    if not claude_name.startswith(MCP_PREFIX):
        return None
    rest = claude_name[len(MCP_PREFIX):]
    if server_hint:
        hinted = f"{sanitize_name_part(server_hint)}__"
        if rest.startswith(hinted) and len(rest) > len(hinted):
            return server_hint, rest[len(hinted):]
    index = rest.find("__")
    if index <= 0 or index + 2 >= len(rest):
        return None
    return rest[:index], rest[index + 2 :]


def to_base_tool_name(claude_name: str, server_hint: str | None = None) -> str:
    """The library's name for a tool Claude Code called."""
    parts = split_mcp_name(claude_name, server_hint)
    if parts is None:
        return BUILTIN_PREFIX + sanitize_name_part(claude_name)
    return base_tool_name(*parts)


def is_builtin(base_name: str) -> bool:
    return base_name.startswith(BUILTIN_PREFIX)
