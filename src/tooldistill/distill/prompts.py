"""Text the agent reads: the file format rules, the nudge, and the propose tool definition."""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from tooldistill.core.tools import PROPOSE_TOOL_NAME

__all__ = [
    "FILE_FORMAT_RULES",
    "INPUT_GUIDANCE",
    "NAMING_GUIDANCE",
    "PROPOSAL_GUIDANCE",
    "SCOPE_GUIDANCE",
    "USE_EXISTING_GUIDANCE",
    "PLUGIN_PROPOSE_DESCRIPTION",
    "PROPOSE_TOOL_DESCRIPTION",
    "distill_command_body",
    "nudge",
    "plugin_session_context",
    "propose_tool",
]

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

NAMING_GUIDANCE = (
    "Name the tool after the kind of task it does, never after this run's particular inputs: a run that invoiced "
    "ACME yields `create_invoice_from_order_email` with a `customer` parameter, not `invoice_acme`. Parameterize what "
    "varies between similar tasks (the customer, the date range, ...) and hard-code nothing that was specific to "
    "this run."
)

INPUT_GUIDANCE = (
    "Inputs are only what the request itself states (a customer, a sender's name, a date range). Anything the run "
    "discovered through tools (a message id, an address, a price, a record found by searching) is derived inside "
    "the tool from the base tool outputs, never passed in. A tool whose inputs are the answer cannot be called "
    "before the work is done, so it never gets used."
)

SCOPE_GUIDANCE = (
    "The tool does everything the task asked, including checks whose results do not change what gets created "
    "(a conflict check, a lookup the user wanted), and returns what the user needs to know (what was created, what "
    "the checks found). The saving comes from leaving out the dead ends, failed calls and repeated reads of the "
    "discovery run, never from dropping steps: a tool that does less than the task is not a shortcut and will not be "
    "used. A distilled tool is worth having when it turns a long search into a few calls; it does not have to be "
    "the shortest possible path."
)

PROPOSAL_GUIDANCE = NAMING_GUIDANCE + "\n\n" + INPUT_GUIDANCE + "\n\n" + SCOPE_GUIDANCE

USE_EXISTING_GUIDANCE = (
    "Distilled tools already in your tool list are the preferred way to do the tasks they describe: use one before "
    "working through a task with base tools. Fall back to base tools only when the distilled tool would do too "
    "little or too much, does something else, or costs more than the task warrants."
)

PROPOSE_TOOL_DESCRIPTION = (
    "Save the sequence of tool calls that solved this task, without its dead ends, as a reusable distilled tool. "
    "It will be verified before it can be used. Call it once the task is done: the module is checked statically, "
    "replayed against this run's recorded tool responses with your test_inputs, and must use clearly fewer base "
    "tool calls than this run did, which comes from leaving out dead ends and failed calls, never from dropping "
    "steps the task needs. The result tells you the verdict; fix the module and call again if it is rejected. "
    "Do not propose a tool that duplicates a distilled tool you already have.\n\n"
    + USE_EXISTING_GUIDANCE
    + "\n\n"
    + PROPOSAL_GUIDANCE
    + "\n\n"
    + FILE_FORMAT_RULES
)


def propose_tool() -> dict[str, Any]:
    """The ``propose_distilled_tool`` definition, as a fresh Anthropic tool dict."""
    from tooldistill.core.proposals import PROPOSE_TOOL_INPUT_SCHEMA  # kept lazy: hooks import this module

    return {
        "name": PROPOSE_TOOL_NAME,
        "description": PROPOSE_TOOL_DESCRIPTION,
        "input_schema": deepcopy(PROPOSE_TOOL_INPUT_SCHEMA),
    }


# --------------------------------------------------------------------- Claude Code plugin

PLUGIN_PROPOSE_DESCRIPTION = (
    "Save the sequence of MCP tool calls that solved the task just completed, without its dead ends, as a "
    "verified, reusable distilled tool. Use it after the /tooldistill:distill command, which gives the full rules. "
    "Pass the session's trace_id from the session context; the proposal is replayed against that session's "
    "recorded tool calls and must use clearly fewer calls, by leaving out dead ends rather than steps the task "
    "needs. The result is the verdict; fix the module and call again if it is rejected."
)

SESSION_CONTEXT_LIMIT = 10_000


