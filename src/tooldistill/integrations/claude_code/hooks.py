"""Hook handlers for the Claude Code plugin.

SessionStart creates the session's trace (its id is the Claude Code session id) and tells Claude about it;
PostToolUse and PostToolUseFailure record every tool call Claude makes as a base call; SessionEnd closes the
trace. Hooks run on every tool call, so this module imports only the registry, the config and the prompts:
no verifier, no MCP SDK. A hook never breaks the user's session: internal errors exit 1 with a note on
stderr, and a project without ``tooldistill.toml`` gets a silent exit 0.
"""

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tooldistill.core.config import CONFIG_FILENAME, Config, load_config
from tooldistill.core.models import EXPOSED_SUSPECT_REASONS, CallKind, TraceRecord
from tooldistill.core.registry import Registry
from tooldistill.core.serialization import canonical_json
from tooldistill.core.tools import PROPOSE_TOOL_NAME
from tooldistill.distill.prompts import plugin_session_context
from tooldistill.integrations.claude_code.names import is_builtin, is_own_tool, own_prefix, own_tool_name, to_base_tool_name
from tooldistill.integrations.mcp import error_text, result_value

__all__ = [
    "BUILTIN_LIMIT",
    "EVENTS",
    "HookResult",
    "ToolResponse",
    "handle_hook",
    "normalize_event",
    "normalize_tool_response",
    "resolve_config",
]

EVENTS: dict[str, str] = {
    "session-start": "SessionStart",
    "post-tool-use": "PostToolUse",
    "post-tool-use-failure": "PostToolUseFailure",
    "session-end": "SessionEnd",
}
BUILTIN_LIMIT = 2000  # built-in tool inputs/outputs never replay; keep the trace readable
ERROR_LIMIT = 4000


@dataclass(slots=True)
class HookResult:
    stdout: str = ""
    exit_code: int = 0
    stderr: str = ""


@dataclass(slots=True)
class ToolResponse:
    value: Any
    is_error: bool = False
    error: str | None = None


def normalize_event(event: str) -> str:
    """``SessionStart`` / ``session-start`` / ``session_start`` -> ``session-start``."""
    key = event.strip().replace("_", "-")
    if key in EVENTS:
        return key
    for slug, camel in EVENTS.items():
        if key.lower() == camel.lower() or key.lower() == slug:
            return slug
    raise ValueError(f"unknown hook event '{event}' (one of: {', '.join(EVENTS)})")


def resolve_config(explicit: str | Path | None, payload: Mapping[str, Any] | None = None) -> Path | None:
    """``--config``, else $TOOLDISTILL_CONFIG, else tooldistill.toml in $CLAUDE_PROJECT_DIR, the payload's cwd, or the cwd."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("TOOLDISTILL_CONFIG")
    if env:
        return Path(env)
    payload = payload or {}
    for base in (os.environ.get("CLAUDE_PROJECT_DIR"), payload.get("cwd"), os.getcwd()):
        if isinstance(base, str) and base:
            candidate = Path(base) / CONFIG_FILENAME
            if candidate.is_file():
                return candidate
    return None


def normalize_tool_response(value: Any) -> ToolResponse:
    """Turn whatever Claude Code put in ``tool_response`` into the value the executor would have returned.

    MCP results arrive as a content-block list or a ``{"content": [...], "structuredContent": ..., "isError": ...}``
    object (possibly JSON-encoded as a string); they go through the adapter's ``result_value`` so recorded
    fixtures match live executor results. Anything else is kept as is.
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return ToolResponse(value)
        if isinstance(parsed, (dict, list)):
            return normalize_tool_response(parsed)
        return ToolResponse(parsed)
    if isinstance(value, Mapping) and isinstance(value.get("content"), list) and (
        "isError" in value or "is_error" in value or "structuredContent" in value or "structured_content" in value
        or all(isinstance(b, Mapping) and "type" in b for b in value["content"])
    ):
        content = value["content"]
        structured = value.get("structuredContent", value.get("structured_content"))
        is_error = bool(value.get("isError", value.get("is_error", False)))
        if is_error:
            return ToolResponse(None, True, error_text(content))
        return ToolResponse(result_value(content, structured))
    if isinstance(value, list) and value and all(isinstance(b, Mapping) and "type" in b for b in value):
        return ToolResponse(result_value(value))
    return ToolResponse(value)


