"""Library settings and the ``tooldistill.toml`` loader."""

import importlib
import json
import os
import re
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from tooldistill.core.errors import ConfigError

__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_DISTILLER_MODEL",
    "SAMPLE_CONFIG",
    "Config",
    "ServerSpec",
    "Settings",
    "expand_env_refs",
    "import_string",
    "init_config",
    "load_config",
]

DEFAULT_DISTILLER_MODEL = "claude-sonnet-5-5"
CONFIG_FILENAME = "tooldistill.toml"
SAMPLE_CONFIG = """store = ".tooldistill"
executor = "myapp.tools:execute_tool"      # imported by the CLI: execute_tool(name, args) -> Any
tools = "myapp.tools:TOOLS"                # list of Anthropic-format tool dicts
# health_probe = "myapp.tools:probe"       # optional: probe(tool_name) -> bool
# distiller = "myapp.distill:DISTILLER"    # optional: a custom Distiller (instance or factory) instead of ClaudeDistiller
# mcp_config = ".mcp.json"                 # instead of executor/tools: base tools from MCP servers, in Claude Code's
#                                          # .mcp.json structure; inline [mcpServers.<name>] tables are merged over it
distiller_model = "claude-sonnet-5-5"
fallback_distiller = true
auto_promote = false
min_calls_to_distill = 8
min_call_reduction = 0.3
max_exposed_tools = 20
allow_live_verification = false            # live verification has real side effects
check_schemas_on_start = true
"""

_SETTING_TYPES: dict[str, type] = {
    "distiller_model": str,
    "fallback_distiller": bool,
    "auto_promote": bool,
    "min_calls_to_distill": int,
    "min_call_reduction": float,
    "max_exposed_tools": int,
    "allow_live_verification": bool,
    "check_schemas_on_start": bool,
}


@dataclass(slots=True)
class Settings:
    """Tunable behaviour; defaults and their consequences are documented in CLAUDE.md."""

    distiller_model: str = DEFAULT_DISTILLER_MODEL
    fallback_distiller: bool = True
    auto_promote: bool = False
    min_calls_to_distill: int = 8
    min_call_reduction: float = 0.3
    max_exposed_tools: int = 20
    allow_live_verification: bool = False
    check_schemas_on_start: bool = True

    def __post_init__(self) -> None:
        if self.min_calls_to_distill < 1:
            raise ValueError("min_calls_to_distill must be at least 1")
        if not 0.0 <= self.min_call_reduction < 1.0:
            raise ValueError("min_call_reduction must be in [0, 1)")
        if self.max_exposed_tools < 0:
            raise ValueError("max_exposed_tools must not be negative")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "Settings":
        kwargs: dict[str, Any] = {}
        for key, value in values.items():
            if key not in _SETTING_TYPES:
                raise ConfigError(f"unknown setting '{key}' (known: {', '.join(_SETTING_TYPES)})")
            kwargs[key] = _coerce(key, value, _SETTING_TYPES[key])
        try:
            return cls(**kwargs)
        except ValueError as exc:
            raise ConfigError(str(exc)) from None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _coerce(key: str, value: Any, expected: type) -> Any:
    if expected is bool:
        if isinstance(value, bool):
            return value
    elif expected is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif expected is float:
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    elif isinstance(value, expected):
        return value
    raise ConfigError(f"setting '{key}' must be {expected.__name__}, got {type(value).__name__}")


# --------------------------------------------------------------------------- MCP servers

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SERVER_KEYS = frozenset({"type", "command", "args", "env", "cwd", "url", "headers"})


def expand_env_refs(value: Any, *, environ: Mapping[str, str] | None = None) -> Any:
    """Expand ``${VAR}`` and ``${VAR:-default}`` in strings, recursively through lists and dict values.

    An unset variable without a default stays literal, with one warning on stderr (Claude Code's rule).
    """
    env = os.environ if environ is None else environ

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        print(f"tooldistill: environment variable {name} is not set; leaving ${{{name}}} as is", file=sys.stderr)
        return match.group(0)

    if isinstance(value, str):
        return _ENV_REF.sub(replace, value)
    if isinstance(value, list):
        return [expand_env_refs(item, environ=environ) for item in value]
    if isinstance(value, Mapping):
        return {key: expand_env_refs(item, environ=environ) for key, item in value.items()}
    return value


