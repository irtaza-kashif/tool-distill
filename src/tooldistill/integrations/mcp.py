"""MCP executor adapter: connect to MCP servers and expose their tools as base tools with a synchronous executor.

``MCPToolkit`` runs every client session on one background thread with its own event loop; each server has
one long-lived task that owns its connection (anyio requires a context to be closed by the task that opened
it, so connects and reconnects happen there). The executor, the health probe and ``refresh_tools`` schedule
coroutines onto that loop and wait for them, so the rest of the library, synchronous by contract, never
touches asyncio. Server specs come from ``tooldistill.toml`` or a ``.mcp.json`` file in Claude Code's
structure (``core.config.ServerSpec``).
"""

import asyncio
import atexit
import concurrent.futures
import hashlib
import json
import logging
import re
import threading
from collections.abc import Mapping
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from typing import Any

from tooldistill.core.config import ServerSpec
from tooldistill.core.errors import MCPToolError
from tooldistill.core.serialization import schema_hash

__all__ = ["MAX_NAME", "MCPToolkit", "base_tool_name", "error_text", "result_value", "sanitize_name_part"]

log = logging.getLogger("tooldistill.mcp")
MAX_NAME = 64  # the library's tool name limit (core.tools.TOOL_NAME_RE)
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


# ------------------------------------------------------------------------------- names


def sanitize_name_part(part: str) -> str:
    return _UNSAFE.sub("_", part)


def base_tool_name(server: str, tool: str) -> str:
    """``<server>__<tool>`` with unsafe characters replaced; shortened with a hash suffix beyond 64 characters.

    The Claude Code hook mapping uses the same function on the pieces of ``mcp__<server>__<tool>``.
    """
    name = f"{sanitize_name_part(server)}__{sanitize_name_part(tool)}"
    if len(name) > MAX_NAME:
        digest = hashlib.sha1(f"{server}__{tool}".encode("utf-8")).hexdigest()[:8]
        name = f"{name[: MAX_NAME - 9]}_{digest}"
    return name


# ----------------------------------------------------------------------------- results


def _block_dict(block: Any) -> dict[str, Any] | None:
    if isinstance(block, Mapping):
        return dict(block)
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    return None


def _block_value(block: Any) -> Any:
    data = _block_dict(block)
    if data is None:
        return block
    if data.get("type") == "text" and isinstance(data.get("text"), str):
        try:
            return json.loads(data["text"])
        except ValueError:
            return data["text"]
    if "data" in data:
        data["data"] = "<omitted>"
    return data


def result_value(content: list[Any] | None, structured_content: Any = None) -> Any:
    """The executor's value for a tool result. The Claude Code hook uses the same mapping, so hook-recorded
    fixtures match what ``call()`` returns live, byte for byte in canonical JSON."""
    if structured_content is not None:
        return structured_content
    values = [_block_value(block) for block in content or []]
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def error_text(content: list[Any] | None) -> str:
    texts = []
    for block in content or []:
        data = _block_dict(block) or {}
        if data.get("type") == "text" and isinstance(data.get("text"), str):
            texts.append(data["text"])
    return "\n".join(texts) or "the tool returned an error"


# ----------------------------------------------------------------------------- toolkit


@dataclass(slots=True)
class _Connection:
    """One server's state, owned by its ``_serve`` task on the loop thread."""

    spec: ServerSpec
    session: Any = None
    failure: MCPToolError | None = None
    dead: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    wake: asyncio.Event = field(default_factory=asyncio.Event)


