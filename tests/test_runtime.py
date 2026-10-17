import json
import sys
from pathlib import Path
from typing import Any

import pytest
from helpers import sample_tools

from tooldistill import call, distilled_tool
from tooldistill.core.errors import DistilledToolCallError, ReplayMismatchError, ToolLoadError
from tooldistill.core.models import Fixture
from tooldistill.core.runtime import (
    META_ATTR,
    CallLog,
    RunContext,
    current_context,
    load_tool,
    run_tool,
    structured_error,
)
from tooldistill.core.serialization import canonical_json as cj
from tooldistill.core.tools import ToolSet

TOOLS_DIR = Path(__file__).parent / "fixtures" / "tools"
LINES = [{"sku": "A", "qty": 2}]


def fixtures_for_acme() -> list[Fixture]:
    return [
        Fixture("t", 1, 1, "gmail_search", cj({"query": "from:ACME order", "since": "1d"}), cj([{"id": "m1"}])),
        Fixture("t", 1, 2, "gmail_get_message", cj({"id": "m1"}), cj({"id": "m1", "lines": LINES})),
        Fixture("t", 1, 3, "billing_find_customer", cj({"name": "ACME"}), cj({"id": "c1"})),
        Fixture("t", 1, 4, "billing_create_invoice", cj({"customer_id": "c1", "lines": LINES}), cj({"id": "inv1", "status": "draft"})),
    ]


class FakeExecutor:
    """Canned base tools; ``failing`` names raise, ``weird`` names return non-JSON values."""

    def __init__(self, failing: dict[str, str] | None = None, weird: set[str] | None = None) -> None:
        self.failing = failing or {}
        self.weird = weird or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        if name in self.failing:
            raise RuntimeError(self.failing[name])
        if name in self.weird:
            return object()
        match name:
            case "gmail_search":
                return ({"id": "m1"},)  # a tuple: must come back as a list
            case "gmail_get_message":
                return {"id": args["id"], "lines": LINES}
            case "billing_find_customer":
                return {"id": "c1", "name": args["name"]}
            case "billing_create_invoice":
                return {"id": "inv1", "status": "draft", "lines": args["lines"]}
        raise AssertionError(f"unexpected tool {name}")


@pytest.fixture
def tools() -> ToolSet:
    return ToolSet(sample_tools())


def write_tool(tmp_path: Path, body: str, name: str = "tool.py") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


HEADER = (
    "from tooldistill import distilled_tool, call\n\n"
    '@distilled_tool(name="demo", description="d", input_schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]})\n'
)


# ------------------------------------------------------------------ decorator / context


def test_decorator_attaches_metadata_and_returns_function():
    @distilled_tool(name="demo", description="d", input_schema={"type": "object"})
    def demo(x: str) -> str:
        return x

    meta = getattr(demo, META_ATTR)
    assert (meta.name, meta.description, meta.input_schema) == ("demo", "d", {"type": "object"})
    assert demo("ok") == "ok"
    with pytest.raises(TypeError):
        distilled_tool(name=1, description="d", input_schema={})  # type: ignore[arg-type]


def test_call_outside_a_run_raises():
    assert current_context() is None
    with pytest.raises(RuntimeError, match="inside a distilled tool run"):
        call("gmail_search", query="x")


def test_context_validation(tools):
    with pytest.raises(ValueError, match="executor"):
        RunContext(mode="live", tools=tools)
    with pytest.raises(ValueError, match="allow_live"):
        RunContext(mode="replay", tools=tools, allow_live=True)


# ------------------------------------------------------------------------- live mode


