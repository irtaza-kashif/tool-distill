"""Library settings and the ``tooldistill.toml`` loader."""

import importlib
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tooldistill.core.errors import ConfigError

__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_DISTILLER_MODEL",
    "SAMPLE_CONFIG",
    "Config",
    "Settings",
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


@dataclass(slots=True)
class Config:
    """A parsed ``tooldistill.toml``. The executor, tools and probe are imported on demand."""

    path: Path
    store: Path
    settings: Settings
    executor_ref: str | None
    tools_ref: str | None
    health_probe_ref: str | None
    distiller_ref: str | None = None

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
    settings = Settings.from_mapping(data)
    return Config(
        path=file,
        store=store_path.resolve(),
        settings=settings,
        executor_ref=refs["executor"],
        tools_ref=refs["tools"],
        health_probe_ref=refs["health_probe"],
        distiller_ref=refs["distiller"],
    )


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
