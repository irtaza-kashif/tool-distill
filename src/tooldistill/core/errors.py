"""Exception types shared across the library."""


class ToolDistillError(Exception):
    """Base class for every error raised by tooldistill."""


class ToolDefinitionError(ToolDistillError, ValueError):
    """A base tool definition handed to the library is invalid (bad name, missing schema, duplicate)."""


class RegistryError(ToolDistillError):
    """The store does not contain something the caller asked for (unknown trace, version, ...)."""


class ProposalRejected(ToolDistillError):
    """A proposal failed basic validation and was not stored."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidTransition(ToolDistillError):
    """A status change that the lifecycle table does not allow."""

    def __init__(self, tool_name: str, version: int, from_status: str, to_status: str) -> None:
        super().__init__(
            f"distilled tool '{tool_name}' v{version}: cannot move from {from_status} to {to_status}"
        )
        self.tool_name = tool_name
        self.version = version
        self.from_status = from_status
        self.to_status = to_status


class ToolLoadError(ToolDistillError):
    """A distilled tool file could not be loaded into a runnable tool."""


class DistilledToolCallError(ToolDistillError):
    """An inner base tool call made through ``call()`` failed (executor raised, or unknown base tool)."""

    def __init__(self, step: int, base_tool: str, message: str) -> None:
        super().__init__(f"step {step}: call to '{base_tool}' failed: {message}")
        self.step = step
        self.base_tool = base_tool
        self.message = message


class ReplayMismatchError(ToolDistillError):
    """In replay mode, a ``call()`` matched no recorded fixture."""

    def __init__(
        self,
        step: int,
        base_tool: str,
        input_json: str,
        message: str,
        *,
        nearest_input_json: str | None,
        nearest_seq: int | None,
        tool_has_fixtures: bool,
    ) -> None:
        super().__init__(message)
        self.step = step
        self.base_tool = base_tool
        self.input_json = input_json
        self.message = message
        self.nearest_input_json = nearest_input_json
        self.nearest_seq = nearest_seq
        self.tool_has_fixtures = tool_has_fixtures


class LifecycleError(ToolDistillError):
    """A lifecycle operation cannot be carried out as requested (nothing to promote, live verification off, ...)."""
