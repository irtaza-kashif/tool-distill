---
description: Save the task just completed as a verified, reusable distilled tool
argument-hint: [what the task was]
disable-model-invocation: true
---

The user confirms that the task just completed in this session went well. Turn it into a distilled tool:

1. Call `mcp__plugin_tooldistill_tooldistill__propose_distilled_tool` once, with `trace_id` set to this session's trace id from the session context and `task` set to "$ARGUMENTS" (or a one-line description of the task if no arguments were given).
2. Give the tool a short snake_case name, a one-sentence description, an input_schema for the inputs that vary between similar tasks, the Python module, and test_inputs: the arguments that reproduce exactly this session's run.
3. Inside the module, `call()` may only use the base tools this session called through MCP, named `<server>__<tool>` (the MCP tool `mcp__invoice__gmail_search` is `invoice__gmail_search`). Built-in tools such as Bash, Read or Edit can never be part of a distilled tool.
4. If the verdict is a rejection, fix the module and call the tool again, at most twice.

Name the tool after the kind of task it does, never after this run's particular inputs: a run that invoiced ACME yields `create_invoice_from_order_email` with a `customer` parameter, not `invoice_acme`. Parameterize what varies between similar tasks (the customer, the date range, ...) and hard-code nothing that was specific to this run.

A distilled tool is one Python module. Example:

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

Rules (a static check enforces them before anything runs; a broken rule is reported with its line):
- The module contains only: `from tooldistill import distilled_tool, call`, optional imports of typing, json, re, datetime or math, exactly one function decorated with @distilled_tool, and an optional module-level `check(result) -> bool`. No module docstring and no other statements.
- The decorator's name, description and input_schema are literals. The name matches ^[a-z][a-z0-9_]{2,63}$ and must not be a base tool's name. input_schema is a JSON Schema of type "object": every property is a parameter of the function, and every parameter without a default is listed in "required".
- Every base tool call goes through call("<tool name>", key=value, ...): the tool name is a string literal naming an existing base tool, arguments are explicit keywords (no *args or **kwargs), and all of the tool's required properties are passed. call() reaches base tools only, never another distilled tool.
- Pass exactly the keyword arguments the source run passed for each call. Verification replays the module against the tool responses recorded in this run and matches each call on its exact arguments, so an argument the run never sent (for example a default `since="1d"` when the run omitted `since`) makes replay fail.
- Allowed: if/for, comprehensions, return, raise, lambda, nested helper functions. Not allowed: try/except (a failed base tool call must fail the tool so the agent can fall back), while, with, class, async, global, nonlocal, yield, imports inside functions, any name starting with "__", and open, eval, exec, compile, getattr, globals, locals, os, sys, subprocess, socket, requests.
- The tool returns a JSON-serializable value. The optional check(result) must not call base tools.
- Do not put status, version, baseline or dependency information in the file: the library measures those itself and ignores any claims.
