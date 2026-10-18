import pytest
from helpers import (
    GOOD_INVOICE,
    INVOICE_NAME as NAME,
    TWO_CALL_SCHEMA,
    TWO_CALL_TOOL,
    FakeDistiller,
    acme_executor,
    acme_fixtures,
    add_version,
    sample_tools,
    sample_tools_with_currency,
)

from tooldistill import ToolDistill
from tooldistill.core.config import Settings
from tooldistill.core.errors import LifecycleError
from tooldistill.core.lifecycle import active_tool_defs, apply_verification, health, on_tool_updated
from tooldistill.core.models import REASON_AVOIDED_TOOL_RECOVERED, REASON_DEPENDENCY_CHANGED, Proposal, Status
from tooldistill.core.proposals import rebuild
from tooldistill.core.tools import ToolSet
from tooldistill.core.verifier import verify

AVOIDED = {"billing_import_from_email": "every call failed in the source run"}
SEARCH_ONLY = '''from tooldistill import distilled_tool, call


@distilled_tool(
    name="search_orders",
    description="Find a customer's order emails.",
    input_schema={"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]},
)
def search_orders(customer: str) -> list:
    return call("gmail_search", query=f"from:{customer} order", since="1d")
'''


def activate(registry, tools, *, code=GOOD_INVOICE, name=NAME, fixtures=None, avoided=None, schema=None):
    settings = Settings(auto_promote=True)
    version = add_version(
        registry, code, name=name, fixtures=acme_fixtures() if fixtures is None else fixtures, avoided=avoided, schema=schema
    )
    report = verify(registry, tools, version, settings)
    assert report.passed, report.verdict
    return apply_verification(registry, settings, version, report)


def events(registry, name=NAME, version=1):
    return [(e.to_status, e.reason) for e in registry.list_status_events(name, version)]


@pytest.fixture
def tools() -> ToolSet:
    return ToolSet(sample_tools())


# ------------------------------------------------------------------------ on_tool_updated


def test_breaking_dependency_change_deprecates_the_tool(registry, tools):
    activate(registry, tools)
    changed = ToolSet(sample_tools_with_currency())
    report = on_tool_updated(registry, changed, Settings(), "billing_create_invoice")
    assert report.current_hash == changed.schema_hash("billing_create_invoice")
    [action] = report.actions
    assert (action.tool_name, action.version) == (NAME, 1)
    assert (action.from_status, action.to_status, action.reason) == (Status.ACTIVE, Status.DEPRECATED, "static_failed")
    assert "missing required property 'currency'" in action.detail
    assert events(registry)[-2:] == [(Status.SUSPECT, REASON_DEPENDENCY_CHANGED), (Status.DEPRECATED, "static_failed")]
    assert active_tool_defs(registry, Settings()) == []


def test_compatible_dependency_change_reactivates_and_refreshes_the_hash(registry, tools):
    activate(registry, tools)
    relaxed = sample_tools()
    relaxed[0]["input_schema"]["properties"]["label"] = {"type": "string"}  # gmail_search gained an optional field
    relaxed_tools = ToolSet(relaxed)
    report = on_tool_updated(registry, relaxed_tools, Settings(), "gmail_search")
    [action] = report.actions
    assert (action.from_status, action.to_status, action.reason) == (Status.ACTIVE, Status.ACTIVE, "reverified")
    assert action.detail == "verified: 4 base tool calls vs baseline 31"
    assert events(registry)[-2:] == [(Status.SUSPECT, REASON_DEPENDENCY_CHANGED), (Status.ACTIVE, "reverified")]
    stored = {d.base_tool: d.schema_hash for d in registry.list_dependencies(NAME, 1)}
    assert stored["gmail_search"] == relaxed_tools.schema_hash("gmail_search")
    assert on_tool_updated(registry, relaxed_tools, Settings(), "gmail_search").actions == []  # hashes now match


def test_unchanged_or_unrelated_tools_do_nothing(registry, tools):
    activate(registry, tools)
    assert on_tool_updated(registry, tools, Settings(), "billing_create_invoice").actions == []
    assert on_tool_updated(registry, tools, Settings(), "billing_import_from_email").actions == []
    report = on_tool_updated(registry, tools, Settings(), "unknown_tool")
    assert report.current_hash is None and report.actions == []
    assert registry.get_version(NAME, 1).status is Status.ACTIVE
    assert events(registry) == [(Status.DRAFT, "pending_verification"), (Status.VERIFIED, "checks_passed"), (Status.ACTIVE, "auto_promote")]


def test_removed_tool_deprecates_its_dependents(registry, tools):
    activate(registry, tools)
    without = ToolSet([t for t in sample_tools() if t["name"] != "billing_create_invoice"])
    report = on_tool_updated(registry, without, Settings(), "billing_create_invoice")
    assert report.current_hash is None
    assert report.actions[0].to_status is Status.DEPRECATED and "no such base tool" in report.actions[0].detail