class MCPToolkit:
    """Connects to MCP servers and offers their tools as base tools: ``tools``, ``executor``, ``probe``."""

    def __init__(
        self,
        servers: Mapping[str, ServerSpec],
        *,
        connect_timeout: float = 30.0,
        call_timeout: float = 120.0,
    ) -> None:
        self._specs: dict[str, ServerSpec] = dict(servers)
        self.connect_timeout = float(connect_timeout)
        self.call_timeout = float(call_timeout)
        self._state = "new"  # new | running | closed
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._failure: BaseException | None = None
        self._conns: dict[str, _Connection] = {}
        self._by_server: dict[str, list[dict[str, Any]]] = {}
        self._tools: list[dict[str, Any]] = []
        self._index: dict[str, tuple[str, str]] = {}

    # ----------------------------------------------------------------- lifecycle

    def __enter__(self) -> "MCPToolkit":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def servers(self) -> tuple[str, ...]:
        return tuple(self._specs)

    @property
    def tools(self) -> list[dict[str, Any]]:
        """Anthropic-format tool dicts named ``<server>__<tool>``."""
        return [dict(tool) for tool in self._tools]

    def server_of(self, name: str) -> str | None:
        target = self._index.get(name)
        return target[0] if target else None

    def start(self) -> "MCPToolkit":
        """Connect to every configured server; fails if any is unreachable (never a partial tool list)."""
        with self._state_lock:
            if self._state != "new":
                raise MCPToolError("an MCPToolkit can only be started once; create a new one")
            self._state = "running"
        self._thread = threading.Thread(target=self._run, name="tooldistill-mcp", daemon=True)
        self._thread.start()
        budget = self.connect_timeout * max(1, len(self._specs)) * 2 + 5
        if not self._ready.wait(budget):
            self.close()
            raise MCPToolError(f"timed out after {budget:g}s connecting to the MCP servers")
        if self._failure is not None:
            failure = self._failure
            self.close()
            if isinstance(failure, MCPToolError):
                raise failure
            raise MCPToolError(str(failure)) from failure
        atexit.register(self.close)
        return self

    def close(self) -> None:
        with self._state_lock:
            if self._state == "closed":
                return
            self._state = "closed"
        with suppress(Exception):
            atexit.unregister(self.close)
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            with suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._request_stop)
            self._thread.join(10)

    # ----------------------------------------------------------------- the loop

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:  # pragma: no cover - defensive: the loop itself failed
            self._failure = exc
            self._ready.set()

    def _request_stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        for conn in self._conns.values():
            conn.wake.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._conns = {name: _Connection(spec) for name, spec in self._specs.items()}
        try:
            async with asyncio.TaskGroup() as group:
                for name, conn in self._conns.items():
                    group.create_task(self._serve(name, conn), name=f"mcp:{name}")
                for name, conn in self._conns.items():
                    await conn.ready.wait()
                    if conn.failure is not None:
                        self._failure = conn.failure
                        self._request_stop()
                        break
                self._ready.set()
                await self._stop.wait()
                self._request_stop()
        except BaseException as exc:  # pragma: no cover - a serve task crashed
            self._failure = self._failure or exc
            self._ready.set()

    async def _serve(self, name: str, conn: _Connection) -> None:
        """Own the connection to one server: connect, serve until asked to reconnect or stop, close, repeat."""
        while not self._stop.is_set():  # type: ignore[union-attr]
            stack = AsyncExitStack()
            try:
                session = await asyncio.wait_for(self._open_session(conn.spec, stack), self.connect_timeout)
                listed = await asyncio.wait_for(session.list_tools(), self.connect_timeout)
                self._register(name, listed.tools)
            except Exception as exc:
                with suppress(Exception):
                    await stack.aclose()
                conn.session = None
                conn.failure = MCPToolError(f"cannot connect to MCP server '{name}': {exc}")
                log.warning("%s", conn.failure)
            else:
                conn.session = session
                conn.failure = None
                conn.dead = False
                log.info("connected to MCP server '%s' (%d tools)", name, len(listed.tools))
            conn.ready.set()
            await conn.wake.wait()
            conn.wake.clear()
            conn.ready.clear()
            conn.session = None
            with suppress(Exception):
                await stack.aclose()

    async def _open_session(self, spec: ServerSpec, stack: AsyncExitStack) -> Any:
        from mcp.client.session import ClientSession

        if spec.transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

            assert spec.command is not None
            env = {**get_default_environment(), **spec.env}
            params = StdioServerParameters(command=spec.command, args=list(spec.args), env=env, cwd=spec.cwd)
            read, write = await stack.enter_async_context(stdio_client(params))
        elif spec.transport == "http":
            from mcp.client.streamable_http import streamable_http_client

            assert spec.url is not None
            http_client = _http_client(spec.headers)
            streams = await stack.enter_async_context(streamable_http_client(spec.url, http_client=http_client))
            read, write = streams[0], streams[1]
        else:
            from mcp.client.sse import sse_client

            assert spec.url is not None
            streams = await stack.enter_async_context(sse_client(spec.url, headers=spec.headers or None))
            read, write = streams[0], streams[1]
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _session_for(self, server: str) -> Any:
        conn = self._conns[server]
        if conn.session is None or conn.dead:
            log.warning("reconnecting to MCP server '%s'", server)
            conn.ready.clear()
            conn.wake.set()  # the serve task closes the old connection and opens a new one
            try:
                await asyncio.wait_for(conn.ready.wait(), self.connect_timeout * 2 + 5)
            except asyncio.TimeoutError:
                raise MCPToolError(f"MCP server '{server}' did not reconnect in time") from None
            if conn.session is None:
                raise conn.failure or MCPToolError(f"MCP server '{server}' is unavailable")
        return conn.session

    def _register(self, server: str, tools: list[Any]) -> None:
        entries: list[dict[str, Any]] = []
        for tool in tools:
            schema = tool.input_schema if isinstance(tool.input_schema, dict) else {"type": "object", "properties": {}}
            entries.append(
                {
                    "name": base_tool_name(server, tool.name),
                    "description": tool.description or "",
                    "input_schema": schema,
                    "_server": server,
                    "_tool": tool.name,
                }
            )
        by_server = {**self._by_server, server: entries}
        index: dict[str, tuple[str, str]] = {}
        flat: list[dict[str, Any]] = []
        for name, server_tools in by_server.items():
            for entry in server_tools:
                if entry["name"] in index:
                    other = index[entry["name"]]
                    raise MCPToolError(
                        f"MCP tools '{other[1]}' on '{other[0]}' and '{entry['_tool']}' on '{name}' both map to "
                        f"the base tool name '{entry['name']}'"
                    )
                index[entry["name"]] = (name, entry["_tool"])
                flat.append({"name": entry["name"], "description": entry["description"], "input_schema": entry["input_schema"]})
        self._by_server = by_server
        self._index = index
        self._tools = flat

    # ----------------------------------------------------------------- bridging

    def _require_running(self) -> None:
        if self._state != "running" or self._loop is None:
            raise MCPToolError("the MCPToolkit is not running (start() it, and do not use it after close())")

    def _submit(self, coro: Any, timeout: float) -> Any:
        if self._state != "running" or self._loop is None:
            coro.close()
            raise MCPToolError("the MCPToolkit is not running (start() it, and do not use it after close())")
        if threading.current_thread() is self._thread:
            coro.close()
            raise MCPToolError("the executor cannot be used from the MCP loop thread")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise MCPToolError(f"MCP call timed out after {timeout:g}s") from None

    # ----------------------------------------------------------------- public API

    def executor(self, name: str, args: Mapping[str, Any] | None) -> Any:
        """The executor contract: run a base tool, raise on failure, return a JSON-serializable value."""
        self._require_running()
        target = self._index.get(name)
        if target is None:
            raise MCPToolError(f"unknown MCP tool '{name}'")
        server, tool = target
        return self._submit(self._call(server, tool, dict(args or {})), self.call_timeout + 5)

    async def _call(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        session = await self._session_for(server)
        conn = self._conns[server]
        try:
            result = await asyncio.wait_for(session.call_tool(tool, args), self.call_timeout)
        except asyncio.TimeoutError:
            raise MCPToolError(f"MCP server '{server}' did not answer '{tool}' within {self.call_timeout:g}s") from None
        except MCPToolError:
            raise
        except Exception as exc:
            conn.dead = True
            raise MCPToolError(f"MCP server '{server}' is not responding: {exc}") from exc
        if getattr(result, "is_error", False):
            raise MCPToolError(error_text(result.content))
        return result_value(result.content, getattr(result, "structured_content", None))

    def probe(self, name: str) -> bool:
        """Health probe: the tool's server answers a ping and still lists the tool. Never calls the tool."""
        target = self._index.get(name)
        if target is None or self._state != "running":
            return False
        server, tool = target

        async def check() -> bool:
            session = await self._session_for(server)
            await asyncio.wait_for(session.send_ping(), 5)
            listed = await asyncio.wait_for(session.list_tools(), 5)
            return any(t.name == tool for t in listed.tools)

        try:
            return bool(self._submit(check(), self.connect_timeout * 2 + 15))
        except Exception as exc:
            log.info("probe of '%s' failed: %s", name, exc)
            return False

    def refresh_tools(self) -> list[str]:
        """Re-list every server; returns the base tool names whose schema changed, appeared or disappeared."""
        self._require_running()

        async def relist() -> list[str]:
            before = {t["name"]: schema_hash(t["input_schema"]) for t in self._tools}
            for server in list(self._specs):
                session = await self._session_for(server)
                listed = await asyncio.wait_for(session.list_tools(), self.connect_timeout)
                self._register(server, listed.tools)
            after = {t["name"]: schema_hash(t["input_schema"]) for t in self._tools}
            return sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))

        return self._submit(relist(), self.connect_timeout * max(1, len(self._specs)) * 2 + 5)


def _http_client(headers: Mapping[str, str]) -> Any:
    """An httpx client carrying the configured headers, or None for the SDK default."""
    if not headers:
        return None
    try:
        from mcp.client.streamable_http import create_mcp_http_client

        return create_mcp_http_client(headers=dict(headers))
    except Exception:  # pragma: no cover - depends on the SDK's factory signature
        import httpx2

        return httpx2.AsyncClient(headers=dict(headers), follow_redirects=True)