def test_live_mode_routes_to_executor_and_records(tools):
    executor = FakeExecutor()
    recorded: list[CallLog] = []
    ctx = RunContext(mode="live", tools=tools, executor=executor, recorder=recorded.append)
    tool = load_tool(TOOLS_DIR / "good_invoice.py")
    outcome = run_tool(tool, {"customer": "ACME"}, ctx)
    assert outcome.ok and outcome.failure is None
    assert outcome.result == {"id": "inv1", "status": "draft", "lines": LINES}
    assert outcome.measured_calls == 4
    assert [c.base_tool for c in outcome.calls] == ["gmail_search", "gmail_get_message", "billing_find_customer", "billing_create_invoice"]
    assert [c.step for c in outcome.calls] == [1, 2, 3, 4]
    assert all(c.source == "live" and c.error is None for c in outcome.calls)
    assert outcome.calls[0].input_json == cj({"query": "from:ACME order", "since": "1d"})
    assert outcome.calls[0].output_json == '[{"id":"m1"}]'  # the tuple came back as a JSON list
    assert recorded == outcome.calls
    assert executor.calls[0] == ("gmail_search", {"query": "from:ACME order", "since": "1d"})
    assert outcome.new_fixtures == []
    assert current_context() is None


def test_live_executor_exception_becomes_call_failure(tools):
    executor = FakeExecutor(failing={"billing_find_customer": "503 Service Unavailable"})
    recorded: list[CallLog] = []
    ctx = RunContext(mode="live", tools=tools, executor=executor, recorder=recorded.append)
    outcome = run_tool(load_tool(TOOLS_DIR / "good_invoice.py"), {"customer": "ACME"}, ctx)
    assert not outcome.ok and outcome.result is None
    failure = outcome.failure
    assert (failure.kind, failure.step, failure.base_tool, failure.message) == ("call", 3, "billing_find_customer", "503 Service Unavailable")
    assert isinstance(failure.exception, DistilledToolCallError)
    assert len(outcome.calls) == 3 and outcome.calls[-1].error == "503 Service Unavailable" and outcome.calls[-1].output_json is None
    assert recorded[-1].error == "503 Service Unavailable"
    assert current_context() is None  # context reset even after a failure


def test_live_non_json_output_is_a_call_failure(tools):
    ctx = RunContext(mode="live", tools=tools, executor=FakeExecutor(weird={"gmail_search"}))
    outcome = run_tool(load_tool(TOOLS_DIR / "good_invoice.py"), {"customer": "ACME"}, ctx)
    assert outcome.failure.kind == "call" and outcome.failure.step == 1
    assert "non-JSON-serializable" in outcome.failure.message


def test_unknown_base_tool_fails_the_call(tools, tmp_path):
    path = write_tool(tmp_path, HEADER + 'def demo(x):\n    return call("billing_find_customr", name=x)\n')
    ctx = RunContext(mode="live", tools=tools, executor=FakeExecutor())
    outcome = run_tool(load_tool(path), {"x": "ACME"}, ctx)
    assert outcome.failure.kind == "call" and outcome.failure.base_tool == "billing_find_customr"
    assert "unknown base tool" in outcome.failure.message
    assert outcome.calls[0].error.startswith("unknown base tool")


def test_bare_exception_message_falls_back_to_type_name(tools):
    class Boom(Exception):
        pass

    def executor(name, args):
        raise Boom()

    ctx = RunContext(mode="live", tools=tools, executor=executor)
    outcome = run_tool(load_tool(TOOLS_DIR / "good_invoice.py"), {"customer": "ACME"}, ctx)
    assert outcome.failure.message == "Boom"


# ----------------------------------------------------------------------- replay mode


def test_replay_answers_from_fixtures_in_order(tools):
    ctx = RunContext(mode="replay", tools=tools, fixtures=fixtures_for_acme())
    outcome = run_tool(load_tool(TOOLS_DIR / "good_with_check.py"), {"customer": "ACME"}, ctx)
    assert outcome.ok
    assert outcome.result == {"id": "inv1", "status": "draft"}
    assert [c.fixture_seq for c in outcome.calls] == [1, 2, 3, 4]
    assert all(c.source == "fixture" for c in outcome.calls)
    assert ctx.consumed == {0, 1, 2, 3}
    assert outcome.measured_calls == 4


