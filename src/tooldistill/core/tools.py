"""The developer's base tool list, validated once and queried by name."""

import copy
import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from tooldistill.core.errors import ToolDefinitionError
from tooldistill.core.models import BaseTool
from tooldistill.core.serialization import schema_hash

__all__ = ["DISTILLED_NAME_RE", "PROPOSE_TOOL_NAME", "TOOL_NAME_RE", "ToolSet"]

TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
DISTILLED_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
PROPOSE_TOOL_NAME = "propose_distilled_tool"


class ToolSet:
    """An ordered, name-unique collection of base tools in Anthropic format."""

    def __init__(self, tools: Iterable[Mapping[str, Any]] = ()) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._hashes: dict[str, str] = {}
        for index, tool in enumerate(tools):
            self._add(index, tool)

    def _add(self, index: int, tool: Mapping[str, Any]) -> None:
        if not isinstance(tool, Mapping):
            raise ToolDefinitionError(f"tool #{index} is not a dict: {tool!r}")
        name = tool.get("name")
        if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
            raise ToolDefinitionError(
                f"tool #{index} has an invalid name {name!r}: names must match ^[a-zA-Z0-9_-]{{1,64}}$ "
                "(the Anthropic API rejects dots: use gmail_search, never gmail.search)"
            )
        if name == PROPOSE_TOOL_NAME:
            raise ToolDefinitionError(f"'{PROPOSE_TOOL_NAME}' is reserved for the propose tool")
        if name in self._tools:
            raise ToolDefinitionError(f"duplicate base tool name '{name}'")
        schema = tool.get("input_schema")
        if not isinstance(schema, Mapping):
            raise ToolDefinitionError(f"base tool '{name}' has no input_schema dict")
        description = tool.get("description", "")
        if not isinstance(description, str):
            raise ToolDefinitionError(f"base tool '{name}': description must be a string")
        base = BaseTool(name=name, description=description, input_schema=copy.deepcopy(dict(schema)))
        self._tools[name] = base
        self._hashes[name] = schema_hash(base.input_schema)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[BaseTool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def __getitem__(self, name: str) -> BaseTool:
        return self._tools[name]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def schema_hash(self, name: str) -> str:
        """Current schema hash of a base tool; ``KeyError`` for unknown names."""
        return self._hashes[name]

    def hashes(self) -> dict[str, str]:
        return dict(self._hashes)

    def as_dicts(self) -> list[dict[str, Any]]:
        """Fresh Anthropic-format tool dicts (safe for the caller to mutate)."""
        return [tool.as_dict() for tool in self._tools.values()]
