import json
from typing import Any

import pytest
from helpers import (
    GOOD_INVOICE,
    INVOICE_NAME as NAME,
    INVOICE_SCHEMA,
    LINES,
    acme_executor,
    acme_fixtures,
    add_version,
    sample_tools,
)

from tooldistill import ToolDistill
from tooldistill.core.errors import ToolDefinitionError
from tooldistill.core.models import REASON_RUNTIME_FAILURE, CallKind, Proposal, Status
from tooldistill.core.serialization import canonical_json as cj
from tooldistill.core.tools import PROPOSE_TOOL_NAME
from tooldistill.distill.prompts import FILE_FORMAT_RULES

PROPOSAL_PAYLOAD = {
    "name": NAME,
    "description": "Create a draft invoice for a customer from their most recent order email.",
    "input_schema": INVOICE_SCHEMA,
    "code": GOOD_INVOICE,
    "test_inputs": {"customer": "ACME"},
}


class FakeDistiller:
    def __init__(self, proposal: Proposal | None) -> None:
        self.proposal = proposal
        self.calls: list[dict[str, Any]] = []

    def distill(self, trace, tools, *, note=None, name=None):
        self.calls.append({"trace_id": trace.trace.id, "tools": tools.names, "note": note, "name": name})
        return self.proposal


def make_td(store, executor=acme_executor, **settings) -> ToolDistill:
    settings = {"fallback_distiller": False, "min_calls_to_distill": 4, **settings}
    return ToolDistill(store, executor=executor, tools=sample_tools(), **settings)


@pytest.fixture
def td(store):
    instance = make_td(store)
    try:
        yield instance
    finally:
        instance.close()


def run_discovery(trace) -> None:
    """Eight base calls through trace.execute: four dead ends, then exactly the path good_invoice.py takes."""
    for i in range(4):
        trace.execute("gmail_search", {"query": f"dead end {i}"})
    trace.execute("gmail_search", {"query": "from:ACME order", "since": "1d"})
    trace.execute("gmail_get_message", {"id": "m1"})
    trace.execute("billing_find_customer", {"name": "ACME"})
    trace.execute("billing_create_invoice", {"customer_id": "c1", "lines": LINES})


def activate_good_tool(td) -> None:
    add_version(td.registry, fixtures=acme_fixtures())
    td.registry.set_measured_calls(NAME, 1, 4)
    td.registry.set_status(NAME, 1, Status.ACTIVE, "promoted")


# ------------------------------------------------------------------------------ routing


def test_base_tool_call_is_recorded(td):
    trace = td.start_trace("Invoice ACME")
    out = trace.execute("gmail_search", {"query": "from:ACME order"})
    assert not out.is_error and out.content == json.dumps([{"id": "m1"}])
    call = td.registry.list_tool_calls(trace.id)[0]
    assert call.kind is CallKind.BASE and call.tool_name == "gmail_search" and call.parent_call_id is None
    assert call.input_json == cj({"query": "from:ACME order"}) and call.output == [{"id": "m1"}]
    assert call.latency_ms is not None and not call.is_error
    assert trace.execute("gmail_search", None).is_error is False  # missing input means no arguments


def test_base_tool_errors_are_results_not_exceptions(store):
    def executor(name, args):
        if name == "billing_find_customer":
            raise RuntimeError("503 Service Unavailable")
        if name == "gmail_get_message":
            return object()
        return acme_executor(name, args)

    td = make_td(store, executor)
    trace = td.start_trace("t")
    failed = trace.execute("billing_find_customer", {"name": "ACME"})
    assert failed.is_error and failed.content == "503 Service Unavailable"
    weird = trace.execute("gmail_get_message", {"id": "m1"})
    assert weird.is_error and "non-JSON-serializable" in weird.content
    calls = td.registry.list_tool_calls(trace.id)
    assert [c.is_error for c in calls] == [True, True] and calls[0].error == "503 Service Unavailable"
    td.close()


def test_unknown_tool(td):
    trace = td.start_trace("t")
    out = trace.execute("nope", {})
    assert out.is_error and out.content == "Unknown tool: nope"
    assert td.registry.list_tool_calls(trace.id) == []


def test_tool_definitions_and_name_clash(td, store):
    assert td.propose_tool()["name"] == PROPOSE_TOOL_NAME and "input_schema" in td.propose_tool()
    assert td.propose_tool()["description"].endswith(FILE_FORMAT_RULES.splitlines()[-1])
    assert td.active_tools() == []
    add_version(td.registry, name="gmail_search", code=GOOD_INVOICE.replace(f'name="{NAME}"', 'name="gmail_search"'))
    with pytest.raises(ToolDefinitionError, match="collide with existing distilled tools: gmail_search"):
        make_td(store)


# ------------------------------------------------------------------------- distilled tools


