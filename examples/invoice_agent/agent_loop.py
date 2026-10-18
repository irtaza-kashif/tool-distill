"""The agent loop shared by ``run.py`` and the scripted end-to-end tests.

It mirrors the SDK integration in CLAUDE.md: every tool call the model requests goes through
``trace.execute``; the developer's own ``task_succeeded`` decides the outcome; the nudge gives the model one
turn to propose a distilled tool; ``trace.finish()`` may run the fallback distiller.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tooldistill import ToolDistill
from tooldistill.core.models import CallKind
from tooldistill.core.proposals import IngestResult


@dataclass(slots=True)
class RunReport:
    trace_id: str
    task: str
    outcome: bool | None
    model_turns: int
    base_calls: int  # top-level base tool calls (the baseline measure)
    distilled_calls: int
    distilled_failures: int
    inner_calls: int  # base tool calls made inside distilled tools
    propose_calls: int
    input_tokens: int
    output_tokens: int
    seconds: float
    nudged: bool
    final_text: str
    distill_result: IngestResult | None = None
    steps: list[tuple[str, bool]] = field(default_factory=list)  # (tool name, is_error) in order


def run_agent(
    td: ToolDistill,
    client: Any,
    task: str,
    *,
    model: str = "claude-sonnet-5-5",
    max_steps: int = 30,
    max_tokens: int = 4096,
    system: str | None = None,
    task_succeeded: Callable[[str], bool] | None = None,
    log: Callable[[str], None] | None = None,
) -> RunReport:
    trace = td.start_trace(task)
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    started = time.perf_counter()
    final_text = ""
    turns = 0
    steps: list[tuple[str, bool]] = []
    for _ in range(max_steps):
        tools = td.tools.as_dicts() + td.active_tools() + [td.propose_tool()]
        request: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "tools": tools, "messages": messages}
        if system:
            request["system"] = system
        resp = client.messages.create(**request)
        turns += 1
        trace.record_usage(resp.usage)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            final_text = "".join(getattr(block, "text", "") for block in resp.content if getattr(block, "type", None) == "text")
            if trace.outcome is None:
                success = task_succeeded(task) if task_succeeded is not None else True
                trace.set_outcome(success=success)
            nudge = trace.distill_nudge()
            if nudge:
                if log:
                    log("nudge: asking the model to propose a distilled tool")
                messages.append({"role": "user", "content": nudge})
                continue
            break
        results: list[dict[str, Any]] = []
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            out = trace.execute(block.name, block.input)
            steps.append((block.name, out.is_error))
            if log:
                flag = "error" if out.is_error else "ok"
                log(f"{block.name} -> {flag}: {out.content[:120]}")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": out.content, "is_error": out.is_error})
        messages.append({"role": "user", "content": results})
    else:
        if trace.outcome is None:
            trace.set_outcome(success=False)
    distill_result = trace.finish()
    seconds = time.perf_counter() - started

    record = td.registry.get_trace(trace.id)
    calls = td.registry.list_tool_calls(trace.id)
    assert record is not None
    return RunReport(
        trace_id=trace.id,
        task=task,
        outcome=record.outcome,
        model_turns=turns,
        base_calls=sum(1 for c in calls if c.kind is CallKind.BASE and c.parent_call_id is None),
        distilled_calls=sum(1 for c in calls if c.kind is CallKind.DISTILLED),
        distilled_failures=sum(1 for c in calls if c.kind is CallKind.DISTILLED and c.is_error),
        inner_calls=sum(1 for c in calls if c.kind is CallKind.BASE and c.parent_call_id is not None),
        propose_calls=sum(1 for c in calls if c.kind is CallKind.PROPOSE),
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        seconds=seconds,
        nudged=record.nudged,
        final_text=final_text,
        distill_result=distill_result,
        steps=steps,
    )
