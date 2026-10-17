"""Runtime for distilled tools.

Owns ``@distilled_tool``, ``call()`` and the live/replay run context (a ``ContextVar``, so ``call()`` knows
its mode, executor and recorder without any global state), loading a tool module from its file, running a
tool with failure classification, and the structured error text an agent receives after a failed live run.
"""

import importlib.util
import json
import re
import time
import types
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from tooldistill.core.errors import DistilledToolCallError, ReplayMismatchError, ToolLoadError
from tooldistill.core.models import Fixture
from tooldistill.core.serialization import canonical_json, is_json_serializable
from tooldistill.core.tools import ToolSet

__all__ = [
    "META_ATTR",
    "CallLog",
    "DistilledToolMeta",
    "Executor",
    "LoadedTool",
    "Recorder",
    "RunContext",
    "RunResult",
    "ToolRunFailure",
    "call",
    "current_context",
    "distilled_tool",
    "load_tool",
    "run_tool",
    "structured_error",
]

Executor = Callable[[str, dict[str, Any]], Any]
Mode = Literal["live", "replay"]
FailureKind = Literal["call", "replay", "code", "check"]
META_ATTR = "_tooldistill_meta"
_MISSING = object()


# ----------------------------------------------------------------------------- decorator


@dataclass(frozen=True, slots=True)
class DistilledToolMeta:
    name: str
    description: str
    input_schema: dict[str, Any]


def distilled_tool[F: Callable[..., Any]](
    *, name: str, description: str, input_schema: Mapping[str, Any]
) -> Callable[[F], F]:
    """Mark the one function in a distilled tool module that implements the tool.

    The decorator only attaches metadata; every rule about the module is enforced by the static check
    before the module is ever imported.
    """
    if not isinstance(name, str) or not isinstance(description, str) or not isinstance(input_schema, Mapping):
        raise TypeError("distilled_tool() needs a str name, a str description and a dict input_schema")
    meta = DistilledToolMeta(name=name, description=description, input_schema=dict(input_schema))

    def decorate(fn: F) -> F:
        setattr(fn, META_ATTR, meta)
        return fn

    return decorate


# --------------------------------------------------------------------------- run context


@dataclass(frozen=True, slots=True)
class CallLog:
    """One ``call()`` made during a run, in the order it happened."""

    step: int
    base_tool: str
    input_json: str
    output_json: str | None
    error: str | None
    latency_ms: int
    source: Literal["live", "fixture"]
    fixture_seq: int | None = None


Recorder = Callable[[CallLog], None]


