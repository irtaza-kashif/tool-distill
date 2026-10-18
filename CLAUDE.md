# CLAUDE.md — tooldistill

> Working name: `tooldistill`. If the package is renamed, rename it everywhere (package, CLI command, store folder, config file).

## What this project is

An agent solving a complex task often takes 30+ tool calls of trial and error to find a path that only needed 4–5. `tooldistill` is a Python library that turns that discovered path into a reusable **distilled tool**, so future runs call one tool instead of reasoning through the task again.

Other projects already generate tools like this. **Our differentiator is trust and upkeep:**

1. **Verification.** Every distilled tool must pass checks (static, replay, efficiency) before an agent can use it.
2. **Lifecycle.** Distilled tools move through statuses (draft → verified → active → suspect → deprecated) based on evidence, not on what the model says about them.
3. **Reacting to base tool changes.** When a base tool changes schema, breaks, or comes back after an outage, affected distilled tools are flagged, re-verified, or rebuilt.

**Core principle: the agent proposes, the library verifies.** An LLM writes distilled tools. The library never trusts the LLM's claims about cost, dependencies, or correctness. It measures them itself.

The product is a **library** (plus a thin CLI in the same package). It never runs on its own. It is embedded in a developer's agent loop. A Claude Code plugin is a stretch goal and is a thin adapter over the same library.

---

## Terminology (use these exact words in code, comments, docs, and CLI output)

### The world the library lives in
| Term | Meaning |
|---|---|
| **Agent** | The developer's LLM-driven program that calls tools to complete tasks. |
| **Agent loop** | The cycle: call the model → run the tools it requested → send results back, until done or a step limit is hit. |
| **Task** | What the agent was asked to do, e.g. "Invoice ACME from their order email". |
| **Run** | One execution of the agent loop on one task. (Do not say "session" — that means something specific in Claude Code.) |
| **Outcome** | Whether a run actually accomplished its task. **Decided by the developer's code, never inferred from the model stopping.** |
| **Base tool** | An ordinary tool the agent already has (MCP tool, API wrapper, function). Distilled tools are built from base tools. |
| **Tool call** | One request from the model to run one tool with specific inputs. |
| **Executor** | The developer's function that actually runs a base tool: `execute_tool(name: str, args: dict) -> Any`. Raises to signal failure; the return value must be JSON-serializable (see Executor contract). |

### Creating distilled tools
| Term | Meaning |
|---|---|
| **Trace** | The recorded list of every tool call in a run: inputs, outputs, errors, latency, and (if reported) token usage. |
| **Source trace** | The trace a distilled tool version was distilled from. Supplies its baseline, fixtures, and avoided tools. |
| **Worth distilling** | A trace whose outcome is success and that has no proposal yet, and then either: it contains a **failed** distilled tool call (that run is the source for the broken tool's next version, whatever else it did), or it made no successful distilled tool call and made ≥ `min_calls_to_distill` base tool calls outside distilled tools. |
| **Distillation** | Turning a successful trace into a distilled tool. |
| **Distilled tool** | A Python function with a name, description, and input schema that completes a task by calling base tools in a fixed, known-good sequence. |
| **`call()`** | The library helper a distilled tool must use for every base tool call. Routes to the executor (live mode) or fixtures (replay mode) and lets the library track dependencies. |
| **Proposal** | A distilled tool submitted for review but not yet stored, from the agent (via the propose tool) or from a Distiller. |
| **Propose tool** | A tool the library gives the agent so it can submit a proposal: `propose_distilled_tool`. |
| **Test inputs** | Arguments, supplied with a proposal, that make the distilled tool reproduce its source run. Used by the replay check. |
| **Distiller** | Anything that turns a trace into a proposal. The built-in `ClaudeDistiller` is the fallback when the agent doesn't propose one itself. |

### Verification and lifecycle
| Term | Meaning |
|---|---|
| **Verifier** | The component that decides whether a distilled tool can be trusted. |
| **Checks** | The three verification layers: **static**, **replay**, **efficiency**. |
| **Live mode** | `call()` runs base tools through the real executor. Used when an agent uses a distilled tool, and for live verification. |
| **Replay mode** | `call()` returns recorded fixtures instead of calling the executor. Used by the replay check. No side effects. |
| **Fixtures** | Recorded base tool responses from the source trace, used in replay mode. |
| **Baseline** | The measured cost of the source run: **base tool calls only** (excludes propose tool calls and calls made inside distilled tools), plus tokens and latency when available. |
| **Status** | Lifecycle position: **draft → verified → active → suspect → deprecated**. |
| **Exposed** | A distilled tool version that `active_tools()` returns to the agent (see exposure rules). |
| **Promotion / demotion** | Moving a distilled tool up or down a status. |
| **Version** | A numbered revision of a distilled tool. All versions share the tool's name; at most one is active. |

### Keeping distilled tools current
| Term | Meaning |
|---|---|
| **Dependencies** | The base tools a distilled tool calls, each stored with its schema hash at verification time. |
| **Avoided tools** | Base tools where **every** call in the source trace failed and that the distilled tool doesn't call. If one recovers, a shorter version may exist. |
| **Schema hash** | SHA-256 of a base tool's canonicalized `input_schema` JSON (description changes don't count). Used to detect changes. |
| **Health check** | Detecting schema changes, outages, and recoveries of base tools. |
| **Tool update** | A notice (CLI, code, or health check) that a base tool changed or recovered. Triggers re-verification. |
| **Structured error** | The fixed-format `is_error` result a failing distilled tool returns to the agent (see format below). |
| **Fallback** | When a distilled tool fails at run time: it returns a structured error, the agent completes the task with base tools, and the distilled tool is demoted to suspect. The fallback run is worth distilling; its nudge asks for a new version under the same name. |
| **Rebuild** | Creating a new version of a suspect distilled tool. |
| **Rebuild queue** | Stored list of distilled tools waiting for a rebuild, with the reason and the base tool involved. |

