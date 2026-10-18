"""Shared test data: a small base tool list in Anthropic format, plus the ACME invoice scenario."""

from copy import deepcopy
from pathlib import Path
from typing import Any

from tooldistill.core.models import VersionRecord
from tooldistill.core.registry import Registry
from tooldistill.core.serialization import canonical_json

GMAIL_SEARCH: dict[str, Any] = {
    "name": "gmail_search",
    "description": "Search mail.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Gmail search query"},
            "since": {"type": "string", "description": "Relative window such as 1d"},
        },
        "required": ["query"],
    },
}

GMAIL_GET_MESSAGE: dict[str, Any] = {
    "name": "gmail_get_message",
    "description": "Fetch one message by id.",
    "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
}

BILLING_FIND_CUSTOMER: dict[str, Any] = {
    "name": "billing_find_customer",
    "description": "Find a customer by exact name.",
    "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
}

BILLING_CREATE_INVOICE: dict[str, Any] = {
    "name": "billing_create_invoice",
    "description": "Create a draft invoice.",
    "input_schema": {
        "type": "object",
        "properties": {
            "customer_id": {"type": "string"},
            "lines": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["customer_id", "lines"],
    },
}

BILLING_IMPORT_FROM_EMAIL: dict[str, Any] = {
    "name": "billing_import_from_email",
    "description": "Create an invoice straight from an order email.",
    "input_schema": {
        "type": "object",
        "properties": {"message_id": {"type": "string"}},
        "required": ["message_id"],
    },
}


def sample_tools() -> list[dict[str, Any]]:
    return deepcopy([GMAIL_SEARCH, GMAIL_GET_MESSAGE, BILLING_FIND_CUSTOMER, BILLING_CREATE_INVOICE, BILLING_IMPORT_FROM_EMAIL])


def sample_tools_with_currency() -> list[dict[str, Any]]:
    """The same tools after the billing API grew a required ``currency`` field."""
    tools = sample_tools()
    for tool in tools:
        if tool["name"] in ("billing_create_invoice", "billing_import_from_email"):
            tool["input_schema"]["properties"]["currency"] = {"type": "string"}
            tool["input_schema"]["required"].append("currency")
    return tools


# ------------------------------------------------------------------- the ACME scenario

TOOLS_DIR = Path(__file__).parent / "fixtures" / "tools"
INVOICE_NAME = "create_invoice_from_email"
INVOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"customer": {"type": "string"}, "since": {"type": "string", "default": "1d"}},
    "required": ["customer"],
}
GOOD_INVOICE = (TOOLS_DIR / "good_invoice.py").read_text(encoding="utf-8")
GOOD_WITH_CHECK = (TOOLS_DIR / "good_with_check.py").read_text(encoding="utf-8")
LINES = [{"sku": "A", "qty": 2}]
TWO_CALL_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]}
TWO_CALL_TOOL = '''from tooldistill import distilled_tool, call


@distilled_tool(
    name="create_invoice_from_email",
    description="Import the latest order email as an invoice.",
    input_schema={"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]},
)
def create_invoice_from_email(customer: str) -> dict:
    msg = call("gmail_search", query=f"from:{customer} order", since="1d")[0]
    return call("billing_import_from_email", message_id=msg["id"])
'''


class FakeDistiller:
    """A Distiller that returns a fixed proposal (or None) and records how it was called."""

    def __init__(self, proposal: Any) -> None:
        self.proposal = proposal
        self.calls: list[dict[str, Any]] = []

    def distill(self, trace: Any, tools: Any, *, note: str | None = None, name: str | None = None) -> Any:
        self.calls.append({"trace_id": trace.trace.id, "tools": tools.names, "note": note, "name": name})
        return self.proposal


def acme_fixtures(*, with_since: bool = True) -> list[tuple[str, str, str]]:
    """Fixtures ``good_invoice.py`` replays for customer ACME: (base_tool, input_json, output_json)."""
    cj = canonical_json
    search_input = {"query": "from:ACME order", "since": "1d"} if with_since else {"query": "from:ACME order"}
    return [
        ("gmail_search", cj(search_input), cj([{"id": "m1"}])),
        ("gmail_get_message", cj({"id": "m1"}), cj({"id": "m1", "lines": LINES})),
        ("billing_find_customer", cj({"name": "ACME"}), cj({"id": "c1"})),
        ("billing_create_invoice", cj({"customer_id": "c1", "lines": LINES}), cj({"id": "inv1", "status": "draft"})),
    ]


def acme_executor(name: str, args: dict[str, Any]) -> Any:
    """A live executor consistent with ``acme_fixtures``."""
    match name:
        case "gmail_search":
            return [{"id": "m1"}]
        case "gmail_get_message":
            return {"id": "m1", "lines": LINES}
        case "billing_find_customer":
            return {"id": "c1"}
        case "billing_create_invoice":
            return {"id": "inv1", "status": "draft"}
        case "billing_import_from_email":
            return {"id": "inv2", "status": "draft", "from_message": args["message_id"]}
    raise RuntimeError(f"unknown tool {name}")


def add_version(
    registry: Registry,
    code: str = GOOD_INVOICE,
    *,
    name: str = INVOICE_NAME,
    test_inputs: dict[str, Any] | None = None,
    baseline: int | None = 31,
    fixtures: list[tuple[str, str, str]] | None = None,
    avoided: dict[str, str] | None = None,
    schema: dict[str, Any] | None = None,
) -> VersionRecord:
    """Store a draft version of a tool straight into the registry (bypassing proposals)."""
    trace = registry.create_trace("Invoice ACME")
    version = registry.create_version(
        name=name,
        description="Create a draft invoice for a customer from their most recent order email.",
        input_schema_json=canonical_json(schema or INVOICE_SCHEMA),
        code=code,
        test_inputs_json=canonical_json(test_inputs if test_inputs is not None else {"customer": "ACME"}),
        source_trace_id=trace.id,
        baseline_calls=baseline,
    )
    if fixtures is not None:
        registry.replace_fixtures(name, version.version, fixtures)
    if avoided:
        registry.replace_avoided_tools(name, version.version, avoided)
    return version
