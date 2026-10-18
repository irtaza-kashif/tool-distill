"""The SDK facade: ``ToolDistill`` and ``Trace``. This is the only module an agent loop touches."""

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from tooldistill.core import lifecycle, proposals
from tooldistill.core.config import CONFIG_FILENAME, Settings, load_config
from tooldistill.core.errors import ConfigError, ProposalRejected, ToolDefinitionError, ToolLoadError
from tooldistill.core.models import (
    REASON_RUNTIME_FAILURE,
    CallKind,
    Distiller,
    Proposal,
    Status,
    ToolResult,
    TraceRecord,
    VersionRecord,
)
from tooldistill.core.proposals import IngestResult
from tooldistill.core.registry import Registry
from tooldistill.core.runtime import (
    CallLog,
    Executor,
    LoadedTool,
    RunContext,
    ToolRunFailure,
    load_tool,
    run_tool,
    structured_error,
)
from tooldistill.core.serialization import canonical_json, code_hash, to_content
from tooldistill.core.tools import PROPOSE_TOOL_NAME, ToolSet
from tooldistill.core.verifier import HealthProbe
from tooldistill.distill import prompts

__all__ = ["ToolDistill", "Trace"]


class ToolDistill:
    """Opens (or creates) a store and ties the developer's executor and base tools to it."""

    def __init__(
        self,
        store: str | Path,
        executor: Executor,
        tools: list[dict[str, Any]],
        *,
        distiller: Distiller | None = None,
        health_probe: HealthProbe | None = None,
        **settings: Any,
    ) -> None:
        if not callable(executor):
            raise ToolDefinitionError("executor must be callable: execute_tool(name: str, args: dict) -> Any")
        try:
            self.settings = Settings(**settings)
        except TypeError as exc:
            raise ConfigError(f"unknown setting: {exc}") from None
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
        self.tools = ToolSet(tools)
        self.executor = executor
        self.health_probe = health_probe
        self.registry = Registry(store)
        self._distiller = distiller
        self._loaded: dict[tuple[str, int, str], LoadedTool] = {}
        clashes = [tool.name for tool in self.registry.list_tools() if tool.name in self.tools]
        if clashes:
            raise ToolDefinitionError(f"base tool names collide with existing distilled tools: {', '.join(clashes)}")
        # check_schemas_on_start is applied by lifecycle.health() (build step 7).

    @classmethod
    def from_config(
        cls, path: str | Path = CONFIG_FILENAME, *, distiller: Distiller | None = None, **overrides: Any
    ) -> "ToolDistill":
        """Build from ``tooldistill.toml``; keyword overrides replace settings from the file."""
        config = load_config(path)
        settings = {**config.settings.as_dict(), **overrides}
        return cls(
            config.store,
            config.load_executor(),
            config.load_tools(),
            distiller=distiller,
            health_probe=config.load_health_probe(),
            **settings,
        )

    def close(self) -> None:
        self.registry.close()

    def __enter__(self) -> "ToolDistill":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------- the agent loop

    def start_trace(self, task: str) -> "Trace":
        """Begin recording a run."""
        return Trace(self, self.registry.create_trace(task))

    def active_tools(self) -> list[dict[str, Any]]:
        """Exposed distilled tools as Anthropic tool dicts. Re-reads the store, so CLI changes take effect."""
        return lifecycle.active_tool_defs(self.registry, self.settings)

    def propose_tool(self) -> dict[str, Any]:
        """The ``propose_distilled_tool`` definition."""
        return prompts.propose_tool()

    # ----------------------------------------------------------------------- distilling

    @property
    def distiller(self) -> Distiller:
        """The fallback Distiller: the one given, or a ``ClaudeDistiller`` using ``distiller_model``."""
        if self._distiller is None:
            from tooldistill.distill.claude import ClaudeDistiller

            self._distiller = ClaudeDistiller(model=self.settings.distiller_model)
        return self._distiller

    def distill(self, trace_id: str, *, name: str | None = None, note: str | None = None) -> IngestResult | None:
        """Run the fallback Distiller on a stored trace and ingest what it proposes."""
        data = self.registry.get_trace_data(trace_id)
        proposal = self.distiller.distill(data, self.tools, note=note, name=name)
        if proposal is None:
            return None
        result = self.ingest(proposal, trace_id)
        self._record_propose(trace_id, _proposal_payload(proposal), result, source="distiller")
        return result

    def ingest(self, proposal: Proposal, source_trace_id: str, *, rebuild_of: Any = None) -> IngestResult:
        """Ingest a proposal against a stored trace (validation, draft, verification, lifecycle)."""
        return proposals.ingest(
            self.registry,
            self.tools,
            self.settings,
            proposal,
            source_trace_id=source_trace_id,
            executor=self.executor,
            health_probe=self.health_probe,
            rebuild_of=rebuild_of,
        )

    def _record_propose(self, trace_id: str, payload: Mapping[str, Any], result: IngestResult, *, source: str) -> None:
        self.registry.add_tool_call(
            trace_id,
            kind=CallKind.PROPOSE,
            tool_name=PROPOSE_TOOL_NAME,
            input_json=canonical_json({**payload, "_source": source}),
            output_json=canonical_json(result.verdict),
            is_error=result.is_error,
            error=result.verdict if result.is_error else None,
        )

    # -------------------------------------------------------------------------- running

    def load_version(self, version: VersionRecord) -> LoadedTool:
        """Import a verified version's module (cached per code hash); refuses a file edited after verification."""
        key = (version.tool_name, version.version, version.code_hash)
        tool = self._loaded.get(key)
        if tool is None:
            source = self.registry.read_code(version)
            if code_hash(source) != version.code_hash:
                raise ToolLoadError(f"the code file of {version.label} was modified after it was verified")
            tool = load_tool(self.registry.code_file(version))
            self._loaded[key] = tool
        return tool


