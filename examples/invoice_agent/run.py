"""Run the invoice agent on a task with a real model and print what tooldistill did.

Usage, from the repo root:

    uv run python examples/invoice_agent/run.py --customer ACME [--outage] [--schema-currency] [--tools-updated]

The demo script in CLAUDE.md:
  1. --outage                      ~20-30 base tool calls, the agent proposes a distilled tool, verified, promoted
  2. --customer Globex             one distilled tool call (4 base calls inside)
  3. tooldistill tool-updated billing_import_from_email; tooldistill rebuild ...   (CLI, build step 8)
  4. --schema-currency             the API changed before its definition: structured error, fallback, new version
     --schema-currency --tools-updated   the definition changed too: the startup check catches it first

Credentials: ANTHROPIC_API_KEY from the environment, or from a .env file in the repo root or this folder.
"""

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import tools as invoice  # noqa: E402
from agent_loop import RunReport, run_agent  # noqa: E402

from tooldistill import ToolDistill  # noqa: E402
from tooldistill.core.config import load_config  # noqa: E402

SYSTEM = (
    "You are an operations assistant with tools for email and billing. Complete the task with the tools, "
    "preferring the most direct path you can find; if a tool fails, work around it with the other tools. "
    "When the task is done, reply with one line stating what you did."
)


def load_dotenv(*paths: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, optional quotes; never overrides existing variables."""
    for path in paths:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            if key and value:
                os.environ.setdefault(key, value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--customer", default="ACME", help="ACME, Globex or Initech (default ACME)")
    parser.add_argument("--task", help="override the task text (default: the invoice task for --customer)")
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--outage", action="store_true", help="billing_import_from_email returns 503")
    parser.add_argument("--schema-currency", action="store_true", help="the billing API now requires 'currency'")
    parser.add_argument("--tools-updated", action="store_true", help="the tool definitions include 'currency' too")
    parser.add_argument("--no-fallback", action="store_true", help="skip the fallback distiller at the end of the run")
    return parser.parse_args(argv)


def print_report(report: RunReport, td: ToolDistill) -> None:
    print()
    print(f"outcome:          {'success' if report.outcome else 'failure'}")
    print(f"model turns:      {report.model_turns}")
    print(f"base tool calls:  {report.base_calls} (top level)")
    print(f"distilled calls:  {report.distilled_calls} ({report.distilled_failures} failed, {report.inner_calls} base calls inside)")
    print(f"propose calls:    {report.propose_calls}")
    print(f"tokens:           {report.input_tokens} in / {report.output_tokens} out")
    print(f"time:             {report.seconds:.1f}s")
    print(f"nudged:           {'yes' if report.nudged else 'no'}")
    if report.distill_result is not None:
        print(f"fallback distill: {report.distill_result.verdict}")
    if report.final_text:
        print(f"model said:       {report.final_text.strip()[:300]}")
    versions = td.registry.list_versions()
    if versions:
        print()
        print("distilled tools:")
        for version in versions:
            print(f"  {version.label:<34} {version.status:<10} {version.status_reason or '':<24} "
                  f"baseline={version.baseline_calls} measured={version.measured_calls}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy code page
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    load_dotenv(ROOT / ".env", HERE / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print("No ANTHROPIC_API_KEY found: put it in <repo root>/.env (see .env.example) or export it.", file=sys.stderr)
        return 2

    if args.outage:
        invoice.set_switch("import_outage", True)
    if args.schema_currency:
        invoice.set_switch("schema_currency", True)
    if args.tools_updated:
        invoice.set_switch("tools_updated", True)
    tools = invoice.current_tools()

    config = load_config(HERE / "tooldistill.toml")
    settings = config.settings.as_dict()
    if args.no_fallback:
        settings["fallback_distiller"] = False
    td = ToolDistill(config.store, invoice.execute_tool, tools, health_probe=invoice.probe, **settings)

    import anthropic  # the demo's own dependency on the model; the library core never imports it

    client = anthropic.Anthropic()
    task = args.task or invoice.task_for(args.customer)
    switches = [name for name in ("import_outage", "schema_currency", "tools_updated") if invoice.switch(name)]
    print(f"task:     {task}")
    print(f"store:    {config.store}")
    print(f"switches: {', '.join(switches) or 'none'}")
    print(f"exposed:  {', '.join(t['name'] for t in td.active_tools()) or 'no distilled tools yet'}")
    print()
    try:
        report = run_agent(
            td, client, task, model=args.model, max_steps=args.max_steps, system=SYSTEM,
            task_succeeded=invoice.task_succeeded, log=print,
        )
        print_report(report, td)
    finally:
        td.close()
    return 0 if report.outcome else 1


if __name__ == "__main__":
    sys.exit(main())