def handle_hook(event: str, payload: Mapping[str, Any], *, config: str | Path | None = None) -> HookResult:
    """Run one hook event; never raises."""
    dump = os.environ.get("TOOLDISTILL_HOOK_DUMP")
    if dump:
        try:
            with open(dump, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": event, "payload": payload}, default=str) + "\n")
        except OSError:
            pass
    try:
        key = normalize_event(event)
    except ValueError as exc:
        return HookResult(exit_code=1, stderr=f"tooldistill hook: {exc}")
    try:
        path = resolve_config(config, payload)
        if path is None:
            return HookResult()  # this project does not use tooldistill
        if not path.is_file():
            return HookResult(exit_code=1, stderr=f"tooldistill hook: config file not found: {path}")
        loaded = load_config(path)
        registry = Registry(loaded.store)
        try:
            return _HANDLERS[key](registry, loaded, payload)
        finally:
            registry.close()
    except Exception as exc:
        return HookResult(exit_code=1, stderr=f"tooldistill hook {key}: {type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------------- handlers


def _session_id(payload: Mapping[str, Any]) -> str:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("the hook payload has no session_id")
    return session_id


def _ensure_trace(registry: Registry, payload: Mapping[str, Any]) -> TraceRecord:
    cwd = payload.get("cwd")
    where = cwd if isinstance(cwd, str) and cwd else "an unknown folder"
    return registry.ensure_trace(_session_id(payload), f"Claude Code session in {where}")


def _session_start(registry: Registry, config: Config, payload: Mapping[str, Any]) -> HookResult:
    trace = _ensure_trace(registry, payload)
    exposed = registry.exposed_versions(suspect_reasons=EXPOSED_SUSPECT_REASONS, limit=config.settings.max_exposed_tools)
    tools = [{"name": version.tool_name, "description": version.description} for version in exposed]
    text = plugin_session_context(trace.id, tools, prefix=own_prefix())
    output = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    return HookResult(stdout=json.dumps(output))


def _truncated(value: Any, limit: int) -> Any:
    try:
        text = canonical_json(value)
    except (TypeError, ValueError):
        return {"_unserializable": str(value)[:limit]}
    if len(text) <= limit:
        return value
    return {"_truncated": text[:limit]}


def _record(registry: Registry, trace_id: str, base_name: str, tool_input: Any, response: ToolResponse, duration: Any) -> None:
    builtin = is_builtin(base_name)
    input_value: Any = tool_input if isinstance(tool_input, Mapping) else ({} if tool_input is None else {"_input": tool_input})
    input_value = _truncated(input_value, BUILTIN_LIMIT) if builtin else _truncated(input_value, 10**9)
    output_json: str | None = None
    if not response.is_error:
        output_value = _truncated(response.value, BUILTIN_LIMIT if builtin else 10**9)
        output_json = canonical_json(output_value)
    latency = int(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None
    registry.add_tool_call(
        trace_id,
        kind=CallKind.BASE,
        tool_name=base_name,
        input_json=canonical_json(input_value),
        output_json=output_json,
        is_error=response.is_error,
        error=(response.error or "tool error")[:ERROR_LIMIT] if response.is_error else None,
        latency_ms=latency,
    )


def _tool_name(payload: Mapping[str, Any]) -> str:
    name = payload.get("tool_name")
    if not isinstance(name, str) or not name:
        raise ValueError("the hook payload has no tool_name")
    return name


def _server_hint(payload: Mapping[str, Any]) -> str | None:
    server = payload.get("mcp_server")
    if isinstance(server, Mapping) and isinstance(server.get("name"), str):
        return server["name"]
    return None


def _own_tool_notes(payload: Mapping[str, Any], name: str) -> HookResult:
    """The server records its own tools itself; only cross-check the trace id Claude passed."""
    tool_input = payload.get("tool_input")
    if own_tool_name(name) == PROPOSE_TOOL_NAME and isinstance(tool_input, Mapping):
        passed = tool_input.get("trace_id")
        session_id = payload.get("session_id")
        if passed and session_id and passed != session_id:
            return HookResult(stderr=f"tooldistill hook: propose_distilled_tool was called with trace_id {passed!r} in session {session_id!r}")
    return HookResult()


def _post_tool_use(registry: Registry, config: Config, payload: Mapping[str, Any]) -> HookResult:
    name = _tool_name(payload)
    if is_own_tool(name):
        return _own_tool_notes(payload, name)
    trace = _ensure_trace(registry, payload)
    response = normalize_tool_response(payload.get("tool_response"))
    _record(registry, trace.id, to_base_tool_name(name, _server_hint(payload)), payload.get("tool_input"), response, payload.get("duration_ms"))
    return HookResult()


def _post_tool_use_failure(registry: Registry, config: Config, payload: Mapping[str, Any]) -> HookResult:
    name = _tool_name(payload)
    if is_own_tool(name):
        return HookResult()
    trace = _ensure_trace(registry, payload)
    error = payload.get("error")
    if error in (None, ""):
        text = "interrupted" if payload.get("is_interrupt") else "tool call failed"
    else:
        text = error if isinstance(error, str) else json.dumps(error, default=str)
    response = ToolResponse(None, True, text)
    _record(registry, trace.id, to_base_tool_name(name, _server_hint(payload)), payload.get("tool_input"), response, payload.get("duration_ms"))
    return HookResult()


def _session_end(registry: Registry, config: Config, payload: Mapping[str, Any]) -> HookResult:
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        trace = registry.get_trace(session_id)
        if trace is not None and trace.finished_at is None:
            registry.finish_trace(session_id)
    return HookResult()


_HANDLERS: dict[str, Callable[[Registry, Config, Mapping[str, Any]], HookResult]] = {
    "session-start": _session_start,
    "post-tool-use": _post_tool_use,
    "post-tool-use-failure": _post_tool_use_failure,
    "session-end": _session_end,
}