def test_avoided_tool_recovery_queues_a_rebuild_and_keeps_the_tool_exposed(registry, tools):
    activate(registry, tools, avoided=AVOIDED)
    report = on_tool_updated(registry, tools, Settings(), "billing_import_from_email")
    [action] = report.actions
    assert (action.from_status, action.to_status, action.reason, action.detail) == (
        Status.ACTIVE, Status.SUSPECT, REASON_AVOIDED_TOOL_RECOVERED, "queued for rebuild",
    )
    version = registry.get_version(NAME, 1)
    assert (version.status, version.status_reason) == (Status.SUSPECT, REASON_AVOIDED_TOOL_RECOVERED)
    assert [t["name"] for t in active_tool_defs(registry, Settings())] == [NAME]
    queue = registry.list_rebuild_queue()
    assert [(i.tool_name, i.version, i.base_tool) for i in queue] == [(NAME, 1, "billing_import_from_email")]
    # a repeated notice changes nothing
    assert on_tool_updated(registry, tools, Settings(), "billing_import_from_email").actions == []
    assert len(registry.list_rebuild_queue()) == 1


def test_dependency_change_on_an_exposed_suspect_version(registry, tools):
    activate(registry, tools, avoided=AVOIDED)
    on_tool_updated(registry, tools, Settings(), "billing_import_from_email")
    report = on_tool_updated(registry, ToolSet(sample_tools_with_currency()), Settings(), "billing_create_invoice")
    [action] = report.actions
    assert (action.from_status, action.to_status) == (Status.SUSPECT, Status.DEPRECATED)
    assert events(registry)[-3:] == [
        (Status.SUSPECT, REASON_AVOIDED_TOOL_RECOVERED),
        (Status.SUSPECT, REASON_DEPENDENCY_CHANGED),
        (Status.DEPRECATED, "static_failed"),
    ]


# ------------------------------------------------------------------------------- health


def test_health_detects_schema_changes_and_missing_tools(registry, tools):
    activate(registry, tools)
    activate(registry, tools, code=SEARCH_ONLY, name="search_orders", schema=TWO_CALL_SCHEMA, fixtures=acme_fixtures()[:1])
    report = health(registry, ToolSet(sample_tools_with_currency()), Settings(), schemas_only=True)
    assert report.changed == ["billing_create_invoice"] and report.missing == []
    assert report.recovered == [] and report.down == []
    assert [(a.tool_name, a.to_status) for a in report.actions] == [(NAME, Status.DEPRECATED)]
    assert registry.get_version("search_orders", 1).status is Status.ACTIVE
    without_search = ToolSet([t for t in sample_tools() if t["name"] != "gmail_search"])
    report = health(registry, without_search, Settings(), schemas_only=True)
    assert report.changed == ["gmail_search"] and report.missing == ["gmail_search"]
    assert registry.get_version("search_orders", 1).status is Status.DEPRECATED
    assert health(registry, tools, Settings()).actions == []  # nothing exposed any more


def test_health_probe_reports_outages_and_queues_recoveries(registry, tools):
    activate(registry, tools, avoided=AVOIDED)

    def partial_outage(name: str) -> bool:
        return name not in ("gmail_search", "billing_import_from_email")

    report = health(registry, tools, Settings(), health_probe=partial_outage)
    assert report.changed == [] and report.recovered == [] and report.down == ["gmail_search"] and report.actions == []
    assert registry.get_version(NAME, 1).status is Status.ACTIVE  # a dependency outage is reported, not acted on

    report = health(registry, tools, Settings(), health_probe=lambda name: True)
    assert report.recovered == ["billing_import_from_email"] and report.down == []
    assert [a.reason for a in report.actions] == [REASON_AVOIDED_TOOL_RECOVERED]
    assert len(registry.list_rebuild_queue()) == 1

    probed: list[str] = []

    def broken_probe(name: str) -> bool:
        probed.append(name)
        raise RuntimeError("probe broken")

    quiet = health(registry, tools, Settings(), health_probe=broken_probe, schemas_only=True)
    assert probed == [] and quiet.recovered == [] and quiet.down == []
    loud = health(registry, tools, Settings(), health_probe=broken_probe)
    assert loud.recovered == [] and loud.down == ["billing_create_invoice", "billing_find_customer", "gmail_get_message", "gmail_search"]