def test_distilled_tool_runs_live_and_records_children(td):
    activate_good_tool(td)
    assert [t["name"] for t in td.active_tools()] == [NAME]
    trace = td.start_trace("Invoice ACME")
    out = trace.execute(NAME, {"customer": "ACME"})
    assert not out.is_error and json.loads(out.content) == {"id": "inv1", "status": "draft"}
    calls = td.registry.list_tool_calls(trace.id)
    parent, children = calls[0], calls[1:]
    assert parent.kind is CallKind.DISTILLED and parent.version == 1 and parent.output == {"id": "inv1", "status": "draft"}
    assert [c.tool_name for c in children] == ["gmail_search", "gmail_get_message", "billing_find_customer", "billing_create_invoice"]
    assert all(c.kind is CallKind.BASE and c.parent_call_id == parent.id and not c.is_error for c in children)
    stats = td.registry.run_stats_for_trace(trace.id)
    assert len(stats) == 1 and stats[0].success and stats[0].calls == 4 and stats[0].call_id == parent.id
    trace.set_outcome(True)
    assert not trace.worth_distilling()  # a successful distilled call means the run was already short
    assert trace.distill_nudge() is None


def test_distilled_tool_input_validation_does_not_demote(td):
    activate_good_tool(td)
    trace = td.start_trace("t")
    out = trace.execute(NAME, {"customer": 5})
    assert out.is_error and out.content.startswith(f"Invalid input for distilled tool '{NAME}': 5 is not of type 'string'")
    assert td.registry.get_version(NAME, 1).status is Status.ACTIVE
    assert td.registry.run_stats_for_trace(trace.id) == []
    row = td.registry.list_tool_calls(trace.id)[0]
    assert row.kind is CallKind.DISTILLED and row.is_error and row.version == 1


def test_distilled_tool_runtime_failure_returns_structured_error_and_demotes(store):
    def executor(name, args):
        if name == "billing_create_invoice":
            raise RuntimeError("missing required property 'currency'")
        return acme_executor(name, args)

    td = make_td(store, executor)
    activate_good_tool(td)
    trace = td.start_trace("Invoice ACME")
    out = trace.execute(NAME, {"customer": "ACME"})
    assert out.is_error
    assert out.content == (
        "Distilled tool 'create_invoice_from_email' (v1) failed at step 4 of 4.\n"
        "Base tool: billing_create_invoice\n"
        "Error: missing required property 'currency'\n"
        "This distilled tool has been disabled. Complete the task using the base tools directly."
    )
    version = td.registry.get_version(NAME, 1)
    assert (version.status, version.status_reason) == (Status.SUSPECT, REASON_RUNTIME_FAILURE)
    calls = td.registry.list_tool_calls(trace.id)
    assert calls[0].is_error and calls[0].error == out.content
    assert [c.is_error for c in calls[1:]] == [False, False, False, True]
    stats = td.registry.run_stats_for_trace(trace.id)
    assert len(stats) == 1 and not stats[0].success and stats[0].error == "missing required property 'currency'"
    assert td.active_tools() == []
    # the tool is now hidden; calling it again is answered without running it
    again = trace.execute(NAME, {"customer": "ACME"})
    assert again.is_error and again.content.startswith(f"Distilled tool '{NAME}' is not available (v1: suspect, runtime_failure)")
    assert len(td.registry.run_stats_for_trace(trace.id)) == 1
    # the fallback run that follows is worth distilling even without many base calls
    trace.set_outcome(True)
    assert trace.worth_distilling() and trace.failed_distilled_tool() == NAME
    nudge = trace.distill_nudge()
    assert nudge is not None and nudge.startswith(f"The distilled tool '{NAME}' failed in this run")
    assert f"same name, '{NAME}'" in nudge
    td.close()


def test_modified_tool_file_is_a_runtime_failure(td):
    activate_good_tool(td)
    version = td.registry.get_version(NAME, 1)
    td.registry.code_file(version).write_text(GOOD_INVOICE + "\n# tampered\n", encoding="utf-8")
    trace = td.start_trace("t")
    out = trace.execute(NAME, {"customer": "ACME"})
    assert out.is_error and out.content.startswith(f"Distilled tool '{NAME}' (v1) failed after step 0 of 4.\nError: ")
    assert "modified after it was verified" in out.content
    assert td.registry.get_version(NAME, 1).status is Status.SUSPECT


# --------------------------------------------------------------------- proposing / nudging


def test_propose_flow_through_the_trace(td):
    trace = td.start_trace("Create a draft invoice for ACME from their most recent order email.")
    run_discovery(trace)
    assert trace.distill_nudge() is None  # no outcome yet
    trace.set_outcome(True)
    assert trace.outcome is True and trace.worth_distilling()
    nudge = trace.distill_nudge()
    assert nudge is not None
    assert nudge.startswith("This run succeeded and took 8 base tool calls. Call `propose_distilled_tool` once, now")
    assert "Task: Create a draft invoice for ACME" in nudge and FILE_FORMAT_RULES in nudge
    assert "Base tools available: gmail_search, gmail_get_message" in nudge
    assert trace.distill_nudge() is None  # at most once per trace
    assert td.registry.get_trace(trace.id).nudged

    out = trace.execute(PROPOSE_TOOL_NAME, PROPOSAL_PAYLOAD)
    assert not out.is_error and out.content == "verified: 4 base tool calls vs baseline 8"
    row = td.registry.list_tool_calls(trace.id)[-1]
    assert row.kind is CallKind.PROPOSE and not row.is_error and row.input["_source"] == "agent"
    assert td.registry.get_version(NAME, 1).status is Status.VERIFIED
    assert not trace.worth_distilling()  # the proposal is in
    assert trace.finish() is None


