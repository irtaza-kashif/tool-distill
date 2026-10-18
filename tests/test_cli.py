import json
from pathlib import Path

import pytest
from helpers import GOOD_INVOICE, INVOICE_NAME as NAME, TWO_CALL_TOOL, acme_fixtures, add_version, sample_tools
from typer.testing import CliRunner

from tooldistill.cli import app
from tooldistill.core.models import CallKind, Status
from tooldistill.core.registry import Registry

runner = CliRunner()

TOOLS_MODULE = f'''
import json
from tooldistill.core.models import Proposal

TOOLS = json.loads({json.dumps(json.dumps(sample_tools()))})
LINES = [{{"sku": "A", "qty": 2}}]
TWO_CALL_SCHEMA = {{"type": "object", "properties": {{"customer": {{"type": "string"}}}}, "required": ["customer"]}}
TWO_CALL_TOOL = {TWO_CALL_TOOL!r}


def execute_tool(name, args):
    match name:
        case "gmail_search":
            return [{{"id": "m1"}}]
        case "gmail_get_message":
            return {{"id": "m1", "lines": LINES}}
        case "billing_find_customer":
            return {{"id": "c1"}}
        case "billing_create_invoice":
            return {{"id": "inv1", "status": "draft"}}
        case "billing_import_from_email":
            return {{"id": "inv2", "status": "draft", "from_message": args["message_id"]}}
    raise RuntimeError(f"unknown tool {{name}}")


def probe(name):
    return True


class FakeDistiller:
    def distill(self, trace, tools, *, note=None, name=None):
        return Proposal("{NAME}", "Import the latest order email.", TWO_CALL_SCHEMA, TWO_CALL_TOOL, {{"customer": "ACME"}})


DISTILLER = FakeDistiller()
'''


@pytest.fixture
def project(tmp_path: Path) -> Path:
    package = tmp_path / "cliapp"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "tools.py").write_text(TOOLS_MODULE, encoding="utf-8")
    config = tmp_path / "tooldistill.toml"
    config.write_text(
        'store = ".tooldistill"\nexecutor = "cliapp.tools:execute_tool"\ntools = "cliapp.tools:TOOLS"\n'
        'health_probe = "cliapp.tools:probe"\ndistiller = "cliapp.tools:DISTILLER"\n'
        "auto_promote = true\nallow_live_verification = true\nmin_calls_to_distill = 4\nfallback_distiller = false\n",
        encoding="utf-8",
    )
    return config


def invoke(*args: str):
    return runner.invoke(app, [str(a) for a in args])


def seed(config: Path, *, avoided: dict[str, str] | None = None) -> Registry:
    """A draft version straight in the store, as a proposal would leave it before verification."""
    registry = Registry(config.parent / ".tooldistill")
    add_version(registry, GOOD_INVOICE, fixtures=acme_fixtures(), avoided=avoided)
    return registry


def test_version_and_help():
    assert invoke("version").exit_code == 0
    result = invoke("--help")
    assert result.exit_code == 0
    for command in ("init", "list", "show", "traces", "promote", "deprecate", "verify", "tool-updated", "health", "rebuild"):
        assert command in result.output


def test_init_creates_store_and_sample_config(tmp_path: Path):
    config = tmp_path / "tooldistill.toml"
    store = tmp_path / "mystore"
    result = invoke("init", "--config", config, "--store", store)
    assert result.exit_code == 0 and "wrote" in result.output
    assert (store / "registry.db").is_file() and "registry.db*" in (store / ".gitignore").read_text()
    assert 'store = "' in config.read_text() and "mystore" in config.read_text()
    again = invoke("init", "--config", config, "--store", store)
    assert again.exit_code == 0 and "already exists" in again.output


