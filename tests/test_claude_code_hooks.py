import json
from pathlib import Path
from typing import Any

import pytest
from helpers import GOOD_INVOICE, INVOICE_NAME as NAME, acme_fixtures, add_version
from typer.testing import CliRunner

from tooldistill.cli import app
from tooldistill.core.models import CallKind, Status
from tooldistill.core.registry import Registry
from tooldistill.integrations.claude_code.hooks import (
    BUILTIN_LIMIT,
    handle_hook,
    normalize_event,
    normalize_tool_response,
    resolve_config,
)
from tooldistill.integrations.claude_code.names import DEFAULT_OWN_PREFIX

SESSION = "sess-0123456789abcdef"


def payload(event: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"session_id": SESSION, "transcript_path": "/tmp/t.jsonl", "cwd": "/work", "hook_event_name": event}
    base.update(fields)
    return base


def mcp_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("TOOLDISTILL_CONFIG", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("TOOLDISTILL_HOOK_DUMP", raising=False)
    config = tmp_path / "tooldistill.toml"
    config.write_text('store = ".tooldistill"\nmax_exposed_tools = 5\n', encoding="utf-8")
    return config


def rows(project: Path, trace_id: str = SESSION):
    registry = Registry(project.parent / ".tooldistill")
    try:
        return registry.list_tool_calls(trace_id)
    finally:
        registry.close()


# ------------------------------------------------------------------------ pure helpers


def test_normalize_event_accepts_every_spelling():
    assert normalize_event("SessionStart") == "session-start"
    assert normalize_event("post_tool_use") == "post-tool-use"
    assert normalize_event("PostToolUseFailure") == "post-tool-use-failure"
    assert normalize_event("session-end") == "session-end"
    with pytest.raises(ValueError, match="unknown hook event"):
        normalize_event("PreToolUse")


def test_normalize_tool_response_shapes():
    assert normalize_tool_response(mcp_result({"a": 1})).value == {"a": 1}
    assert normalize_tool_response(json.dumps(mcp_result([1, 2]))).value == [1, 2]  # JSON-encoded result object
    assert normalize_tool_response([{"type": "text", "text": "plain"}]).value == "plain"
    assert normalize_tool_response({"content": [{"type": "text", "text": "x"}], "structuredContent": {"s": 1}}).value == {"s": 1}
    failed = normalize_tool_response(mcp_result("503 Service Unavailable", is_error=True))
    assert failed.is_error and failed.error == "503 Service Unavailable" and failed.value is None
    assert normalize_tool_response("just text").value == "just text"
    assert normalize_tool_response('"quoted"').value == "quoted"
    assert normalize_tool_response({"stdout": "hi", "stderr": ""}).value == {"stdout": "hi", "stderr": ""}  # a built-in's dict
    assert normalize_tool_response(None).value is None and normalize_tool_response(42).value == 42


def test_resolve_config_chain(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TOOLDISTILL_CONFIG", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert resolve_config(tmp_path / "x.toml", {}) == tmp_path / "x.toml"
    monkeypatch.setenv("TOOLDISTILL_CONFIG", str(tmp_path / "env.toml"))
    assert resolve_config(None, {}) == tmp_path / "env.toml"
    monkeypatch.delenv("TOOLDISTILL_CONFIG")
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "tooldistill.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    assert resolve_config(None, {"cwd": str(tmp_path)}) == project_dir / "tooldistill.toml"
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    assert resolve_config(None, {"cwd": str(project_dir)}) == project_dir / "tooldistill.toml"
    monkeypatch.chdir(tmp_path)
    assert resolve_config(None, {"cwd": str(tmp_path / "nowhere")}) is None


# --------------------------------------------------------------------------- handlers


def test_session_start_creates_the_trace_and_injects_context(project: Path):
    registry = Registry(project.parent / ".tooldistill")
    add_version(registry, GOOD_INVOICE, fixtures=acme_fixtures())
    registry.set_status(NAME, 1, Status.ACTIVE, "promoted")
    registry.close()
    result = handle_hook("SessionStart", payload("SessionStart", source="startup"), config=project)
    assert result.exit_code == 0 and result.stderr == ""
    output = json.loads(result.stdout)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert f"trace id of this session is {SESSION}" in context
    assert f"{DEFAULT_OWN_PREFIX}{NAME}: Create a draft invoice" in context
    assert "/tooldistill:distill" in context
    registry = Registry(project.parent / ".tooldistill")
    try:
        trace = registry.get_trace(SESSION)
        assert trace is not None and trace.task == "Claude Code session in /work"
        count = len(registry.list_traces())  # the session trace plus the seeded version's source trace
    finally:
        registry.close()
    again = handle_hook("session-start", payload("SessionStart", source="resume"), config=project)
    assert again.exit_code == 0 and json.loads(again.stdout)
    registry = Registry(project.parent / ".tooldistill")
    try:
        assert len(registry.list_traces()) == count  # resumed sessions reuse the trace
    finally:
        registry.close()


def test_session_start_without_tools(project: Path):
    result = handle_hook("session-start", payload("SessionStart"), config=project)
    assert "No distilled tools exist yet." in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def test_post_tool_use_records_mcp_calls(project: Path):
    hits = [{"id": "msg_1001", "from": "orders@acme.example"}]
    result = handle_hook(
        "PostToolUse",
        payload(
            "PostToolUse",
            tool_name="mcp__invoice__gmail_search",
            tool_input={"query": "from:ACME order", "since": "7d"},
            tool_response=mcp_result(hits),
            tool_use_id="toolu_1",
            duration_ms=42.7,
            mcp_server={"name": "invoice", "source": "project"},
        ),
        config=project,
    )
    assert result.exit_code == 0 and result.stdout == ""
    [call] = rows(project)
    assert call.kind is CallKind.BASE and call.tool_name == "invoice__gmail_search" and call.parent_call_id is None
    assert call.input == {"query": "from:ACME order", "since": "7d"} and call.output == hits
    assert call.latency_ms == 42 and not call.is_error
    registry = Registry(project.parent / ".tooldistill")
    try:
        assert registry.get_trace(SESSION) is not None  # created on the fly without a SessionStart
    finally:
        registry.close()


def test_post_tool_use_records_builtins_truncated(project: Path):
    big = "x" * 5000
    handle_hook("PostToolUse", payload("PostToolUse", tool_name="Bash", tool_input={"command": big}, tool_response={"stdout": big, "stderr": ""}), config=project)
    handle_hook("PostToolUse", payload("PostToolUse", tool_name="Read", tool_input={"file_path": "a.py"}, tool_response="short"), config=project)
    bash, read = rows(project)
    assert bash.tool_name == "builtin__Bash" and "_truncated" in bash.input and len(bash.input_json) < BUILTIN_LIMIT + 50
    assert "_truncated" in bash.output and len(bash.output_json) < BUILTIN_LIMIT + 50
    assert read.tool_name == "builtin__Read" and read.input == {"file_path": "a.py"} and read.output == "short"


def test_post_tool_use_skips_the_plugins_own_tools(project: Path):
    own = DEFAULT_OWN_PREFIX + "create_invoice_from_email"
    result = handle_hook("PostToolUse", payload("PostToolUse", tool_name=own, tool_input={"customer": "ACME", "trace_id": SESSION}, tool_response="{}"), config=project)
    assert result.exit_code == 0 and rows(project) == []
    propose = DEFAULT_OWN_PREFIX + "propose_distilled_tool"
    mismatch = handle_hook("PostToolUse", payload("PostToolUse", tool_name=propose, tool_input={"trace_id": "other"}, tool_response="verified"), config=project)
    assert mismatch.exit_code == 0 and "trace_id 'other' in session" in mismatch.stderr and rows(project) == []


def test_error_results_and_failures_are_recorded_as_errors(project: Path):
    handle_hook(
        "PostToolUse",
        payload("PostToolUse", tool_name="mcp__invoice__billing_import_from_email", tool_input={"message_id": "msg_1001"}, tool_response=mcp_result("503 Service Unavailable", is_error=True), duration_ms=7),
        config=project,
    )
    handle_hook(
        "PostToolUseFailure",
        payload("PostToolUseFailure", tool_name="mcp__invoice__billing_find_customer", tool_input={"name": "Nobody"}, error="no customer named 'Nobody'", is_interrupt=False, duration_ms=3),
        config=project,
    )
    handle_hook("PostToolUseFailure", payload("PostToolUseFailure", tool_name="Bash", tool_input={"command": "x"}, is_interrupt=True), config=project)
    first, second, third = rows(project)
    assert first.is_error and first.error == "503 Service Unavailable" and first.output_json is None and first.latency_ms == 7
    assert second.is_error and second.error == "no customer named 'Nobody'" and second.tool_name == "invoice__billing_find_customer"
    assert third.is_error and third.error == "interrupted" and third.tool_name == "builtin__Bash"
    own = DEFAULT_OWN_PREFIX + "propose_distilled_tool"
    assert handle_hook("PostToolUseFailure", payload("PostToolUseFailure", tool_name=own, error="x"), config=project).exit_code == 0
    assert len(rows(project)) == 3


def test_session_end_finishes_the_trace(project: Path):
    assert handle_hook("SessionEnd", payload("SessionEnd", reason="exit"), config=project).exit_code == 0  # unknown session: no-op
    handle_hook("session-start", payload("SessionStart"), config=project)
    assert handle_hook("session-end", payload("SessionEnd", reason="exit"), config=project).exit_code == 0
    registry = Registry(project.parent / ".tooldistill")
    try:
        assert registry.get_trace(SESSION).finished_at is not None
    finally:
        registry.close()


def test_projects_without_tooldistill_are_ignored(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TOOLDISTILL_CONFIG", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    result = handle_hook("session-start", payload("SessionStart", cwd=str(tmp_path)))
    assert result == handle_hook("post-tool-use", payload("PostToolUse", tool_name="Bash", cwd=str(tmp_path)))
    assert result.exit_code == 0 and result.stdout == "" and result.stderr == ""
    assert not (tmp_path / ".tooldistill").exists()


def test_errors_never_block(project: Path, tmp_path: Path):
    unknown = handle_hook("PreToolUse", payload("PreToolUse"), config=project)
    assert unknown.exit_code == 1 and "unknown hook event" in unknown.stderr
    missing = handle_hook("session-start", payload("SessionStart"), config=tmp_path / "nope.toml")
    assert missing.exit_code == 1 and "config file not found" in missing.stderr
    project.write_text("store = \n", encoding="utf-8")
    broken = handle_hook("session-start", payload("SessionStart"), config=project)
    assert broken.exit_code == 1 and broken.stderr.startswith("tooldistill hook session-start: ConfigError")
    project.write_text('store = ".tooldistill"\n', encoding="utf-8")
    no_session = handle_hook("post-tool-use", {"tool_name": "Bash"}, config=project)
    assert no_session.exit_code == 1 and "no session_id" in no_session.stderr


def test_hook_dump_captures_payloads(project: Path, tmp_path: Path, monkeypatch):
    dump = tmp_path / "hooks.jsonl"
    monkeypatch.setenv("TOOLDISTILL_HOOK_DUMP", str(dump))
    handle_hook("session-start", payload("SessionStart"), config=project)
    handle_hook("post-tool-use", payload("PostToolUse", tool_name="Bash", tool_input={}, tool_response="x"), config=project)
    lines = [json.loads(line) for line in dump.read_text(encoding="utf-8").splitlines()]
    assert [line["event"] for line in lines] == ["session-start", "post-tool-use"] and lines[1]["payload"]["tool_name"] == "Bash"


def test_hook_cli_reads_stdin(project: Path):
    runner = CliRunner()
    result = runner.invoke(app, ["hook", "session-start", "--config", str(project)], input=json.dumps(payload("SessionStart")))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    quiet = runner.invoke(app, ["hook", "post-tool-use", "--config", str(project)], input=json.dumps(payload("PostToolUse", tool_name="Bash", tool_input={}, tool_response="x")))
    assert quiet.exit_code == 0 and quiet.stdout == ""
    bad = runner.invoke(app, ["hook", "post-tool-use", "--config", str(project)], input="not json")
    assert bad.exit_code == 1 and "invalid hook JSON" in bad.output
    unknown = runner.invoke(app, ["hook", "nope", "--config", str(project)], input="{}")
    assert unknown.exit_code == 1 and "unknown hook event" in unknown.output
    empty = runner.invoke(app, ["hook", "session-end", "--config", str(project)], input="")
    assert empty.exit_code == 1 and "no session_id" not in empty.output or empty.exit_code == 0
