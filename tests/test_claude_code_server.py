"""The plugin's MCP server, exercised in-process (no transport, no network)."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import tools as invoice
from helpers import GOOD_INVOICE, INVOICE_NAME as NAME, acme_executor, acme_fixtures, add_version, sample_tools

from tooldistill import ToolDistill
from tooldistill.core import lifecycle
from tooldistill.core.models import REASON_RUNTIME_FAILURE, CallKind, Status
from tooldistill.core.registry import Registry
from tooldistill.core.tools import PROPOSE_TOOL_NAME
from tooldistill.integrations.claude_code.hooks import handle_hook
from tooldistill.integrations.claude_code.names import DEFAULT_OWN_PREFIX
from tooldistill.integrations.claude_code.server import DistillServer, plugin_propose_schema, with_trace_id

pytestmark = pytest.mark.anyio

PROPOSAL = {
    "name": NAME,
    "description": "Create a draft invoice for a customer from their most recent order email.",
    "input_schema": {"type": "object", "properties": {"customer": {"type": "string"}, "since": {"type": "string", "default": "1d"}}, "required": ["customer"]},
    "code": GOOD_INVOICE,
    "test_inputs": {"customer": "ACME"},
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeSession:
    def __init__(self) -> None:
        self.notifications = 0

    async def send_tool_list_changed(self) -> None:
        self.notifications += 1


class FakeContext:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.notified = 0

    async def notify_tools_changed(self) -> None:
        self.notified += 1


def make_td(store: Path, executor=acme_executor, tools=None, **settings) -> ToolDistill:
    return ToolDistill(
        store, executor, tools if tools is not None else sample_tools(),
        auto_promote=True, allow_live_verification=True, min_calls_to_distill=4, fallback_distiller=False, **settings,
    )


def discovery_trace(td: ToolDistill, trace_id: str) -> None:
    """Record the eight-call ACME discovery under a session id, the way the hooks would."""
    registry = td.registry
    registry.ensure_trace(trace_id, "Claude Code session in /work")
    from tooldistill.core.serialization import canonical_json as cj

    for i in range(4):
        registry.add_tool_call(trace_id, kind=CallKind.BASE, tool_name="gmail_search", input_json=cj({"query": f"dead end {i}"}), output_json="[]")
    for base_tool, input_json, output_json in acme_fixtures():
        registry.add_tool_call(trace_id, kind=CallKind.BASE, tool_name=base_tool, input_json=input_json, output_json=output_json)


def text_of(result) -> str:
    return result.content[0].text


# ------------------------------------------------------------------------------ surface


async def test_list_tools_without_a_config(tmp_path: Path):
    server = DistillServer(tmp_path / "missing.toml")
    tools = await server.list_tools()
    assert [t.name for t in tools] == [PROPOSE_TOOL_NAME]
    assert tools[0].input_schema["required"][-1] == "trace_id" and "task" in tools[0].input_schema["properties"]
    result = await server.call_tool("anything", {"trace_id": "x"})
    assert result.is_error and "not configured" in text_of(result) and "missing.toml" in text_of(result)
    assert server.td is None and "no tooldistill.toml" in server.unavailable_reason
    server.close()


def test_schema_helpers():
    schema = plugin_propose_schema()
    assert schema["properties"]["trace_id"]["type"] == "string" and schema["required"].count("trace_id") == 1
    assert "code" in schema["required"]
    base = {"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]}
    assert with_trace_id(base)["properties"]["trace_id"] and "trace_id" not in base["properties"]
    assert with_trace_id(base)["required"] == ["customer"]  # optional on distilled tools


async def test_propose_and_run_through_the_server(store: Path):
    td = make_td(store)
    server = DistillServer(None, td=td)
    discovery_trace(td, "sess-a")
    tools = await server.list_tools()
    assert [t.name for t in tools] == [PROPOSE_TOOL_NAME]

    context = FakeContext()
    result = await server.call_tool(PROPOSE_TOOL_NAME, {**PROPOSAL, "trace_id": "sess-a", "task": "Invoice ACME"}, context)
    assert not result.is_error, text_of(result)
    assert text_of(result) == "verified: 4 base tool calls vs baseline 8"
    assert td.registry.get_trace("sess-a").outcome is True and td.registry.get_trace("sess-a").task == "Invoice ACME"
    assert context.session.notifications == 1 and context.notified == 1  # the exposed set changed

    tools = await server.list_tools()
    assert [t.name for t in tools] == [PROPOSE_TOOL_NAME, NAME]
    assert "trace_id" in tools[1].input_schema["properties"] and tools[1].input_schema["required"] == ["customer"]
    assert tools[1].description.startswith("Create a draft invoice")

    td.registry.ensure_trace("sess-b", "Claude Code session in /work")
    result = await server.call_tool(NAME, {"customer": "Globex", "trace_id": "sess-b"}, context)
    assert not result.is_error and json.loads(text_of(result)) == {"id": "inv1", "status": "draft"}
    calls = td.registry.list_tool_calls("sess-b")
    assert calls[0].kind is CallKind.DISTILLED and [c.tool_name for c in calls[1:]] == ["gmail_search", "gmail_get_message", "billing_find_customer", "billing_create_invoice"]
    assert context.session.notifications == 1  # nothing changed this time
    stats = td.registry.run_stats_for_trace("sess-b")
    assert len(stats) == 1 and stats[0].success

    again = await server.call_tool(PROPOSE_TOOL_NAME, {**PROPOSAL, "trace_id": "sess-a"}, context)
    assert not again.is_error and text_of(again).endswith("; v2 is now active and replaces v1"), text_of(again)
    assert td.registry.get_version(NAME, 1).status_reason == "superseded_by_v2"
    assert context.session.notifications == 2  # the exposed set changed again (v1 out, v2 in)
    server.close()


async def test_trace_id_handling(store: Path):
    td = make_td(store)
    server = DistillServer(None, td=td)
    missing = await server.call_tool(PROPOSE_TOOL_NAME, {**PROPOSAL})
    assert missing.is_error and "needs trace_id" in text_of(missing)
    unknown = await server.call_tool(PROPOSE_TOOL_NAME, {**PROPOSAL, "trace_id": "ghost"})
    assert unknown.is_error and "unknown trace id 'ghost'" in text_of(unknown)
    assert td.registry.get_trace("ghost") is None

    add_version(td.registry, GOOD_INVOICE, fixtures=acme_fixtures())
    td.registry.set_measured_calls(NAME, 1, 4)
    td.registry.set_status(NAME, 1, Status.ACTIVE, "promoted")
    td.registry.ensure_trace("sess-open", "Claude Code session in /work")
    result = await server.call_tool(NAME, {"customer": "ACME"})  # no trace_id: the latest open trace
    assert not result.is_error and td.registry.list_tool_calls("sess-open")[0].kind is CallKind.DISTILLED
    for record in td.registry.list_traces():  # close everything, including the seeded version's source trace
        if record.finished_at is None:
            td.registry.finish_trace(record.id)
    result = await server.call_tool(NAME, {"customer": "ACME"})  # nothing open: a synthetic trace
    assert not result.is_error
    synthetic = [t for t in td.registry.list_traces() if t.id.startswith("plugin-")]
    assert len(synthetic) == 1 and synthetic[0].task == "Claude Code session (id unknown)"
    result = await server.call_tool(NAME, {"customer": "ACME", "trace_id": "sess-explicit"})  # unknown explicit id: falls back too
    assert not result.is_error
    server.close()


async def test_runtime_failure_hides_the_tool_and_notifies(store: Path):
    def executor(name, args):
        if name == "billing_create_invoice":
            raise RuntimeError("missing required property 'currency'")
        return acme_executor(name, args)

    td = make_td(store, executor)
    add_version(td.registry, GOOD_INVOICE, fixtures=acme_fixtures())
    td.registry.set_measured_calls(NAME, 1, 4)
    td.registry.set_status(NAME, 1, Status.ACTIVE, "promoted")
    server = DistillServer(None, td=td)
    assert [t.name for t in await server.list_tools()] == [PROPOSE_TOOL_NAME, NAME]
    context = FakeContext()
    td.registry.ensure_trace("sess-c", "Claude Code session in /work")
    result = await server.call_tool(NAME, {"customer": "ACME", "trace_id": "sess-c"}, context)
    assert result.is_error and text_of(result).startswith(f"Distilled tool '{NAME}' (v1) failed at step 4 of 4.\nBase tool: billing_create_invoice")
    version = td.registry.get_version(NAME, 1)
    assert (version.status, version.status_reason) == (Status.SUSPECT, REASON_RUNTIME_FAILURE)
    assert context.session.notifications == 1
    assert [t.name for t in await server.list_tools()] == [PROPOSE_TOOL_NAME]
    hidden = await server.call_tool(NAME, {"customer": "ACME", "trace_id": "sess-c"}, context)
    assert hidden.is_error and "is not available" in text_of(hidden)
    server.close()


async def test_store_changes_from_another_process_are_noticed(store: Path):
    td = make_td(store)
    add_version(td.registry, GOOD_INVOICE, fixtures=acme_fixtures())
    td.registry.set_status(NAME, 1, Status.ACTIVE, "promoted")
    server = DistillServer(None, td=td, poll_interval=0.05)
    await server.list_tools()
    assert not server.changed(td)
    other = Registry(store)  # the CLI in another process
    lifecycle.deprecate(other, NAME, 1)
    other.close()
    assert server.changed(td) and not server.changed(td)
    # the watcher loop notifies through the captured session
    session = FakeSession()
    server._session = session
    lifecycle.transition(td.registry, NAME, 1, Status.ACTIVE, "rollback")
    import anyio

    async with anyio.create_task_group() as group:
        group.start_soon(server.watch_store)
        for _ in range(40):
            await anyio.sleep(0.05)
            if session.notifications:
                break
        group.cancel_scope.cancel()
    assert session.notifications == 1
    server.close()


# ------------------------------------------------------------------- the plugin story


async def test_plugin_story_with_hooks_and_server(tmp_path: Path, monkeypatch):
    """SessionStart -> hook-recorded discovery -> propose via the server -> reuse -> schema change -> v2."""
    monkeypatch.delenv("TOOLDISTILL_CONFIG", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    invoice.reset()
    for name in ("import_outage", "schema_currency", "tools_updated"):
        invoice.set_switch(name, None)
    config = tmp_path / "tooldistill.toml"
    config.write_text('store = ".tooldistill"\n', encoding="utf-8")
    prefixed = [{**t, "name": f"invoice__{t['name']}"} for t in invoice.tool_definitions(currency=False)]

    def executor(name: str, args: dict[str, Any]) -> Any:
        return invoice.execute_tool(name.removeprefix("invoice__"), args)

    td = make_td(tmp_path / ".tooldistill", executor, prefixed)
    server = DistillServer(config, td=td)
    tool = "create_invoice_from_email"
    code = '''from tooldistill import distilled_tool, call


@distilled_tool(
    name="create_invoice_from_email",
    description="Create a draft invoice for a customer from their most recent order email.",
    input_schema={"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]},
)
def create_invoice_from_email(customer: str) -> dict:
    messages = call("invoice__gmail_search", query=f"from:{customer} order", since="7d")
    order = call("invoice__gmail_get_message", id=messages[0]["id"])
    cust = call("invoice__billing_find_customer", name=customer)
    return call("invoice__billing_create_invoice", customer_id=cust["id"], lines=order["lines"])
'''

    def hook_call(session: str, name: str, args: dict[str, Any]) -> Any:
        """Claude calls an MCP tool: the fake server answers, the PostToolUse hook records it."""
        base = payload = {"session_id": session, "cwd": str(tmp_path), "tool_name": f"mcp__invoice__{name}", "tool_input": args, "mcp_server": {"name": "invoice", "source": "project"}, "duration_ms": 5}
        try:
            value = invoice.execute_tool(name, args)
        except invoice.ToolError as exc:
            result = handle_hook("PostToolUseFailure", {**payload, "error": str(exc), "is_interrupt": False}, config=config)
            assert result.exit_code == 0, result.stderr
            return None
        response = {"content": [{"type": "text", "text": json.dumps(value)}], "isError": False}
        result = handle_hook("PostToolUse", {**base, "tool_response": response}, config=config)
        assert result.exit_code == 0, result.stderr
        return value

    # session A: discovery with the shortcut down
    invoice.set_switch("import_outage", True)
    start = handle_hook("SessionStart", {"session_id": "sess-A", "cwd": str(tmp_path)}, config=config)
    assert "No distilled tools exist yet." in json.loads(start.stdout)["hookSpecificOutput"]["additionalContext"]
    hits = hook_call("sess-A", "gmail_search", {"query": "from:ACME order", "since": "7d"})
    hook_call("sess-A", "billing_import_from_email", {"message_id": hits[0]["id"]})  # 503 -> PostToolUseFailure
    order = hook_call("sess-A", "gmail_get_message", {"id": hits[0]["id"]})
    customer = hook_call("sess-A", "billing_find_customer", {"name": "ACME"})
    hook_call("sess-A", "crm_get_notes", {"customer_id": customer["id"]})
    hook_call("sess-A", "billing_create_invoice", {"customer_id": customer["id"], "lines": order["lines"]})
    handle_hook("PostToolUse", {"session_id": "sess-A", "cwd": str(tmp_path), "tool_name": "Read", "tool_input": {"file_path": "notes.md"}, "tool_response": "..."}, config=config)
    calls = td.registry.list_tool_calls("sess-A")
    assert [c.tool_name for c in calls] == ["invoice__gmail_search", "invoice__billing_import_from_email", "invoice__gmail_get_message", "invoice__billing_find_customer", "invoice__crm_get_notes", "invoice__billing_create_invoice", "builtin__Read"]
    assert calls[1].is_error and calls[1].error == "503 Service Unavailable"

    # /distill -> propose through the server, replayed against the hook-recorded fixtures
    proposal = {"name": tool, "description": "Create a draft invoice for a customer from their most recent order email.", "input_schema": {"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]}, "code": code, "test_inputs": {"customer": "ACME"}, "trace_id": "sess-A", "task": "Create a draft invoice for ACME"}
    context = FakeContext()
    result = await server.call_tool(PROPOSE_TOOL_NAME, proposal, context)
    assert text_of(result) == "verified: 4 base tool calls vs baseline 7", text_of(result)
    assert td.registry.get_version(tool, 1).status is Status.ACTIVE
    assert [a.base_tool for a in td.registry.list_avoided_tools(tool, 1)] == ["invoice__billing_import_from_email"]
    assert context.session.notifications == 1

    # session B: the distilled tool does the whole task
    start = handle_hook("SessionStart", {"session_id": "sess-B", "cwd": str(tmp_path)}, config=config)
    assert f"{DEFAULT_OWN_PREFIX}{tool}: Create a draft invoice" in json.loads(start.stdout)["hookSpecificOutput"]["additionalContext"]
    result = await server.call_tool(tool, {"customer": "Globex", "trace_id": "sess-B"}, context)
    assert not result.is_error and json.loads(text_of(result))["customer_id"] == "cust_globex"
    assert invoice.task_succeeded(invoice.task_for("Globex"))
    handle_hook("PostToolUse", {"session_id": "sess-B", "cwd": str(tmp_path), "tool_name": DEFAULT_OWN_PREFIX + tool, "tool_input": {"customer": "Globex", "trace_id": "sess-B"}, "tool_response": text_of(result)}, config=config)
    b_calls = td.registry.list_tool_calls("sess-B")
    assert [c.kind for c in b_calls] == [CallKind.DISTILLED] + [CallKind.BASE] * 4  # the hook did not double-record

    # session C: the billing API now requires currency (the tool definitions are stale)
    invoice.set_switch("import_outage", False)
    invoice.set_switch("schema_currency", True)
    handle_hook("SessionStart", {"session_id": "sess-C", "cwd": str(tmp_path)}, config=config)
    result = await server.call_tool(tool, {"customer": "Initech", "trace_id": "sess-C"}, context)
    assert result.is_error and "failed at step 4 of 4" in text_of(result) and "Base tool: invoice__billing_create_invoice" in text_of(result)
    assert td.registry.get_version(tool, 1).status is Status.SUSPECT and context.session.notifications == 2
    hits = hook_call("sess-C", "gmail_search", {"query": "from:Initech order", "since": "7d"})
    hook_call("sess-C", "billing_import_from_email", {"message_id": hits[0]["id"]})  # fails: currency
    imported = hook_call("sess-C", "billing_import_from_email", {"message_id": hits[0]["id"], "currency": "USD"})
    assert imported["currency"] == "USD"
    v2_code = '''from tooldistill import distilled_tool, call


@distilled_tool(
    name="create_invoice_from_email",
    description="Import a customer's most recent order email as a draft invoice in USD.",
    input_schema={"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]},
)
def create_invoice_from_email(customer: str) -> dict:
    messages = call("invoice__gmail_search", query=f"from:{customer} order", since="7d")
    return call("invoice__billing_import_from_email", message_id=messages[0]["id"], currency="USD")
'''
    result = await server.call_tool(PROPOSE_TOOL_NAME, {**proposal, "code": v2_code, "trace_id": "sess-C", "task": "Create a draft invoice for Initech"}, context)
    assert text_of(result) == "verified: 2 base tool calls vs baseline 7", text_of(result)
    assert td.registry.get_version(tool, 2).status is Status.ACTIVE
    assert td.registry.get_version(tool, 1).status_reason == "superseded_by_v2"
    assert [t.name for t in await server.list_tools()] == [PROPOSE_TOOL_NAME, tool]
    assert (await server.list_tools())[1].description.endswith("in USD.")
    server.close()
    invoice.reset()
    for name in ("import_outage", "schema_currency", "tools_updated"):
        invoice.set_switch(name, None)
