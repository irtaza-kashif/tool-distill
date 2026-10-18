"""The plugin's MCP server: exposes distilled tools and ``propose_distilled_tool`` to Claude Code over stdio.

The tool list is dynamic (the exposed distilled tools, each with an optional ``trace_id`` property), calls run
the synchronous library in a worker thread under one lock, and ``tools/list_changed`` goes out after any
status change the server causes and whenever the CLI changes the store from another process.
"""

import logging
import os
import sys
import threading
import uuid
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, Tool

from tooldistill.agent import ToolDistill, Trace
from tooldistill.core import lifecycle
from tooldistill.core.models import ToolResult
from tooldistill.core.proposals import PROPOSE_TOOL_INPUT_SCHEMA
from tooldistill.core.tools import PROPOSE_TOOL_NAME
from tooldistill.distill.prompts import PLUGIN_PROPOSE_DESCRIPTION
from tooldistill.integrations.claude_code.hooks import resolve_config

__all__ = ["DESCRIPTION_LIMIT", "DistillServer", "plugin_propose_schema", "serve", "with_trace_id"]

log = logging.getLogger("tooldistill.plugin")
INSTRUCTIONS = (
    "tooldistill: verified distilled tools built from this project's own successful runs. Every tool here "
    "accepts trace_id, the trace id given in the session context."
)
DESCRIPTION_LIMIT = 4096  # Claude Code cuts upfront tool descriptions here
TRACE_ID_PROPERTY = {"type": "string", "description": "The trace id of this session, from the session context."}


def plugin_propose_schema() -> dict[str, Any]:
    """The propose tool's schema in the plugin: trace_id is required, task is optional."""
    schema = deepcopy(PROPOSE_TOOL_INPUT_SCHEMA)
    schema["properties"]["trace_id"] = dict(TRACE_ID_PROPERTY)
    schema["properties"]["task"] = {"type": "string", "description": "What the task was, in one line."}
    schema["required"] = [key for key in schema["required"] if key != "trace_id"] + ["trace_id"]
    return schema


def with_trace_id(schema: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(schema)
    out.setdefault("properties", {})["trace_id"] = dict(TRACE_ID_PROPERTY)
    return out


def _text_result(text: str, is_error: bool) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=is_error)


