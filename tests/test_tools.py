import pytest
from helpers import sample_tools

from tooldistill.core.errors import ToolDefinitionError
from tooldistill.core.serialization import schema_hash
from tooldistill.core.tools import PROPOSE_TOOL_NAME, ToolSet


def test_toolset_indexes_tools_in_order():
    tools = ToolSet(sample_tools())
    assert tools.names == (
        "gmail_search",
        "gmail_get_message",
        "billing_find_customer",
        "billing_create_invoice",
        "billing_import_from_email",
    )
    assert "gmail_search" in tools and "nope" not in tools
    assert len(tools) == 5
    assert tools["gmail_search"].description == "Search mail."
    assert tools.get("nope") is None
    assert [t.name for t in tools] == list(tools.names)


def test_toolset_hashes_match_serialization():
    tools = ToolSet(sample_tools())
    assert tools.schema_hash("gmail_search") == schema_hash(sample_tools()[0]["input_schema"])
    assert set(tools.hashes()) == set(tools.names)
    with pytest.raises(KeyError):
        tools.schema_hash("nope")


def test_toolset_copies_schemas_and_as_dicts_returns_fresh_copies():
    source = sample_tools()
    tools = ToolSet(source)
    source[0]["input_schema"]["properties"]["query"]["type"] = "integer"  # caller mutates their list
    assert tools["gmail_search"].input_schema["properties"]["query"]["type"] == "string"
    dicts = tools.as_dicts()
    assert dicts == sample_tools()
    dicts[0]["input_schema"]["properties"]["query"]["type"] = "integer"
    assert tools["gmail_search"].input_schema["properties"]["query"]["type"] == "string"


@pytest.mark.parametrize("bad_name", ["gmail.search", "", "a" * 65, "with space", "üml", None])
def test_invalid_names_are_rejected(bad_name):
    with pytest.raises(ToolDefinitionError, match="invalid name"):
        ToolSet([{"name": bad_name, "description": "", "input_schema": {"type": "object"}}])


def test_duplicate_reserved_and_schemaless_tools_are_rejected():
    good = {"name": "a_tool", "description": "", "input_schema": {"type": "object"}}
    with pytest.raises(ToolDefinitionError, match="duplicate"):
        ToolSet([good, dict(good)])
    with pytest.raises(ToolDefinitionError, match="reserved"):
        ToolSet([{"name": PROPOSE_TOOL_NAME, "description": "", "input_schema": {"type": "object"}}])
    with pytest.raises(ToolDefinitionError, match="input_schema"):
        ToolSet([{"name": "no_schema", "description": ""}])
    with pytest.raises(ToolDefinitionError, match="description must be a string"):
        ToolSet([{"name": "bad_desc", "description": 3, "input_schema": {"type": "object"}}])
    with pytest.raises(ToolDefinitionError, match="not a dict"):
        ToolSet(["gmail_search"])  # type: ignore[list-item]