def _string_map(name: str, key: str, value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ConfigError(f"MCP server '{name}': '{key}' must be a table of string values")
    return dict(value)


@dataclass(frozen=True, slots=True)
class ServerSpec:
    """One MCP server the library may connect to, in Claude Code's ``.mcp.json`` structure."""

    name: str
    transport: Literal["stdio", "http", "sse"]
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        name: str,
        mapping: Mapping[str, Any],
        *,
        base_dir: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "ServerSpec":
        if not isinstance(name, str) or not _SERVER_NAME_RE.match(name):
            raise ConfigError(f"MCP server name {name!r} must match [A-Za-z0-9_-]+")
        if not isinstance(mapping, Mapping):
            raise ConfigError(f"MCP server '{name}' must be a table with command/args/env or url")
        unknown = sorted(set(mapping) - _SERVER_KEYS)
        if unknown:
            raise ConfigError(f"MCP server '{name}': unknown key(s) {', '.join(unknown)}")
        data = expand_env_refs(dict(mapping), environ=environ)
        command, url = data.get("command"), data.get("url")
        if (command is None) == (url is None):
            raise ConfigError(f"MCP server '{name}' needs exactly one of 'command' (stdio) or 'url' (http/sse)")
        if command is not None and not isinstance(command, str):
            raise ConfigError(f"MCP server '{name}': 'command' must be a string")
        if url is not None and not isinstance(url, str):
            raise ConfigError(f"MCP server '{name}': 'url' must be a string")
        transport = data.get("type")
        if transport is None:
            transport = "stdio" if command is not None else "http"
        if transport not in ("stdio", "http", "sse"):
            raise ConfigError(f"MCP server '{name}': type must be stdio, http or sse")
        if (transport == "stdio") != (command is not None):
            raise ConfigError(f"MCP server '{name}': type {transport} does not match the given command/url")
        args = data.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ConfigError(f"MCP server '{name}': 'args' must be a list of strings")
        cwd = data.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str):
                raise ConfigError(f"MCP server '{name}': 'cwd' must be a string")
            path = Path(cwd).expanduser()
            if not path.is_absolute() and base_dir is not None:
                path = base_dir / path
            cwd = str(path)
        elif base_dir is not None and command is not None:
            cwd = str(base_dir)  # a server listed in a project's config runs from that project, like Claude Code's own
        return cls(
            name=name,
            transport=transport,
            command=command,
            args=tuple(args),
            env=_string_map(name, "env", data.get("env")),
            cwd=cwd,
            url=url,
            headers=_string_map(name, "headers", data.get("headers")),
        )


