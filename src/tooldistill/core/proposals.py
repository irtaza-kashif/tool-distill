"""Proposals: a distilled tool submitted for review, validated, stored as a draft version and verified.

The agent proposes, the library verifies: nothing the proposer claims about cost, dependencies or
correctness is trusted. Baseline, fixtures and avoided tools are measured from the source trace.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from tooldistill.core import lifecycle
from tooldistill.core.config import Settings
from tooldistill.core.errors import ProposalRejected
from tooldistill.core.models import CallKind, Proposal, RebuildItem, TraceData, VersionRecord
from tooldistill.core.registry import Registry
from tooldistill.core.runtime import Executor
from tooldistill.core.serialization import canonical_json, is_json_serializable
from tooldistill.core.tools import DISTILLED_NAME_RE, PROPOSE_TOOL_NAME, ToolSet
from tooldistill.core.verifier import HealthProbe, VerifyReport, verify

__all__ = [
    "AVOIDED_REASON",
    "NAME_TAKEN",
    "PROPOSE_TOOL_INPUT_SCHEMA",
    "IngestResult",
    "Measurements",
    "ingest",
    "measure",
    "proposal_from_input",
    "validate",
]

AVOIDED_REASON = "every call failed in the source run"
NAME_TAKEN = "name is taken by an active distilled tool; pick a new name or deprecate it first"

PROPOSE_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]{2,63}$"},
        "description": {"type": "string"},
        "input_schema": {"type": "object"},
        "code": {"type": "string", "description": "Python module following the distilled tool file format"},
        "test_inputs": {"type": "object", "description": "Arguments that reproduce this run"},
        "trace_id": {
            "type": "string",
            "description": "Only needed in the Claude Code plugin; the SDK fills it in automatically",
        },
    },
    "required": ["name", "description", "input_schema", "code", "test_inputs"],
}


@dataclass(slots=True)
class IngestResult:
    """What came of a proposal. ``accepted`` means a draft version exists; ``verdict`` is the text the proposer reads."""

    accepted: bool
    verdict: str
    version: VersionRecord | None = None
    report: VerifyReport | None = None

    @property
    def verified(self) -> bool:
        return self.report is not None and self.report.passed

    @property
    def is_error(self) -> bool:
        """Flag the propose tool result as an error unless the version verified (or only needs live verification)."""
        if not self.accepted or self.report is None:
            return True
        return not (self.report.passed or self.report.needs_live_verification)


@dataclass(slots=True)
class Measurements:
    """What the library measures from a source trace; nothing here comes from the proposer."""

    baseline_calls: int
    baseline_tokens: int | None
    baseline_latency_ms: int | None
    fixtures: list[tuple[str, str, str]]
    avoided: dict[str, str]


def measure(trace: TraceData) -> Measurements:
    """Baseline (top-level base tool calls only), fixtures (every successful base call) and avoided tools."""
    base = [c for c in trace.calls if c.kind is CallKind.BASE]
    top_level = [c for c in base if c.parent_call_id is None]
    fixtures = [(c.tool_name, c.input_json, c.output_json) for c in base if not c.is_error and c.output_json is not None]
    outcomes: dict[str, list[bool]] = {}
    for c in base:
        outcomes.setdefault(c.tool_name, []).append(not c.is_error)
    avoided = {name: AVOIDED_REASON for name, oks in outcomes.items() if not any(oks)}
    tokens = trace.trace.input_tokens + trace.trace.output_tokens
    latencies = [c.latency_ms for c in top_level if c.latency_ms is not None]
    return Measurements(
        baseline_calls=len(top_level),
        baseline_tokens=tokens or None,
        baseline_latency_ms=sum(latencies) if latencies else None,
        fixtures=fixtures,
        avoided=avoided,
    )


def proposal_from_input(payload: Mapping[str, Any]) -> Proposal:
    """Build a Proposal from a propose tool call's input; rejects payloads that do not match the tool schema."""
    errors = sorted(Draft202012Validator(PROPOSE_TOOL_INPUT_SCHEMA).iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.path) or "input"
        raise ProposalRejected(f"invalid propose_distilled_tool input at {where}: {first.message}")
    return Proposal(
        name=payload["name"],
        description=payload["description"],
        input_schema=dict(payload["input_schema"]),
        code=payload["code"],
        test_inputs=dict(payload["test_inputs"]),
        trace_id=payload.get("trace_id"),
    )