def test_startup_check_catches_a_changed_definition(store):
    first = ToolDistill(store, acme_executor, sample_tools(), fallback_distiller=False)
    activate(first.registry, first.tools)
    assert first.startup_report is not None and first.startup_report.changed == []
    first.close()

    quiet = ToolDistill(store, acme_executor, sample_tools_with_currency(), fallback_distiller=False, check_schemas_on_start=False)
    assert quiet.startup_report is None and quiet.registry.get_version(NAME, 1).status is Status.ACTIVE
    quiet.close()

    checked = ToolDistill(store, acme_executor, sample_tools_with_currency(), fallback_distiller=False)
    assert checked.startup_report is not None and checked.startup_report.changed == ["billing_create_invoice"]
    [action] = checked.startup_report.actions
    assert action.to_status is Status.DEPRECATED and "missing required property 'currency'" in action.detail
    assert checked.active_tools() == []
    checked.close()


# ------------------------------------------------------------------------------ rebuild


def test_rebuild_flow(registry, tools):
    activate(registry, tools, avoided=AVOIDED)
    with pytest.raises(LifecycleError, match="no open rebuild item"):
        rebuild(registry, tools, Settings(), NAME, distiller=FakeDistiller(None))
    on_tool_updated(registry, tools, Settings(), "billing_import_from_email")

    declined = FakeDistiller(None)
    result = rebuild(registry, tools, Settings(), NAME, distiller=declined)
    assert not result.rebuilt and result.result is None and "stays queued" in result.message
    [call] = declined.calls
    assert call["name"] == NAME and call["trace_id"] == registry.get_version(NAME, 1).source_trace_id
    assert "'billing_import_from_email' is available again" in call["note"]
    assert '"message_id"' in call["note"] and "makes 4 base tool calls" in call["note"]

    wrong_name = FakeDistiller(Proposal("other_tool", "d", TWO_CALL_SCHEMA, TWO_CALL_TOOL, {"customer": "ACME"}))
    result = rebuild(registry, tools, Settings(), NAME, distiller=wrong_name)
    assert not result.rebuilt and "proposed 'other_tool' instead of" in result.message

    settings = Settings(auto_promote=True, allow_live_verification=True)
    same_length = FakeDistiller(Proposal(NAME, "d", {"type": "object", "properties": {"customer": {"type": "string"}, "since": {"type": "string"}}, "required": ["customer"]}, GOOD_INVOICE, {"customer": "ACME"}))
    result = rebuild(registry, tools, settings, NAME, distiller=same_length, executor=acme_executor)
    assert not result.rebuilt and "not fewer than the 4 of v1 it replaces" in result.message
    assert len(registry.list_rebuild_queue()) == 1
    assert registry.get_version(NAME, 1).status is Status.SUSPECT  # still the exposed version
    assert registry.get_version(NAME, 2).status is Status.DRAFT

    shorter = FakeDistiller(Proposal(NAME, "Import the latest order email.", TWO_CALL_SCHEMA, TWO_CALL_TOOL, {"customer": "ACME"}))
    result = rebuild(registry, tools, settings, NAME, distiller=shorter, executor=acme_executor)
    assert result.rebuilt and result.message == f"rebuilt '{NAME}' as v3: verified: 2 base tool calls vs baseline 31"
    assert result.result is not None and result.result.report is not None and result.result.report.used_live
    assert registry.list_rebuild_queue() == []
    assert (registry.get_version(NAME, 3).status, registry.get_version(NAME, 3).measured_calls) == (Status.ACTIVE, 2)
    assert registry.get_version(NAME, 1).status_reason == "superseded_by_v3"
    # the decorator's description is what gets served, not the proposal's
    assert [t["description"] for t in active_tool_defs(registry, Settings())] == ["Import the latest order email as an invoice."]
    with pytest.raises(LifecycleError, match="no open rebuild item"):
        rebuild(registry, tools, settings, NAME, distiller=shorter)


def test_tooldistill_rebuild_records_the_proposal(store):
    td = ToolDistill(
        store, acme_executor, sample_tools(), fallback_distiller=False, auto_promote=True, allow_live_verification=True,
        distiller=FakeDistiller(Proposal(NAME, "Import the latest order email.", TWO_CALL_SCHEMA, TWO_CALL_TOOL, {"customer": "ACME"})),
    )
    activate(td.registry, td.tools, avoided=AVOIDED)
    assert [a.reason for a in td.on_tool_updated("billing_import_from_email").actions] == [REASON_AVOIDED_TOOL_RECOVERED]
    result = td.rebuild(NAME)
    assert result.rebuilt and td.registry.get_version(NAME, 2).status is Status.ACTIVE
    source = td.registry.get_version(NAME, 2).source_trace_id
    recorded = td.registry.list_tool_calls(source)[-1]
    assert recorded.tool_name == "propose_distilled_tool" and recorded.input["_source"] == "rebuild" and not recorded.is_error
    assert td.health().changed == [] and td.health(probe=False).recovered == []
    td.close()