### Infrastructure
| Term | Meaning |
|---|---|
| **Store** | The `.tooldistill/` folder: SQLite database plus distilled tool `.py` files. |
| **Registry** | The component that reads and writes the store. |
| **CLI** | The `tooldistill` terminal command; a thin wrapper over library functions. |
| **SDK integration** | Using the library inside a developer's own agent loop. **Primary target.** |
| **Plugin integration** | Using the library inside Claude Code via hooks and an MCP server. **Stretch goal.** |

### Words NOT to use
- "skill" for the generated artifact (clashes with Anthropic Agent Skills / SKILL.md)
- "recipe", "custom tool", "shortcut" → say **distilled tool**
- "runner" as a separate component → running distilled tools is part of `runtime`
- "session" in SDK context → say **run**

---

## Tech stack and conventions

- Python **3.12 or newer** (`requires-python = ">=3.12"`; development runs on 3.14, pinned with `uv python pin 3.14`), managed with **uv**. Package layout under `src/tooldistill/`. Syntax up to 3.12 is fine (`type` aliases, PEP 695 generics, `X | None`); nothing newer than 3.12 from the standard library (no `uuid.uuid7`, `copy.replace`, `warnings.deprecated`). The suite must pass on 3.12, 3.13 and 3.14.
- Dependencies: `anthropic` (ClaudeDistiller), `typer` (CLI), `jsonschema` (schema validation). Storage uses stdlib `sqlite3`. Keep dependencies minimal.
- Tests: `pytest`. **No network in tests.** Mock the Anthropic client; use in-process fake base tools.
- Full type hints; `dataclasses` for internal models.
- Default distiller model: `claude-sonnet-5-5` (configurable).
- The library is framework-agnostic. Nothing in `core/` may import `anthropic` or know about any specific agent framework. Anthropic-specific code lives in `distill/claude.py` and `integrations/`.
- Every CLI command must be a thin call into a public library function. No logic in `cli.py`.

### Naming rules
- **All tool names** (base and distilled) must match `^[a-zA-Z0-9_-]{1,64}$`. The Anthropic API rejects dots, so use `gmail_search`, never `gmail.search`.
- Distilled tool names additionally match `^[a-z][a-z0-9_]{2,63}$` and **must not collide** with any base tool name or the propose tool's name. Reject the proposal if they do.
- A proposal whose name matches an **existing distilled tool** becomes that tool's next version, whatever the status of the earlier versions. It is verified like any other version and inherits the larger baseline (see Efficiency). If it is activated (auto-promote or `promote`), the previously active version is deprecated as `superseded_by_v<N>`; with auto-promote off it waits at verified while the old version keeps serving. The verdict says which happened. A run that used the active version successfully is never worth distilling, so a replacement can only come from a run where the agent chose not to use it.

### Serialization rules
- Executor return values must be JSON-serializable. If not, `trace.execute` records an error and returns `is_error=True`.
- The store keeps inputs and outputs as **canonical JSON** (`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`). Use the same function for fixture matching and schema hashing.
- `ToolResult.content` is always a string: strings pass through; everything else is `json.dumps(obj)`.

