"""The verifier: static, replay and efficiency checks, run in order, stopping at the first failure.

Every result is written to ``check_results``. The verifier never changes a status; lifecycle reads the
report and decides.
"""

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from tooldistill.core.config import Settings
from tooldistill.core.errors import ReplayMismatchError, ToolLoadError
from tooldistill.core.models import REASON_AVOIDED_TOOL_RECOVERED, CheckName, VersionRecord
from tooldistill.core.registry import Registry
from tooldistill.core.runtime import Executor, RunContext, load_tool, run_tool
from tooldistill.core.serialization import code_hash
from tooldistill.core.static_check import static_check
from tooldistill.core.tools import ToolSet

__all__ = ["HealthProbe", "VerifyReport", "verify"]

HealthProbe = Callable[[str], bool]


@dataclass(slots=True)
class VerifyReport:
    tool_name: str
    version: int
    passed: bool
    failed_check: CheckName | None = None
    message: str = ""
    measured_calls: int | None = None
    baseline_calls: int | None = None
    dependencies: tuple[str, ...] = ()
    used_live: bool = False
    needs_live_verification: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """The one-line text the proposer reads."""
        if self.passed:
            return f"verified: {self.measured_calls} base tool calls vs baseline {self.baseline_calls}"
        if self.needs_live_verification:
            return f"not verified (replay): {self.message}"
        return f"rejected ({self.failed_check}): {self.message}"