class DistillServer(MCPServer):
    """``MCPServer`` whose tools are the exposed distilled tools plus ``propose_distilled_tool``."""

    def __init__(self, config: str | Path | None, *, poll_interval: float = 2.0, td: ToolDistill | None = None) -> None:
        super().__init__("tooldistill", instructions=INSTRUCTIONS)
        self.config_path = Path(config) if config is not None else None
        self.poll_interval = poll_interval
        self._td = td
        self.unavailable_reason: str | None = None
        self._lock = threading.Lock()
        self._served: tuple[tuple[str, int], ...] | None = None
        self._session: Any = None

    # ------------------------------------------------------------------ ToolDistill

    def _ensure_td(self) -> ToolDistill | None:
        """Build ToolDistill from the config on first use; without a usable config the server stays empty."""
        with self._lock:
            if self._td is not None:
                return self._td
            if self.config_path is None or not self.config_path.is_file():
                self.unavailable_reason = f"no tooldistill.toml ({self.config_path or 'no path configured'})"
                return None
            try:
                self._td = ToolDistill.from_config(self.config_path)
            except Exception as exc:
                self.unavailable_reason = f"{type(exc).__name__}: {exc}"
                log.warning("tooldistill is not available: %s", self.unavailable_reason)
                return None
            self.unavailable_reason = None
            log.info("tooldistill ready: store %s, %d base tools", self._td.registry.path, len(self._td.tools))
            return self._td

    @property
    def td(self) -> ToolDistill | None:
        return self._td

    def close(self) -> None:
        if self._td is not None:
            with suppress(Exception):
                self._td.close()

    def _snapshot(self, td: ToolDistill) -> tuple[tuple[str, int], ...]:
        return tuple((v.tool_name, v.version) for v in lifecycle.exposed_versions(td.registry, td.settings))

    def changed(self, td: ToolDistill) -> bool:
        """Whether the exposed set differs from the last list served (and remember the new one)."""
        with self._lock:
            current = self._snapshot(td)
            if current == self._served:
                return False
            self._served = current
            return True

    # ------------------------------------------------------------------ MCP surface

    async def _handle_list_tools(self, ctx: Any, params: Any) -> Any:  # type: ignore[override]
        """Remember the session on list requests too, so the store watcher can notify before any call."""
        session = getattr(ctx, "session", None)
        if session is not None:
            self._session = session
        return await super()._handle_list_tools(ctx, params)

    async def list_tools(self) -> list[Tool]:
        td = await anyio.to_thread.run_sync(self._ensure_td)
        tools = [Tool(name=PROPOSE_TOOL_NAME, description=PLUGIN_PROPOSE_DESCRIPTION, input_schema=plugin_propose_schema())]
        if td is not None:
            defs = await anyio.to_thread.run_sync(self._exposed_defs, td)
            tools += [
                Tool(name=d["name"], description=str(d["description"])[:DESCRIPTION_LIMIT], input_schema=with_trace_id(d["input_schema"]))
                for d in defs
            ]
        return tools

    def _exposed_defs(self, td: ToolDistill) -> list[dict[str, Any]]:
        with self._lock:
            defs = td.active_tools()
            self._served = self._snapshot(td)
            return defs

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> CallToolResult:
        if context is not None and getattr(context, "session", None) is not None:
            self._session = context.session
        td = await anyio.to_thread.run_sync(self._ensure_td)
        if td is None:
            return _text_result(
                f"tooldistill is not configured for this project ({self.unavailable_reason}). Use the base tools directly.", True
            )
        result = await anyio.to_thread.run_sync(self.call, td, name, dict(arguments or {}))
        if await anyio.to_thread.run_sync(self.changed, td):
            await self.notify(context)
        return _text_result(result.content, result.is_error)

    def call(self, td: ToolDistill, name: str, args: dict[str, Any]) -> ToolResult:
        """Run one call synchronously under the lock: the same routing as ``trace.execute``, keyed by trace id."""
        with self._lock:
            trace_id = args.pop("trace_id", None)
            if trace_id is not None and not isinstance(trace_id, str):
                trace_id = str(trace_id)
            registry = td.registry
            if name == PROPOSE_TOOL_NAME:
                if not trace_id:
                    return ToolResult("propose_distilled_tool needs trace_id: the trace id from this session's context.", True)
                record = registry.get_trace(trace_id)
                if record is None:
                    return ToolResult(f"unknown trace id '{trace_id}'; use the trace id from this session's context.", True)
                task = args.pop("task", None)
                if isinstance(task, str) and task.strip():
                    registry.set_task(trace_id, task.strip())
                    record = registry.require_trace(trace_id)
                trace = Trace(td, record)
                if trace.outcome is None:
                    trace.set_outcome(True)  # the user's /distill is the outcome
                return trace.execute(PROPOSE_TOOL_NAME, args)
            record = registry.get_trace(trace_id) if trace_id else None
            if record is None:
                record = registry.latest_open_trace()
            if record is None:
                record = registry.ensure_trace(f"plugin-{uuid.uuid4().hex}", "Claude Code session (id unknown)")
            return Trace(td, record).execute(name, args)

    async def notify(self, context: Any = None) -> None:
        """Tell Claude Code the tool list changed, through the request context and the session."""
        session = getattr(context, "session", None) or self._session
        if session is None:
            log.info("tool list changed but no session is known yet; Claude Code will see it on its next tools/list")
            return
        try:
            await session.send_tool_list_changed()
            log.info("sent tools/list_changed")
        except Exception as exc:
            log.warning("could not send tools/list_changed: %s", exc)
        if context is not None and hasattr(context, "notify_tools_changed"):
            with suppress(Exception):
                await context.notify_tools_changed()

    async def watch_store(self) -> None:
        """Poll the store for changes made by the CLI in another process and notify."""
        while True:
            await anyio.sleep(self.poll_interval)
            td = self._td
            if td is None or self._session is None:
                continue
            try:
                changed = await anyio.to_thread.run_sync(self.changed, td)
            except Exception as exc:  # the store may be mid-migration or locked; try again later
                log.debug("store check failed: %s", exc)
                continue
            if changed:
                log.info("the store changed outside this process")
                await self.notify(None)

    async def run_stdio_async(self) -> None:
        """stdio transport with ``tools.listChanged`` advertised and the store watcher running alongside."""
        from mcp.server.lowlevel.server import NotificationOptions
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            async with anyio.create_task_group() as group:
                group.start_soon(self.watch_store)
                options = self._lowlevel_server.create_initialization_options(NotificationOptions(tools_changed=True))
                await self._lowlevel_server.run(read_stream, write_stream, options)
                group.cancel_scope.cancel()


def serve(config: str | Path | None = None, *, poll_interval: float = 2.0) -> None:
    """Run the plugin server over stdio until Claude Code closes the pipe. Logs go to stderr only."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="tooldistill serve: %(message)s")
    _start_parent_watchdog()
    server = DistillServer(resolve_config(config), poll_interval=poll_interval)
    try:
        anyio.run(server.run_stdio_async)
    finally:
        server.close()


def _start_parent_watchdog() -> None:
    """On POSIX, exit when the parent (Claude Code) is gone and we were re-parented to init."""
    if os.name == "nt":
        return

    def watch() -> None:
        import time

        while True:
            time.sleep(5)
            if os.getppid() == 1:
                os._exit(0)

    threading.Thread(target=watch, name="tooldistill-ppid-watchdog", daemon=True).start()