def test_repeated_identical_calls_consume_successive_fixtures(tools, tmp_path):
    path = write_tool(
        tmp_path,
        HEADER + 'def demo(x):\n    before = call("gmail_search", query=x)\n    after = call("gmail_search", query=x)\n    return [before, after]\n',
    )
    fixtures = [
        Fixture("t", 1, 1, "gmail_search", cj({"query": "q"}), cj(["first"])),
        Fixture("t", 1, 2, "gmail_search", cj({"query": "q"}), cj(["second"])),
    ]
    ctx = RunContext(mode="replay", tools=tools, fixtures=fixtures)
    outcome = run_tool(load_tool(path), {"x": "q"}, ctx)
    assert outcome.ok and outcome.result == [["first"], ["second"]]
    assert [c.fixture_seq for c in outcome.calls] == [1, 2]


def test_replay_mismatch_names_call_args_and_nearest_fixture(tools, tmp_path):
    path = write_tool(tmp_path, HEADER + 'def demo(x):\n    return call("gmail_search", query=x, since="7d")\n')
    fixtures = [
        Fixture("t", 1, 1, "gmail_search", cj({"query": "other", "since": "7d"}), cj([])),
        Fixture("t", 1, 2, "gmail_search", cj({"query": "q"}), cj([])),
    ]
    ctx = RunContext(mode="replay", tools=tools, fixtures=fixtures)
    outcome = run_tool(load_tool(path), {"x": "q"}, ctx)
    failure = outcome.failure
    assert failure.kind == "replay" and failure.step == 1 and failure.base_tool == "gmail_search"
    exc = failure.exception
    assert isinstance(exc, ReplayMismatchError)
    assert exc.tool_has_fixtures and exc.nearest_seq in (1, 2)
    assert "step 1: call to 'gmail_search' with " + cj({"query": "q", "since": "7d"}) in failure.message
    assert "matched no fixture; nearest fixture for 'gmail_search' (seq" in failure.message
    assert outcome.calls[0].error == failure.message


def test_replay_mismatch_prefers_fixture_with_fewest_differing_keys(tools, tmp_path):
    path = write_tool(tmp_path, HEADER + 'def demo(x):\n    return call("gmail_search", query=x, since="1d")\n')
    fixtures = [
        Fixture("t", 1, 1, "gmail_search", cj({"query": "zzz", "since": "9d"}), cj([])),  # 2 keys differ
        Fixture("t", 1, 2, "gmail_search", cj({"query": "q", "since": "9d"}), cj([])),  # 1 key differs
    ]
    ctx = RunContext(mode="replay", tools=tools, fixtures=fixtures)
    outcome = run_tool(load_tool(path), {"x": "q"}, ctx)
    assert outcome.failure.exception.nearest_seq == 2
    assert "(seq 2) has input " + cj({"query": "q", "since": "9d"}) in outcome.failure.message


def test_replay_mismatch_reports_consumed_fixture_and_missing_tool(tools, tmp_path):
    path = write_tool(
        tmp_path,
        HEADER + 'def demo(x):\n    call("gmail_search", query=x)\n    return call("gmail_search", query=x)\n',
    )
    ctx = RunContext(mode="replay", tools=tools, fixtures=[Fixture("t", 1, 1, "gmail_search", cj({"query": "q"}), cj([]))])
    outcome = run_tool(load_tool(path), {"x": "q"}, ctx)
    assert outcome.failure.step == 2 and "already used by an earlier call" in outcome.failure.message

    ctx = RunContext(mode="replay", tools=tools, fixtures=[])
    outcome = run_tool(load_tool(path), {"x": "q"}, ctx)
    assert "recorded no successful calls to 'gmail_search'" in outcome.failure.message
    assert outcome.failure.exception.tool_has_fixtures is False and outcome.failure.exception.nearest_seq is None


