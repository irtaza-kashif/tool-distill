"""tooldistill: turn an agent's trial-and-error runs into verified, reusable distilled tools."""

from tooldistill.core.models import ToolResult
from tooldistill.core.runtime import call, distilled_tool

__version__ = "0.1.0"

__all__ = ["ToolResult", "__version__", "call", "distilled_tool"]