def verify(
    registry: Registry,
    tools: ToolSet,
    version: VersionRecord,
    settings: Settings,
    *,
    executor: Executor | None = None,
    allow_live: bool = False,
    full_live: bool = False,
    rebuild_reason: str | None = None,
    replaces: VersionRecord | None = None,
    health_probe: HealthProbe | None = None,
) -> VerifyReport:
    """Run the three checks on a stored version.

    ``allow_live`` lets the replay check fall back to ``executor`` for calls no fixture covers (live
    verification: real side effects). ``full_live`` additionally ignores the stored fixtures so every
    call runs live and the fixtures are replaced by the fresh responses (``verify --live``).
    ``replaces`` is the version a rebuild supersedes; with ``rebuild_reason == "avoided_tool_recovered"``
    the new version must beat its measured call count.
    """
    name, number = version.tool_name, version.version
    report = VerifyReport(name, number, passed=False, baseline_calls=version.baseline_calls)

    # 1. static ---------------------------------------------------------------------------------
    source = registry.read_code(version)
    if code_hash(source) != version.code_hash:
        return _fail(registry, version, report, CheckName.STATIC, "the code file was modified after the version was stored")
    static = static_check(source, tools=tools, expected_name=name)
    if not static.passed:
        return _fail(registry, version, report, CheckName.STATIC, static.message)
    _record(registry, version, CheckName.STATIC, True, static.message, dependencies=list(static.dependencies))
    report.dependencies = static.dependencies
    registry.replace_dependencies(name, number, {dep: tools.schema_hash(dep) for dep in static.dependencies})
    avoided = {
        item.base_tool: item.reason
        for item in registry.list_avoided_tools(name, number)
        if item.base_tool not in static.dependencies
    }
    registry.replace_avoided_tools(name, number, avoided)

    # 2. replay ---------------------------------------------------------------------------------
    test_inputs = version.test_inputs
    schema_errors = sorted(Draft202012Validator(version.input_schema).iter_errors(test_inputs), key=lambda e: list(e.path))
    if schema_errors:
        return _fail(registry, version, report, CheckName.REPLAY, f"test_inputs do not match input_schema: {schema_errors[0].message}")
    try:
        tool = load_tool(registry.code_file(version))
    except ToolLoadError as exc:
        return _fail(registry, version, report, CheckName.REPLAY, f"importing the module failed: {exc}")
    live_ok = allow_live and executor is not None
    full_live = full_live and live_ok
    ctx = RunContext(
        mode="replay",
        tools=tools,
        fixtures=[] if full_live else registry.list_fixtures(name, number),
        allow_live=live_ok,
        executor=executor if live_ok else None,
    )
    outcome = run_tool(tool, test_inputs, ctx)
    if outcome.failure is not None:
        failure = outcome.failure
        extra: dict[str, Any] = {"kind": failure.kind, "step": failure.step, "base_tool": failure.base_tool}
        if (
            failure.kind == "replay"
            and isinstance(failure.exception, ReplayMismatchError)
            and not failure.exception.tool_has_fixtures
            and not live_ok
        ):
            report.needs_live_verification = True
            extra["needs_live_verification"] = True
            message = f"{failure.message}; live verification is needed (allow_live_verification)"
        elif failure.kind == "call":
            message = f"step {failure.step}: call to '{failure.base_tool}' failed: {failure.message}"
        elif failure.kind == "code":
            message = f"the tool raised {failure.message} after step {failure.step}"
        elif failure.kind == "check":
            message = f"{failure.message} after step {failure.step}"
        else:
            message = failure.message
        return _fail(registry, version, report, CheckName.REPLAY, message, **extra)
    measured = outcome.measured_calls
    if full_live:
        registry.replace_fixtures(name, number, outcome.new_fixtures)
    elif outcome.new_fixtures:
        registry.add_fixtures(name, number, outcome.new_fixtures)
    registry.set_measured_calls(name, number, measured)
    report.measured_calls = measured
    report.used_live = any(log.source == "live" for log in outcome.calls)
    live_count = sum(1 for log in outcome.calls if log.source == "live")
    replay_message = f"{measured} base tool calls replayed"
    if live_count:
        replay_message += f", {live_count} of them verified live"
    _record(registry, version, CheckName.REPLAY, True, replay_message, measured_calls=measured, live_calls=live_count)

    # 3. efficiency -----------------------------------------------------------------------------
    baseline = version.baseline_calls
    if baseline is None:
        return _fail(registry, version, report, CheckName.EFFICIENCY, "no baseline recorded for this version")
    reduction = settings.min_call_reduction
    threshold = baseline * (1 - reduction)
    if measured > threshold:
        return _fail(
            registry,
            version,
            report,
            CheckName.EFFICIENCY,
            f"{measured} base tool calls vs baseline {baseline}: must be at most {math.floor(threshold)} "
            f"(min_call_reduction={reduction:g})",
            measured_calls=measured,
            baseline_calls=baseline,
        )
    if (
        rebuild_reason == REASON_AVOIDED_TOOL_RECOVERED
        and replaces is not None
        and replaces.measured_calls is not None
        and measured >= replaces.measured_calls
    ):
        return _fail(
            registry,
            version,
            report,
            CheckName.EFFICIENCY,
            f"rebuild uses {measured} base tool calls, not fewer than the {replaces.measured_calls} of "
            f"v{replaces.version} it replaces: the recovered tool bought nothing",
            measured_calls=measured,
            baseline_calls=baseline,
            replaces_measured_calls=replaces.measured_calls,
        )
    notes: list[str] = []
    if health_probe is not None:
        for base_tool in avoided:
            if _probe(health_probe, base_tool):
                notes.append(f"avoided tool '{base_tool}' is currently healthy: a shorter version may exist")
    message = f"{measured} base tool calls vs baseline {baseline}"
    _record(
        registry,
        version,
        CheckName.EFFICIENCY,
        True,
        message,
        measured_calls=measured,
        baseline_calls=baseline,
        threshold=threshold,
        tokens=0,
        baseline_tokens=version.baseline_tokens,
        notes=notes,
    )
    report.passed = True
    report.message = message
    report.notes = notes
    return report


def _probe(health_probe: HealthProbe, base_tool: str) -> bool:
    try:
        return bool(health_probe(base_tool))
    except Exception:
        return False


def _record(registry: Registry, version: VersionRecord, check: CheckName, passed: bool, message: str, **extra: Any) -> None:
    details = json.dumps({"message": message, **extra})
    registry.add_check_result(version.tool_name, version.version, check, passed, details)


def _fail(registry: Registry, version: VersionRecord, report: VerifyReport, check: CheckName, message: str, **extra: Any) -> VerifyReport:
    _record(registry, version, check, False, message, **extra)
    report.passed = False
    report.failed_check = check
    report.message = message
    return report
