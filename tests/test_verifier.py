import json
from pathlib import Path
from typing import Any

import pytest
from helpers import sample_tools

from tooldistill.core.config import Settings
from tooldistill.core.models import REASON_AVOIDED_TOOL_RECOVERED, CheckName, Status, VersionRecord
from tooldistill.core.registry import Registry
from tooldistill.core.serialization import canonical_json as cj
from tooldistill.core.tools import ToolSet
from tooldistill.core.verifier import verify

TOOLS_DIR = Path(__file__).parent / "fixtures" / "tools"
LINES = [{"sku": "A", "qty": 2}]
NAME = "create_invoice_from_email"
SCHEMA = {
    "type": "object",
    "properties": {"customer": {"type": "string"}, "since": {"type": "string", "default": "1d"}},
    "required": ["customer"],
}
TWO_CALL_SCHEMA = {"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]}
GOOD = (TOOLS_DIR / "good_invoice.py").read_text(encoding="utf-8")
GOOD_CHECK = (TOOLS_DIR / "good_with_check.py").read_text(encoding="utf-8")
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


def acme_fixtures(*, with_since: bool = True) -> list[tuple[str, str, str]]:
    search_input = {"query": "from:ACME order", "since": "1d"} if with_since else {"query": "from:ACME order"}
    return [
        ("gmail_search", cj(search_input), cj([{"id": "m1"}])),
        ("gmail_get_message", cj({"id": "m1"}), cj({"id": "m1", "lines": LINES})),
        ("billing_find_customer", cj({"name": "ACME"}), cj({"id": "c1"})),
        ("billing_create_invoice", cj({"customer_id": "c1", "lines": LINES}), cj({"id": "inv1", "status": "draft"})),
    ]


def executor(name: str, args: dict[str, Any]) -> Any:
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


@pytest.fixture
def tools() -> ToolSet:
    return ToolSet(sample_tools())


def add_version(
    registry: Registry,
    code: str,
    *,
    name: str = NAME,
    test_inputs: dict[str, Any] | None = None,
    baseline: int | None = 31,
    fixtures: list[tuple[str, str, str]] | None = None,
    avoided: dict[str, str] | None = None,
    schema: dict[str, Any] | None = None,
) -> VersionRecord:
    trace = registry.create_trace("Invoice ACME")
    version = registry.create_version(
        name=name,
        description="d",
        input_schema_json=cj(schema or SCHEMA),
        code=code,
        test_inputs_json=cj(test_inputs if test_inputs is not None else {"customer": "ACME"}),
        source_trace_id=trace.id,
        baseline_calls=baseline,
    )
    if fixtures is not None:
        registry.replace_fixtures(name, version.version, fixtures)
    if avoided:
        registry.replace_avoided_tools(name, version.version, avoided)
    return version


def results(registry: Registry, version: VersionRecord) -> list[tuple[CheckName, bool, dict[str, Any]]]:
    return [(r.check, r.passed, json.loads(r.details)) for r in registry.list_check_results(version.tool_name, version.version)]


# ---------------------------------------------------------------------------- happy path


def test_good_tool_passes_all_checks(registry, tools):
    version = add_version(registry, GOOD, fixtures=acme_fixtures())
    report = verify(registry, tools, version, Settings())
    assert report.passed and report.failed_check is None
    assert report.verdict == "verified: 4 base tool calls vs baseline 31"
    assert report.measured_calls == 4
    assert report.dependencies == ("billing_create_invoice", "billing_find_customer", "gmail_get_message", "gmail_search")
    assert not report.used_live and not report.needs_live_verification and report.notes == []
    assert registry.get_version(NAME, 1).measured_calls == 4
    deps = registry.list_dependencies(NAME, 1)
    assert [d.base_tool for d in deps] == list(report.dependencies)
    assert all(d.schema_hash == tools.schema_hash(d.base_tool) for d in deps)
    recorded = results(registry, version)
    assert [(check, passed) for check, passed, _ in recorded] == [
        (CheckName.STATIC, True),
        (CheckName.REPLAY, True),
        (CheckName.EFFICIENCY, True),
    ]
    assert recorded[0][2]["dependencies"] == list(report.dependencies)
    assert recorded[1][2]["measured_calls"] == 4 and recorded[1][2]["live_calls"] == 0
    assert recorded[2][2]["tokens"] == 0 and recorded[2][2]["baseline_calls"] == 31
    assert registry.get_version(NAME, 1).status is Status.DRAFT  # the verifier never changes status


# ------------------------------------------------------------------------------- static


def test_static_failure_stops_early(registry, tools):
    bad = GOOD.replace('call("billing_find_customer"', 'call("billing_find_customr"')
    version = add_version(registry, bad, fixtures=acme_fixtures())
    report = verify(registry, tools, version, Settings())
    assert not report.passed and report.failed_check is CheckName.STATIC
    assert report.verdict.startswith("rejected (static): line ")
    assert "call to 'billing_find_customr' — no such base tool" in report.verdict
    assert [check for check, _, _ in results(registry, version)] == [CheckName.STATIC]
    assert registry.list_dependencies(NAME, 1) == []
    assert registry.get_version(NAME, 1).measured_calls is None


def test_modified_code_file_fails_static(registry, tools):
    version = add_version(registry, GOOD, fixtures=acme_fixtures())
    registry.code_file(version).write_text(GOOD + "\n# edited after verification\n", encoding="utf-8")
    report = verify(registry, tools, version, Settings())
    assert report.failed_check is CheckName.STATIC and "modified after the version was stored" in report.message


# ------------------------------------------------------------------------------- replay


def test_added_default_argument_fails_replay_with_nearest_fixture(registry, tools):
    version = add_version(registry, GOOD, fixtures=acme_fixtures(with_since=False))  # the run never sent since
    report = verify(registry, tools, version, Settings())
    assert report.failed_check is CheckName.REPLAY and not report.needs_live_verification
    assert report.message.startswith("step 1: call to 'gmail_search' with " + cj({"query": "from:ACME order", "since": "1d"}))
    assert "nearest fixture for 'gmail_search' (seq 1) has input " + cj({"query": "from:ACME order"}) in report.message
    assert report.verdict.startswith("rejected (replay): step 1")
    replay = results(registry, version)[1]
    assert replay[0] is CheckName.REPLAY and replay[2]["kind"] == "replay" and replay[2]["step"] == 1


def test_test_inputs_are_validated_against_the_schema(registry, tools):
    version = add_version(registry, GOOD, fixtures=acme_fixtures(), test_inputs={"customer": 5})
    report = verify(registry, tools, version, Settings())
    assert report.failed_check is CheckName.REPLAY
    assert report.message.startswith("test_inputs do not match input_schema: 5 is not of type 'string'")


def test_check_false_fails_replay(registry, tools):
    fixtures = acme_fixtures()
    fixtures[3] = ("billing_create_invoice", fixtures[3][1], cj({"id": "inv1", "status": "void"}))
    version = add_version(registry, GOOD_CHECK, fixtures=fixtures)
    report = verify(registry, tools, version, Settings())
    assert report.failed_check is CheckName.REPLAY
    assert report.message == "check(result) returned False after step 4"


def test_code_error_between_calls_is_reported(registry, tools):
    broken = GOOD.replace('order["lines"]', 'order["lins"]')  # raised while building the 4th call's arguments
    version = add_version(registry, broken, fixtures=acme_fixtures())
    report = verify(registry, tools, version, Settings())
    assert report.failed_check is CheckName.REPLAY
    assert report.message.startswith("the tool raised KeyError: 'lins' after step 3")


def test_uncovered_call_needs_live_verification(registry, tools):
    version = add_version(registry, TWO_CALL_TOOL, fixtures=acme_fixtures()[:1], schema=TWO_CALL_SCHEMA)
    report = verify(registry, tools, version, Settings())
    assert not report.passed and report.failed_check is CheckName.REPLAY and report.needs_live_verification
    assert report.verdict.startswith("not verified (replay): step 2: call to 'billing_import_from_email'")
    assert "recorded no successful calls to 'billing_import_from_email'" in report.verdict
    assert report.verdict.endswith("live verification is needed (allow_live_verification)")
    assert results(registry, version)[1][2]["needs_live_verification"] is True
    # the flag alone is not enough: without an executor nothing can run live
    report = verify(registry, tools, version, Settings(), allow_live=True)
    assert report.needs_live_verification


def test_live_verification_saves_fixtures(registry, tools):
    version = add_version(registry, TWO_CALL_TOOL, fixtures=acme_fixtures()[:1], schema=TWO_CALL_SCHEMA)
    report = verify(registry, tools, version, Settings(), executor=executor, allow_live=True)
    assert report.passed and report.used_live and report.measured_calls == 2
    assert report.verdict == "verified: 2 base tool calls vs baseline 31"
    fixtures = registry.list_fixtures(NAME, 1)
    assert [(f.seq, f.base_tool) for f in fixtures] == [(1, "gmail_search"), (2, "billing_import_from_email")]
    assert fixtures[1].output == {"id": "inv2", "status": "draft", "from_message": "m1"}
    assert results(registry, version)[1][2]["live_calls"] == 1
    # the saved fixture lets a later replay-only verification pass
    again = verify(registry, tools, version, Settings())
    assert again.passed and not again.used_live


def test_live_verification_failure_is_reported(registry, tools):
    def failing(name: str, args: dict[str, Any]) -> Any:
        if name == "billing_import_from_email":
            raise RuntimeError("503 Service Unavailable")
        return executor(name, args)

    version = add_version(registry, TWO_CALL_TOOL, fixtures=acme_fixtures()[:1], schema=TWO_CALL_SCHEMA)
    report = verify(registry, tools, version, Settings(), executor=failing, allow_live=True)
    assert report.failed_check is CheckName.REPLAY and not report.needs_live_verification
    assert report.message == "step 2: call to 'billing_import_from_email' failed: 503 Service Unavailable"
    assert [f.base_tool for f in registry.list_fixtures(NAME, 1)] == ["gmail_search"]  # nothing new saved


# --------------------------------------------------------------------------- efficiency


@pytest.mark.parametrize(
    "baseline,reduction,expected",
    [(5, 0.3, False), (6, 0.3, True), (4, 0.0, True), (3, 0.0, False), (31, 0.3, True)],
)
def test_efficiency_threshold(registry, tools, baseline, reduction, expected):
    version = add_version(registry, GOOD, fixtures=acme_fixtures(), baseline=baseline)
    report = verify(registry, tools, version, Settings(min_call_reduction=reduction))
    assert report.passed is expected
    if not expected:
        assert report.failed_check is CheckName.EFFICIENCY
        assert report.message.startswith(f"4 base tool calls vs baseline {baseline}: must be at most")


def test_efficiency_message_and_measured_calls_are_kept(registry, tools):
    version = add_version(registry, GOOD, fixtures=acme_fixtures(), baseline=5)
    report = verify(registry, tools, version, Settings())
    assert report.message == "4 base tool calls vs baseline 5: must be at most 3 (min_call_reduction=0.3)"
    assert report.verdict == "rejected (efficiency): " + report.message
    assert registry.get_version(NAME, 1).measured_calls == 4  # replay passed and was recorded
    assert [passed for _, passed, _ in results(registry, version)] == [True, True, False]


def test_missing_baseline_fails_efficiency(registry, tools):
    version = add_version(registry, GOOD, fixtures=acme_fixtures(), baseline=None)
    report = verify(registry, tools, version, Settings())
    assert report.failed_check is CheckName.EFFICIENCY and "no baseline" in report.message


def test_rebuild_must_beat_the_replaced_version(registry, tools):
    old = add_version(registry, GOOD, fixtures=acme_fixtures())
    registry.set_measured_calls(NAME, old.version, 4)
    old = registry.get_version(NAME, old.version)

    same = add_version(registry, GOOD, fixtures=acme_fixtures())
    report = verify(registry, tools, same, Settings(), rebuild_reason=REASON_AVOIDED_TOOL_RECOVERED, replaces=old)
    assert report.failed_check is CheckName.EFFICIENCY
    assert report.message == "rebuild uses 4 base tool calls, not fewer than the 4 of v1 it replaces: the recovered tool bought nothing"
    assert results(registry, same)[2][2]["replaces_measured_calls"] == 4

    recovered_fixture = ("billing_import_from_email", cj({"message_id": "m1"}), cj({"id": "inv2"}))
    shorter = add_version(registry, TWO_CALL_TOOL, fixtures=acme_fixtures()[:1] + [recovered_fixture], schema=TWO_CALL_SCHEMA)
    report = verify(registry, tools, shorter, Settings(), rebuild_reason=REASON_AVOIDED_TOOL_RECOVERED, replaces=old)
    assert report.passed and report.measured_calls == 2

    # without the rebuild reason the comparison does not apply
    another = add_version(registry, GOOD, fixtures=acme_fixtures())
    assert verify(registry, tools, another, Settings(), replaces=old).passed


def test_avoided_tools_drop_dependencies_and_healthy_ones_are_noted(registry, tools):
    version = add_version(
        registry,
        GOOD,
        fixtures=acme_fixtures(),
        avoided={"gmail_search": "every call failed", "billing_import_from_email": "every call failed"},
    )
    report = verify(registry, tools, version, Settings(), health_probe=lambda name: name == "billing_import_from_email")
    assert report.passed
    assert [a.base_tool for a in registry.list_avoided_tools(NAME, 1)] == ["billing_import_from_email"]
    assert report.notes == ["avoided tool 'billing_import_from_email' is currently healthy: a shorter version may exist"]
    assert results(registry, version)[2][2]["notes"] == report.notes


def test_unhealthy_or_failing_probe_adds_no_note(registry, tools):
    avoided = {"billing_import_from_email": "every call failed"}

    def broken_probe(name: str) -> bool:
        raise RuntimeError("probe down")

    first = add_version(registry, GOOD, fixtures=acme_fixtures(), avoided=avoided)
    assert verify(registry, tools, first, Settings(), health_probe=broken_probe).notes == []
    second = add_version(registry, GOOD, fixtures=acme_fixtures(), avoided=avoided)
    assert verify(registry, tools, second, Settings(), health_probe=lambda name: False).notes == []
    assert verify(registry, tools, second, Settings()).notes == []
