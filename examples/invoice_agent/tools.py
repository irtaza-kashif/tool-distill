"""Fake base tools for the invoice demo: no real services, everything in memory, deterministic and fast.

Switches (environment variables, read at call time; ``set_switch()`` overrides them in-process):

  INVOICE_IMPORT_OUTAGE=1    billing_import_from_email raises "503 Service Unavailable"
  INVOICE_SCHEMA_CURRENCY=1  the billing API now requires a ``currency`` field (what the executor enforces)
  INVOICE_TOOLS_UPDATED=1    the tool definitions in TOOLS include the required ``currency`` field

Keeping the API change (INVOICE_SCHEMA_CURRENCY) separate from the definition change (INVOICE_TOOLS_UPDATED)
reproduces an API that changed before its tool definition did.
"""

import os
from copy import deepcopy
from typing import Any

__all__ = [
    "CUSTOMERS",
    "MESSAGES",
    "TOOLS",
    "ToolError",
    "current_tools",
    "customer_in_task",
    "execute_tool",
    "probe",
    "reset",
    "set_switch",
    "switch",
    "task_for",
    "task_succeeded",
    "tool_definitions",
]


class ToolError(Exception):
    """Raised by a fake base tool to signal failure: the executor contract."""


# ------------------------------------------------------------------------------- switches

_SWITCH_ENV = {
    "import_outage": "INVOICE_IMPORT_OUTAGE",
    "schema_currency": "INVOICE_SCHEMA_CURRENCY",
    "tools_updated": "INVOICE_TOOLS_UPDATED",
}
_overrides: dict[str, bool] = {}


def switch(name: str) -> bool:
    if name not in _SWITCH_ENV:
        raise KeyError(name)
    if name in _overrides:
        return _overrides[name]
    return os.environ.get(_SWITCH_ENV[name], "").strip().lower() in ("1", "true", "yes", "on")


def set_switch(name: str, value: bool | None) -> None:
    """Override a switch in-process (``None`` goes back to the environment variable)."""
    if name not in _SWITCH_ENV:
        raise KeyError(name)
    if value is None:
        _overrides.pop(name, None)
    else:
        _overrides[name] = bool(value)


# ----------------------------------------------------------------------------- fake world

CUSTOMERS: list[dict[str, Any]] = [
    {"id": "cust_acme", "name": "ACME", "email": "orders@acme.example", "currency": "USD"},
    {"id": "cust_globex", "name": "Globex", "email": "purchasing@globex.example", "currency": "USD"},
    {"id": "cust_initech", "name": "Initech", "email": "ap@initech.example", "currency": "USD"},
]

MESSAGES: list[dict[str, Any]] = [
    {
        "id": "msg_1001",
        "from": "orders@acme.example",
        "subject": "Order #4471",
        "date": "2026-10-14",
        "body": "Please invoice the attached purchase order. Lines: 3x WIDGET-4, 1x SHIP-STD. All prices in USD.",
        "lines": [
            {"sku": "WIDGET-4", "description": "Widget, 4-pack", "qty": 3, "unit_price": 19.5},
            {"sku": "SHIP-STD", "description": "Standard shipping", "qty": 1, "unit_price": 8.0},
        ],
        "attachments": [{"id": "att_1", "filename": "PO-4471.pdf"}],
    },
    {
        "id": "msg_1002",
        "from": "orders@acme.example",
        "subject": "Re: delivery window",
        "date": "2026-10-09",
        "body": "Thursday afternoon works for us.",
        "lines": [],
        "attachments": [],
    },
    {
        "id": "msg_2001",
        "from": "purchasing@globex.example",
        "subject": "Order #9920 for October",
        "date": "2026-10-13",
        "body": "Order details below. Lines: 10x GADGET-1. Prices are in USD.",
        "lines": [{"sku": "GADGET-1", "description": "Gadget", "qty": 10, "unit_price": 42.0}],
        "attachments": [],
    },
    {
        "id": "msg_3001",
        "from": "ap@initech.example",
        "subject": "Order #310 (TPS reports)",
        "date": "2026-10-12",
        "body": "New order: 25x COVER-SHEET. Bill in USD as usual.",
        "lines": [{"sku": "COVER-SHEET", "description": "TPS cover sheet", "qty": 25, "unit_price": 1.2}],
        "attachments": [{"id": "att_2", "filename": "order-310.pdf"}],
    },
    {
        "id": "msg_4001",
        "from": "news@vendor.example",
        "subject": "October newsletter",
        "date": "2026-10-11",
        "body": "What's new this month.",
        "lines": [],
        "attachments": [],
    },
]

_ATTACHMENT_TEXT = {
    "att_1": "PURCHASE ORDER 4471\nACME\n3 x WIDGET-4 @ 19.50\n1 x SHIP-STD @ 8.00",
    "att_2": "ORDER 310\nInitech\n25 x COVER-SHEET @ 1.20",
}