### Executor contract
- Signature: `execute_tool(name: str, args: dict) -> Any`.
- **Failure is an exception.** The executor raises to signal that a call failed; the exception message becomes the recorded `error`. A returned value is always a success, even `{"error": ...}`: the library cannot tell. The demo's fake tools raise (e.g. `ToolError("503 Service Unavailable")`).
- Return values must be JSON-serializable (see Serialization rules).
- The library never calls the executor on its own. Only `trace.execute`, `call()` in live mode, and live verification do.

### SQLite rules
- The agent process and the CLI may write at the same time. On every connection: `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000`, `PRAGMA foreign_keys=ON`.
- Short transactions only; never hold a transaction open while running tools or calling a model.

### Git
- The `init` step (or first store creation) writes `.tooldistill/.gitignore` ignoring `registry.db*`. **Fixtures and traces contain real tool outputs, which may include personal data.** Distilled tool `.py` files may be committed.

### Packaging
- One repo, one package. `pyproject.toml` uses the `uv_build` backend with the src layout, so only `src/tooldistill/` ships; `plugin/`, `examples/` and `tests/` stay out of the wheel.
- `[project.scripts] tooldistill = "tooldistill.cli:app"` installs the CLI command.
- `anthropic`, `typer` and `jsonschema` are plain dependencies. The MCP SDK is the extra `tooldistill[plugin]`, needed only by `tooldistill serve` and the hooks.
- Ship an empty `src/tooldistill/py.typed`.
- The Claude Code plugin lives in `plugin/` (manifest, `.mcp.json`, `hooks/hooks.json`, `commands/distill.md`) with a root `.claude-plugin/marketplace.json` pointing at it. It bundles no Python: its MCP server and hooks run `uvx --from "tooldistill[plugin]==<version>" tooldistill ...`, pinned to the library version it was released with. During development, point `--from` at the local checkout.
- Before the demo: `uv build`, then `uvx --from ./dist/<wheel> tooldistill --help` from a clean environment.

---

## How the parts fit together

```
 Developer's agent loop                              tooldistill
 ──────────────────────                              ───────────
 model ──tool_use──▶ trace.execute(name, input) ──▶ agent.py: Trace
                                                       │ routes by name
                         ┌─────────────────────────────┼──────────────────────────┐
                         ▼                             ▼                          ▼
                  base tool                     distilled tool             propose tool
                  executor(name,args)           runtime: run module        proposals.py: ingest
                  record tool_call              in live mode; inner        → verifier.py (3 checks)
                                                call()s → executor,        → lifecycle.py (status)
                                                recorded w/ parent id      → verdict text
                         └─────────────────────────────┼──────────────────────────┘
                                                       ▼
                                          registry.py ──▶ .tooldistill/ (SQLite + tools/*.py)
                                                       ▲
 terminal / cron ──▶ cli.py ──▶ lifecycle.py (tool-updated, health, rebuild, promote)
                                verifier.py  (verify)
```

- **`agent.py`** is the only module the SDK integration touches.
- **`lifecycle.py`** is the only module allowed to change a status.
- **`registry.py`** is the only module that touches SQLite or the `tools/` folder.
- **`runtime.py`** owns `call()` and the live/replay context (use a `contextvars.ContextVar` so `call()` knows its mode, executor, and parent call id).

### `trace.execute(name, input)` step by step
1. **Base tool** (name in `tools`):
   run `executor(name, input)` → time it → write a `tool_calls` row (output or error) → return `ToolResult`. Executor exceptions become `is_error=True` with the exception message; never raise.
2. **Distilled tool** (name in exposed distilled tools):
   validate `input` against its `input_schema` (a validation failure is the model's mistake: return `is_error=True` with the validator's message and do **not** demote) → write a parent `tool_calls` row → run the module in **live mode**; each inner `call()` goes through the executor and writes a child row with `parent_call_id` → run `check(result)` if defined → write a `run_stats` row.
   - Success: return the result as `ToolResult`.
   - A failing inner call (executor raised, or unknown base tool): `call()` records the child row with the error and raises `DistilledToolCallError(step, base_tool, message)`, where `step` is the ordinal of that `call()` in this run. Any other exception in the tool's own code, or `check(result)` returning False, is caught at the same boundary.
   - On any failure → `lifecycle.demote(..., "runtime_failure")` → return the **structured error**. The fallback run that follows is worth distilling (see Terminology).
3. **`propose_distilled_tool`**:
   `proposals.ingest(proposal, source_trace=this trace)` → name/collision/schema validation → write draft version + code file → compute baseline, fixtures, avoided tools from this trace → `verifier.verify()` → `lifecycle` transitions → return verdict text (also written as a `tool_calls` row, excluded from the baseline).