@dataclass(slots=True)
class RunContext:
    """Everything ``call()`` needs during one run of a distilled tool.

    ``mode == "live"`` routes every call through ``executor`` and hands each ``CallLog`` to ``recorder``
    (the agent uses it to write child ``tool_calls`` rows). ``mode == "replay"`` answers calls from
    ``fixtures`` and, only when ``allow_live`` is set, falls back to ``executor`` for unmatched calls,
    collecting the responses in ``new_fixtures``.
    """

    mode: Mode
    tools: ToolSet
    executor: Executor | None = None
    fixtures: Sequence[Fixture] = ()
    allow_live: bool = False
    recorder: Recorder | None = None
    step: int = 0
    calls: list[CallLog] = field(default_factory=list)
    consumed: set[int] = field(default_factory=set)
    new_fixtures: list[tuple[str, str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mode == "live" and self.executor is None:
            raise ValueError("live mode needs an executor")
        if self.mode == "replay" and self.allow_live and self.executor is None:
            raise ValueError("allow_live needs an executor")


_current: ContextVar[RunContext | None] = ContextVar("tooldistill_run_context", default=None)


def current_context() -> RunContext | None:
    return _current.get()


# ------------------------------------------------------------------------------- call()


def call(tool_name: str, /, **kwargs: Any) -> Any:
    """Call a base tool from inside a distilled tool.

    Live mode runs the executor; replay mode answers from fixtures. Either way the result is the
    JSON round-trip of what the base tool returned, so a tool sees identical values in both modes.
    """
    ctx = _current.get()
    if ctx is None:
        raise RuntimeError("call() can only be used inside a distilled tool run")
    if not isinstance(tool_name, str):
        raise TypeError("call() needs a tool name string as its first argument")
    ctx.step += 1
    step = ctx.step
    try:
        input_json = canonical_json(kwargs)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"call('{tool_name}'): arguments must be JSON-serializable: {exc}") from exc
    if tool_name not in ctx.tools:
        _record(ctx, CallLog(step, tool_name, input_json, None, f"unknown base tool '{tool_name}'", 0, "live"))
        raise DistilledToolCallError(step, tool_name, f"unknown base tool '{tool_name}'")
    if ctx.mode == "live":
        return _call_live(ctx, step, tool_name, kwargs, input_json)
    return _call_replay(ctx, step, tool_name, input_json)


def _record(ctx: RunContext, log: CallLog) -> None:
    ctx.calls.append(log)
    if ctx.recorder is not None:
        ctx.recorder(log)


def _describe(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _call_live(ctx: RunContext, step: int, tool_name: str, kwargs: Mapping[str, Any], input_json: str) -> Any:
    assert ctx.executor is not None
    started = time.perf_counter()
    try:
        raw = ctx.executor(tool_name, dict(kwargs))
    except Exception as exc:
        latency = _elapsed_ms(started)
        message = str(exc) or type(exc).__name__
        _record(ctx, CallLog(step, tool_name, input_json, None, message, latency, "live"))
        raise DistilledToolCallError(step, tool_name, message) from exc
    latency = _elapsed_ms(started)
    try:
        output_json = canonical_json(raw)
    except (TypeError, ValueError):
        message = f"executor returned a non-JSON-serializable value ({type(raw).__name__})"
        _record(ctx, CallLog(step, tool_name, input_json, None, message, latency, "live"))
        raise DistilledToolCallError(step, tool_name, message) from None
    _record(ctx, CallLog(step, tool_name, input_json, output_json, None, latency, "live"))
    return json.loads(output_json)


def _call_replay(ctx: RunContext, step: int, tool_name: str, input_json: str) -> Any:
    for index, fixture in enumerate(ctx.fixtures):
        if index in ctx.consumed:
            continue
        if fixture.base_tool == tool_name and fixture.input_json == input_json:
            ctx.consumed.add(index)
            _record(ctx, CallLog(step, tool_name, input_json, fixture.output_json, None, 0, "fixture", fixture.seq))
            return json.loads(fixture.output_json)
    if ctx.allow_live and ctx.executor is not None:
        result = _call_live(ctx, step, tool_name, json.loads(input_json), input_json)
        output_json = ctx.calls[-1].output_json
        assert output_json is not None
        ctx.new_fixtures.append((tool_name, input_json, output_json))
        return result
    nearest, still_open = _nearest_fixture(ctx, tool_name, input_json)
    if nearest is None:
        detail = f"the source run recorded no successful calls to '{tool_name}'"
    else:
        state = "" if still_open else ", already used by an earlier call"
        detail = f"nearest fixture for '{tool_name}' (seq {nearest.seq}{state}) has input {nearest.input_json}"
    message = f"step {step}: call to '{tool_name}' with {input_json} matched no fixture; {detail}"
    _record(ctx, CallLog(step, tool_name, input_json, None, message, 0, "fixture"))
    raise ReplayMismatchError(
        step,
        tool_name,
        input_json,
        message,
        nearest_input_json=None if nearest is None else nearest.input_json,
        nearest_seq=None if nearest is None else nearest.seq,
        tool_has_fixtures=nearest is not None,
    )


def _nearest_fixture(ctx: RunContext, tool_name: str, input_json: str) -> tuple[Fixture | None, bool]:
    """The fixture for ``tool_name`` whose input differs in the fewest top-level keys, and whether it is unconsumed."""
    candidates = [(i, f) for i, f in enumerate(ctx.fixtures) if f.base_tool == tool_name]
    if not candidates:
        return None, False
    unconsumed = [(i, f) for i, f in candidates if i not in ctx.consumed]
    pool = unconsumed or candidates
    given = json.loads(input_json)

    def distance(item: tuple[int, Fixture]) -> tuple[int, int]:
        fixture = item[1]
        recorded = json.loads(fixture.input_json)
        if not isinstance(given, Mapping) or not isinstance(recorded, Mapping):
            return (len(given) + len(recorded), fixture.seq)
        keys = set(given) | set(recorded)
        differing = sum(1 for k in keys if given.get(k, _MISSING) != recorded.get(k, _MISSING))
        return (differing, fixture.seq)

    _, best = min(pool, key=distance)
    return best, bool(unconsumed)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


# --------------------------------------------------------------------------- load / run


@dataclass(frozen=True, slots=True)
class LoadedTool:
    meta: DistilledToolMeta
    fn: Callable[..., Any]
    check: Callable[[Any], Any] | None
    module: types.ModuleType


def load_tool(path: str | Path) -> LoadedTool:
    """Import a distilled tool module from its file.

    Importing runs the module's top-level code, so callers must only load files that passed the static
    check. The module is not registered in ``sys.modules``.
    """
    file = Path(path)
    module_name = f"tooldistill_tools.{re.sub(r'\W', '_', file.parent.name)}.{re.sub(r'\W', '_', file.stem)}"
    spec = importlib.util.spec_from_file_location(module_name, file)
    if spec is None or spec.loader is None:
        raise ToolLoadError(f"cannot load distilled tool module from {file}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ToolLoadError(f"importing {file} failed: {_describe(exc)}") from exc
    functions = [value for value in vars(module).values() if callable(value) and hasattr(value, META_ATTR)]
    if len(functions) != 1:
        raise ToolLoadError(f"expected exactly one @distilled_tool function in {file}, found {len(functions)}")
    check = getattr(module, "check", None)
    if check is not None and not callable(check):
        raise ToolLoadError(f"{file}: 'check' must be a function")
    return LoadedTool(meta=getattr(functions[0], META_ATTR), fn=functions[0], check=check, module=module)


@dataclass(frozen=True, slots=True)
class ToolRunFailure:
    """Why a run failed, in the terms the structured error and the verifier messages use.

    ``kind`` is ``call`` (a base tool call failed), ``replay`` (no fixture matched), ``code`` (the tool's
    own code raised between calls, or returned something that is not JSON) or ``check`` (``check(result)``
    was false or raised). ``step`` is the failing call's ordinal for ``call``/``replay`` and the number of
    calls made so far for ``code``/``check``.
    """

    kind: FailureKind
    step: int
    base_tool: str | None
    message: str
    exception: BaseException | None = field(default=None, compare=False, repr=False)


@dataclass(slots=True)
class RunResult:
    result: Any
    failure: ToolRunFailure | None
    calls: list[CallLog]
    new_fixtures: list[tuple[str, str, str]]
    latency_ms: int

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def measured_calls(self) -> int:
        return len(self.calls)


def run_tool(tool: LoadedTool, inputs: Mapping[str, Any], ctx: RunContext) -> RunResult:
    """Run a loaded distilled tool with ``inputs`` under ``ctx``; never raises for failures inside the tool."""
    token = _current.set(ctx)
    started = time.perf_counter()
    failure: ToolRunFailure | None = None
    result: Any = None
    try:
        try:
            result = tool.fn(**inputs)
        except DistilledToolCallError as exc:
            failure = ToolRunFailure("call", exc.step, exc.base_tool, exc.message, exc)
        except ReplayMismatchError as exc:
            failure = ToolRunFailure("replay", exc.step, exc.base_tool, exc.message, exc)
        except Exception as exc:
            failure = ToolRunFailure("code", ctx.step, None, _describe(exc), exc)
        if failure is None and tool.check is not None:
            try:
                passed = tool.check(result)
            except Exception as exc:
                failure = ToolRunFailure("check", ctx.step, None, f"check(result) raised {_describe(exc)}", exc)
            else:
                if not passed:
                    failure = ToolRunFailure("check", ctx.step, None, "check(result) returned False")
        if failure is None and not is_json_serializable(result):
            failure = ToolRunFailure(
                "code", ctx.step, None, f"the distilled tool returned a non-JSON-serializable value ({type(result).__name__})"
            )
    finally:
        _current.reset(token)
    return RunResult(
        result=None if failure is not None else result,
        failure=failure,
        calls=list(ctx.calls),
        new_fixtures=list(ctx.new_fixtures),
        latency_ms=_elapsed_ms(started),
    )


# -------------------------------------------------------------------------- error text


def structured_error(tool_name: str, version: int, failure: ToolRunFailure, measured_calls: int | None) -> str:
    """The fixed-format ``is_error`` result an agent receives when a distilled tool fails at run time."""
    total = "?" if measured_calls is None else str(measured_calls)
    head = f"Distilled tool '{tool_name}' (v{version})"
    if failure.kind in ("call", "replay"):
        lines = [
            f"{head} failed at step {failure.step} of {total}.",
            f"Base tool: {failure.base_tool}",
            f"Error: {failure.message}",
        ]
    elif failure.kind == "check":
        lines = [f"{head} failed its check after step {failure.step} of {total}.", f"Error: {failure.message}"]
    else:
        lines = [f"{head} failed after step {failure.step} of {total}.", f"Error: {failure.message}"]
    lines.append("This distilled tool has been disabled. Complete the task using the base tools directly.")
    return "\n".join(lines)