_NOTES = {
    "cust_acme": ["Net 30.", "Prefers PDF invoices."],
    "cust_globex": ["Net 15."],
    "cust_initech": ["Asks for TPS cover sheets on every order."],
}

STATE: dict[str, Any] = {"invoices": [], "next_invoice": 1}


def reset() -> None:
    """Forget every invoice created so far (switch overrides are kept)."""
    STATE["invoices"] = []
    STATE["next_invoice"] = 1


# ----------------------------------------------------------------------------- base tools


def _customer_by_id(customer_id: str) -> dict[str, Any]:
    for customer in CUSTOMERS:
        if customer["id"] == customer_id:
            return customer
    raise ToolError(f"unknown customer '{customer_id}'")


def _message_by_id(message_id: str) -> dict[str, Any]:
    for message in MESSAGES:
        if message["id"] == message_id:
            return message
    raise ToolError(f"message '{message_id}' not found")


def _require_currency(currency: str | None) -> str:
    if switch("schema_currency") and currency is None:
        raise ToolError("missing required property 'currency'")
    return currency or "USD"


def gmail_search(query: str, since: str | None = None) -> list[dict[str, Any]]:
    """``from:<sender>`` tokens match the sender address or customer name; other words match subject or body."""
    senders: list[str] = []
    words: list[str] = []
    for token in query.split():
        if token.lower().startswith("from:"):
            senders.append(token[5:].lower())
        else:
            words.append(token.lower())
    hits = []
    for message in MESSAGES:
        sender = message["from"].lower()
        customer = next((c for c in CUSTOMERS if c["email"].lower() == sender), None)
        if not all(s in sender or (customer is not None and s in customer["name"].lower()) for s in senders):
            continue
        haystack = f"{message['subject']} {message['body']}".lower()
        if not all(word in haystack for word in words):
            continue
        hits.append({"id": message["id"], "from": message["from"], "subject": message["subject"], "date": message["date"]})
    hits.sort(key=lambda m: m["date"], reverse=True)
    return hits


def gmail_get_message(id: str) -> dict[str, Any]:
    return deepcopy(_message_by_id(id))


def gmail_get_attachment(message_id: str, attachment_id: str) -> dict[str, Any]:
    message = _message_by_id(message_id)
    for attachment in message["attachments"]:
        if attachment["id"] == attachment_id:
            return {"filename": attachment["filename"], "content_text": _ATTACHMENT_TEXT[attachment_id]}
    raise ToolError(f"attachment '{attachment_id}' not found on message '{message_id}'")


def gmail_list_labels() -> list[str]:
    return ["INBOX", "Orders", "Newsletters"]


def billing_search_customers(query: str) -> list[dict[str, Any]]:
    needle = query.lower()
    return [deepcopy(c) for c in CUSTOMERS if needle in c["name"].lower() or needle in c["email"].lower()]


def billing_find_customer(name: str) -> dict[str, Any]:
    for customer in CUSTOMERS:
        if customer["name"].lower() == name.lower():
            return deepcopy(customer)
    raise ToolError(f"no customer named '{name}'")


def billing_list_invoices(customer_id: str | None = None) -> list[dict[str, Any]]:
    return [deepcopy(i) for i in STATE["invoices"] if customer_id is None or i["customer_id"] == customer_id]


def billing_create_invoice(customer_id: str, lines: list[dict[str, Any]], currency: str | None = None) -> dict[str, Any]:
    customer = _customer_by_id(customer_id)
    currency = _require_currency(currency)
    if not lines:
        raise ToolError("an invoice needs at least one line")
    total = round(sum(float(line.get("qty", 0)) * float(line.get("unit_price", 0)) for line in lines), 2)
    invoice = {
        "id": f"inv_{STATE['next_invoice']:04d}",
        "customer_id": customer["id"],
        "status": "draft",
        "lines": deepcopy(lines),
        "total": total,
        "currency": currency,
    }
    STATE["next_invoice"] += 1
    STATE["invoices"].append(invoice)
    return deepcopy(invoice)


def billing_import_from_email(message_id: str, currency: str | None = None) -> dict[str, Any]:
    if switch("import_outage"):
        raise ToolError("503 Service Unavailable")
    message = _message_by_id(message_id)
    customer = next((c for c in CUSTOMERS if c["email"].lower() == message["from"].lower()), None)
    if customer is None:
        raise ToolError(f"no customer matches sender '{message['from']}'")
    return billing_create_invoice(customer["id"], message["lines"], currency=currency)


def crm_get_notes(customer_id: str) -> list[str]:
    _customer_by_id(customer_id)
    return list(_NOTES.get(customer_id, []))