4. **Anything else** → `is_error=True`, `"Unknown tool: <name>"`.

### Structured error format (returned with `is_error=True`)
```
Distilled tool 'create_invoice_from_email' (v1) failed at step 4 of 4.
Base tool: billing_create_invoice
Error: missing required property 'currency'
This distilled tool has been disabled. Complete the task using the base tools directly.
```

- "of 4" is the version's `measured_calls` from verification, not a count taken from the file: distilled tools may loop, so the live count can differ.
- A failure in the tool's own code between calls reads `failed after step K of N` and omits the `Base tool:` line. A failed `check()` reads `failed its check after step N of N`.

---

## Distilled tool file format

A distilled tool is one Python module stored at `.tooldistill/tools/<name>/v<N>.py`:

```python
from tooldistill import distilled_tool, call

@distilled_tool(
    name="create_invoice_from_email",
    description="Create a draft invoice for a customer from their most recent order email.",
    input_schema={
        "type": "object",
        "properties": {
            "customer": {"type": "string"},
            "since": {"type": "string", "default": "1d"},
        },
        "required": ["customer"],
    },
)
def create_invoice_from_email(customer: str, since: str = "1d") -> dict:
    msg = call("gmail_search", query=f"from:{customer} order", since=since)[0]
    order = call("gmail_get_message", id=msg["id"])
    cust = call("billing_find_customer", name=customer)
    return call("billing_create_invoice", customer_id=cust["id"], lines=order["lines"])


def check(result: dict) -> bool:
    """Optional success check, run by the replay check and after live runs."""
    return result.get("status") == "draft"
```

Rules (enforced by the static check):
- Exactly one function decorated with `@distilled_tool`; optional module-level `check(result) -> bool`.
- **Every base tool call goes through `call("<literal tool name>", **kwargs)`.** The tool name must be a string literal so dependencies can be extracted from the AST.
- **`call()` only reaches base tools.** A distilled tool may not call another distilled tool or the propose tool.
- Allowed imports only: `tooldistill`, `typing`, `json`, `re`, `datetime`, `math`. No `os`, `subprocess`, `socket`, `requests`, `open`, `eval`, `exec`, `compile`, `__import__`, `globals`, `getattr` on modules.
- Normal control flow is allowed: `if`, `for`, comprehensions, `return`, `raise`, `lambda`. **Not allowed:** `try`/`except` (a failed base tool call must fail the distilled tool so the fallback can run), `while`, `with`, `class`, `async`, `global`, `nonlocal`.
- No dunder names anywhere (`__class__`, `__subclasses__`, `__globals__`, `__dict__`, ...): any name or attribute starting with `__` fails the static check. This closes the usual allowlist escapes.
- Top level of the module: only the allowed imports, the decorated function, and `check`. Nothing else, so importing the module runs no code.
- **Pass exactly the keyword arguments the source run passed** for each call. Replay matches on exact canonical input, so adding an argument the source run omitted (e.g. a default `since="1d"` when the run never sent `since`) fails replay. The example above assumes the source run sent both `query` and `since`.
- **Measured metadata (status, version, baseline, dependencies, avoided tools) is NOT in the file.** It lives in SQLite and is computed by the library. Anything the proposer claims about these is ignored.

---

## Store schema (SQLite: `.tooldistill/registry.db`)

| Table | Key columns |
|---|---|
| `traces` | id, task, started_at, finished_at, outcome (null / success / failure), input_tokens, output_tokens, nudged |
| `tool_calls` | id, trace_id, seq, parent_call_id (set for base tool calls made inside a distilled tool), kind (base / distilled / propose), tool_name, input_json, output_json, is_error, error, latency_ms |
| `distilled_tools` | name (PK), description, created_at |
| `versions` | tool_name, version, status, status_reason, code_path, source_trace_id, test_inputs_json, baseline_calls, baseline_tokens, baseline_latency_ms, measured_calls, created_at |
| `dependencies` | tool_name, version, base_tool, schema_hash |
| `avoided_tools` | tool_name, version, base_tool, reason |
| `fixtures` | tool_name, version, seq, base_tool, input_json, output_json |
| `check_results` | tool_name, version, check (static/replay/efficiency), passed, details, ran_at |
| `run_stats` | tool_name, version, ran_at, success, calls, latency_ms, error |
| `status_events` | tool_name, version, from_status, to_status, reason, at (audit log) |
| `rebuild_queue` | tool_name, version, reason, base_tool, queued_at, resolved_at |