def test_replay_live_fallback_records_new_fixtures(tools):
    fixtures = fixtures_for_acme()[:2]  # customer lookup and invoice creation are not recorded
    executor = FakeExecutor()
    ctx = RunContext(mode="replay", tools=tools, fixtures=fixtures, allow_live=True, executor=executor)
    outcome = run_tool(load_tool(TOOLS_DIR / "good_invoice.py"), {"customer": "ACME"}, ctx)
    assert outcome.ok
    assert [c.source for c in outcome.calls] == ["fixture", "fixture", "live", "live"]
    assert [name for name, _ in executor.calls] == ["billing_find_customer", "billing_create_invoice"]
    assert outcome.new_fixtures == [
        ("billing_find_customer", cj({"name": "ACME"}), cj({"id": "c1", "name": "ACME"})),
        ("billing_create_invoice", cj({"customer_id": "c1", "lines": LINES}), cj({"id": "inv1", "lines": LINES, "status": "draft"})),
    ]


def test_replay_live_fallback_failure_is_a_call_failure(tools):
    executor = FakeExecutor(failing={"billing_find_customer": "boom"})
    ctx = RunContext(mode="replay", tools=tools, fixtures=fixtures_for_acme()[:2], allow_live=True, executor=executor)
    outcome = run_tool(load_tool(TOOLS_DIR / "good_invoice.py"), {"customer": "ACME"}, ctx)
    assert outcome.failure.kind == "call" and outcome.failure.step == 3 and outcome.new_fixtures == []


# ------------------------------------------------------------- failure classification


def test_code_failure_between_calls(tools, tmp_path):
    path = write_tool(tmp_path, HEADER + 'def demo(x):\n    msgs = call("gmail_search", query=x)\n    return msgs[5]["id"]\n')
    ctx = RunContext(mode="live", tools=tools, executor=FakeExecutor())
    outcome = run_tool(load_tool(path), {"x": "q"}, ctx)
    assert outcome.failure.kind == "code" and outcome.failure.step == 1 and outcome.failure.base_tool is None
    assert outcome.failure.message.startswith("IndexError")


def test_wrong_parameter_name_is_a_code_failure(tools):
    ctx = RunContext(mode="live", tools=tools, executor=FakeExecutor())
    outcome = run_tool(load_tool(TOOLS_DIR / "good_invoice.py"), {"client": "ACME"}, ctx)
    assert outcome.failure.kind == "code" and outcome.failure.step == 0
    assert "TypeError" in outcome.failure.message and "client" in outcome.failure.message


def test_check_false_and_check_raising(tools, tmp_path):
    ctx = RunContext(mode="live", tools=tools, executor=FakeExecutor())
    fixtures = fixtures_for_acme()
    fixtures[3] = Fixture("t", 1, 4, "billing_create_invoice", fixtures[3].input_json, cj({"id": "inv1", "status": "void"}))
    outcome = run_tool(load_tool(TOOLS_DIR / "good_with_check.py"), {"customer": "ACME"}, RunContext(mode="replay", tools=tools, fixtures=fixtures))
    assert outcome.failure.kind == "check" and outcome.failure.step == 4
    assert outcome.failure.message == "check(result) returned False" and outcome.result is None

    path = write_tool(tmp_path, HEADER + 'def demo(x):\n    return call("gmail_search", query=x)\n\n\ndef check(result):\n    return result["nope"]\n')
    outcome = run_tool(load_tool(path), {"x": "q"}, ctx)
    assert outcome.failure.kind == "check" and outcome.failure.step == 1
    assert outcome.failure.message.startswith("check(result) raised TypeError")


def test_non_json_return_value_is_a_code_failure(tools, tmp_path):
    path = write_tool(tmp_path, HEADER + 'def demo(x):\n    return {x}\n')
    ctx = RunContext(mode="live", tools=tools, executor=FakeExecutor())
    outcome = run_tool(load_tool(path), {"x": "q"}, ctx)
    assert outcome.failure.kind == "code" and "non-JSON-serializable" in outcome.failure.message


