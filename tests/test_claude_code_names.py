from tooldistill.integrations.claude_code.names import (
    DEFAULT_OWN_PREFIX,
    is_builtin,
    is_own_tool,
    own_prefix,
    own_tool_name,
    split_mcp_name,
    to_base_tool_name,
)
from tooldistill.integrations.mcp import base_tool_name


def test_mcp_names_map_to_adapter_names():
    assert to_base_tool_name("mcp__invoice__gmail_search") == "invoice__gmail_search"
    assert to_base_tool_name("mcp__invoice__gmail_search") == base_tool_name("invoice", "gmail_search")
    assert split_mcp_name("mcp__invoice__gmail_search") == ("invoice", "gmail_search")
    assert split_mcp_name("mcp__my_server__tool__with__underscores") == ("my_server", "tool__with__underscores")
    assert split_mcp_name("mcp__my__server__tool", "my__server") == ("my__server", "tool")  # the hint resolves the ambiguity
    assert split_mcp_name("mcp__my__server__tool") == ("my", "server__tool")
    assert split_mcp_name("mcp__my server__do it", "my server") == ("my server", "do it")
    assert to_base_tool_name("mcp__my_server__tool", "my_server") == "my_server__tool"
    assert split_mcp_name("mcp__nounderscore") is None and split_mcp_name("mcp____x") is None and split_mcp_name("mcp__x__") is None
    assert split_mcp_name("Bash") is None


def test_builtins_and_own_tools():
    assert to_base_tool_name("Bash") == "builtin__Bash" and is_builtin("builtin__Bash")
    assert to_base_tool_name("mcp__nounderscore") == "builtin__mcp__nounderscore"
    assert not is_builtin("invoice__gmail_search")
    assert own_prefix() == DEFAULT_OWN_PREFIX
    assert is_own_tool(DEFAULT_OWN_PREFIX + "propose_distilled_tool")
    assert own_tool_name(DEFAULT_OWN_PREFIX + "propose_distilled_tool") == "propose_distilled_tool"
    assert own_tool_name("mcp__invoice__gmail_search") == "mcp__invoice__gmail_search"
    assert not is_own_tool("mcp__invoice__gmail_search")


def test_own_prefix_override(monkeypatch):
    monkeypatch.setenv("TOOLDISTILL_MCP_PREFIX", "mcp__tooldistill__")
    assert own_prefix() == "mcp__tooldistill__"
    assert is_own_tool("mcp__tooldistill__create_invoice") and not is_own_tool(DEFAULT_OWN_PREFIX + "x")
    assert own_tool_name("mcp__tooldistill__create_invoice") == "create_invoice"
