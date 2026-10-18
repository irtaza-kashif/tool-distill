"""The demo script, driven by a scripted fake model against the fake invoice tools. No network."""

import itertools
import json
from types import SimpleNamespace
from typing import Any

import pytest
import tools as invoice
from agent_loop import run_agent
from helpers import FakeDistiller

from tooldistill import ToolDistill
from tooldistill.core.models import REASON_AVOIDED_TOOL_RECOVERED, CallKind, Proposal, Status

TOOL_NAME = "create_invoice_from_email"
SWITCHES = ("import_outage", "schema_currency", "tools_updated")
TOOL_SCHEMA = {"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]}

PROPOSED_V1 = '''from tooldistill import distilled_tool, call


@distilled_tool(
    name="create_invoice_from_email",
    description="Create a draft invoice for a customer from their most recent order email.",
    input_schema={"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]},
)
def create_invoice_from_email(customer: str) -> dict:
    messages = call("gmail_search", query=f"from:{customer} order", since="7d")
    order = call("gmail_get_message", id=messages[0]["id"])
    cust = call("billing_find_customer", name=customer)
    return call("billing_create_invoice", customer_id=cust["id"], lines=order["lines"])


def check(result: dict) -> bool:
    return result.get("status") == "draft"
'''
REBUILT_V2 = '''from tooldistill import distilled_tool, call


@distilled_tool(
    name="create_invoice_from_email",
    description="Import a customer's most recent order email as a draft invoice.",
    input_schema={"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]},
)
def create_invoice_from_email(customer: str) -> dict:
    messages = call("gmail_search", query=f"from:{customer} order", since="7d")
    return call("billing_import_from_email", message_id=messages[0]["id"])


def check(result: dict) -> bool:
    return result.get("status") == "draft"
'''
PROPOSED_V3 = REBUILT_V2.replace('message_id=messages[0]["id"])', 'message_id=messages[0]["id"], currency="USD")').replace(
    "Import a customer's most recent order email as a draft invoice.",
    "Import a customer's most recent order email as a draft invoice in USD.",
)
UPDATED_DEFINITIONS_V2 = PROPOSED_V1.replace('lines=order["lines"])', 'lines=order["lines"], currency="USD")')

STRUCTURED_ERROR_V2 = (
    "Distilled tool 'create_invoice_from_email' (v2) failed at step 2 of 2.\n"
    "Base tool: billing_import_from_email\n"
    "Error: missing required property 'currency'\n"
    "This distilled tool has been disabled. Complete the task using the base tools directly."
)

# ------------------------------------------------------------------------ scripted model

_ids = itertools.count(1)


class Response:
    def __init__(self, content: list[Any], stop_reason: str = "tool_use") -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = SimpleNamespace(input_tokens=500, output_tokens=50)


def tool_use(name: str, input: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=input)


def text(content: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=content)


def end_turn(content: str = "Done.") -> Response:
    return Response([text(content)], stop_reason="end_turn")


def last_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for message in reversed(messages):
        if message["role"] == "user" and isinstance(message["content"], list):
            return [block for block in message["content"] if block.get("type") == "tool_result"]
    return []


def last_user_text(messages: list[dict[str, Any]]) -> str:
    last = messages[-1]
    assert last["role"] == "user" and isinstance(last["content"], str), "expected a plain user message"
    return last["content"]


def result_json(messages: list[dict[str, Any]], index: int = 0) -> Any:
    block = last_results(messages)[index]
    assert not block["is_error"], block["content"]
    return json.loads(block["content"])


def propose(payload: dict[str, Any]) -> Response:
    return Response([tool_use("propose_distilled_tool", payload)])


class ScriptedModel:
    """Stands in for anthropic.Anthropic(): messages.create pops the next scripted turn."""

    def __init__(self, turns: list[Any]) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Response:
        self.requests.append(kwargs)
        assert self.turns, "the script has no more turns"
        turn = self.turns.pop(0)
        return turn(kwargs["messages"]) if callable(turn) else turn

    @property
    def tool_names_offered(self) -> list[list[str]]:
        return [[t["name"] for t in request["tools"]] for request in self.requests]


# ------------------------------------------------------------------------------ scripts


def discovery_script(customer: str, memory: dict[str, Any]) -> list[Any]:
    """Demo step 1: 11 base tool calls of trial and error (two of them the broken shortcut), then a proposal."""

    def get_message(messages):
        memory["message_id"] = result_json(messages)[0]["id"]
        return Response([tool_use("gmail_get_message", {"id": memory["message_id"]})])

    def read_attachment(messages):
        memory["order"] = result_json(messages)
        attachment = memory["order"]["attachments"][0]["id"]
        return Response([tool_use("gmail_get_attachment", {"message_id": memory["message_id"], "attachment_id": attachment})])

    def try_import(messages):
        return Response([tool_use("billing_import_from_email", {"message_id": memory["message_id"]})])

    def retry_import(messages):
        failed = last_results(messages)[0]
        assert failed["is_error"] and "503" in failed["content"]
        return try_import(messages)

    def search_customers(messages):
        assert last_results(messages)[0]["is_error"]
        return Response([tool_use("billing_search_customers", {"query": customer.lower()})])

    def find_customer(messages):
        return Response([tool_use("billing_find_customer", {"name": customer})])

    def distractors(messages):
        memory["customer"] = result_json(messages)
        return Response([tool_use("crm_get_notes", {"customer_id": memory["customer"]["id"]}), tool_use("billing_list_invoices", {"customer_id": memory["customer"]["id"]})])

    def create_invoice(messages):
        assert len(last_results(messages)) == 2
        return Response([tool_use("billing_create_invoice", {"customer_id": memory["customer"]["id"], "lines": memory["order"]["lines"]})])

    def finish(messages):
        memory["invoice"] = result_json(messages)
        return end_turn(f"Created draft invoice {memory['invoice']['id']} for {customer}.")

    def propose_v1(messages):
        nudge = last_user_text(messages)
        assert nudge.startswith("This run succeeded and took 11 base tool calls. Call `propose_distilled_tool` once, now")
        return propose({
            "name": TOOL_NAME,
            "description": "Create a draft invoice for a customer from their most recent order email.",
            "input_schema": TOOL_SCHEMA,
            "code": PROPOSED_V1,
            "test_inputs": {"customer": customer},
        })

    def after_verdict(messages):
        verdict = last_results(messages)[0]
        memory["verdict"] = verdict["content"]
        assert not verdict["is_error"], verdict["content"]
        return end_turn("Saved the distilled tool.")

    return [
        Response([tool_use("gmail_list_labels", {})]),
        Response([tool_use("gmail_search", {"query": f"from:{customer} order", "since": "7d"})]),
        get_message,
        read_attachment,
        try_import,
        retry_import,
        search_customers,
        find_customer,
        distractors,
        create_invoice,
        finish,
        propose_v1,
        after_verdict,
    ]


def reuse_script(customer: str, memory: dict[str, Any]) -> list[Any]:
    """Demo step 2: one distilled tool call does the whole task."""

    def finish(messages):
        memory["invoice"] = result_json(messages)
        return end_turn("Done via the distilled tool.")

    return [Response([tool_use(TOOL_NAME, {"customer": customer})]), finish]


def fallback_script(customer: str, memory: dict[str, Any]) -> list[Any]:
    """Demo step 4: the rebuilt (import-based) tool breaks on the schema change; the model falls back and proposes v3."""

    def after_structured_error(messages):
        failed = last_results(messages)[0]
        assert failed["is_error"] and failed["content"] == STRUCTURED_ERROR_V2
        return Response([tool_use("gmail_search", {"query": f"from:{customer} order", "since": "7d"})])

    def import_without_currency(messages):
        memory["message_id"] = result_json(messages)[0]["id"]
        return Response([tool_use("billing_import_from_email", {"message_id": memory["message_id"]})])

    def import_with_currency(messages):
        failed = last_results(messages)[0]
        assert failed["is_error"] and failed["content"] == "missing required property 'currency'"
        return Response([tool_use("billing_import_from_email", {"message_id": memory["message_id"], "currency": "USD"})])

    def finish(messages):
        memory["invoice"] = result_json(messages)
        return end_turn("Imported the invoice with the base tools.")

    def propose_v3(messages):
        nudge = last_user_text(messages)
        assert nudge.startswith(f"The distilled tool '{TOOL_NAME}' failed in this run and has been disabled")
        assert f"same name, '{TOOL_NAME}'" in nudge
        return propose({
            "name": TOOL_NAME,
            "description": "Import a customer's most recent order email as a draft invoice in USD.",
            "input_schema": TOOL_SCHEMA,
            "code": PROPOSED_V3,
            "test_inputs": {"customer": customer},
        })

    def after_verdict(messages):
        verdict = last_results(messages)[0]
        memory["verdict"] = verdict["content"]
        assert not verdict["is_error"], verdict["content"]
        return end_turn("Replaced the distilled tool.")

    return [
        Response([tool_use(TOOL_NAME, {"customer": customer})]),
        after_structured_error,
        import_without_currency,
        import_with_currency,
        finish,
        propose_v3,
        after_verdict,
    ]


def updated_definitions_script(customer: str, memory: dict[str, Any]) -> list[Any]:
    """Step 4, other variant: the definitions changed too, the old tool is already gone, the model goes straight to v2."""

    def get_message(messages):
        memory["message_id"] = result_json(messages)[0]["id"]
        return Response([tool_use("gmail_get_message", {"id": memory["message_id"]})])

    def find_customer(messages):
        memory["order"] = result_json(messages)
        return Response([tool_use("billing_find_customer", {"name": customer})])

    def create(messages):
        memory["customer"] = result_json(messages)
        return Response([tool_use("billing_create_invoice", {"customer_id": memory["customer"]["id"], "lines": memory["order"]["lines"], "currency": "USD"})])

    def finish(messages):
        memory["invoice"] = result_json(messages)
        return end_turn("Created the invoice.")

    def propose_v2(messages):
        nudge = last_user_text(messages)
        assert nudge.startswith("This run succeeded and took 4 base tool calls.")
        return propose({
            "name": TOOL_NAME,
            "description": "Create a draft invoice for a customer from their most recent order email.",
            "input_schema": TOOL_SCHEMA,
            "code": UPDATED_DEFINITIONS_V2,
            "test_inputs": {"customer": customer},
        })

    def after_verdict(messages):
        verdict = last_results(messages)[0]
        memory["verdict"] = verdict["content"]
        assert not verdict["is_error"], verdict["content"]
        return end_turn("Saved the new version.")

    return [
        Response([tool_use("gmail_search", {"query": f"from:{customer} order", "since": "7d"})]),
        get_message,
        find_customer,
        create,
        finish,
        propose_v2,
        after_verdict,
    ]


# --------------------------------------------------------------------------------- tests


@pytest.fixture
def world():
    invoice.reset()
    for name in SWITCHES:
        invoice.set_switch(name, None)
    yield invoice
    invoice.reset()
    for name in SWITCHES:
        invoice.set_switch(name, None)


def make_td(store, world, *, tools=None, distiller=None, check_schemas_on_start=True) -> ToolDistill:
    return ToolDistill(
        store,
        executor=world.execute_tool,
        tools=tools if tools is not None else world.tool_definitions(currency=False),
        health_probe=world.probe,
        distiller=distiller,
        auto_promote=True,
        allow_live_verification=True,
        min_calls_to_distill=4,
        fallback_distiller=False,
        check_schemas_on_start=check_schemas_on_start,
    )


def run_discovery(td, world) -> tuple[Any, dict[str, Any]]:
    world.set_switch("import_outage", True)
    memory: dict[str, Any] = {}
    model = ScriptedModel(discovery_script("ACME", memory))
    report = run_agent(td, model, world.task_for("ACME"), task_succeeded=world.task_succeeded)
    assert model.turns == [] and report.outcome is True
    assert memory["verdict"] == "verified: 4 base tool calls vs baseline 11"
    return report, memory


def test_demo_script_with_a_stale_tool_list(store, world):
    """Steps 1-4 of the demo. The tool list handed to ToolDistill never learns about `currency`."""
    rebuilt = Proposal(TOOL_NAME, "Import a customer's most recent order email as a draft invoice.", TOOL_SCHEMA, REBUILT_V2, {"customer": "ACME"})
    distiller = FakeDistiller(rebuilt)
    td = make_td(store, world, distiller=distiller)
    registry = td.registry

    # ---- step 1: outage on, discovery run, proposal, verified, promoted -------------------------
    report, memory = run_discovery(td, world)
    assert report.base_calls == 11 and report.distilled_calls == 0 and report.propose_calls == 1
    assert report.nudged and report.model_turns == 13
    assert (report.input_tokens, report.output_tokens) == (13 * 500, 13 * 50)
    assert world.task_succeeded(world.task_for("ACME")) and memory["invoice"]["id"] == "inv_0001"
    v1 = registry.get_version(TOOL_NAME, 1)
    assert (v1.status, v1.status_reason) == (Status.ACTIVE, "auto_promote")
    assert v1.baseline_calls == 11 and v1.measured_calls == 4 and v1.source_trace_id == report.trace_id
    assert [a.base_tool for a in registry.list_avoided_tools(TOOL_NAME, 1)] == ["billing_import_from_email"]
    assert [d.base_tool for d in registry.list_dependencies(TOOL_NAME, 1)] == [
        "billing_create_invoice", "billing_find_customer", "gmail_get_message", "gmail_search",
    ]
    assert [t["name"] for t in td.active_tools()] == [TOOL_NAME]
    assert registry.code_file(v1).read_text(encoding="utf-8") == PROPOSED_V1

    # ---- step 2: a new customer, one distilled call (4 base calls inside) -----------------------
    memory = {}
    model = ScriptedModel(reuse_script("Globex", memory))
    report = run_agent(td, model, world.task_for("Globex"), task_succeeded=world.task_succeeded)
    assert model.turns == [] and report.outcome is True and report.model_turns == 2
    assert TOOL_NAME in model.tool_names_offered[0]
    assert report.distilled_calls == 1 and report.distilled_failures == 0 and report.inner_calls == 4
    assert report.base_calls == 0 and report.propose_calls == 0 and not report.nudged
    assert memory["invoice"]["customer_id"] == "cust_globex" and world.task_succeeded(world.task_for("Globex"))
    stats = registry.list_run_stats(TOOL_NAME, 1)
    assert len(stats) == 1 and stats[0].success and stats[0].calls == 4 and stats[0].trace_id == report.trace_id

    # ---- step 3: the outage ends; tool-updated -> suspect(avoided_tool_recovered) -> rebuild ----
    world.set_switch("import_outage", False)
    update = td.on_tool_updated("billing_import_from_email")
    [action] = update.actions
    assert (action.version, action.to_status, action.reason, action.detail) == (1, Status.SUSPECT, REASON_AVOIDED_TOOL_RECOVERED, "queued for rebuild")
    assert [t["name"] for t in td.active_tools()] == [TOOL_NAME]  # still exposed while it waits for the rebuild
    assert [(i.version, i.base_tool) for i in registry.list_rebuild_queue()] == [(1, "billing_import_from_email")]
    # the health probe would have found the same recovery on its own
    assert td.health().recovered == ["billing_import_from_email"] and td.health().actions == []

    rebuild = td.rebuild(TOOL_NAME)
    assert rebuild.rebuilt and rebuild.message == f"rebuilt '{TOOL_NAME}' as v2: verified: 2 base tool calls vs baseline 11"
    assert distiller.calls[0]["name"] == TOOL_NAME and "'billing_import_from_email' is available again" in distiller.calls[0]["note"]
    assert distiller.calls[0]["trace_id"] == v1.source_trace_id
    v2 = registry.get_version(TOOL_NAME, 2)
    assert (v2.status, v2.status_reason, v2.measured_calls, v2.baseline_calls) == (Status.ACTIVE, "auto_promote", 2, 11)
    assert rebuild.result is not None and rebuild.result.report is not None and rebuild.result.report.used_live
    assert [f.base_tool for f in registry.list_fixtures(TOOL_NAME, 2)][-1] == "billing_import_from_email"  # saved live
    assert registry.list_rebuild_queue() == []
    assert (registry.get_version(TOOL_NAME, 1).status, registry.get_version(TOOL_NAME, 1).status_reason) == (Status.DEPRECATED, "superseded_by_v2")
    assert [d.base_tool for d in registry.list_dependencies(TOOL_NAME, 2)] == ["billing_import_from_email", "gmail_search"]
    assert td.active_tools()[0]["description"] == "Import a customer's most recent order email as a draft invoice."

    # ---- step 4: the billing API now requires currency; the tool definitions do not know yet ----
    world.set_switch("schema_currency", True)
    memory = {}
    model = ScriptedModel(fallback_script("Initech", memory))
    report = run_agent(td, model, world.task_for("Initech"), task_succeeded=world.task_succeeded)
    assert model.turns == [] and report.outcome is True and report.model_turns == 7
    assert report.distilled_calls == 1 and report.distilled_failures == 1 and report.inner_calls == 2
    assert report.base_calls == 3 and report.propose_calls == 1 and report.nudged
    assert report.steps[0] == (TOOL_NAME, True) and report.steps[-1] == ("propose_distilled_tool", False)
    assert memory["verdict"] == "verified: 2 base tool calls vs baseline 11"
    assert world.task_succeeded(world.task_for("Initech")) and memory["invoice"]["currency"] == "USD"

    assert [(e.to_status, e.reason) for e in registry.list_status_events(TOOL_NAME, 2)] == [
        (Status.DRAFT, "pending_verification"),
        (Status.VERIFIED, "checks_passed"),
        (Status.ACTIVE, "auto_promote"),
        (Status.SUSPECT, "runtime_failure"),
        (Status.DEPRECATED, "superseded_by_v3"),
    ]
    v3 = registry.get_version(TOOL_NAME, 3)
    assert (v3.status, v3.status_reason) == (Status.ACTIVE, "auto_promote")
    assert v3.baseline_calls == 11 and v3.measured_calls == 2 and v3.source_trace_id == report.trace_id
    assert registry.list_avoided_tools(TOOL_NAME, 3) == []
    assert td.active_tools()[0]["description"].endswith("in USD.")
    failures = [s for s in registry.list_run_stats(TOOL_NAME, 2) if not s.success]
    assert len(failures) == 1 and failures[0].error == "missing required property 'currency'" and failures[0].calls == 2
    calls = registry.list_tool_calls(report.trace_id)
    assert [(c.kind, c.is_error) for c in calls[:3]] == [(CallKind.DISTILLED, True), (CallKind.BASE, False), (CallKind.BASE, True)]
    assert calls[0].error == STRUCTURED_ERROR_V2
    td.close()


def test_demo_step_4_with_updated_tool_definitions(store, world):
    """The other step-4 variant: the definitions changed too, so the startup check catches it before the first run."""
    td = make_td(store, world)
    run_discovery(td, world)
    assert td.registry.get_version(TOOL_NAME, 1).status is Status.ACTIVE
    td.close()

    world.set_switch("schema_currency", True)
    td = make_td(store, world, tools=world.tool_definitions(currency=True))
    assert td.startup_report is not None and td.startup_report.changed == ["billing_create_invoice"]
    [action] = td.startup_report.actions
    assert (action.version, action.from_status, action.to_status, action.reason) == (1, Status.ACTIVE, Status.DEPRECATED, "static_failed")
    assert "missing required property 'currency'" in action.detail
    assert [(e.to_status, e.reason) for e in td.registry.list_status_events(TOOL_NAME, 1)][-2:] == [
        (Status.SUSPECT, "dependency_changed"),
        (Status.DEPRECATED, "static_failed"),
    ]
    assert td.active_tools() == []

    memory: dict[str, Any] = {}
    model = ScriptedModel(updated_definitions_script("Globex", memory))
    report = run_agent(td, model, world.task_for("Globex"), task_succeeded=world.task_succeeded)
    assert model.turns == [] and report.outcome is True and report.model_turns == 7
    assert TOOL_NAME not in model.tool_names_offered[0]  # the deprecated version was not offered
    assert report.base_calls == 4 and report.distilled_calls == 0 and report.nudged
    assert memory["verdict"] == "verified: 4 base tool calls vs baseline 11"  # baseline inherited from v1
    v2 = td.registry.get_version(TOOL_NAME, 2)
    assert (v2.status, v2.status_reason, v2.measured_calls) == (Status.ACTIVE, "auto_promote", 4)
    assert [d.base_tool for d in td.registry.list_dependencies(TOOL_NAME, 2)] == [
        "billing_create_invoice", "billing_find_customer", "gmail_get_message", "gmail_search",
    ]
    td.close()