def test_list_and_show_without_a_config(tmp_path: Path):
    store = tmp_path / "store"
    missing = invoke("list", "--store", store, "--config", tmp_path / "none.toml")
    assert missing.exit_code == 1 and "no store at" in missing.output
    registry = Registry(store)
    version = add_version(registry, GOOD_INVOICE, fixtures=acme_fixtures())
    registry.add_run_stat(NAME, 1, success=True, calls=4, latency_ms=3)
    registry.close()
    result = invoke("list", "--store", store, "--config", tmp_path / "none.toml")
    assert result.exit_code == 0
    assert NAME in result.output and "draft" in result.output and "pending_verification" in result.output
    filtered = invoke("list", "--store", store, "--config", tmp_path / "none.toml", "--status", "active")
    assert filtered.exit_code == 0 and "no distilled tools with status active" in filtered.output
    bad = invoke("list", "--store", store, "--config", tmp_path / "none.toml", "--status", "bogus")
    assert bad.exit_code == 1 and "unknown status 'bogus'" in bad.output
    shown = invoke("show", NAME, "--store", store, "--config", tmp_path / "none.toml")
    assert shown.exit_code == 0
    assert f"{NAME} v1  draft (pending_verification)" in shown.output
    assert "status history" in shown.output and "pending_verification" in shown.output
    assert "runs (1 total, 1 successful)" in shown.output
    assert "from tooldistill import distilled_tool, call" in shown.output
    assert version.source_trace_id in shown.output
    unknown = invoke("show", "ghost", "--store", store, "--config", tmp_path / "none.toml")
    assert unknown.exit_code == 1 and "unknown distilled tool 'ghost'" in unknown.output
    wrong = invoke("show", NAME, "--version", "9", "--store", store, "--config", tmp_path / "none.toml")
    assert wrong.exit_code == 1 and "v9" in wrong.output


def test_verify_promote_show_deprecate_flow(project: Path):
    registry = seed(project)
    registry.close()
    missing = invoke("verify", NAME, "--config", project.parent / "nope.toml")
    assert missing.exit_code == 1 and "config file not found" in missing.output

    verified = invoke("verify", NAME, "--config", project)
    assert verified.exit_code == 0, verified.output
    assert f"{NAME} v1: verified: 4 base tool calls vs baseline 31" in verified.output
    assert "status: active (auto_promote)" in verified.output  # auto_promote is on in the config

    listed = invoke("list", "--config", project)
    assert listed.exit_code == 0 and "active" in listed.output

    registry = Registry(project.parent / ".tooldistill")
    add_version(registry, GOOD_INVOICE, fixtures=acme_fixtures())  # v2 draft
    registry.close()
    promoted = invoke("promote", NAME, "--version", "2", "--config", project)
    assert promoted.exit_code == 0 and f"{NAME} v2 is now active" in promoted.output
    shown = invoke("show", NAME, "--config", project)
    # a draft is verified first; with auto_promote on in the config the verification itself activates it
    assert "v2  active (auto_promote)" in shown.output and "other versions: v1 deprecated (superseded_by_v2)" in shown.output
    assert "checks" in shown.output and "efficiency" in shown.output and "passed" in shown.output

    nothing = invoke("promote", NAME, "--config", project)
    assert nothing.exit_code == 1 and "no verified version" in nothing.output

    deprecated = invoke("deprecate", NAME, "--config", project)
    assert deprecated.exit_code == 0 and f"{NAME} v2 is now deprecated" in deprecated.output
    again = invoke("deprecate", NAME, "--version", "2", "--config", project)
    assert again.exit_code == 0 and "already deprecated" in again.output
    active = invoke("list", "--status", "active", "--config", project)
    assert "no distilled tools with status active" in active.output

    rollback = invoke("promote", NAME, "--version", "1", "--config", project)
    assert rollback.exit_code == 0 and "v1 is now active (verified: 4 base tool calls vs baseline 31)" in rollback.output


