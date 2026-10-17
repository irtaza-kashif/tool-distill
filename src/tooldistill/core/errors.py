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
