"""The built-in Distiller: asks Claude to turn a trace into a proposal, using structured outputs.

This is the only module in the library that talks to the Anthropic API. The model's answer is a proposal
like any other: nothing it claims is trusted, the verifier measures everything.
"""

import json
import logging
from typing import Any

from tooldistill.core.models import CallKind, Proposal, TraceData
from tooldistill.core.tools import ToolSet
from tooldistill.distill.prompts import FILE_FORMAT_RULES, NAMING_GUIDANCE

__all__ = ["PROPOSAL_OUTPUT_SCHEMA", "SYSTEM_PROMPT", "ClaudeDistiller", "render_prompt"]

log = logging.getLogger("tooldistill.distill")

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_TOKENS = 16000
DEFAULT_OUTPUT_CHARS = 800
MAX_CALLS_RENDERED = 80

# Nested free-form objects are carried as JSON text so the output schema stays simple and strict.
PROPOSAL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "snake_case tool name matching ^[a-z][a-z0-9_]{2,63}$"},
        "description": {"type": "string", "description": "one sentence"},
        "input_schema_json": {"type": "string", "description": "JSON text of the input_schema object (type: object)"},
        "code": {"type": "string", "description": "the complete Python module"},
        "test_inputs_json": {"type": "string", "description": "JSON text of the arguments that reproduce the source run"},
    },
    "required": ["name", "description", "input_schema_json", "code", "test_inputs_json"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You turn one successful agent run into a distilled tool: a small Python module that completes the same kind "
    "of task by calling the base tools in a fixed, known-good sequence. You will be shown the task, the base tools "
    "and every tool call the run made, in order, with its recorded response. Keep only the calls that were needed, "
    "in the order they were needed; skip dead ends and failed calls. Pass exactly the keyword arguments the run "
    "passed, because verification replays your module against the recorded responses and matches each call on its "
    "exact arguments. test_inputs must make the module reproduce this very run.\n\n"
    + NAMING_GUIDANCE
    + "\n\n"
    + FILE_FORMAT_RULES
)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def render_prompt(trace: TraceData, tools: ToolSet, *, note: str | None, name: str | None, output_chars: int = DEFAULT_OUTPUT_CHARS) -> str:
    """The user message: task, base tools with schemas, the recorded calls, the optional note and name."""
    lines = [f"Task: {trace.trace.task}", "Outcome: success", "", "Base tools available (name: input_schema):"]
    for tool in tools:
        lines.append(f"- {tool.name}: {json.dumps(tool.input_schema, separators=(',', ':'))}")
    calls = trace.calls
    shown = calls[:MAX_CALLS_RENDERED]
    lines += ["", f"Recorded tool calls, in order ({len(calls)} calls; responses truncated to {output_chars} characters):"]
    parents = {c.id: c.tool_name for c in calls if c.kind is CallKind.DISTILLED}
    for call in shown:
        prefix = f"{call.seq}. "
        if call.parent_call_id is not None:
            prefix += f"[inside {parents.get(call.parent_call_id, 'a distilled tool')}] "
        if call.kind is CallKind.PROPOSE:
            lines.append(f"{prefix}{call.tool_name}(...) -> {_truncate(call.output_json or '', output_chars)}")
            continue
        head = f"{prefix}{call.tool_name}({_truncate(call.input_json, output_chars)}) -> "
        if call.is_error:
            lines.append(head + f"ERROR: {call.error}")
        else:
            lines.append(head + _truncate(call.output_json or "null", output_chars))
    if len(calls) > len(shown):
        lines.append(f"... {len(calls) - len(shown)} more calls omitted")
    lines.append("")
    if note:
        lines += [note, ""]
    lines.append("Requirements:")
    if name:
        lines.append(f"- The tool must be named '{name}' (the decorator's name and the JSON name field).")
    lines += [
        "- Include only the calls that were needed, with exactly the arguments the run used; skip dead ends and failed calls.",
        "- test_inputs must reproduce exactly this run.",
        "- Return JSON with the fields name, description, input_schema_json, code and test_inputs_json "
        "(input_schema_json and test_inputs_json are JSON objects encoded as strings).",
    ]
    return "\n".join(lines)


class ClaudeDistiller:
    """Produce a proposal from a trace with Claude. The client is created on first use unless one is given."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        client: Any = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        output_chars: int = DEFAULT_OUTPUT_CHARS,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.output_chars = output_chars
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic  # resolved here so the library never needs the SDK unless this distiller runs

            self._client = anthropic.Anthropic()
        return self._client

    def distill(self, trace: TraceData, tools: ToolSet, *, note: str | None = None, name: str | None = None) -> Proposal | None:
        prompt = render_prompt(trace, tools, note=note, name=name, output_chars=self.output_chars)
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": PROPOSAL_OUTPUT_SCHEMA}},
        )
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            log.warning("distiller: the model declined to produce a proposal for trace %s", trace.trace.id)
            return None
        if stop_reason == "max_tokens":
            log.warning("distiller: the proposal for trace %s was cut off at max_tokens=%s", trace.trace.id, self.max_tokens)
            return None
        text = next((block.text for block in response.content if getattr(block, "type", None) == "text"), None)
        if text is None:
            log.warning("distiller: no text block in the response for trace %s", trace.trace.id)
            return None
        try:
            data = json.loads(text)
            input_schema = json.loads(data["input_schema_json"])
            test_inputs = json.loads(data["test_inputs_json"])
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("distiller: unusable proposal JSON for trace %s: %s", trace.trace.id, exc)
            return None
        if not isinstance(input_schema, dict) or not isinstance(test_inputs, dict) or not isinstance(data, dict):
            log.warning("distiller: input_schema and test_inputs must be JSON objects (trace %s)", trace.trace.id)
            return None
        return Proposal(
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            input_schema=input_schema,
            code=str(data.get("code", "")),
            test_inputs=test_inputs,
            trace_id=trace.trace.id,
        )