Use a single `schema_version` pragma and a simple migration function; no ORM.

---

## Verifier: the three checks

Run in order; stop at the first failure. Store every result in `check_results`. Failure messages must name the exact problem (line, tool, property) because the agent reads them and resubmits.

### 1. Static
- Runs on the source text with `ast` **before the module is ever imported**. Nothing else touches the file until this passes, because importing runs top-level code.
- Module parses; exactly one `@distilled_tool`; `input_schema` is valid JSON Schema; name rules pass (including no collision and the name-reuse rule).
- Import, builtin, statement and dunder rules (see file format rules).
- Every `call()` uses a literal tool name that exists in the developer's current base tool list.
- For each `call()` with literal keyword args: keys ⊆ the base tool's schema properties, and all required properties are present.
- Output: the dependency list (base tool + current schema hash), written to `dependencies`.

### 2. Replay
- Run the distilled tool with the proposal's test inputs in **replay mode**.
- Fixtures come from the source trace's successful **top-level** base tool calls. Calls made inside a distilled tool are excluded: after a runtime failure they are the broken path, and a stale inner response (an approval token, a generated id) would otherwise be matched before the agent's own.
- Matching: exact match on (tool name, canonical input JSON). Fixtures are consumed in `seq` order and each is used at most once per replay: a call takes the **first unconsumed** fixture that matches, so a repeated call (e.g. a search before and after a create) gets the right recorded output. An unmatched call fails the check with a message naming the call, its arguments, and the nearest fixture for that tool. (Optional later: a relaxed mode that matches by tool name order.)
- Passes if: no exception, every call matched a fixture, and `check(result)` returns True when `check` is defined.
- **Live verification:** when fixtures cannot cover a call (e.g. a rebuild that uses a recovered tool), replay may use the real executor for unmatched calls **only if** `allow_live_verification = true`. New responses are saved as fixtures for that version. Otherwise the version stays draft with reason `needs_live_verification`. **Live verification has real side effects** (e.g. creates a real invoice); only enable it against mock or sandboxed base tools.

### 3. Efficiency
- `measured_calls` = number of base tool calls made during replay.
- Pass if `measured_calls <= baseline_calls * (1 - min_call_reduction)`.
- A new version of an existing tool takes `baseline_*` from the tool's previous version when that is larger than its own source trace's measurements. A fallback run after a structured error is far cheaper than the original discovery and would otherwise make this check meaningless.
- A rebuild for `avoided_tool_recovered` must also beat the version it replaces: `measured_calls < previous version's measured_calls`. Otherwise the recovered tool bought nothing, and the check fails with both numbers in the message.
- Distilled tools use zero LLM tokens by construction; record this but don't gate on it.
- Not a failure, but recorded as a note: any avoided tool that is currently healthy ("a shorter version may exist").

---

## Lifecycle

| From | To | Trigger |
|---|---|---|
| (none) | draft | Proposal accepted (passes basic validation) |
| draft | verified | All three checks pass |
| draft | draft | A check fails; results recorded. Proposer may resubmit (new version). |
| verified | active | `auto_promote = true`, or `promote` command/function. Activating a version deprecates the previously active version of the same tool. |
| active | suspect | A dependency's schema hash changed (`dependency_changed`), a runtime failure (`runtime_failure`), or an avoided tool recovered (`avoided_tool_recovered`) |
| suspect | active | Re-verification passes (for `dependency_changed`). For `runtime_failure`, replay against old fixtures proves nothing (they still pass): the normal path is a new version distilled from the fallback run, and the only way back for the same version is `verify --live` with `allow_live_verification`. |
| suspect | deprecated | Re-verification fails, or a newer version becomes active |
| deprecated | active | Manual `promote --version N` (rollback); must re-pass verification first |

### Exposure rules (`active_tools()`)
- Exposed: `active`, and `suspect` only when the reason is `avoided_tool_recovered` (it still works; it may just be improvable).
- Hidden: every other status, including `suspect` for `dependency_changed` / `runtime_failure`.
- At most `max_exposed_tools` are returned (ordered by successful run count, then most recent). Every exposed tool costs context tokens on every model call.

Every transition writes a `status_events` row. All transitions go through `lifecycle.py`.

---

## Reacting to base tool changes

### `lifecycle.on_tool_updated(base_tool: str)`
1. Recompute the base tool's schema hash from the current tool list.
2. For each active version that **depends on** it with a different stored hash → `suspect(dependency_changed)` → re-run verification → `active` or `deprecated`.
3. For each active version that **avoided** it → `suspect(avoided_tool_recovered)` → add to the rebuild queue.

