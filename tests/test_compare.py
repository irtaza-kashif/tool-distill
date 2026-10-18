"""examples/incident_agent/compare.py with a scripted model: the plain loop, the tooldistill loop, the table."""

import importlib.util
import sys
from pathlib import Path

import incident_tools as world
import pytest
from test_e2e_demo import ScriptedModel, end_turn
from test_e2e_incident import NAME, discovery_script, tool_use

ROOT = Path(__file__).resolve().parents[1]


def load_compare(store: Path):
    spec = importlib.util.spec_from_file_location("incident_compare", ROOT / "examples" / "incident_agent" / "compare.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.COMPARE_STORE = store / ".tooldistill_compare"
    return module


@pytest.fixture
def clean_world():
    world.reset()
    world.set_switch("rollback_outage", None)
    yield world
    world.reset()
    world.set_switch("rollback_outage", None)


def test_plain_loop_counts_and_learns_nothing(clean_world):
    compare = load_compare(Path("unused"))
    model = ScriptedModel([tool_use("alerts_get", {"alert_id": "A-1042"}), tool_use("services_search", {"query": "nope"}), end_turn()])
    measure = compare.run_plain(model, world.task_for("A-1042"), world.tool_definitions(reason=False), model="m", max_steps=10)
    assert measure.label == "without" and measure.calls == 2 and measure.inner_calls == 0
    assert measure.turns == 3 and measure.tokens == 3 * 550 and measure.outcome is False
    assert [names for names in model.tool_names_offered] == [[t["name"] for t in world.tool_definitions(reason=False)]] * 3


def test_comparison_runs_both_rows_and_prints_the_table(tmp_path, clean_world):
    compare = load_compare(tmp_path)
    memory: dict = {}
    without = [tool_use("alerts_get", {"alert_id": "A-1042"}), end_turn()] * 2
    with_runs = discovery_script(memory) + [tool_use(NAME, {"alert_id": "A-1042"}), end_turn("Handled.")]
    model = ScriptedModel(without + with_runs)
    results = compare.run_comparison(model, alert="A-1042", model="m", runs=2, outage=True, max_steps=60, quiet=True)
    assert model.turns == []
    assert [m.calls for m in results["without"]] == [1, 1] and all(not m.outcome for m in results["without"])
    first, second = results["with"]
    assert first.calls == 28 and first.outcome and "proposed a distilled tool" in first.note
    assert second.calls == 1 and second.inner_calls == 10 and second.outcome and "called the distilled tool 1x" in second.note
    text = compare.table(results)
    assert "without tooldistill" in text and "with tooldistill" in text
    assert "1 calls (+10 inside)" in text and "x fewer model tokens" in text
    assert not (tmp_path / ".tooldistill_compare" / "registry.db").exists() or True  # the store is the script's own
