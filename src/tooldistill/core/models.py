"""Internal data model: one dataclass per store row, plus the objects that cross module boundaries."""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from tooldistill.core.tools import ToolSet

__all__ = [
    "EXPOSED_SUSPECT_REASONS",
    "REASON_AVOIDED_TOOL_RECOVERED",
    "REASON_DEPENDENCY_CHANGED",
    "REASON_NEEDS_LIVE_VERIFICATION",
    "REASON_PENDING_VERIFICATION",
    "REASON_RUNTIME_FAILURE",
    "AvoidedTool",
    "BaseTool",
    "CallKind",
    "CheckName",
    "CheckResult",
    "Dependency",
    "DistilledToolRecord",
    "Distiller",
    "Fixture",
    "Proposal",
    "RebuildItem",
    "RunStat",
    "Status",
    "StatusEvent",
    "ToolCallRecord",
    "ToolResult",
    "TraceData",
    "TraceRecord",
    "VersionRecord",
]


class Status(StrEnum):
    DRAFT = "draft"
    VERIFIED = "verified"
    ACTIVE = "active"
    SUSPECT = "suspect"
    DEPRECATED = "deprecated"


class CallKind(StrEnum):
    BASE = "base"
    DISTILLED = "distilled"
    PROPOSE = "propose"


class CheckName(StrEnum):
    STATIC = "static"
    REPLAY = "replay"
    EFFICIENCY = "efficiency"


# Status reasons the library writes itself. Other reasons (e.g. "manual") are free-form strings.
REASON_PENDING_VERIFICATION = "pending_verification"
REASON_NEEDS_LIVE_VERIFICATION = "needs_live_verification"
REASON_DEPENDENCY_CHANGED = "dependency_changed"
REASON_RUNTIME_FAILURE = "runtime_failure"
REASON_AVOIDED_TOOL_RECOVERED = "avoided_tool_recovered"

# Suspect versions with these reasons still work and stay exposed to the agent.
EXPOSED_SUSPECT_REASONS: tuple[str, ...] = (REASON_AVOIDED_TOOL_RECOVERED,)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What ``trace.execute`` hands back to the agent loop."""

    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class BaseTool:
    """An ordinary tool the agent already has, in Anthropic format."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": json.loads(json.dumps(self.input_schema)),
        }


@dataclass(slots=True)
class TraceRecord:
    id: str
    task: str
    started_at: str
    finished_at: str | None
    outcome: bool | None
    input_tokens: int
    output_tokens: int
    nudged: bool


@dataclass(slots=True)
class ToolCallRecord:
    id: int
    trace_id: str
    seq: int
    parent_call_id: int | None
    kind: CallKind
    tool_name: str
    version: int | None
    input_json: str
    output_json: str | None
    is_error: bool
    error: str | None
    latency_ms: int | None

    @property
    def input(self) -> Any:
        return json.loads(self.input_json)

    @property
    def output(self) -> Any:
        return None if self.output_json is None else json.loads(self.output_json)


@dataclass(slots=True)
class TraceData:
    """A trace together with every tool call it recorded, in ``seq`` order."""

    trace: TraceRecord
    calls: list[ToolCallRecord]


@dataclass(slots=True)
class DistilledToolRecord:
    name: str
    description: str
    created_at: str


@dataclass(slots=True)
class VersionRecord:
    tool_name: str
    version: int
    status: Status
    status_reason: str | None
    description: str
    input_schema_json: str
    code_path: str  # relative to the store folder
    code_hash: str
    source_trace_id: str | None
    test_inputs_json: str
    baseline_calls: int | None
    baseline_tokens: int | None
    baseline_latency_ms: int | None
    measured_calls: int | None
    created_at: str

    @property
    def input_schema(self) -> dict[str, Any]:
        return json.loads(self.input_schema_json)

    @property
    def test_inputs(self) -> dict[str, Any]:
        return json.loads(self.test_inputs_json)

    @property
    def label(self) -> str:
        return f"{self.tool_name} v{self.version}"


@dataclass(frozen=True, slots=True)
class Dependency:
    tool_name: str
    version: int
    base_tool: str
    schema_hash: str


@dataclass(frozen=True, slots=True)
class AvoidedTool:
    tool_name: str
    version: int
    base_tool: str
    reason: str


@dataclass(frozen=True, slots=True)
class Fixture:
    tool_name: str
    version: int
    seq: int
    base_tool: str
    input_json: str
    output_json: str

    @property
    def output(self) -> Any:
        return json.loads(self.output_json)


@dataclass(frozen=True, slots=True)
class CheckResult:
    id: int
    tool_name: str
    version: int
    check: CheckName
    passed: bool
    details: str
    ran_at: str


@dataclass(frozen=True, slots=True)
class RunStat:
    id: int
    tool_name: str
    version: int
    trace_id: str | None
    call_id: int | None
    ran_at: str
    success: bool
    calls: int
    latency_ms: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class StatusEvent:
    id: int
    tool_name: str
    version: int
    from_status: Status | None
    to_status: Status
    reason: str
    at: str


@dataclass(frozen=True, slots=True)
class RebuildItem:
    id: int
    tool_name: str
    version: int
    reason: str
    base_tool: str
    queued_at: str
    resolved_at: str | None


@dataclass(slots=True)
class Proposal:
    """A distilled tool submitted for review but not yet stored."""

    name: str
    description: str
    input_schema: dict[str, Any]
    code: str
    test_inputs: dict[str, Any]
    trace_id: str | None = None


class Distiller(Protocol):
    """Anything that turns a trace into a proposal."""

    def distill(
        self,
        trace: TraceData,
        tools: "ToolSet",
        *,
        note: str | None = None,
        name: str | None = None,
    ) -> Proposal | None: ...
