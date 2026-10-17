import json
from typing import Any

from tooldistill import distilled_tool, call


@distilled_tool(
    name="invoice_all_orders",
    description="Create one draft invoice per order email from a customer.",
    input_schema={
        "type": "object",
        "properties": {
            "customer": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["customer"],
    },
)
def invoice_all_orders(customer: str, limit: int = 2) -> list[dict[str, Any]]:
    messages = call("gmail_search", query=f"from:{customer} order")
    cust = call("billing_find_customer", name=customer)
    invoices = []
    for msg in messages[:limit]:
        order = call("gmail_get_message", id=msg["id"])
        lines = [line for line in order["lines"] if line.get("qty", 0) > 0]
        invoices.append(call("billing_create_invoice", customer_id=cust["id"], lines=lines))
    return json.loads(json.dumps(invoices))
