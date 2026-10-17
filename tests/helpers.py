"""Shared test data: a small base tool list in Anthropic format."""

from copy import deepcopy
from typing import Any

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
