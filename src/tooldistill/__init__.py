"""tooldistill: turn an agent's trial-and-error runs into verified, reusable distilled tools."""

from tooldistill.agent import ToolDistill, Trace
from tooldistill.core.models import ToolResult
from tooldistill.core.runtime import call, distilled_tool

__version__ = "0.1.0"

__all__ = ["ToolDistill", "ToolResult", "Trace", "__version__", "call", "distilled_tool"]
