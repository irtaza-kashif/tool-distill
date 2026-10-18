"""Run the same incident task twice without tooldistill and twice with it, and print the difference.

    uv run --env-file .env python examples/incident_agent/compare.py --alert A-1042

Row 1, "without": a plain tool-use loop, the base tools only, no library, no store. It never learns.
Row 2, "with": the same loop with tooldistill wired in and a fresh, empty store. Run 1 discovers the path and the
model proposes a distilled tool; run 2 calls that tool.

Every run is a new conversation; the fake world is reset before each run; both rows use the same model and the same
system prompt. The one-call rollback is down during every run (``--no-outage`` turns that off), so discovery has to
find the long way round.
"""

import argparse
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
INVOICE_DIR = ROOT / "examples" / "invoice_agent"
if str(INVOICE_DIR) not in sys.path:
    sys.path.append(str(INVOICE_DIR))

from agent_loop import run_agent  # noqa: E402
from run import SYSTEM, _load_world, load_dotenv  # noqa: E402

from tooldistill import ToolDistill  # noqa: E402
from tooldistill.core.config import load_config  # noqa: E402

incidents = _load_world()
COMPARE_STORE = HERE / ".tooldistill_compare"


@dataclass
class Measure:
    label: str
    calls: int  # tool calls the model made (base calls, and in the "with" row the distilled call too)
    inner_calls: int  # base calls made inside distilled tools
    tokens: int
    seconds: float
    turns: int
    outcome: bool
    note: str = ""


# ------------------------------------------------------------------- the plain loop: no library at all


def run_plain(client: Any, task: str, tools: list[dict[str, Any]], *, model: str, max_steps: int, log=None) -> Measure:
    """An ordinary tool-use loop. Nothing is recorded, nothing is learned."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    started = time.perf_counter()
    calls = tokens = turns = 0
    for _ in range(max_steps):
        resp = client.messages.create(model=model, max_tokens=4096, system=SYSTEM, tools=tools, messages=messages)
        turns += 1
        tokens += resp.usage.input_tokens + resp.usage.output_tokens
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        results = []
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            calls += 1
            try:
                value = incidents.execute_tool(block.name, dict(block.input))
                content, is_error = _content(value), False
            except Exception as exc:  # the executor contract: a raise is a failed call
                content, is_error = str(exc), True
            if log:
                log(f"  {block.name} -> {'error' if is_error else 'ok'}")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return Measure("without", calls, 0, tokens, time.perf_counter() - started, turns, incidents.task_succeeded(task))


def _content(value: Any) -> str:
    import json

    return value if isinstance(value, str) else json.dumps(value)


# ------------------------------------------------------------------- the loop with tooldistill


def run_with(td: ToolDistill, client: Any, task: str, *, model: str, max_steps: int, log=None) -> Measure:
    report = run_agent(td, client, task, model=model, max_steps=max_steps, system=SYSTEM, task_succeeded=incidents.task_succeeded, log=log)
    note = ""
    calls = td.registry.list_tool_calls(report.trace_id)
    for call in calls:
        if call.kind == "propose" and not call.is_error and call.output_json:
            note = f"proposed a distilled tool: {call.output_json.strip(chr(34))[:160]}"
    if report.distill_result is not None:
        note = f"fallback distiller: {report.distill_result.verdict[:160]}"
    if report.distilled_calls:
        note = f"called the distilled tool {report.distilled_calls}x ({report.inner_calls} base calls inside)"
    return Measure(
        "with",
        report.base_calls + report.distilled_calls,
        report.inner_calls,
        report.input_tokens + report.output_tokens,
        report.seconds,
        report.model_turns,
        bool(report.outcome),
        note,
    )


# ------------------------------------------------------------------- the comparison


def fresh_world(outage: bool) -> None:
    incidents.reset()
    incidents.set_switch("rollback_outage", outage)


def run_comparison(client: Any, *, alert: str, model: str, runs: int, outage: bool, max_steps: int, log=None, quiet=False) -> dict[str, list[Measure]]:
    task = incidents.task_for(alert)
    tools = incidents.tool_definitions(reason=False)
    results: dict[str, list[Measure]] = {"without": [], "with": []}

    def say(line: str) -> None:
        if not quiet:
            print(line, flush=True)

    say(f"task:  {task}")
    say(f"model: {model}   runs per row: {runs}   one-call rollback: {'down' if outage else 'up'}")
    say("")
    for n in range(1, runs + 1):
        say(f"--- without tooldistill, run {n} ---")
        fresh_world(outage)
        m = run_plain(client, task, tools, model=model, max_steps=max_steps, log=log)
        results["without"].append(m)
        say(f"    {m.calls} tool calls, {m.tokens:,} tokens, {m.seconds:.1f}s, {'done' if m.outcome else 'FAILED'}")

    if COMPARE_STORE.exists():
        shutil.rmtree(COMPARE_STORE)
    config = load_config(HERE / "tooldistill.toml")
    settings = config.settings.as_dict()
    td = ToolDistill(COMPARE_STORE, incidents.execute_tool, tools, health_probe=incidents.probe, **settings)
    try:
        for n in range(1, runs + 1):
            say(f"--- with tooldistill, run {n} ---")
            fresh_world(outage)
            m = run_with(td, client, task, model=model, max_steps=max_steps, log=log)
            results["with"].append(m)
            say(f"    {m.calls} tool calls, {m.tokens:,} tokens, {m.seconds:.1f}s, {'done' if m.outcome else 'FAILED'}")
            if m.note:
                say(f"    {m.note}")
    finally:
        td.close()
    return results


def table(results: dict[str, list[Measure]]) -> str:
    runs = len(results["without"])
    header = f"{'':<22}" + "".join(f"{f'run {n}':>36}" for n in range(1, runs + 1))
    lines = [header, "-" * len(header)]
    for label, title in (("without", "without tooldistill"), ("with", "with tooldistill")):
        cells = []
        for m in results[label]:
            inner = f" (+{m.inner_calls} inside)" if m.inner_calls else ""
            cells.append(f"{m.calls} calls{inner}, {m.tokens / 1000:.0f}k tok, {m.seconds:.0f}s")
        lines.append(f"{title:<22}" + "".join(f"{c:>36}" for c in cells))
    first, last = results["with"][0], results["with"][-1]
    if last.tokens and last.seconds:
        lines.append("")
        lines.append(
            f"with tooldistill, run {runs} vs run 1: {first.tokens / max(last.tokens, 1):.0f}x fewer model tokens, "
            f"{first.seconds / max(last.seconds, 0.1):.0f}x faster"
        )
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--alert", default="A-1042", help="A-1042 (Checkout API) or A-1057 (Payments Worker)")
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--runs", type=int, default=2, help="runs per row (default 2)")
    parser.add_argument("--max-steps", type=int, default=60)
    parser.add_argument("--no-outage", action="store_true", help="leave the one-call rollback up during every run")
    parser.add_argument("--verbose", action="store_true", help="print every tool call as it happens")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    load_dotenv(ROOT / ".env", HERE / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print("No ANTHROPIC_API_KEY found: put it in <repo root>/.env (see .env.example) or export it.", file=sys.stderr)
        return 2

    import anthropic

    client = anthropic.Anthropic()
    results = run_comparison(
        client, alert=args.alert, model=args.model, runs=args.runs, outage=not args.no_outage,
        max_steps=args.max_steps, log=print if args.verbose else None,
    )
    print()
    print(table(results))
    return 0 if all(m.outcome for row in results.values() for m in row) else 1


if __name__ == "__main__":
    sys.exit(main())