def test_propose_with_invalid_payload(td):
    trace = td.start_trace("t")
    out = trace.execute(PROPOSE_TOOL_NAME, {"name": NAME})
    assert out.is_error and out.content.startswith("rejected (proposal): invalid propose_distilled_tool input")
    row = td.registry.list_tool_calls(trace.id)[-1]
    assert row.kind is CallKind.PROPOSE and row.is_error and row.error == out.content
    assert td.registry.list_versions() == []


def test_worth_distilling_matrix(td):
    trace = td.start_trace("t")
    assert not trace.worth_distilling()
    for _ in range(3):
        trace.execute("gmail_search", {"query": "x"})
    trace.set_outcome(False)
    assert not trace.worth_distilling()
    trace.set_outcome(True)
    assert not trace.worth_distilling()  # 3 < min_calls_to_distill
    trace.execute("gmail_search", {"query": "x"})
    assert trace.worth_distilling()
    trace.execute(PROPOSE_TOOL_NAME, {**PROPOSAL_PAYLOAD, "code": GOOD_INVOICE.replace('"billing_find_customer"', '"billing_find_customr"')})
    assert trace.worth_distilling() is False  # a draft exists for this trace, even though it failed static


def test_record_usage_accepts_objects_and_dicts(td):
    from types import SimpleNamespace

    trace = td.start_trace("t")
    trace.record_usage(SimpleNamespace(input_tokens=100, output_tokens=20))
    trace.record_usage({"input_tokens": 5, "output_tokens": 1})
    trace.record_usage(SimpleNamespace())
    record = td.registry.get_trace(trace.id)
    assert (record.input_tokens, record.output_tokens) == (105, 21)


# ----------------------------------------------------------------------- fallback distiller


def test_finish_runs_the_fallback_distiller(store):
    fake = FakeDistiller(Proposal(**PROPOSAL_PAYLOAD))
    td = make_td(store, fallback_distiller=True, distiller=fake)
    trace = td.start_trace("Invoice ACME")
    run_discovery(trace)
    trace.set_outcome(True)
    result = trace.finish()
    assert result is not None and result.verified and result.verdict == "verified: 4 base tool calls vs baseline 8"
    assert fake.calls == [{"trace_id": trace.id, "tools": td.tools.names, "note": None, "name": None}]
    row = td.registry.list_tool_calls(trace.id)[-1]
    assert row.kind is CallKind.PROPOSE and row.input["_source"] == "distiller" and not row.is_error
    assert td.registry.get_trace(trace.id).finished_at is not None
    assert trace.finish() is result  # idempotent
    td.close()


def test_finish_skips_the_distiller_when_not_needed(store):
    fake = FakeDistiller(Proposal(**PROPOSAL_PAYLOAD))
    td = make_td(store, fallback_distiller=True, distiller=fake)
    trace = td.start_trace("t")
    trace.execute("gmail_search", {"query": "x"})
    trace.set_outcome(True)
    assert trace.finish() is None and fake.calls == []
    off = make_td(store, fallback_distiller=False, distiller=fake)
    trace = off.start_trace("t")
    run_discovery(trace)
    trace.set_outcome(True)
    assert trace.finish() is None and fake.calls == []
    td.close()
    off.close()


def test_fallback_distiller_names_the_failed_tool(store):
    def executor(name, args):
        if name == "billing_create_invoice":
            raise RuntimeError("missing required property 'currency'")
        return acme_executor(name, args)

    fake = FakeDistiller(None)
    td = make_td(store, executor, fallback_distiller=True, distiller=fake)
    activate_good_tool(td)
    trace = td.start_trace("Invoice ACME")
    trace.execute(NAME, {"customer": "ACME"})
    trace.set_outcome(True)
    assert trace.finish() is None  # the distiller declined
    assert fake.calls[0]["name"] == NAME and NAME in fake.calls[0]["note"]
    td.close()


def test_explicit_distill_on_a_stored_trace(store):
    fake = FakeDistiller(Proposal(**PROPOSAL_PAYLOAD))
    td = make_td(store, distiller=fake)
    trace = td.start_trace("Invoice ACME")
    run_discovery(trace)
    trace.set_outcome(True)
    trace.finish()
    result = td.distill(trace.id)
    assert result is not None and result.verified
    assert td.registry.list_tool_calls(trace.id)[-1].kind is CallKind.PROPOSE
    assert td.distill(trace.id).verified and td.registry.latest_version(NAME).version == 2
    td.close()
