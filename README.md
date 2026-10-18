# tooldistill

Turn an agent's trial-and-error runs into verified, reusable tools, and keep them working as the tools underneath them change.

An agent solving a task often takes 30 tool calls of trial and error to find a path that only needed 4. `tooldistill` is a Python library that turns that discovered path into a **distilled tool**: a small Python module that calls the base tools in a fixed, known-good sequence, so future runs call one tool instead of reasoning through the task again.

Other projects generate tools like this. The difference here is **trust and upkeep**:

1. **Verification.** Every distilled tool passes three checks before an agent can use it: a static check of the code, a replay against the recorded responses of the run it came from, and an efficiency check against that run's cost.
2. **Lifecycle.** Distilled tools move through `draft → verified → active → suspect → deprecated` on evidence the library measures, never on what the model says about them.
3. **Reacting to base tool changes.** When a base tool changes schema, breaks, or comes back after an outage, the affected distilled tools are flagged, re-verified, or rebuilt.

**The agent proposes, the library verifies.** An LLM writes distilled tools; the library never trusts its claims about cost, dependencies or correctness.

## Install

Requires Python 3.12 or newer. The project is managed with [uv](https://docs.astral.sh/uv/).

```
uv add tooldistill          # in your project
uv sync                     # in this repository
```

## SDK integration

Add a handful of lines to an existing agent loop. Everything goes through `trace.execute`, so the library can record base tool calls, run distilled tools, and ingest proposals.

```python
import anthropic
from tooldistill import ToolDistill

client = anthropic.Anthropic()
td = ToolDistill(store=".tooldistill", executor=execute_tool, tools=TOOLS)
# or: td = ToolDistill.from_config()   # reads tooldistill.toml

def run_agent(task: str, max_steps: int = 30):
    trace = td.start_trace(task)
    messages = [{"role": "user", "content": task}]
    for _ in range(max_steps):
        tools = TOOLS + td.active_tools() + [td.propose_tool()]
        resp = client.messages.create(model="claude-sonnet-5-5", max_tokens=4096, tools=tools, messages=messages)
        trace.record_usage(resp.usage)                 # optional: enables token baselines
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            if trace.outcome is None:
                trace.set_outcome(success=task_succeeded(task))   # your own check
            nudge = trace.distill_nudge()              # None unless the run is worth distilling
            if nudge:
                messages.append({"role": "user", "content": nudge})
                continue                               # the agent gets one turn to propose
            break
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                out = trace.execute(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": out.content, "is_error": out.is_error})
        messages.append({"role": "user", "content": results})
    else:
        trace.set_outcome(success=False)
    trace.finish()                                     # may run the fallback distiller
```

- `executor(name, args)` is your function that runs a base tool. It **raises** to signal failure and returns JSON-serializable values.
- `tools` is your list of Anthropic-format tool dicts (`name`, `description`, `input_schema`).
- `task_succeeded` is yours too. The library never infers success from the model stopping.

What `trace.execute` does with a tool call:

| Tool | Behaviour |
|---|---|
| base tool | runs the executor, records inputs, outputs, errors and latency |
| distilled tool | validates the input against its schema, runs the module live (inner calls recorded with a parent id), returns the result; on failure returns a **structured error**, demotes the version to `suspect(runtime_failure)` and lets the agent fall back to base tools |
| `propose_distilled_tool` | validates the proposal, stores it as the tool's next draft version, verifies it, and returns the verdict so the agent can fix and resubmit |

A structured error looks like this:

```
Distilled tool 'create_invoice_from_email' (v2) failed at step 2 of 2.
Base tool: billing_import_from_email
Error: missing required property 'currency'
This distilled tool has been disabled. Complete the task using the base tools directly.
```

The fallback run that follows is worth distilling: the nudge asks for a replacement under the same name.

## What a distilled tool looks like

```python
from tooldistill import distilled_tool, call

@distilled_tool(
    name="create_invoice_from_email",
    description="Create a draft invoice for a customer from their most recent order email.",
    input_schema={
        "type": "object",
        "properties": {"customer": {"type": "string"}, "since": {"type": "string", "default": "1d"}},
        "required": ["customer"],
    },
)
def create_invoice_from_email(customer: str, since: str = "1d") -> dict:
    msg = call("gmail_search", query=f"from:{customer} order", since=since)[0]
    order = call("gmail_get_message", id=msg["id"])
    cust = call("billing_find_customer", name=customer)
    return call("billing_create_invoice", customer_id=cust["id"], lines=order["lines"])


def check(result: dict) -> bool:
    return result.get("status") == "draft"
```

Every base tool call goes through `call("<literal name>", **kwargs)`, so dependencies can be read from the source. The static check allows only `from tooldistill import distilled_tool, call`, imports of `typing`, `json`, `re`, `datetime` and `math`, one decorated function and an optional `check()`. It bans `try/except` (a failed base tool call must fail the tool so the fallback can run), `while`, `with`, `class`, `async`, `global`, `nonlocal`, `yield`, nested imports, anything starting with `__`, and the usual escape hatches (`open`, `eval`, `exec`, `getattr`, `os`, `subprocess`, ...). Status, baseline and dependencies are never in the file: the library measures them.

## The three checks

1. **Static.** `ast` rules on the source text, before the module is ever imported. Failure messages name the line and the offending token, for example `line 5: call to 'billing_find_customr' — no such base tool`.
2. **Replay.** The module runs with the proposal's `test_inputs` against fixtures recorded from the source run. Calls match on the exact tool name and canonical arguments and consume fixtures in order, so a search repeated before and after a create gets the right recorded output. An unmatched call fails with the nearest recorded fixture named. Calls no fixture covers can run live when `allow_live_verification` is on (real side effects: only for mock or sandboxed tools).
3. **Efficiency.** The replayed call count must be at most `baseline × (1 − min_call_reduction)`, where the baseline is the source run's top-level base tool calls (a new version of an existing tool inherits a larger baseline from its predecessor). A rebuild after a recovered tool must also beat the version it replaces.

## Lifecycle and exposure

| From | To | Trigger |
|---|---|---|
| draft | verified | all three checks pass (`auto_promote` goes straight to active) |
| verified | active | `promote`; the previously active version is deprecated |
| active | suspect | a dependency's schema changed, a runtime failure, or an avoided tool recovered |
| suspect | active | re-verification passes; after a runtime failure only `verify --live` counts |
| suspect | deprecated | re-verification fails, or a newer version becomes active |
| deprecated | active | `promote --version N` (rollback), after re-passing verification |

`active_tools()` exposes active versions plus suspect ones whose only problem is a recovered avoided tool (they still work; they may just be improvable), ranked by successful runs and capped by `max_exposed_tools`.

## Reacting to base tool changes

- `td.on_tool_updated("billing_import_from_email")` (or `tooldistill tool-updated ...`): versions that depend on the tool with a different stored schema hash are re-verified at once; versions that **avoided** it (every call to it failed in their source run) are queued for a rebuild.
- `td.health()`: compares the current tool list's schema hashes against the stored dependencies and, with a `health_probe(tool_name) -> bool` configured, looks for recoveries of avoided tools. The library never calls the executor on its own for health checks.
- `ToolDistill(...)` runs the schema comparison at startup (`check_schemas_on_start`), so a changed definition is caught before the first run instead of at the first failed call.
- `td.rebuild(name)`: hands the source trace and a note about the recovered tool to the Distiller and ingests the result as a new version, verified like any other.

## Configuration: `tooldistill.toml`

```toml
store = ".tooldistill"
executor = "myapp.tools:execute_tool"      # imported by the CLI
tools = "myapp.tools:TOOLS"
# health_probe = "myapp.tools:probe"       # optional
# distiller = "myapp.distill:DISTILLER"    # optional: a custom Distiller instead of ClaudeDistiller
distiller_model = "claude-sonnet-5-5"
fallback_distiller = true
auto_promote = false
min_calls_to_distill = 8
min_call_reduction = 0.3
max_exposed_tools = 20
allow_live_verification = false
check_schemas_on_start = true
```

| Setting | Default | Consequence |
|---|---|---|
| `fallback_distiller` | `true` | one extra Claude call on every worth-distilling run where the agent did not propose |
| `auto_promote` | `false` | verified tools wait for `promote` |
| `min_calls_to_distill` | `8` | runs with fewer top-level base tool calls are not distilled (waived after a distilled tool failed) |
| `min_call_reduction` | `0.3` | a distilled tool must use at least 30% fewer base tool calls than its baseline |
| `max_exposed_tools` | `20` | caps the context cost of distilled tool definitions |
| `allow_live_verification` | `false` | live verification has real side effects |
| `check_schemas_on_start` | `true` | a cheap schema comparison on every `ToolDistill(...)` |

## Base tools from MCP servers

If your base tools live on MCP servers, skip `executor`/`tools` and point the config at them (the `plugin` extra, `uv add "tooldistill[plugin]"`, brings the MCP SDK):

```toml
mcp_config = ".mcp.json"            # Claude Code's format: {"mcpServers": {"name": {"type": "stdio", "command": ..., "args": [...], "env": {...}}}}
[mcpServers.billing]                # inline entries use the same structure and are merged over the file by name
type = "http"
url = "http://localhost:8787/mcp"
headers = { Authorization = "Bearer ${BILLING_TOKEN}" }   # ${VAR} and ${VAR:-default} expand from the environment
```

`ToolDistill.from_config()` then connects to every server (on a background thread, so the executor stays synchronous), names each tool `<server>__<tool>`, and uses the connections as the executor and the health probe. `tooldistill mcp tools` lists what the servers expose. A failed tool result raises, as the executor contract requires; a server that dies is reconnected on the next call. The same adapter is available directly as `tooldistill.integrations.mcp.MCPToolkit`.

## CLI

```
tooldistill init                          create the store and a sample config
tooldistill list [--status S]             distilled tool versions
tooldistill show <name> [--version N]     code, status history, checks, run stats
tooldistill traces list | show <id>       recorded runs
tooldistill promote <name> [--version N]  activate (rollback re-verifies first)
tooldistill deprecate <name> [--version N]
tooldistill verify <name> [--version N] [--live]
tooldistill tool-updated <base_tool>
tooldistill health [--no-probe]
tooldistill rebuild <name>
```

`list`, `show`, `traces` and `deprecate` only need the store. The others import the executor and tools from `tooldistill.toml` (`--config` to point elsewhere).

## Claude Code plugin

The same library runs inside Claude Code as a plugin (`plugin/`, rendered by `scripts/render_plugin.py`; `plugin-dev/` runs this checkout). Hooks record the session's tool calls into a trace keyed by the session id, an MCP server offers `propose_distilled_tool` and the verified distilled tools, and `/tooldistill:distill` is how you confirm a task went well and ask for a tool. The project needs a `tooldistill.toml` whose base tools come from MCP servers (`mcp_config = ".mcp.json"`). Install with `claude plugin marketplace add irtaza-kashif/tool-distill` and `claude plugin install tooldistill@tooldistill`, or try the checkout with `claude --plugin-dir <checkout>/plugin-dev`. Details in `plugin/README.md`; a ready-made project to try it in, with the fake invoice tools served over MCP, is `examples/invoice_agent/claude_code/`.

## The store

`.tooldistill/` holds a SQLite database (`registry.db`, WAL mode, safe for the agent process and the CLI at the same time) and the distilled tool files under `tools/<name>/v<N>.py`. The store's own `.gitignore` excludes the database: traces and fixtures contain real tool outputs, which may include personal data. The `.py` files may be committed.

## Demo

`examples/invoice_agent/` has fake Gmail and billing tools (no real services), an outage switch and a schema-change switch, and a runner that uses a real model. Put `ANTHROPIC_API_KEY=...` in a `.env` file in the repository root, then:

```
uv run python examples/invoice_agent/run.py --customer ACME --outage      # discovery -> proposal -> verified -> active
uv run python examples/invoice_agent/run.py --customer Globex --outage    # one distilled call
cd examples/invoice_agent
uv run tooldistill list                                                   # the model picks the tool's name: read it here
uv run tooldistill tool-updated billing_import_from_email                 # the outage is over: suspect, queued
uv run --env-file ../../.env tooldistill rebuild <name>                   # the distiller builds a shorter version
cd ../..
uv run python examples/invoice_agent/run.py --customer Initech --schema-currency   # structured error, fallback, new version
```

The CLI reads its settings from `tooldistill.toml` in the current folder (or `--config`); it does not read `.env`. `rebuild` is the one command that calls the model, so it needs `ANTHROPIC_API_KEY` in the environment: `uv run --env-file ../../.env ...` loads it from the file, or set the variable in your shell.

The scripted version of the same four steps runs in the test suite against a fake model, without the network.

## Known limitations

- **Replay proves reproduction, not generalization.** It shows the code reproduces the source run with the test inputs; it does not prove the tool works for other inputs. Runtime failures plus fallback are the safety net.
- **`check()` is written by the proposer** and can be trivial. It adds confidence only when meaningful.
- **Distilled tool code is LLM-written and runs in-process.** The static allowlist is the only guard. A subprocess sandbox is future work.
- **Avoided-tool detection is a heuristic** (every call failed). An agent that misused a working tool every time produces a false "avoided" entry.
- **Outcome quality depends on your success check.** If you always pass `success=True`, failed runs can be distilled; verification still catches many of them.

## Development

```
uv sync
uv run pytest -q
uv build && uvx --from dist/tooldistill-0.1.0-py3-none-any.whl tooldistill --help
```

Tests never touch the network: the Anthropic client is stubbed and the base tools are in-process fakes.