def validate(registry: Registry, tools: ToolSet, proposal: Proposal) -> None:
    """Basic validation. A failure means no draft is created."""
    name = proposal.name
    if not isinstance(name, str) or not DISTILLED_NAME_RE.match(name):
        raise ProposalRejected(f"name {name!r} must match ^[a-z][a-z0-9_]{{2,63}}$")
    if name in tools:
        raise ProposalRejected(f"name '{name}' collides with a base tool")
    if name == PROPOSE_TOOL_NAME:
        raise ProposalRejected(f"name '{name}' collides with the propose tool")
    if registry.active_version(name) is not None:
        raise ProposalRejected(NAME_TAKEN)
    if not isinstance(proposal.description, str):
        raise ProposalRejected("description must be a string")
    if not isinstance(proposal.input_schema, Mapping) or not is_json_serializable(proposal.input_schema):
        raise ProposalRejected("input_schema must be a JSON object")
    if not isinstance(proposal.test_inputs, Mapping) or not is_json_serializable(proposal.test_inputs):
        raise ProposalRejected("test_inputs must be a JSON object")
    if not isinstance(proposal.code, str) or not proposal.code.strip():
        raise ProposalRejected("code must be a non-empty Python module")


def _max_optional(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def ingest(
    registry: Registry,
    tools: ToolSet,
    settings: Settings,
    proposal: Proposal,
    *,
    source_trace_id: str,
    executor: Executor | None = None,
    health_probe: HealthProbe | None = None,
    rebuild_of: RebuildItem | None = None,
) -> IngestResult:
    """Validate, store as the tool's next draft version, measure the source trace, verify, apply the lifecycle.

    ``rebuild_of`` marks the proposal as the rebuild for a queued item: the efficiency check must then also
    beat the version being replaced, and the item is resolved when the new version verifies.
    """
    try:
        validate(registry, tools, proposal)
    except ProposalRejected as exc:
        return IngestResult(accepted=False, verdict=f"rejected (proposal): {exc.reason}")

    trace = registry.get_trace_data(source_trace_id)
    measured = measure(trace)
    baseline_calls = measured.baseline_calls
    baseline_tokens = measured.baseline_tokens
    baseline_latency = measured.baseline_latency_ms
    previous = registry.latest_version(proposal.name)
    if previous is not None:
        # A fallback run after a structured error is far cheaper than the original discovery.
        baseline_calls = max(baseline_calls, previous.baseline_calls or 0)
        baseline_tokens = _max_optional(baseline_tokens, previous.baseline_tokens)
        baseline_latency = _max_optional(baseline_latency, previous.baseline_latency_ms)

    version = registry.create_version(
        name=proposal.name,
        description=proposal.description,
        input_schema_json=canonical_json(proposal.input_schema),
        code=proposal.code,
        test_inputs_json=canonical_json(proposal.test_inputs),
        source_trace_id=source_trace_id,
        baseline_calls=baseline_calls,
        baseline_tokens=baseline_tokens,
        baseline_latency_ms=baseline_latency,
    )
    registry.replace_fixtures(version.tool_name, version.version, measured.fixtures)
    registry.replace_avoided_tools(version.tool_name, version.version, measured.avoided)

    replaces: VersionRecord | None = None
    rebuild_reason: str | None = None
    if rebuild_of is not None:
        rebuild_reason = rebuild_of.reason
        replaces = registry.get_version(rebuild_of.tool_name, rebuild_of.version)
    report = verify(
        registry,
        tools,
        version,
        settings,
        executor=executor,
        allow_live=settings.allow_live_verification and executor is not None,
        rebuild_reason=rebuild_reason,
        replaces=replaces,
        health_probe=health_probe,
    )
    final = lifecycle.apply_verification(registry, settings, version, report)
    if rebuild_of is not None and report.passed:
        registry.resolve_rebuild(rebuild_of.id)
    return IngestResult(accepted=True, verdict=report.verdict, version=final, report=report)