def plugin_session_context(trace_id: str, tools: Sequence[Mapping[str, Any]], *, prefix: str) -> str:
    """What the SessionStart hook adds to Claude's context (facts, not instructions; under 10,000 characters)."""
    lines = [
        f"tooldistill is active for this project. The trace id of this session is {trace_id}.",
        f"The tooldistill MCP server offers tools named {prefix}<tool>. Each of them takes a trace_id "
        f"parameter; the value for this session is {trace_id}. Its propose_distilled_tool saves a completed "
        "multi-step task as a verified distilled tool; the /tooldistill:distill command explains how.",
    ]
    if tools:
        lines.append(
            "Distilled tools available now, each completing a whole task in one call. They are the preferred way "
            "to do the tasks they describe; base tools are the fallback when one would do too little, too much, "
            "or something else:"
        )
        for tool in tools:
            lines.append(f"- {prefix}{tool['name']}: {str(tool.get('description', '')).strip()}")
    else:
        lines.append("No distilled tools exist yet.")
    text = "\n".join(lines)
    if len(text) > SESSION_CONTEXT_LIMIT:
        text = text[: SESSION_CONTEXT_LIMIT - 20] + "\n(list truncated)"
    return text


def distill_command_body(*, prefix: str) -> str:
    """The body of the plugin's /tooldistill:distill command (Markdown)."""
    return "\n".join(
        [
            "The user confirms that the task just completed in this session went well. Turn it into a distilled tool:",
            "",
            f"1. Call `{prefix}{PROPOSE_TOOL_NAME}` once, with `trace_id` set to this session's trace id from the session "
            'context and `task` set to "$ARGUMENTS" (or a one-line description of the task if no arguments were given).',
            "2. Give the tool a short snake_case name, a one-sentence description, an input_schema for the inputs that vary "
            "between similar tasks, the Python module, and test_inputs: the arguments that reproduce exactly this session's run.",
            "3. Inside the module, `call()` may only use the base tools this session called through MCP, named "
            "`<server>__<tool>` (the MCP tool `mcp__invoice__gmail_search` is `invoice__gmail_search`). Built-in tools "
            "such as Bash, Read or Edit can never be part of a distilled tool.",
            "4. If the verdict is a rejection, fix the module and call the tool again, at most twice.",
            "",
            PROPOSAL_GUIDANCE,
            "",
            FILE_FORMAT_RULES,
        ]
    )


def nudge(
    task: str,
    *,
    base_calls: int,
    tool_names: Sequence[str],
    failed_tool: str | None = None,
    distilled_tools: Sequence[str] = (),
) -> str:
    """The message that asks the agent to propose a distilled tool for the run it just completed.

    ``distilled_tools`` names the distilled tools that are already exposed, so the agent is told not to duplicate one.
    """
    if failed_tool:
        opening = (
            f"The distilled tool '{failed_tool}' failed in this run and has been disabled, and you then completed "
            f"the task with base tools. Call `{PROPOSE_TOOL_NAME}` once, now, to propose a replacement under the "
            f"same name, '{failed_tool}', that reflects what worked this time."
        )
    else:
        opening = (
            f"This run succeeded and took {base_calls} base tool calls. Call `{PROPOSE_TOOL_NAME}` once, now, to "
            "save the sequence of base tool calls that solved the task, without its dead ends, as a reusable "
            "distilled tool."
        )
    lines = [
        opening,
        "",
        f"Task: {task}",
        "",
        "Give it a short snake_case name, a one-sentence description, an input_schema for the inputs that vary "
        "between similar tasks (for example the customer), the Python module, and test_inputs: the arguments "
        "that reproduce exactly this run. Verification replays the module against this run's recorded tool "
        "responses, so use exactly the arguments this run used, keep every step the task needed, and leave out "
        "the dead ends and failed calls.",
        "",
        PROPOSAL_GUIDANCE,
        "",
        FILE_FORMAT_RULES,
        "",
        f"Base tools available: {', '.join(tool_names)}.",
    ]
    if distilled_tools and not failed_tool:
        lines += [
            "",
            f"Distilled tools that already exist: {', '.join(distilled_tools)}. If one of them does this task, do "
            "not propose a duplicate: reply with one line naming it and saying why you did not use it.",
        ]
    return "\n".join(lines)
