"""Text the agent reads: the file format rules, the nudge, and the propose tool definition."""

from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from tooldistill.core.proposals import PROPOSE_TOOL_INPUT_SCHEMA
from tooldistill.core.tools import PROPOSE_TOOL_NAME

__all__ = ["FILE_FORMAT_RULES", "PROPOSE_TOOL_DESCRIPTION", "nudge", "propose_tool"]

FILE_FORMAT_RULES = '''A distilled tool is one Python module. Example:

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
- Do not put status, version, baseline or dependency information in the file: the library measures those itself and ignores any claims.'''

PROPOSE_TOOL_DESCRIPTION = (
    "Save the minimal sequence of tool calls that solved this task as a reusable tool. It will be verified "
    "before it can be used: the module is checked statically, replayed against this run's recorded tool "
    "responses with your test_inputs, and must use clearly fewer base tool calls than this run did. "
    "The result tells you the verdict; fix the module and call again if it is rejected.\n\n" + FILE_FORMAT_RULES
)


def propose_tool() -> dict[str, Any]:
    """The ``propose_distilled_tool`` definition, as a fresh Anthropic tool dict."""
    return {
        "name": PROPOSE_TOOL_NAME,
        "description": PROPOSE_TOOL_DESCRIPTION,
        "input_schema": deepcopy(PROPOSE_TOOL_INPUT_SCHEMA),
    }


def nudge(task: str, *, base_calls: int, tool_names: Sequence[str], failed_tool: str | None = None) -> str:
    """The message that asks the agent to propose a distilled tool for the run it just completed."""
    if failed_tool:
        opening = (
            f"The distilled tool '{failed_tool}' failed in this run and has been disabled, and you then completed "
            f"the task with base tools. Call `{PROPOSE_TOOL_NAME}` once, now, to propose a replacement under the "
            f"same name, '{failed_tool}', that reflects what worked this time."
        )
    else:
        opening = (
            f"This run succeeded and took {base_calls} base tool calls. Call `{PROPOSE_TOOL_NAME}` once, now, to "
            "save the minimal sequence of base tool calls that solved the task as a reusable distilled tool."
        )
    return "\n".join(
        [
            opening,
            "",
            f"Task: {task}",
            "",
            "Give it a short snake_case name, a one-sentence description, an input_schema for the inputs that vary "
            "between similar tasks (for example the customer), the Python module, and test_inputs: the arguments "
            "that reproduce exactly this run. Verification replays the module against this run's recorded tool "
            "responses, so include only the calls that were needed, with exactly the arguments this run used, and "
            "skip dead ends and failed calls.",
            "",
            FILE_FORMAT_RULES,
            "",
            f"Base tools available: {', '.join(tool_names)}.",
        ]
    )
