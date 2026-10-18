"""The MCP executor adapter against the demo's fake-tools MCP server, spawned over stdio. No network."""

import json
import sys
from pathlib import Path

import pytest
import tools as invoice
from mcp.types import TextContent

from tooldistill import ToolDistill
from tooldistill.core.config import ServerSpec
from tooldistill.core.errors import MCPToolError
from tooldistill.core.models import CallKind, Proposal, Status
from tooldistill.core.serialization import schema_hash
from tooldistill.integrations.mcp import MAX_NAME, MCPToolkit, base_tool_name, error_text, result_value, sanitize_name_part

SERVER = Path(__file__).resolve().parents[1] / "examples" / "invoice_agent" / "mcp_server.py"
IMPORT_TOOL = '''from tooldistill import distilled_tool, call


@distilled_tool(
    name="import_latest_order",
    description="Import a customer's most recent order email as a draft invoice.",
    input_schema={"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]},
)
def import_latest_order(customer: str) -> dict:
    messages = call("invoice__gmail_search", query=f"from:{customer} order", since="7d")
    return call("invoice__billing_import_from_email", message_id=messages[0]["id"])
'''


def spec(**env: object) -> ServerSpec:
    return ServerSpec.from_mapping(
        "invoice", {"command": sys.executable, "args": [str(SERVER)], "env": {k: str(v) for k, v in env.items()}}
    )


@pytest.fixture(scope="module")
def toolkit():
    with MCPToolkit({"invoice": spec(INVOICE_IMPORT_OUTAGE=0)}, call_timeout=20) as tk:
        yield tk


# ------------------------------------------------------------------------ pure helpers


def test_base_tool_name_sanitises_and_shortens():
    assert base_tool_name("gmail", "search") == "gmail__search"
    assert base_tool_name("my server", "do.it") == "my_server__do_it"
    assert sanitize_name_part("a-b_c") == "a-b_c"
    long_name = base_tool_name("x" * 40, "y" * 40)
    assert len(long_name) == MAX_NAME and long_name[:55] == ("x" * 40 + "__" + "y" * 13) and long_name[55] == "_"
    assert long_name == base_tool_name("x" * 40, "y" * 40)  # deterministic
    assert long_name != base_tool_name("x" * 40, "y" * 39 + "z")


def test_result_value_and_error_text():
    text = lambda s: {"type": "text", "text": s}  # noqa: E731
    assert result_value([text('{"a": 1}')]) == {"a": 1}
    assert result_value([text("plain")]) == "plain"
    assert result_value([text("1"), text("x")]) == [1, "x"]
    assert result_value([], None) is None
    assert result_value([text("ignored")], {"s": 1}) == {"s": 1}
    assert result_value([{"type": "image", "data": "AAAA", "mimeType": "image/png"}]) == {"type": "image", "data": "<omitted>", "mimeType": "image/png"}
    assert result_value([TextContent(type="text", text="[1]")]) == [1]
    assert result_value([TextContent(type="text", text="raw text")]) == "raw text"
    assert error_text([text("boom"), text("more")]) == "boom\nmore"
    assert error_text([]) == "the tool returned an error"
    assert error_text([TextContent(type="text", text="bad")]) == "bad"


# ---------------------------------------------------------------------- over the wire


def test_tools_match_the_fake_definitions(toolkit):
    expected = invoice.tool_definitions(currency=False)
    assert [t["name"] for t in toolkit.tools] == [f"invoice__{t['name']}" for t in expected]
    for served, source in zip(toolkit.tools, expected, strict=True):
        assert served["input_schema"] == source["input_schema"] and served["description"] == source["description"]
        assert schema_hash(served["input_schema"]) == schema_hash(source["input_schema"])
    assert toolkit.servers == ("invoice",)
    assert toolkit.server_of("invoice__gmail_search") == "invoice" and toolkit.server_of("nope") is None
    assert toolkit.tools is not toolkit.tools  # fresh copies