def test_non_json_call_arguments_are_a_code_failure(tools, tmp_path):
    path = write_tool(tmp_path, HEADER + 'def demo(x):\n    return call("gmail_search", query={x})\n')
    ctx = RunContext(mode="live", tools=tools, executor=FakeExecutor())
    outcome = run_tool(load_tool(path), {"x": "q"}, ctx)
    assert outcome.failure.kind == "code" and "JSON-serializable" in outcome.failure.message
    assert outcome.calls == []


# ---------------------------------------------------------------------------- loading


def test_load_tool_rejects_bad_modules(tmp_path):
    with pytest.raises(ToolLoadError, match="found 0"):
        load_tool(write_tool(tmp_path, "from tooldistill import call\n", "none.py"))
    two = HEADER + "def a(x):\n    return x\n\n" + HEADER + "def b(x):\n    return x\n"
    with pytest.raises(ToolLoadError, match="found 2"):
        load_tool(write_tool(tmp_path, two, "two.py"))
    with pytest.raises(ToolLoadError, match="'check' must be a function"):
        load_tool(write_tool(tmp_path, HEADER + "def demo(x):\n    return x\n\ncheck = 3\n", "badcheck.py"))
    with pytest.raises(ToolLoadError, match="importing .* failed: ZeroDivisionError"):
        load_tool(write_tool(tmp_path, HEADER + "def demo(x):\n    return x\n\n1 / 0\n", "boom.py"))
    with pytest.raises(ToolLoadError):
        load_tool(tmp_path / "missing.py")


def test_load_tool_exposes_metadata_and_keeps_sys_modules_clean():
    before = set(sys.modules)
    tool = load_tool(TOOLS_DIR / "good_with_check.py")
    assert tool.meta.name == "create_invoice_from_email"
    assert tool.meta.input_schema["required"] == ["customer"]
    assert tool.check is not None and tool.check({"status": "draft"}) is True
    assert tool.module.__name__ == "tooldistill_tools.tools.good_with_check"
    assert set(sys.modules) == before


# ---------------------------------------------------------------------- error text


def test_structured_error_formats():
    from tooldistill.core.runtime import ToolRunFailure

    call_failure = ToolRunFailure("call", 4, "billing_create_invoice", "missing required property 'currency'")
    assert structured_error("create_invoice_from_email", 1, call_failure, 4) == (
        "Distilled tool 'create_invoice_from_email' (v1) failed at step 4 of 4.\n"
        "Base tool: billing_create_invoice\n"
        "Error: missing required property 'currency'\n"
        "This distilled tool has been disabled. Complete the task using the base tools directly."
    )
    code_failure = ToolRunFailure("code", 2, None, "KeyError: 'lines'")
    text = structured_error("create_invoice_from_email", 2, code_failure, 4)
    assert text.startswith("Distilled tool 'create_invoice_from_email' (v2) failed after step 2 of 4.\nError: KeyError: 'lines'\n")
    assert "Base tool:" not in text
    check_failure = ToolRunFailure("check", 4, None, "check(result) returned False")
    assert structured_error("x", 3, check_failure, None).startswith("Distilled tool 'x' (v3) failed its check after step 4 of ?.")


def test_result_json_round_trip_matches_between_modes(tools):
    live = run_tool(load_tool(TOOLS_DIR / "good_invoice.py"), {"customer": "ACME"}, RunContext(mode="live", tools=tools, executor=FakeExecutor()))
    fixtures = [(c.base_tool, c.input_json, c.output_json) for c in live.calls]
    replay_fixtures = [Fixture("t", 1, i + 1, *f) for i, f in enumerate(fixtures)]
    replay = run_tool(load_tool(TOOLS_DIR / "good_invoice.py"), {"customer": "ACME"}, RunContext(mode="replay", tools=tools, fixtures=replay_fixtures))
    assert replay.ok and replay.result == live.result
    assert json.dumps(replay.result) == json.dumps(live.result)
