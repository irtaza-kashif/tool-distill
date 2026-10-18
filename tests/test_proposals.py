import pytest
from helpers import (
    GOOD_INVOICE,
    INVOICE_NAME as NAME,
    INVOICE_SCHEMA,
    acme_executor,
    acme_fixtures,
    add_version,
    sample_tools,
)

from tooldistill.core.config import Settings
from tooldistill.core.errors import ProposalRejected
from tooldistill.core.models import REASON_AVOIDED_TOOL_RECOVERED, CallKind, Proposal, Status, TraceRecord
from tooldistill.core.proposals import (
    AVOIDED_REASON,
    ingest,
    measure,
    proposal_from_input,
    validate,
)
from tooldistill.core.registry import Registry
from tooldistill.core.serialization import canonical_json as cj
from tooldistill.core.tools import PROPOSE_TOOL_NAME, ToolSet

TWO_CALL_SCHEMA = {"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]}
TWO_CALL_TOOL = '''from tooldistill import distilled_tool, call


@distilled_tool(
    name="create_invoice_from_email",
    description="Import the latest order email as an invoice.",
    input_schema={"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]},
)
def create_invoice_from_email(customer: str) -> dict:
    msg = call("gmail_search", query=f"from:{customer} order", since="1d")[0]
    return call("billing_import_from_email", message_id=msg["id"])
'''


@pytest.fixture
def tools() -> ToolSet:
    return ToolSet(sample_tools())


def record_discovery(registry: Registry, *, noise: int = 5, tokens: tuple[int, int] = (1000, 200)) -> TraceRecord:
    """A run that found the 4-call path after some noise and two failed shortcut attempts."""
    trace = registry.create_trace("Invoice ACME")
    for i in range(noise):
        registry.add_tool_call(trace.id, kind=CallKind.BASE, tool_name="gmail_search", input_json=cj({"query": f"noise {i}"}), output_json="[]", latency_ms=5)
    for base_tool, input_json, output_json in acme_fixtures():
        registry.add_tool_call(trace.id, kind=CallKind.BASE, tool_name=base_tool, input_json=input_json, output_json=output_json, latency_ms=10)
        if base_tool == "gmail_get_message":
            for _ in range(2):
                registry.add_tool_call(
                    trace.id, kind=CallKind.BASE, tool_name="billing_import_from_email",
                    input_json=cj({"message_id": "m1"}), is_error=True, error="503 Service Unavailable", latency_ms=7,
                )
    registry.add_usage(trace.id, *tokens)
    registry.set_outcome(trace.id, True)
    return trace


def good_proposal(**overrides) -> Proposal:
    fields = {
        "name": NAME,
        "description": "Create a draft invoice for a customer from their most recent order email.",
        "input_schema": INVOICE_SCHEMA,
        "code": GOOD_INVOICE,
        "test_inputs": {"customer": "ACME"},
    }
    fields.update(overrides)
    return Proposal(**fields)


# ------------------------------------------------------------------------------ measure


def test_measure_counts_only_top_level_base_calls(registry):
    trace = record_discovery(registry)
    parent = registry.add_tool_call(trace.id, kind=CallKind.DISTILLED, tool_name="other_tool", version=1, input_json="{}")
    registry.add_tool_call(trace.id, kind=CallKind.BASE, tool_name="gmail_search", input_json=cj({"query": "inner"}), output_json='["inner"]', parent_call_id=parent.id, latency_ms=99)
    registry.add_tool_call(trace.id, kind=CallKind.PROPOSE, tool_name=PROPOSE_TOOL_NAME, input_json="{}", output_json='"rejected"')
    measured = measure(registry.get_trace_data(trace.id))
    assert measured.baseline_calls == 11  # 5 noise + 4 path + 2 failed imports; inner and propose calls excluded
    assert measured.baseline_tokens == 1200
    assert measured.baseline_latency_ms == 5 * 5 + 4 * 10 + 2 * 7
    assert len(measured.fixtures) == 9  # 5 noise + 4 path; the successful inner call is not a fixture
    assert ("gmail_search", cj({"query": "inner"}), '["inner"]') not in measured.fixtures
    assert measured.avoided == {"billing_import_from_email": AVOIDED_REASON}


def test_measure_without_usage_or_latency(registry):
    trace = registry.create_trace("t")
    registry.add_tool_call(trace.id, kind=CallKind.BASE, tool_name="gmail_search", input_json="{}", output_json="[]")
    measured = measure(registry.get_trace_data(trace.id))
    assert measured.baseline_calls == 1 and measured.baseline_tokens is None and measured.baseline_latency_ms is None


# -------------------------------------------------------------------- proposal_from_input


def test_proposal_from_input_validates_the_payload():
    payload = {"name": NAME, "description": "d", "input_schema": INVOICE_SCHEMA, "code": GOOD_INVOICE, "test_inputs": {"customer": "ACME"}, "trace_id": "abc"}
    proposal = proposal_from_input(payload)
    assert proposal.name == NAME and proposal.trace_id == "abc" and proposal.test_inputs == {"customer": "ACME"}
    with pytest.raises(ProposalRejected, match="invalid propose_distilled_tool input at input: 'code' is a required property"):
        proposal_from_input({k: v for k, v in payload.items() if k != "code"})
    with pytest.raises(ProposalRejected, match="at name:"):
        proposal_from_input({**payload, "name": "Bad Name"})
    with pytest.raises(ProposalRejected, match="at input_schema:"):
        proposal_from_input({**payload, "input_schema": "not an object"})


# ----------------------------------------------------------------------------- validate


def test_validate_rejections(registry, tools):
    validate(registry, tools, good_proposal())
    with pytest.raises(ProposalRejected, match="must match"):
        validate(registry, tools, good_proposal(name="Create"))
    with pytest.raises(ProposalRejected, match="collides with a base tool"):
        validate(registry, tools, good_proposal(name="gmail_search"))
    with pytest.raises(ProposalRejected, match="collides with the propose tool"):
        validate(registry, tools, good_proposal(name=PROPOSE_TOOL_NAME))
    with pytest.raises(ProposalRejected, match="non-empty Python module"):
        validate(registry, tools, good_proposal(code="   "))
    with pytest.raises(ProposalRejected, match="test_inputs must be a JSON object"):
        validate(registry, tools, good_proposal(test_inputs={"x": object()}))
    with pytest.raises(ProposalRejected, match="input_schema must be a JSON object"):
        validate(registry, tools, good_proposal(input_schema=[]))  # type: ignore[arg-type]
    add_version(registry)
    registry.set_status(NAME, 1, Status.ACTIVE, "promoted")
    validate(registry, tools, good_proposal())  # an active version does not block a proposal: it becomes v2
    registry.set_status(NAME, 1, Status.SUSPECT, "runtime_failure")
    validate(registry, tools, good_proposal())


# ------------------------------------------------------------------------------- ingest


def test_ingest_verifies_and_records_everything(registry, tools):
    trace = record_discovery(registry)
    result = ingest(registry, tools, Settings(), good_proposal(), source_trace_id=trace.id)
    assert result.accepted and result.verified and not result.is_error
    assert result.verdict == "verified: 4 base tool calls vs baseline 11; the run spent 1,200 model tokens, the tool spends none; v1 waits for promotion"
    version = result.version
    assert version.status is Status.VERIFIED and version.version == 1
    assert (version.baseline_calls, version.baseline_tokens, version.baseline_latency_ms) == (11, 1200, 79)
    assert version.source_trace_id == trace.id and version.measured_calls == 4
    assert registry.versions_from_trace(trace.id)[0].version == 1
    assert [f.base_tool for f in registry.list_fixtures(NAME, 1)][-4:] == ["gmail_search", "gmail_get_message", "billing_find_customer", "billing_create_invoice"]
    assert [a.base_tool for a in registry.list_avoided_tools(NAME, 1)] == ["billing_import_from_email"]
    assert [d.base_tool for d in registry.list_dependencies(NAME, 1)] == ["billing_create_invoice", "billing_find_customer", "gmail_get_message", "gmail_search"]
    assert registry.read_code(version) == GOOD_INVOICE
    assert registry.get_version(NAME, 1).description.startswith("Create a draft invoice")


def test_ingest_with_auto_promote_activates(registry, tools):
    trace = record_discovery(registry)
    result = ingest(registry, tools, Settings(auto_promote=True), good_proposal(), source_trace_id=trace.id)
    assert result.version.status is Status.ACTIVE and result.version.status_reason == "auto_promote"


def test_ingest_rejection_creates_no_draft(registry, tools):
    trace = record_discovery(registry)
    result = ingest(registry, tools, Settings(), good_proposal(name="gmail_search"), source_trace_id=trace.id)
    assert not result.accepted and result.is_error and result.version is None
    assert result.verdict == "rejected (proposal): name 'gmail_search' collides with a base tool"
    assert registry.list_versions() == [] and registry.list_tools() == []


def test_ingest_static_failure_is_a_failed_draft(registry, tools):
    trace = record_discovery(registry)
    broken = GOOD_INVOICE.replace('call("billing_find_customer"', 'call("billing_find_customr"')
    result = ingest(registry, tools, Settings(), good_proposal(code=broken), source_trace_id=trace.id)
    assert result.accepted and not result.verified and result.is_error
    assert result.verdict.startswith("rejected (static): line ")
    assert "call to 'billing_find_customr' — no such base tool" in result.verdict
    assert (result.version.status, result.version.status_reason) == (Status.DRAFT, "static_failed")
    # the proposer fixes the module and resubmits: a new version of the same tool
    again = ingest(registry, tools, Settings(), good_proposal(), source_trace_id=trace.id)
    assert again.verified and again.version.version == 2


def test_ingest_needs_live_verification_is_not_an_error(registry, tools):
    trace = record_discovery(registry)  # billing_import_from_email only ever failed: no fixture for it
    proposal = good_proposal(code=TWO_CALL_TOOL, input_schema=TWO_CALL_SCHEMA)
    result = ingest(registry, tools, Settings(), proposal, source_trace_id=trace.id)
    assert result.accepted and not result.verified and not result.is_error
    assert result.report.needs_live_verification
    assert result.verdict.startswith("not verified (replay): step 2: call to 'billing_import_from_email'")
    assert (result.version.status, result.version.status_reason) == (Status.DRAFT, "needs_live_verification")
    live = ingest(registry, tools, Settings(allow_live_verification=True), proposal, source_trace_id=trace.id, executor=acme_executor)
    assert live.verified and live.report.used_live and live.version.version == 2
    assert live.verdict == "verified: 2 base tool calls vs baseline 11; the run spent 1,200 model tokens, the tool spends none; v2 waits for promotion"


def test_baseline_inherits_from_the_previous_version(registry, tools):
    first = ingest(registry, tools, Settings(), good_proposal(), source_trace_id=record_discovery(registry).id)
    assert first.version.baseline_calls == 11
    # a fallback run is short: only the 4 calls that worked, few tokens
    short = registry.create_trace("Invoice ACME again")
    for base_tool, input_json, output_json in acme_fixtures():
        registry.add_tool_call(short.id, kind=CallKind.BASE, tool_name=base_tool, input_json=input_json, output_json=output_json, latency_ms=1)
    registry.add_usage(short.id, 10, 5)
    registry.set_outcome(short.id, True)
    second = ingest(registry, tools, Settings(), good_proposal(), source_trace_id=short.id)
    assert second.verified and second.version.version == 2
    assert (second.version.baseline_calls, second.version.baseline_tokens, second.version.baseline_latency_ms) == (11, 1200, 79)
    assert second.verdict == "verified: 4 base tool calls vs baseline 11; the run spent 1,200 model tokens, the tool spends none; v2 waits for promotion"


def test_rebuild_resolves_the_queue_item_only_on_success(registry, tools):
    trace = record_discovery(registry)
    first = ingest(registry, tools, Settings(auto_promote=True), good_proposal(), source_trace_id=trace.id)
    assert first.version.status is Status.ACTIVE and first.version.measured_calls == 4
    item = registry.enqueue_rebuild(NAME, 1, REASON_AVOIDED_TOOL_RECOVERED, "billing_import_from_email")
    registry.set_status(NAME, 1, Status.SUSPECT, REASON_AVOIDED_TOOL_RECOVERED)

    same_length = ingest(registry, tools, Settings(auto_promote=True), good_proposal(), source_trace_id=trace.id, rebuild_of=item)
    assert not same_length.verified and same_length.report.failed_check.value == "efficiency"
    assert "not fewer than the 4 of v1 it replaces" in same_length.verdict
    assert registry.list_rebuild_queue() == [item]  # still open

    shorter = good_proposal(code=TWO_CALL_TOOL, input_schema=TWO_CALL_SCHEMA)
    rebuilt = ingest(registry, tools, Settings(auto_promote=True, allow_live_verification=True), shorter, source_trace_id=trace.id, executor=acme_executor, rebuild_of=item)
    assert rebuilt.verified and rebuilt.version.version == 3 and rebuilt.version.status is Status.ACTIVE
    assert rebuilt.verdict == "verified: 2 base tool calls vs baseline 11; the run spent 1,200 model tokens, the tool spends none; v3 is now active and replaces v1"
    assert registry.list_rebuild_queue() == []
    assert registry.get_version(NAME, 1).status is Status.DEPRECATED
    assert registry.get_version(NAME, 1).status_reason == "superseded_by_v3"