def test_executor_results_match_direct_calls(toolkit):
    assert toolkit.executor("invoice__gmail_search", {"query": "from:ACME order", "since": "7d"}) == invoice.gmail_search("from:ACME order", since="7d")
    message = toolkit.executor("invoice__gmail_get_message", {"id": "msg_1001"})
    assert message == invoice.gmail_get_message("msg_1001")
    created = toolkit.executor("invoice__billing_create_invoice", {"customer_id": "cust_acme", "lines": message["lines"]})
    assert created["status"] == "draft" and created["total"] == 66.5 and created["currency"] == "USD"
    assert toolkit.executor("invoice__gmail_list_labels", {}) == ["INBOX", "Orders", "Newsletters"]
    assert toolkit.executor("invoice__calendar_list_events", {"day": "2026-10-17"}) == []
    assert toolkit.executor("invoice__billing_list_invoices", None) == toolkit.executor("invoice__billing_list_invoices", {})


def test_tool_failures_raise(toolkit):
    with pytest.raises(MCPToolError, match="no customer named 'Nobody'"):
        toolkit.executor("invoice__billing_find_customer", {"name": "Nobody"})
    with pytest.raises(MCPToolError, match="unknown MCP tool 'nope'"):
        toolkit.executor("nope", {})
    with pytest.raises(MCPToolError, match="invalid arguments"):
        toolkit.executor("invoice__gmail_get_message", {"identifier": "x"})


def test_probe(toolkit):
    assert toolkit.probe("invoice__gmail_search") is True
    assert toolkit.probe("invoice__billing_import_from_email") is True
    assert toolkit.probe("nope") is False


def test_executor_refuses_the_loop_thread(toolkit):
    async def from_inside():
        return toolkit.executor("invoice__gmail_list_labels", {})

    with pytest.raises(MCPToolError, match="loop thread"):
        toolkit._submit(from_inside(), 5)


def test_outage_through_mcp():
    with MCPToolkit({"invoice": spec(INVOICE_IMPORT_OUTAGE=1)}, call_timeout=20) as tk:
        with pytest.raises(MCPToolError, match="503 Service Unavailable"):
            tk.executor("invoice__billing_import_from_email", {"message_id": "msg_1001"})
        assert tk.probe("invoice__billing_import_from_email") is True  # reachable and listed; the probe never calls it


def test_switch_file_and_refresh_tools(tmp_path: Path):
    switches = tmp_path / "switches.json"
    switches.write_text("{}", encoding="utf-8")
    with MCPToolkit({"invoice": spec(INVOICE_SWITCH_FILE=switches)}, call_timeout=20) as tk:
        create = next(t for t in tk.tools if t["name"] == "invoice__billing_create_invoice")
        assert "currency" not in create["input_schema"]["properties"]
        assert tk.refresh_tools() == []
        switches.write_text(json.dumps({"tools_updated": True}), encoding="utf-8")
        assert tk.refresh_tools() == ["invoice__billing_create_invoice", "invoice__billing_import_from_email"]
        create = next(t for t in tk.tools if t["name"] == "invoice__billing_create_invoice")
        assert create["input_schema"]["required"] == ["customer_id", "lines", "currency"]
        switches.write_text(json.dumps({"schema_currency": True}), encoding="utf-8")
        with pytest.raises(MCPToolError, match="missing required property 'currency'"):
            tk.executor("invoice__billing_create_invoice", {"customer_id": "cust_acme", "lines": [{"qty": 1, "unit_price": 1}]})
        switches.write_text(json.dumps({"import_outage": True}), encoding="utf-8")
        with pytest.raises(MCPToolError, match="503"):
            tk.executor("invoice__billing_import_from_email", {"message_id": "msg_1001"})


def test_crash_then_reconnect():
    with MCPToolkit({"invoice": spec(INVOICE_MCP_CRASH_ON="gmail_list_labels")}, call_timeout=5, connect_timeout=20) as tk:
        with pytest.raises(MCPToolError):
            tk.executor("invoice__gmail_list_labels", {})
        assert tk.executor("invoice__gmail_search", {"query": "from:ACME"}) == invoice.gmail_search("from:ACME")
        assert tk.probe("invoice__gmail_search") is True