def calendar_list_events(day: str) -> list[dict[str, Any]]:
    return []


_HANDLERS = {
    "gmail_search": gmail_search,
    "gmail_get_message": gmail_get_message,
    "gmail_get_attachment": gmail_get_attachment,
    "gmail_list_labels": gmail_list_labels,
    "billing_search_customers": billing_search_customers,
    "billing_find_customer": billing_find_customer,
    "billing_list_invoices": billing_list_invoices,
    "billing_create_invoice": billing_create_invoice,
    "billing_import_from_email": billing_import_from_email,
    "crm_get_notes": crm_get_notes,
    "calendar_list_events": calendar_list_events,
}


def execute_tool(name: str, args: dict[str, Any]) -> Any:
    """The executor: raises on failure, returns JSON-serializable values."""
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"unknown tool '{name}'")
    return handler(**args)


def probe(tool_name: str) -> bool:
    """Health probe: is the base tool reachable right now?"""
    if tool_name == "billing_import_from_email":
        return not switch("import_outage")
    return tool_name in _HANDLERS


# ---------------------------------------------------------------------- tool definitions


def tool_definitions(*, currency: bool) -> list[dict[str, Any]]:
    """Anthropic-format definitions; ``currency=True`` is the billing API after its schema change."""
    lines_schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "description": {"type": "string"},
                "qty": {"type": "number"},
                "unit_price": {"type": "number"},
            },
        },
    }
    create_props: dict[str, Any] = {"customer_id": {"type": "string"}, "lines": lines_schema}
    create_required = ["customer_id", "lines"]
    import_props: dict[str, Any] = {"message_id": {"type": "string"}}
    import_required = ["message_id"]
    if currency:
        create_props["currency"] = {"type": "string", "description": "ISO 4217 code, e.g. USD"}
        create_required.append("currency")
        import_props["currency"] = {"type": "string", "description": "ISO 4217 code, e.g. USD"}
        import_required.append("currency")
    return [
        {
            "name": "gmail_search",
            "description": "Search the mailbox. Use from:<sender or customer> plus keywords such as 'order'.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "since": {"type": "string", "description": "Relative window such as 7d"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "gmail_get_message",
            "description": "Fetch a message with its body, parsed order lines and attachment list.",
            "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        },
        {
            "name": "gmail_get_attachment",
            "description": "Fetch an attachment's text content.",
            "input_schema": {
                "type": "object",
                "properties": {"message_id": {"type": "string"}, "attachment_id": {"type": "string"}},
                "required": ["message_id", "attachment_id"],
            },
        },
        {
            "name": "gmail_list_labels",
            "description": "List mailbox labels.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "billing_search_customers",
            "description": "Fuzzy search customers by name or email.",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
        {
            "name": "billing_find_customer",
            "description": "Look up a customer by exact name.",
            "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
        {
            "name": "billing_list_invoices",
            "description": "List invoices, optionally for one customer.",
            "input_schema": {"type": "object", "properties": {"customer_id": {"type": "string"}}},
        },
        {
            "name": "billing_create_invoice",
            "description": "Create a draft invoice for a customer from order lines.",
            "input_schema": {"type": "object", "properties": create_props, "required": create_required},
        },
        {
            "name": "billing_import_from_email",
            "description": (
                "The fastest way to invoice an order email: creates the draft invoice straight from a message id, "
                "matching the customer by sender and parsing the order lines. Try this first for order emails."
            ),
            "input_schema": {"type": "object", "properties": import_props, "required": import_required},
        },
        {
            "name": "crm_get_notes",
            "description": "Account notes for a customer.",
            "input_schema": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]},
        },
        {
            "name": "calendar_list_events",
            "description": "Calendar events on a day (YYYY-MM-DD).",
            "input_schema": {"type": "object", "properties": {"day": {"type": "string"}}, "required": ["day"]},
        },
    ]


def current_tools() -> list[dict[str, Any]]:
    """The definitions matching the INVOICE_TOOLS_UPDATED switch right now."""
    return tool_definitions(currency=switch("tools_updated"))


TOOLS: list[dict[str, Any]] = current_tools()


# ------------------------------------------------------------------------------- the task


def task_for(customer: str) -> str:
    return f"Create a draft invoice for {customer} from their most recent order email."


def customer_in_task(task: str) -> dict[str, Any] | None:
    lowered = task.lower()
    return next((c for c in CUSTOMERS if c["name"].lower() in lowered), None)


def task_succeeded(task: str) -> bool:
    """The developer's own success check: a draft invoice with lines exists for the customer the task names."""
    customer = customer_in_task(task)
    if customer is None:
        return False
    return any(
        inv["customer_id"] == customer["id"] and inv["status"] == "draft" and inv["lines"] for inv in STATE["invoices"]
    )
