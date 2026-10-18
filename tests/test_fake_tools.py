import pytest
import tools as invoice

SWITCHES = ("import_outage", "schema_currency", "tools_updated")


@pytest.fixture(autouse=True)
def clean_world():
    invoice.reset()
    for name in SWITCHES:
        invoice.set_switch(name, None)
    yield
    invoice.reset()
    for name in SWITCHES:
        invoice.set_switch(name, None)


def test_search_and_get_message():
    hits = invoice.execute_tool("gmail_search", {"query": "from:ACME order", "since": "7d"})
    assert [h["id"] for h in hits] == ["msg_1001"]
    assert [h["id"] for h in invoice.gmail_search("from:acme")] == ["msg_1001", "msg_1002"]
    assert [h["id"] for h in invoice.gmail_search("from:orders@acme.example order")] == ["msg_1001"]
    assert invoice.gmail_search("newsletter")[0]["id"] == "msg_4001"
    assert invoice.gmail_search("from:nobody") == []
    message = invoice.execute_tool("gmail_get_message", {"id": "msg_1001"})
    assert message["lines"][0]["sku"] == "WIDGET-4" and message["attachments"][0]["id"] == "att_1"
    message["lines"].clear()
    assert invoice.MESSAGES[0]["lines"]  # callers get copies
    with pytest.raises(invoice.ToolError, match="not found"):
        invoice.execute_tool("gmail_get_message", {"id": "nope"})
    attachment = invoice.execute_tool("gmail_get_attachment", {"message_id": "msg_1001", "attachment_id": "att_1"})
    assert attachment["filename"] == "PO-4471.pdf" and "4471" in attachment["content_text"]
    with pytest.raises(invoice.ToolError, match="attachment 'att_9' not found"):
        invoice.gmail_get_attachment("msg_1001", "att_9")
    assert invoice.gmail_list_labels() == ["INBOX", "Orders", "Newsletters"]


def test_customers_and_invoices():
    assert [c["id"] for c in invoice.billing_search_customers("ACME")] == ["cust_acme"]
    assert [c["id"] for c in invoice.billing_search_customers(".example")] == ["cust_acme", "cust_globex", "cust_initech"]
    assert invoice.billing_find_customer("acme")["id"] == "cust_acme"
    with pytest.raises(invoice.ToolError, match="no customer named 'Nobody'"):
        invoice.billing_find_customer("Nobody")
    created = invoice.execute_tool("billing_create_invoice", {"customer_id": "cust_acme", "lines": invoice.MESSAGES[0]["lines"]})
    assert created["id"] == "inv_0001" and created["status"] == "draft"
    assert created["total"] == 66.5 and created["currency"] == "USD"
    assert invoice.billing_list_invoices("cust_acme")[0]["id"] == "inv_0001"
    assert invoice.billing_list_invoices("cust_globex") == [] and len(invoice.billing_list_invoices()) == 1
    with pytest.raises(invoice.ToolError, match="at least one line"):
        invoice.billing_create_invoice("cust_acme", [])
    with pytest.raises(invoice.ToolError, match="unknown customer"):
        invoice.billing_create_invoice("cust_x", [{"qty": 1, "unit_price": 1}])
    assert invoice.crm_get_notes("cust_acme") == ["Net 30.", "Prefers PDF invoices."]
    assert invoice.calendar_list_events("2026-10-17") == []
    assert invoice.task_succeeded(invoice.task_for("ACME"))
    assert not invoice.task_succeeded(invoice.task_for("Globex"))
    assert not invoice.task_succeeded("Do something unrelated")
    invoice.reset()
    assert invoice.billing_list_invoices() == [] and not invoice.task_succeeded(invoice.task_for("ACME"))


def test_import_from_email_and_outage():
    created = invoice.execute_tool("billing_import_from_email", {"message_id": "msg_2001"})
    assert created["customer_id"] == "cust_globex" and created["total"] == 420.0
    invoice.set_switch("import_outage", True)
    with pytest.raises(invoice.ToolError, match="503 Service Unavailable"):
        invoice.billing_import_from_email("msg_2001")
    assert invoice.probe("billing_import_from_email") is False
    assert invoice.probe("gmail_search") is True and invoice.probe("nope") is False
    invoice.set_switch("import_outage", None)
    assert invoice.probe("billing_import_from_email") is True
    with pytest.raises(invoice.ToolError, match="no customer matches"):
        invoice.billing_import_from_email("msg_4001")


def test_schema_switch_requires_currency(monkeypatch):
    invoice.set_switch("schema_currency", True)
    with pytest.raises(invoice.ToolError, match="missing required property 'currency'"):
        invoice.billing_create_invoice("cust_acme", invoice.MESSAGES[0]["lines"])
    created = invoice.billing_create_invoice("cust_acme", invoice.MESSAGES[0]["lines"], currency="EUR")
    assert created["currency"] == "EUR"
    with pytest.raises(invoice.ToolError, match="currency"):
        invoice.billing_import_from_email("msg_2001")
    invoice.set_switch("schema_currency", None)
    monkeypatch.setenv("INVOICE_SCHEMA_CURRENCY", "true")
    assert invoice.switch("schema_currency")
    monkeypatch.setenv("INVOICE_SCHEMA_CURRENCY", "0")
    assert not invoice.switch("schema_currency")
    with pytest.raises(KeyError):
        invoice.switch("nope")


def test_tool_definition_variants():
    stale = {t["name"]: t for t in invoice.tool_definitions(currency=False)}
    fresh = {t["name"]: t for t in invoice.tool_definitions(currency=True)}
    assert "currency" not in stale["billing_create_invoice"]["input_schema"]["properties"]
    assert fresh["billing_create_invoice"]["input_schema"]["required"] == ["customer_id", "lines", "currency"]
    assert fresh["billing_import_from_email"]["input_schema"]["required"] == ["message_id", "currency"]
    assert set(stale) == set(invoice._HANDLERS)
    invoice.set_switch("tools_updated", True)
    current = {t["name"]: t for t in invoice.current_tools()}
    assert "currency" in current["billing_create_invoice"]["input_schema"]["properties"]


def test_unknown_tool_and_bad_arguments():
    with pytest.raises(invoice.ToolError, match="unknown tool"):
        invoice.execute_tool("nope", {})
    with pytest.raises(TypeError):
        invoice.execute_tool("gmail_get_message", {"identifier": "x"})
