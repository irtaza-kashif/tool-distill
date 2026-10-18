import json
from types import SimpleNamespace

import pytest
from helpers import GOOD_INVOICE, INVOICE_NAME as NAME, INVOICE_SCHEMA, acme_fixtures, sample_tools

from tooldistill.core.models import CallKind
from tooldistill.core.serialization import canonical_json as cj
from tooldistill.core.tools import ToolSet
from tooldistill.distill.claude import PROPOSAL_OUTPUT_SCHEMA, SYSTEM_PROMPT, ClaudeDistiller, render_prompt
from tooldistill.distill.prompts import FILE_FORMAT_RULES

GOOD_OUTPUT = json.dumps(
    {
        "name": NAME,
        "description": "Create a draft invoice from the latest order email.",
        "input_schema_json": json.dumps(INVOICE_SCHEMA),
        "code": GOOD_INVOICE,
        "test_inputs_json": json.dumps({"customer": "ACME"}),
    }
)


class FakeClient:
    """Stands in for anthropic.Anthropic(): records the request and returns one canned response."""

    def __init__(self, text: str | None = GOOD_OUTPUT, *, stop_reason: str = "end_turn", content: list | None = None) -> None:
        blocks = content if content is not None else [SimpleNamespace(type="text", text=text)]
        self.response = SimpleNamespace(content=blocks, stop_reason=stop_reason, usage=SimpleNamespace(input_tokens=1, output_tokens=1))
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.response


def recorded_trace(registry, *, long_output: bool = False):
    trace = registry.create_trace("Invoice ACME")
    for base_tool, input_json, output_json in acme_fixtures():
        registry.add_tool_call(trace.id, kind=CallKind.BASE, tool_name=base_tool, input_json=input_json, output_json=output_json, latency_ms=3)
        if base_tool == "gmail_get_message":
            registry.add_tool_call(
                trace.id, kind=CallKind.BASE, tool_name="billing_import_from_email", input_json=cj({"message_id": "m1"}),
                is_error=True, error="503 Service Unavailable",
            )
    parent = registry.add_tool_call(trace.id, kind=CallKind.DISTILLED, tool_name="older_tool", version=1, input_json="{}")
    registry.add_tool_call(trace.id, kind=CallKind.BASE, tool_name="gmail_search", input_json=cj({"query": "inner"}), output_json="[]", parent_call_id=parent.id)
    if long_output:
        registry.add_tool_call(trace.id, kind=CallKind.BASE, tool_name="gmail_search", input_json=cj({"query": "big"}), output_json=cj(["x" * 5000]))
    registry.add_tool_call(trace.id, kind=CallKind.PROPOSE, tool_name="propose_distilled_tool", input_json="{}", output_json=cj("rejected (static): ..."))
    registry.set_outcome(trace.id, True)
    return registry.get_trace_data(trace.id)


def test_request_shape_and_parsing(registry):
    client = FakeClient()
    distiller = ClaudeDistiller(model="claude-sonnet-5-5", client=client)
    trace = recorded_trace(registry)
    proposal = distiller.distill(trace, ToolSet(sample_tools()), note="The base tool 'billing_import_from_email' is available again.", name=NAME)
    assert proposal is not None
    assert proposal.name == NAME and proposal.code == GOOD_INVOICE and proposal.trace_id == trace.trace.id
    assert proposal.input_schema == INVOICE_SCHEMA and proposal.test_inputs == {"customer": "ACME"}
    assert proposal.description == "Create a draft invoice from the latest order email."

    [request] = client.requests
    assert request["model"] == "claude-sonnet-5-5" and request["max_tokens"] == 16000
    assert request["output_config"] == {"format": {"type": "json_schema", "schema": PROPOSAL_OUTPUT_SCHEMA}}
    assert "thinking" not in request and "tool_choice" not in request and "tools" not in request
    assert request["system"] == SYSTEM_PROMPT and FILE_FORMAT_RULES in request["system"]
    [message] = request["messages"]
    assert message["role"] == "user"
    text = message["content"]
    assert text.startswith("Task: Invoice ACME\nOutcome: success")
    assert "- gmail_search: " in text and '"required":["query"]' in text
    assert "1. gmail_search(" + cj({"query": "from:ACME order", "since": "1d"}) + ") -> " + cj([{"id": "m1"}]) in text
    assert "billing_import_from_email(" + cj({"message_id": "m1"}) + ") -> ERROR: 503 Service Unavailable" in text
    assert "[inside older_tool] gmail_search(" in text
    assert "propose_distilled_tool(...) -> " in text
    assert "The base tool 'billing_import_from_email' is available again." in text
    assert f"- The tool must be named '{NAME}'" in text
    assert "test_inputs must reproduce exactly this run." in text


def test_prompt_without_name_or_note_and_truncation(registry):
    trace = recorded_trace(registry, long_output=True)
    text = render_prompt(trace, ToolSet(sample_tools()), note=None, name=None, output_chars=100)
    assert "must be named" not in text
    assert "responses truncated to 100 characters" in text
    big = next(line for line in text.splitlines() if '"query":"big"' in line)
    assert big.endswith("...") and len(big) < 200


@pytest.mark.parametrize(
    "client",
    [
        FakeClient(stop_reason="refusal"),
        FakeClient(stop_reason="max_tokens"),
        FakeClient("this is not json"),
        FakeClient(json.dumps({"name": NAME, "description": "d", "input_schema_json": "oops", "code": "x", "test_inputs_json": "{}"})),
        FakeClient(json.dumps({"name": NAME, "description": "d", "input_schema_json": "[]", "code": "x", "test_inputs_json": "{}"})),
        FakeClient(json.dumps({"name": NAME, "description": "d", "code": "x", "test_inputs_json": "{}"})),
        FakeClient(content=[]),
        FakeClient(content=[SimpleNamespace(type="thinking", thinking="")]),
    ],
    ids=["refusal", "max_tokens", "not_json", "bad_schema_json", "schema_not_object", "missing_field", "no_blocks", "no_text_block"],
)
def test_unusable_responses_give_no_proposal(registry, client):
    distiller = ClaudeDistiller(client=client)
    assert distiller.distill(recorded_trace(registry), ToolSet(sample_tools())) is None


def test_client_is_created_lazily(monkeypatch, registry):
    import anthropic

    created: list[dict] = []

    class FakeAnthropic:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.messages = SimpleNamespace(create=lambda **_: SimpleNamespace(content=[SimpleNamespace(type="text", text=GOOD_OUTPUT)], stop_reason="end_turn"))

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    distiller = ClaudeDistiller()
    assert created == [] and distiller.model == "claude-sonnet-5-5"
    assert distiller.distill(recorded_trace(registry), ToolSet(sample_tools())) is not None
    assert created == [{}]
    assert distiller.distill(recorded_trace(registry), ToolSet(sample_tools())) is not None
    assert created == [{}]  # one client, reused