### `lifecycle.health()`
- **Schema changes:** compare the current tool list's schema hashes to stored dependency hashes. No probing needed. Calls `on_tool_updated` for each change.
- **At startup:** `ToolDistill(...)` runs this schema comparison when `check_schemas_on_start = true` (default), so a changed base tool is caught before the process's first run instead of at its first failed call. It only sees the tool list the developer passes: if the API changed but the tool definition didn't, the change surfaces as a runtime failure. The outage half never runs automatically.
- **Recoveries/outages:** only if the developer configures a `health_probe(tool_name) -> bool`. The library never calls the executor on its own for health (base tools may have side effects). A recovered avoided tool triggers `on_tool_updated`.

### Rebuild
`rebuild(tool_name)` takes a queued item, gives the ClaudeDistiller the source trace plus a note ("base tool X is available again; its schema is ..."), and ingests the resulting proposal as a new version. That version is verified normally (it will usually need live verification for the recovered tool's calls).

---

## SDK integration (primary target)

The developer adds a handful of lines to their existing agent loop:

```python
import anthropic
from tooldistill import ToolDistill

client = anthropic.Anthropic()
td = ToolDistill(store=".tooldistill", executor=execute_tool, tools=TOOLS)
# or: td = ToolDistill.from_config()  (reads tooldistill.toml)

def run_agent(task: str, max_steps: int = 30):
    trace = td.start_trace(task)
    messages = [{"role": "user", "content": task}]
    tools = TOOLS + td.active_tools() + [td.propose_tool()]
    for _ in range(max_steps):
        resp = client.messages.create(model="claude-sonnet-5-5", max_tokens=4096,
                                      tools=tools, messages=messages)
        trace.record_usage(resp.usage)                 # optional: enables token baselines
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            if trace.outcome is None:
                trace.set_outcome(success=task_succeeded(task, resp))  # developer's own check
            nudge = trace.distill_nudge()              # None unless outcome is success and worth distilling
            if nudge:
                messages.append({"role": "user", "content": nudge})
                continue                               # agent gets one turn to call propose tool
            trace.finish()
            return resp
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                out = trace.execute(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": out.content, "is_error": out.is_error})
        messages.append({"role": "user", "content": results})
    if trace.outcome is None:                              # step limit hit before the task was judged
        trace.set_outcome(success=False)
    trace.finish()
```

`task_succeeded` is the developer's responsibility. In the demo it checks the fake billing system for the expected invoice. If a developer has no way to check, they may pass `success=True`, but the docs must warn that failed runs could then be distilled (verification will still catch many of them).

### Public API

| Member | Behavior |
|---|---|
| `ToolDistill(store, executor, tools, distiller=None, health_probe=None, **settings)` | Opens/creates the store. `tools` is a list of Anthropic-format tool dicts (`name`, `description`, `input_schema`). Validates tool names at startup and, unless `check_schemas_on_start = false`, compares schema hashes against stored dependencies (see `lifecycle.health()`). |
| `ToolDistill.from_config(path="tooldistill.toml")` | Same, from config. |
| `td.start_trace(task) -> Trace` | Begin recording a run. |
| `td.active_tools() -> list[dict]` | Exposed distilled tools as Anthropic tool dicts. Re-reads the store each call so CLI changes take effect. |
| `td.propose_tool() -> dict` | The `propose_distilled_tool` tool definition. |
| `trace.execute(name, input) -> ToolResult` | See "step by step" above. Never raises for tool errors; returns `ToolResult(content: str, is_error: bool)`. |
| `trace.record_usage(usage)` | Add token counts to the trace. |
| `trace.set_outcome(success: bool)` | Record whether the task was actually accomplished. |
| `trace.outcome -> bool \| None` | Current outcome. |
| `trace.distill_nudge() -> str \| None` | Returns a message asking the agent to propose a distilled tool, **at most once per trace**, only if the trace is worth distilling. Otherwise None. |
| `trace.finish()` | Close the trace. If it is worth distilling, the agent didn't propose, and `fallback_distiller` is on, run the fallback Distiller. |
| `td.distill(trace_id)` | Explicitly run the fallback Distiller on a stored trace. |

### Propose tool input schema

```json
{
  "name": "propose_distilled_tool",
  "description": "Save the sequence of tool calls that solved this task, without its dead ends, as a reusable distilled tool. It will be verified before it can be used.",
  "input_schema": {
    "type": "object",
    "properties": {
      "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]{2,63}$"},
      "description": {"type": "string"},
      "input_schema": {"type": "object"},
      "code": {"type": "string", "description": "Python module following the distilled tool file format"},
      "test_inputs": {"type": "object", "description": "Arguments that reproduce this run"},
      "trace_id": {"type": "string", "description": "Only needed in the Claude Code plugin; the SDK fills it in automatically"}
    },
    "required": ["name", "description", "input_schema", "code", "test_inputs"]
  }
}
```

The library links the proposal to its source trace automatically (SDK: the trace that executed the call; plugin: `trace_id`). Its tool result gives the agent the verdict, e.g. `"verified: 4 base tool calls vs baseline 31"` or `"rejected (static): call to 'billing_find_customr' — no such base tool"`, so the agent can fix and resubmit in the same run. The nudge text and propose tool description live in `distill/prompts.py` and must include the file format rules. After a fallback, the nudge names the failed distilled tool and asks for a replacement under the same name.

---

## Config: `tooldistill.toml`

```toml
store = ".tooldistill"
executor = "myapp.tools:execute_tool"      # imported by the CLI
tools = "myapp.tools:TOOLS"
health_probe = "myapp.tools:probe"         # optional
distiller_model = "claude-sonnet-5-5"
fallback_distiller = true
auto_promote = false
min_calls_to_distill = 8
min_call_reduction = 0.3
max_exposed_tools = 20
allow_live_verification = false
check_schemas_on_start = true
```

### Defaults and their consequences (all tunable)
| Setting | Default | Consequence |
|---|---|---|
| `fallback_distiller` | `true` | An extra Claude API call on every worth-distilling run where the agent didn't propose. |
| `auto_promote` | `false` | Verified tools wait for `promote`. Set `true` for a smoother demo. |
| `min_calls_to_distill` | `8` | Arbitrary starting point; tune on the demo. |
| `min_call_reduction` | `0.3` | Distilled tool must use ≥ 30% fewer base tool calls than the baseline. Arbitrary; tune. |
| `max_exposed_tools` | `20` | Caps context cost of distilled tool definitions. |
| `allow_live_verification` | `false` | Off because live verification has real side effects. Turn on for the demo's fake tools. |
| `check_schemas_on_start` | `true` | A schema comparison and possibly a re-verification (replay only, no side effects) on every `ToolDistill(...)`. Cheap; turn off only if startup time matters. |

---

## CLI

`tooldistill` (typer). Each command maps to one library function.

| Command | Needs executor? |
|---|---|
| `init` (create store, `.gitignore`, sample config) | no |
| `list [--status S]` | no |
| `show <name> [--version N]` (code, status history, check results, stats) | no |
| `traces list` / `traces show <id>` | no |
| `promote <name> [--version N]` | no (rollback re-verifies → yes) |
| `deprecate <name> [--version N]` | no |
| `verify <name> [--version N] [--live]` | yes |
| `tool-updated <base_tool>` | yes (for re-verification) |
| `health` | yes |
| `rebuild <name>` | yes |

Commands that need the executor import it from `tooldistill.toml`. Output should be readable tables (plain text is fine).

---

## Demo: `examples/invoice_agent/`

- Fake base tools as plain Python functions (no real services): `gmail_search`, `gmail_get_message`, `gmail_get_attachment`, `billing_search_customers`, `billing_find_customer`, `billing_create_invoice`, `billing_import_from_email`, plus a few distractors.
- An outage switch (env var or a JSON file) that makes `billing_import_from_email` raise a 503, and a schema switch that adds a required `currency` field to **both** `billing_create_invoice` and `billing_import_from_email` (one billing API, one change). Step 3's rebuilt version uses `billing_import_from_email`, so a switch on `billing_create_invoice` alone would never break it in step 4.
- `task_succeeded()` checks the fake billing state for the expected invoice.
- `run.py` runs the agent on a task and prints tool call count, tokens, and time.
- `tooldistill.toml` in the example folder sets `auto_promote = true`, `allow_live_verification = true` (the base tools are fake, so live verification is safe) and `min_calls_to_distill = 4` (fallback and re-verification runs are short).

**Demo script:**
1. Outage on. Run task → ~20–30 base tool calls → agent proposes → verified → promoted.
2. New run, different customer → 1 distilled tool call (4 base tool calls).
3. Outage off. `tooldistill tool-updated billing_import_from_email` → suspect → `tooldistill rebuild` → v2 with fewer calls → active, v1 deprecated.
4. Schema switch on, with the tool list handed to `ToolDistill` left stale (an API that changed before its tool definition did). Next run → distilled tool fails → structured error → agent solves with base tools → suspect (`runtime_failure`) → the run is worth distilling → agent proposes v3 under the same name → verified against this run's fixtures → active, v2 deprecated. (If the tool list is updated as well, the startup check catches it first: suspect (`dependency_changed`) → static check fails on the missing `currency` → deprecated → the next run proposes the new version.)

---

## Build order (with acceptance criteria)

1. **Models + registry + schema hash + serialization helpers.** Can create/read traces, tool calls, versions; WAL and migrations work. Tests pass.
2. **Runtime.** `call()` works in live mode (via executor) and replay mode (via fixtures); running a distilled tool from a file works; inner calls are recorded with parent ids.
3. **Verifier.** Static, replay, efficiency checks with precise failure messages. Test with hand-written good and bad distilled tools.
4. **Lifecycle.** All transitions in the table, with `status_events`. Exposure rules for `active_tools()`.
5. **SDK facade.** `ToolDistill`, `Trace`, routing in `trace.execute`, propose tool ingestion, outcome handling, `distill_nudge`. Build the demo's fake base tools first (they depend only on the executor contract), then an end-to-end test with a scripted fake model that drives demo steps 1, 2 and 4 against them.
6. **Demo agent** steps 1–2 working with a real model.
7. **Tool changes.** `on_tool_updated`, `health`, the startup check, rebuild queue, `ClaudeDistiller` rebuild. Extend the end-to-end test to demo step 3 and the `dependency_changed` path of step 4, then run steps 3–4 with the real model.
8. **CLI.**
9. **Stretch: Claude Code plugin** (see below).
10. **Stretch: MCP executor adapter** — an executor that wraps an MCP client session so developers using MCP servers can plug in directly.

Don't start a step until the previous step's tests pass.

---

## Stretch: Claude Code plugin

The same library behind a different adapter. Because Claude Code's agent loop can't be edited, the plugin maps each SDK touchpoint to a Claude Code mechanism:

| SDK touchpoint | Plugin equivalent |
|---|---|
| `td.start_trace()` | **SessionStart hook** creates a trace keyed by the Claude Code session id, and injects into context: the trace id plus a short list of active distilled tools. |
| `trace.execute()` recording | **PostToolUse hook** records each tool call into that trace (it receives `session_id`, `tool_name`, `tool_input`, `tool_response` on stdin). |
| `td.active_tools()` + `td.propose_tool()` | **MCP server** (`tooldistill serve`) exposes exposed distilled tools and `propose_distilled_tool`; sends `tools/list_changed` when statuses change. |
| `distill_nudge()` | **`/distill` command** asks Claude to propose a distilled tool for the work just done. The user confirming the task went well serves as the outcome. |
| Executor | The MCP server must open its own MCP client connections to the servers in Claude Code's MCP config. **Hardest part; out of scope until the SDK path is done.** |

Plugin-specific issues to handle:
- **CLI additions:** `serve` (the MCP server) and `hook <event>` (reads the hook's JSON from stdin). Both are thin wrappers over `integrations/claude_code/`, like every other command.
- **Trace correlation:** the MCP server doesn't know which Claude Code session is calling it. Claude passes the `trace_id` it was given at SessionStart as an argument to `propose_distilled_tool`.
- **Name mapping:** Claude Code reports MCP tools as `mcp__<server>__<tool>`. Translate to base tool names when recording, and back when executing.
- **Built-in tools** (Bash, Edit, Read...) are recorded but cannot appear in a distilled tool.
- **No token data:** hooks don't report usage, so plugin baselines are tool calls and latency only.

---

## Known limitations (state these honestly in docs and the pitch)

- **Replay proves reproduction, not generalization.** It shows the code reproduces the source run with the test inputs; it doesn't prove the tool works for other inputs. Runtime failures plus fallback are the safety net.
- **`check()` is written by the proposer** and could be trivial (`return True`). It adds confidence only when meaningful.
- **Distilled tool code is LLM-written and runs in-process.** The AST allowlist is the only guard. A subprocess sandbox is future work.
- **Avoided-tool detection is a heuristic** (every call failed). An agent that misused a working tool every time will produce a false "avoided" entry.
- **Outcome quality depends on the developer's success check.**

## Non-goals (for the hackathon)

- An equivalence/LLM-judge check (deliberately dropped: replay + `check()` covers correctness).
- Recording or replaying non-tool actions (file edits, shell commands).
- A web UI or long-running service. All state is in the store folder.
- Multi-user / concurrent writers beyond SQLite WAL.