def test_call_timeout():
    with MCPToolkit({"invoice": spec(INVOICE_MCP_SLEEP_MS=3000)}, call_timeout=0.5, connect_timeout=20) as tk:
        with pytest.raises(MCPToolError, match="did not answer 'gmail_list_labels' within 0.5s"):
            tk.executor("invoice__gmail_list_labels", {})


def test_lifecycle_errors():
    tk = MCPToolkit({"invoice": spec()})
    with pytest.raises(MCPToolError, match="not running"):
        tk.executor("invoice__gmail_search", {"query": "x"})
    tk.start()
    tk.close()
    tk.close()  # idempotent
    with pytest.raises(MCPToolError, match="only be started once"):
        tk.start()
    with pytest.raises(MCPToolError, match="not running"):
        tk.executor("invoice__gmail_search", {"query": "x"})
    assert tk.probe("invoice__gmail_search") is False


def test_connection_failure_names_the_server():
    broken = ServerSpec.from_mapping("broken", {"command": sys.executable, "args": ["-c", "import sys; sys.exit(3)"]})
    with pytest.raises(MCPToolError, match="cannot connect to MCP server 'broken'"):
        MCPToolkit({"broken": broken}, connect_timeout=10).start()


# ---------------------------------------------------------------- the SDK path over MCP


def test_tooldistill_from_config_over_mcp(tmp_path: Path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"invoice": {"type": "stdio", "command": sys.executable, "args": [str(SERVER)], "env": {"INVOICE_IMPORT_OUTAGE": "${DEMO_OUTAGE:-0}"}}}}),
        encoding="utf-8",
    )
    (tmp_path / "tooldistill.toml").write_text(
        'store = ".tooldistill"\nmcp_config = ".mcp.json"\nauto_promote = true\nallow_live_verification = true\n'
        "min_calls_to_distill = 4\nfallback_distiller = false\n",
        encoding="utf-8",
    )
    td = ToolDistill.from_config(tmp_path / "tooldistill.toml")
    try:
        assert td.toolkit is not None and td.tools.names[0] == "invoice__gmail_search" and td.health_probe == td.toolkit.probe
        trace = td.start_trace("Create a draft invoice for ACME from their most recent order email.")
        out = trace.execute("invoice__gmail_search", {"query": "from:ACME order", "since": "7d"})
        assert not out.is_error and json.loads(out.content)[0]["id"] == "msg_1001"
        failed = trace.execute("invoice__billing_find_customer", {"name": "Nobody"})
        assert failed.is_error and failed.content == "no customer named 'Nobody'"
        for _ in range(3):
            trace.execute("invoice__gmail_list_labels", {})
        trace.set_outcome(True)
        calls = td.registry.list_tool_calls(trace.id)
        assert [c.kind for c in calls] == [CallKind.BASE] * 5 and calls[1].is_error
        proposal = Proposal("import_latest_order", "d", {"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]}, IMPORT_TOOL, {"customer": "ACME"})
        result = td.ingest(proposal, trace.id)
        assert result.verified, result.verdict
        assert result.report is not None and result.report.used_live  # the import call had no fixture: verified live over MCP
        assert result.version is not None and result.version.status is Status.ACTIVE
        second = td.start_trace("Create a draft invoice for Globex from their most recent order email.")
        out = second.execute("import_latest_order", {"customer": "Globex"})
        assert not out.is_error and json.loads(out.content)["customer_id"] == "cust_globex"
        children = [c for c in td.registry.list_tool_calls(second.id) if c.parent_call_id is not None]
        assert [c.tool_name for c in children] == ["invoice__gmail_search", "invoice__billing_import_from_email"]
    finally:
        td.close()
    with pytest.raises(MCPToolError, match="not running"):
        td.toolkit.executor("invoice__gmail_search", {"query": "x"})