def _proposal_payload(proposal: Proposal) -> dict[str, Any]:
    return {
        "name": proposal.name,
        "description": proposal.description,
        "input_schema": proposal.input_schema,
        "code": proposal.code,
        "test_inputs": proposal.test_inputs,
    }


def _usage_field(usage: Any, key: str) -> int:
    if isinstance(usage, Mapping):
        value = usage.get(key)
    else:
        value = getattr(usage, key, None)
    return int(value or 0)


class Trace:
    """One run of the agent loop on one task."""

    def __init__(self, td: ToolDistill, record: TraceRecord) -> None:
        self._td = td
        self.id = record.id
        self.task = record.task
        self._outcome: bool | None = None
        self._nudged = False
        self._finished = False
        self.distill_result: IngestResult | None = None

    @property
    def outcome(self) -> bool | None:
        return self._outcome

    def record_usage(self, usage: Any) -> None:
        """Add a model response's token counts (an object with ``input_tokens``/``output_tokens`` or a dict)."""
        self._td.registry.add_usage(self.id, _usage_field(usage, "input_tokens"), _usage_field(usage, "output_tokens"))

    def set_outcome(self, success: bool) -> None:
        """Record whether the task was actually accomplished. Decided by the developer's code."""
        self._outcome = bool(success)
        self._td.registry.set_outcome(self.id, self._outcome)

    # ------------------------------------------------------------------------- execute

    def execute(self, name: str, input: Mapping[str, Any] | None) -> ToolResult:
        """Run one tool the model asked for: base tool, exposed distilled tool, or the propose tool."""
        args = dict(input or {})
        if name in self._td.tools:
            return self._execute_base(name, args)
        if name == PROPOSE_TOOL_NAME:
            return self._execute_propose(args)
        version = lifecycle.runnable_version(self._td.registry, name)
        if version is not None:
            return self._execute_distilled(version, args)
        if self._td.registry.get_tool(name) is not None:
            return self._unavailable(name, args)
        return ToolResult(f"Unknown tool: {name}", is_error=True)

    def _execute_base(self, name: str, args: dict[str, Any]) -> ToolResult:
        registry = self._td.registry
        input_json = canonical_json(args)
        started = time.perf_counter()
        try:
            raw = self._td.executor(name, args)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            registry.add_tool_call(
                self.id, kind=CallKind.BASE, tool_name=name, input_json=input_json, is_error=True, error=error,
                latency_ms=_elapsed_ms(started),
            )
            return ToolResult(error, is_error=True)
        latency = _elapsed_ms(started)
        try:
            output_json = canonical_json(raw)
        except (TypeError, ValueError):
            error = f"executor returned a non-JSON-serializable value ({type(raw).__name__})"
            registry.add_tool_call(
                self.id, kind=CallKind.BASE, tool_name=name, input_json=input_json, is_error=True, error=error,
                latency_ms=latency,
            )
            return ToolResult(error, is_error=True)
        registry.add_tool_call(
            self.id, kind=CallKind.BASE, tool_name=name, input_json=input_json, output_json=output_json, latency_ms=latency
        )
        return ToolResult(to_content(raw))

    def _execute_distilled(self, version: VersionRecord, args: dict[str, Any]) -> ToolResult:
        td = self._td
        registry = td.registry
        name, number = version.tool_name, version.version
        input_json = canonical_json(args)
        errors = sorted(Draft202012Validator(version.input_schema).iter_errors(args), key=lambda e: list(e.path))
        if errors:
            # The model's mistake, not the tool's: report it, do not demote.
            message = f"Invalid input for distilled tool '{name}': {errors[0].message}"
            registry.add_tool_call(
                self.id, kind=CallKind.DISTILLED, tool_name=name, version=number, input_json=input_json,
                is_error=True, error=message,
            )
            return ToolResult(message, is_error=True)
        parent = registry.add_tool_call(
            self.id, kind=CallKind.DISTILLED, tool_name=name, version=number, input_json=input_json
        )

        def recorder(log: CallLog) -> None:
            registry.add_tool_call(
                self.id,
                kind=CallKind.BASE,
                tool_name=log.base_tool,
                input_json=log.input_json,
                output_json=log.output_json,
                is_error=log.error is not None,
                error=log.error,
                latency_ms=log.latency_ms,
                parent_call_id=parent.id,
            )

        started = time.perf_counter()
        try:
            tool = td.load_version(version)
        except ToolLoadError as exc:
            failure: ToolRunFailure | None = ToolRunFailure("code", 0, None, str(exc))
            calls = 0
            result: Any = None
        else:
            ctx = RunContext(mode="live", tools=td.tools, executor=td.executor, recorder=recorder)
            outcome = run_tool(tool, args, ctx)
            failure, calls, result = outcome.failure, outcome.measured_calls, outcome.result
        latency = _elapsed_ms(started)
        if failure is None:
            registry.update_tool_call(parent.id, output_json=canonical_json(result), latency_ms=latency)
            registry.add_run_stat(
                name, number, success=True, calls=calls, latency_ms=latency, trace_id=self.id, call_id=parent.id
            )
            return ToolResult(to_content(result))
        text = structured_error(name, number, failure, version.measured_calls)
        registry.update_tool_call(parent.id, is_error=True, error=text, latency_ms=latency)
        registry.add_run_stat(
            name, number, success=False, calls=calls, latency_ms=latency, error=failure.message,
            trace_id=self.id, call_id=parent.id,
        )
        lifecycle.demote(registry, name, number, REASON_RUNTIME_FAILURE)
        return ToolResult(text, is_error=True)

    def _unavailable(self, name: str, args: dict[str, Any]) -> ToolResult:
        registry = self._td.registry
        versions = registry.list_versions(name)
        shown = next((v for v in reversed(versions) if v.status is not Status.DRAFT), versions[-1])
        message = (
            f"Distilled tool '{name}' is not available (v{shown.version}: {shown.status}, {shown.status_reason}). "
            "Complete the task using the base tools directly."
        )
        registry.add_tool_call(
            self.id, kind=CallKind.DISTILLED, tool_name=name, version=shown.version, input_json=canonical_json(args),
            is_error=True, error=message,
        )
        return ToolResult(message, is_error=True)

    def _execute_propose(self, args: dict[str, Any]) -> ToolResult:
        try:
            proposal = proposals.proposal_from_input(args)
        except ProposalRejected as exc:
            result = IngestResult(accepted=False, verdict=f"rejected (proposal): {exc.reason}")
        else:
            result = self._td.ingest(proposal, self.id)
        self._td._record_propose(self.id, args, result, source="agent")
        return ToolResult(result.verdict, is_error=result.is_error)

    # ----------------------------------------------------------------------- distilling

    def _top_level_base_calls(self) -> int:
        calls = self._td.registry.list_tool_calls(self.id)
        return sum(1 for c in calls if c.kind is CallKind.BASE and c.parent_call_id is None)

    def failed_distilled_tool(self) -> str | None:
        """The name of the distilled tool that failed at run time in this trace, if any."""
        for stat in self._td.registry.run_stats_for_trace(self.id):
            if not stat.success:
                return stat.tool_name
        return None

    def worth_distilling(self) -> bool:
        """Outcome is success, no accepted proposal yet, no successful distilled call, and enough base calls
        (the threshold is waived when a distilled tool failed: this run is the source of its next version)."""
        if self._outcome is not True:
            return False
        registry = self._td.registry
        if registry.versions_from_trace(self.id):
            return False
        stats = registry.run_stats_for_trace(self.id)
        if any(stat.success for stat in stats):
            return False
        if any(not stat.success for stat in stats):
            return True
        return self._top_level_base_calls() >= self._td.settings.min_calls_to_distill

    def distill_nudge(self) -> str | None:
        """The message asking the agent to propose a distilled tool; at most once per trace, only when worth it."""
        if self._nudged or not self.worth_distilling():
            return None
        self._nudged = True
        self._td.registry.mark_nudged(self.id)
        return prompts.nudge(
            self.task,
            base_calls=self._top_level_base_calls(),
            tool_names=self._td.tools.names,
            failed_tool=self.failed_distilled_tool(),
        )

    def finish(self) -> IngestResult | None:
        """Close the trace. Runs the fallback Distiller when the run is worth distilling and nobody proposed."""
        if self._finished:
            return self.distill_result
        self._finished = True
        td = self._td
        td.registry.finish_trace(self.id)
        if td.settings.fallback_distiller and self.worth_distilling():
            failed = self.failed_distilled_tool()
            note = None
            if failed:
                note = f"The distilled tool '{failed}' failed in this run; propose a replacement under the same name."
            data = td.registry.get_trace_data(self.id)
            proposal = td.distiller.distill(data, td.tools, note=note, name=failed)
            if proposal is not None:
                self.distill_result = td.ingest(proposal, self.id)
                td._record_propose(self.id, _proposal_payload(proposal), self.distill_result, source="distiller")
        return self.distill_result


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