def test_tool_updated_health_and_rebuild(project: Path):
    registry = seed(project, avoided={"billing_import_from_email": "every call failed in the source run"})
    registry.close()
    assert invoke("verify", NAME, "--config", project).exit_code == 0

    noop = invoke("tool-updated", "gmail_search", "--config", project)
    assert noop.exit_code == 0 and "nothing to do" in noop.output
    unknown = invoke("tool-updated", "nope", "--config", project)
    assert unknown.exit_code == 0 and "not in the current tool list" in unknown.output

    queued = invoke("tool-updated", "billing_import_from_email", "--config", project)
    assert queued.exit_code == 0
    assert f"{NAME} v1: active -> suspect (avoided_tool_recovered) queued for rebuild" in queued.output

    healthy = invoke("health", "--config", project)
    assert healthy.exit_code == 0
    assert "changed schemas: none" in healthy.output and "recovered:       billing_import_from_email" in healthy.output
    assert "down:            none" in healthy.output
    quiet = invoke("health", "--no-probe", "--config", project)
    assert quiet.exit_code == 0 and "probe:           skipped" in quiet.output

    rebuilt = invoke("rebuild", NAME, "--config", project)
    assert rebuilt.exit_code == 0, rebuilt.output
    assert f"rebuilt '{NAME}' as v2: verified: 2 base tool calls vs baseline 31" in rebuilt.output
    shown = invoke("show", NAME, "--config", project)
    assert "v2  active (auto_promote)" in shown.output and "billing_import_from_email" in shown.output
    assert "other versions: v1 deprecated (superseded_by_v2)" in shown.output

    nothing = invoke("rebuild", NAME, "--config", project)
    assert nothing.exit_code == 1 and "no open rebuild item" in nothing.output


def test_verify_live_needs_the_setting(project: Path):
    registry = seed(project)
    registry.close()
    project.write_text(project.read_text(encoding="utf-8").replace("allow_live_verification = true", "allow_live_verification = false"), encoding="utf-8")
    result = invoke("verify", NAME, "--live", "--config", project)
    assert result.exit_code == 1 and "allow_live_verification" in result.output


def test_traces_list_and_show(project: Path):
    registry = Registry(project.parent / ".tooldistill")
    trace = registry.create_trace("Create a draft invoice for ACME from their most recent order email.")
    registry.add_tool_call(trace.id, kind=CallKind.BASE, tool_name="gmail_search", input_json='{"query":"x"}', output_json="[]", latency_ms=4)
    parent = registry.add_tool_call(trace.id, kind=CallKind.DISTILLED, tool_name=NAME, version=1, input_json='{"customer":"ACME"}', is_error=True, error="Distilled tool failed")
    registry.add_tool_call(trace.id, kind=CallKind.BASE, tool_name="billing_find_customer", input_json='{"name":"ACME"}', is_error=True, error="boom", parent_call_id=parent.id)
    registry.set_outcome(trace.id, True)
    registry.add_usage(trace.id, 100, 10)
    registry.mark_nudged(trace.id)
    registry.close()

    empty = invoke("traces", "list", "--config", project, "--store", project.parent / "elsewhere")
    assert empty.exit_code == 1 and "no store" in empty.output
    listed = invoke("traces", "list", "--config", project)
    assert listed.exit_code == 0 and trace.id in listed.output and "success" in listed.output and "yes" in listed.output
    shown = invoke("traces", "show", trace.id[:8], "--config", project)
    assert shown.exit_code == 0, shown.output
    assert f"trace {trace.id}" in shown.output and "tokens:    100 in / 10 out" in shown.output
    assert "tool calls (3)" in shown.output and "ERROR" in shown.output and "(in #" in shown.output
    missing = invoke("traces", "show", "zzz", "--config", project)
    assert missing.exit_code == 1 and "no trace 'zzz'" in missing.output


def test_read_only_commands_do_not_import_the_executor(project: Path, monkeypatch):
    registry = seed(project)
    registry.close()
    (project.parent / "cliapp" / "tools.py").write_text("raise RuntimeError('must not be imported')\n", encoding="utf-8")
    monkeypatch.delitem(__import__("sys").modules, "cliapp.tools", raising=False)
    assert invoke("list", "--config", project).exit_code == 0
    assert invoke("show", NAME, "--config", project).exit_code == 0
    assert invoke("traces", "list", "--config", project).exit_code == 0
    assert invoke("deprecate", NAME, "--config", project).exit_code == 0