@dataclass(slots=True)
class Config:
    """A parsed ``tooldistill.toml``. The executor, tools, probe, distiller and MCP servers are loaded on demand."""

    path: Path
    store: Path
    settings: Settings
    executor_ref: str | None
    tools_ref: str | None
    health_probe_ref: str | None
    distiller_ref: str | None = None
    mcp_config: Path | None = None
    mcp_servers: dict[str, Any] = field(default_factory=dict)

    @property
    def uses_mcp(self) -> bool:
        """Base tools come from MCP servers rather than from ``executor``/``tools`` references."""
        return self.mcp_config is not None or bool(self.mcp_servers)

    def load_mcp_servers(self, *, environ: Mapping[str, str] | None = None) -> dict[str, ServerSpec]:
        """The configured MCP servers: the ``mcp_config`` file first, inline ``[mcpServers]`` entries merged over it."""
        raw: dict[str, Any] = {}
        if self.mcp_config is not None:
            try:
                data = json.loads(self.mcp_config.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise ConfigError(f"mcp_config file not found: {self.mcp_config}") from None
            except ValueError as exc:
                raise ConfigError(f"{self.mcp_config}: {exc}") from None
            if not isinstance(data, dict):
                raise ConfigError(f"{self.mcp_config}: expected an object with an mcpServers map")
            servers = data.get("mcpServers", data)
            if not isinstance(servers, dict):
                raise ConfigError(f"{self.mcp_config}: mcpServers must be an object")
            raw.update(servers)
        raw.update(self.mcp_servers)
        return {
            name: ServerSpec.from_mapping(name, spec, base_dir=self.path.parent, environ=environ)
            for name, spec in raw.items()
        }

    def load_distiller(self) -> Any:
        """The configured Distiller: an instance, or a zero-argument factory that returns one."""
        if self.distiller_ref is None:
            return None
        obj = import_string(self.distiller_ref, base_dir=self.path.parent)
        if callable(obj) and not hasattr(obj, "distill"):
            obj = obj()
        if not hasattr(obj, "distill"):
            raise ConfigError(f"'{self.distiller_ref}' is not a Distiller (it needs a distill() method)")
        return obj

    def load_executor(self) -> Any:
        if self.executor_ref is None:
            raise ConfigError(f"{self.path}: 'executor' is not set (module:attr of execute_tool)")
        return import_string(self.executor_ref, base_dir=self.path.parent)

    def load_tools(self) -> list[dict[str, Any]]:
        if self.tools_ref is None:
            raise ConfigError(f"{self.path}: 'tools' is not set (module:attr of the base tool list)")
        tools = import_string(self.tools_ref, base_dir=self.path.parent)
        if not isinstance(tools, list):
            raise ConfigError(f"'{self.tools_ref}' must be a list of tool dicts, got {type(tools).__name__}")
        return tools

    def load_health_probe(self) -> Any:
        if self.health_probe_ref is None:
            return None
        return import_string(self.health_probe_ref, base_dir=self.path.parent)


def load_config(path: str | Path = CONFIG_FILENAME) -> Config:
    file = Path(path).expanduser().resolve()
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {file}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{file}: {exc}") from None
    refs: dict[str, str | None] = {}
    for key in ("executor", "tools", "health_probe", "distiller"):
        value = data.pop(key, None)
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"{file}: '{key}' must be a 'module:attr' string")
        refs[key] = value
    store = data.pop("store", ".tooldistill")
    if not isinstance(store, str):
        raise ConfigError(f"{file}: 'store' must be a path string")
    store_path = Path(store).expanduser()
    if not store_path.is_absolute():
        store_path = file.parent / store_path
    mcp_config = data.pop("mcp_config", None)
    if mcp_config is not None and not isinstance(mcp_config, str):
        raise ConfigError(f"{file}: 'mcp_config' must be a path string")
    mcp_servers = data.pop("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        raise ConfigError(f"{file}: 'mcpServers' must be a table of servers")
    settings = Settings.from_mapping(data)
    mcp_path: Path | None = None
    if mcp_config is not None:
        mcp_path = Path(mcp_config).expanduser()
        if not mcp_path.is_absolute():
            mcp_path = file.parent / mcp_path
        mcp_path = mcp_path.resolve()
    config = Config(
        path=file,
        store=store_path.resolve(),
        settings=settings,
        executor_ref=refs["executor"],
        tools_ref=refs["tools"],
        health_probe_ref=refs["health_probe"],
        distiller_ref=refs["distiller"],
        mcp_config=mcp_path,
        mcp_servers=mcp_servers,
    )
    if config.uses_mcp and (refs["executor"] is not None or refs["tools"] is not None):
        raise ConfigError(f"{file}: use either executor/tools or MCP servers (mcp_config / mcpServers), not both")
    return config


def init_config(path: str | Path = CONFIG_FILENAME, *, store: str | Path = ".tooldistill") -> bool:
    """Write the sample ``tooldistill.toml`` pointing at ``store``; returns False when the file already exists."""
    file = Path(path)
    if file.exists():
        return False
    text = SAMPLE_CONFIG.replace('store = ".tooldistill"', f'store = "{Path(store).as_posix()}"', 1)
    file.write_text(text, encoding="utf-8")
    return True


def import_string(ref: str, *, base_dir: Path | None = None) -> Any:
    """Import ``"package.module:attr.sub"``; ``base_dir`` is added to ``sys.path`` so project code resolves."""
    module_name, sep, attr_path = ref.partition(":")
    if not sep or not module_name or not attr_path:
        raise ConfigError(f"'{ref}' is not a 'module:attr' reference")
    if base_dir is not None:
        base = str(base_dir)
        if base not in sys.path:
            sys.path.insert(0, base)
    try:
        obj: Any = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(f"cannot import '{module_name}' for '{ref}': {exc}") from exc
    for part in attr_path.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError:
            raise ConfigError(f"'{module_name}' has no attribute '{attr_path}'") from None
    return obj
