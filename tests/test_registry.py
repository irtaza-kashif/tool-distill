import sqlite3
from pathlib import Path

import pytest

from tooldistill.core.errors import RegistryError
from tooldistill.core.models import CallKind, CheckName, Status
from tooldistill.core.registry import SCHEMA_VERSION, Registry
from tooldistill.core.serialization import code_hash

CODE = "from tooldistill import distilled_tool, call\n"


def make_version(reg: Registry, name: str = "create_invoice", trace_id: str | None = None, **kw):
    return reg.create_version(
        name=name,
        description="desc",
        input_schema_json='{"type":"object"}',
        code=CODE,
        test_inputs_json="{}",
        source_trace_id=trace_id,
        **kw,
    )


def test_store_layout_pragmas_and_gitignore(store: Path):
    reg = Registry(store)
    try:
        assert (store / "registry.db").is_file()
        assert (store / "tools").is_dir()
        assert "registry.db*" in (store / ".gitignore").read_text()
        pragmas = reg.pragmas()
        assert pragmas["journal_mode"] == "wal"
        assert pragmas["busy_timeout"] == 5000
        assert pragmas["foreign_keys"] == 1
        assert pragmas["user_version"] == SCHEMA_VERSION
    finally:
        reg.close()


def test_reopen_keeps_data_and_does_not_touch_gitignore(store: Path):
    reg = Registry(store)
    trace = reg.create_trace("task one")
    reg.close()
    (store / ".gitignore").write_text("custom\n")
    reg = Registry(store)
    try:
        assert reg.get_trace(trace.id).task == "task one"
        assert reg.pragmas()["user_version"] == SCHEMA_VERSION
        assert (store / ".gitignore").read_text() == "custom\n"
    finally:
        reg.close()


def test_newer_schema_is_refused(store: Path):
    Registry(store).close()
    raw = sqlite3.connect(store / "registry.db")
    raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    raw.close()
    with pytest.raises(RegistryError, match="newer"):
        Registry(store)


def test_trace_fields(registry: Registry):
    t = registry.create_trace("Invoice ACME")
    assert len(t.id) == 32 and int(t.id, 16)
    assert t.outcome is None and t.finished_at is None and not t.nudged
    registry.set_outcome(t.id, True)
    registry.add_usage(t.id, 10, 5)
    registry.add_usage(t.id, 1, 1)
    registry.mark_nudged(t.id)
    registry.finish_trace(t.id)
    got = registry.get_trace(t.id)
    assert got.outcome is True
    assert (got.input_tokens, got.output_tokens) == (11, 6)
    assert got.nudged and got.finished_at
    registry.set_outcome(t.id, False)
    assert registry.get_trace(t.id).outcome is False
    assert registry.get_trace("missing") is None
    with pytest.raises(RegistryError):
        registry.require_trace("missing")
    with pytest.raises(RegistryError):
        registry.set_outcome("missing", True)


def test_list_traces_newest_first(registry: Registry):
    a = registry.create_trace("a")
    b = registry.create_trace("b")
    ids = [t.id for t in registry.list_traces()]
    assert ids.index(b.id) < ids.index(a.id)
    assert len(registry.list_traces(limit=1)) == 1


def test_tool_call_sequence_parent_and_update(registry: Registry):
    t = registry.create_trace("t")
    other = registry.create_trace("o")
    c1 = registry.add_tool_call(
        t.id, kind=CallKind.BASE, tool_name="gmail_search", input_json='{"query":"x"}', output_json="[]", latency_ms=3
    )
    parent = registry.add_tool_call(t.id, kind=CallKind.DISTILLED, tool_name="create_invoice", version=1, input_json="{}")
    child = registry.add_tool_call(
        t.id, kind=CallKind.BASE, tool_name="gmail_get_message", input_json='{"id":"m1"}', parent_call_id=parent.id
    )
    o1 = registry.add_tool_call(other.id, kind=CallKind.PROPOSE, tool_name="propose_distilled_tool", input_json="{}")
    assert (c1.seq, parent.seq, child.seq, o1.seq) == (1, 2, 3, 1)
    assert child.parent_call_id == parent.id and parent.version == 1
    registry.update_tool_call(parent.id, output_json='{"ok":true}', latency_ms=42)
    registry.update_tool_call(child.id, is_error=True, error="boom")
    calls = registry.list_tool_calls(t.id)
    assert [c.id for c in calls] == [c1.id, parent.id, child.id]
    assert calls[1].output == {"ok": True} and calls[1].latency_ms == 42
    assert calls[2].is_error and calls[2].error == "boom" and calls[2].output is None
    assert calls[0].input == {"query": "x"} and calls[0].kind is CallKind.BASE
    assert registry.get_tool_call(child.id).parent_call_id == parent.id
    data = registry.get_trace_data(t.id)
    assert data.trace.id == t.id and len(data.calls) == 3
    registry.update_tool_call(c1.id)  # nothing to change: no-op
    with pytest.raises(RegistryError):
        registry.update_tool_call(999, error="x")


def test_tool_call_needs_existing_trace(registry: Registry):
    with pytest.raises(sqlite3.IntegrityError):
        registry.add_tool_call("ghost", kind=CallKind.BASE, tool_name="x", input_json="{}")


def test_create_version_numbers_files_and_events(registry: Registry, store: Path):
    t = registry.create_trace("t")
    v1 = make_version(registry, trace_id=t.id, baseline_calls=31, baseline_tokens=1200, baseline_latency_ms=800)
    v2 = make_version(registry, trace_id=t.id)
    assert (v1.version, v2.version) == (1, 2)
    assert v1.status is Status.DRAFT and v1.status_reason == "pending_verification"
    assert v1.code_path == "tools/create_invoice/v1.py"
    assert registry.code_file(v1) == store.resolve() / "tools" / "create_invoice" / "v1.py"
    assert registry.read_code(v2) == CODE and v2.code_hash == code_hash(CODE)
    assert v1.baseline_calls == 31 and v1.baseline_tokens == 1200 and v1.measured_calls is None
    assert registry.get_tool("create_invoice").description == "desc"
    assert [tool.name for tool in registry.list_tools()] == ["create_invoice"]
    assert registry.latest_version("create_invoice").version == 2
    assert registry.active_version("create_invoice") is None
    events = registry.list_status_events("create_invoice", 1)
    assert len(events) == 1 and events[0].from_status is None and events[0].to_status is Status.DRAFT
    assert registry.get_version("create_invoice", 3) is None
    with pytest.raises(RegistryError):
        registry.require_version("create_invoice", 3)
    got = registry.get_version("create_invoice", 1)
    assert got.input_schema == {"type": "object"} and got.test_inputs == {}
    assert got.source_trace_id == t.id and got.label == "create_invoice v1"
    registry.set_tool_description("create_invoice", "new")
    assert registry.get_tool("create_invoice").description == "new"


def test_list_versions_filters(registry: Registry):
    make_version(registry, "alpha")
    make_version(registry, "beta")
    make_version(registry, "beta")
    assert [(v.tool_name, v.version) for v in registry.list_versions()] == [("alpha", 1), ("beta", 1), ("beta", 2)]
    assert len(registry.list_versions("beta")) == 2
    registry.set_status("beta", 2, Status.ACTIVE, None)
    assert [v.label for v in registry.list_versions(statuses=[Status.ACTIVE])] == ["beta v2"]
    assert registry.list_versions(statuses=[]) == []
    assert registry.active_version("beta").version == 2


def test_only_one_active_version_per_tool(registry: Registry):
    make_version(registry)
    make_version(registry)
    registry.set_status("create_invoice", 1, Status.ACTIVE, None)
    with pytest.raises(sqlite3.IntegrityError):
        registry.set_status("create_invoice", 2, Status.ACTIVE, None)
    assert registry.get_version("create_invoice", 2).status is Status.DRAFT  # rolled back
    registry.set_status("create_invoice", 1, Status.DEPRECATED, "superseded_by_v2")
    registry.set_status("create_invoice", 2, Status.ACTIVE, None)
    assert registry.get_version("create_invoice", 1).status_reason == "superseded_by_v2"
    with pytest.raises(RegistryError):
        registry.set_status("create_invoice", 9, Status.ACTIVE, None)
    registry.set_measured_calls("create_invoice", 2, 4)
    assert registry.get_version("create_invoice", 2).measured_calls == 4


def test_dependencies_avoided_and_fixtures(registry: Registry):
    make_version(registry)
    registry.replace_dependencies("create_invoice", 1, {"gmail_search": "h1", "billing_create_invoice": "h2"})
    deps = registry.list_dependencies("create_invoice", 1)
    assert [(d.base_tool, d.schema_hash) for d in deps] == [("billing_create_invoice", "h2"), ("gmail_search", "h1")]
    registry.replace_dependencies("create_invoice", 1, {"gmail_search": "h9"})
    assert [(d.base_tool, d.schema_hash) for d in registry.list_dependencies("create_invoice", 1)] == [("gmail_search", "h9")]
    assert registry.versions_depending_on("gmail_search")[0].version == 1
    assert registry.versions_depending_on("billing_create_invoice") == []
    registry.replace_avoided_tools("create_invoice", 1, {"billing_import_from_email": "every call failed"})
    assert registry.versions_avoiding("billing_import_from_email")[0].reason == "every call failed"
    assert registry.list_avoided_tools("create_invoice", 1)[0].base_tool == "billing_import_from_email"
    fx = registry.replace_fixtures(
        "create_invoice", 1, [("gmail_search", '{"query":"a"}', "[1]"), ("gmail_search", '{"query":"a"}', "[2]")]
    )
    assert [f.seq for f in fx] == [1, 2]
    more = registry.add_fixtures("create_invoice", 1, [("billing_create_invoice", '{"customer_id":"c"}', '{"id":"inv"}')])
    assert more[0].seq == 3
    stored = registry.list_fixtures("create_invoice", 1)
    assert [(f.seq, f.base_tool, f.output) for f in stored] == [
        (1, "gmail_search", [1]),
        (2, "gmail_search", [2]),
        (3, "billing_create_invoice", {"id": "inv"}),
    ]
    registry.replace_fixtures("create_invoice", 1, [])
    assert registry.list_fixtures("create_invoice", 1) == []


def test_dependency_rows_need_a_version(registry: Registry):
    with pytest.raises(sqlite3.IntegrityError):
        registry.replace_dependencies("ghost", 1, {"x": "h"})


def test_check_results_run_stats_and_events(registry: Registry):
    t = registry.create_trace("t")
    make_version(registry)
    registry.add_check_result("create_invoice", 1, CheckName.STATIC, True, "ok")
    registry.add_check_result("create_invoice", 1, CheckName.REPLAY, False, "step 3 mismatch")
    results = registry.list_check_results("create_invoice", 1)
    assert [(r.check, r.passed) for r in results] == [(CheckName.STATIC, True), (CheckName.REPLAY, False)]
    assert results[1].details == "step 3 mismatch"
    registry.add_run_stat("create_invoice", 1, success=True, calls=4, latency_ms=10, trace_id=t.id, call_id=7)
    registry.add_run_stat("create_invoice", 1, success=False, calls=2, error="boom", trace_id=t.id)
    assert registry.success_count("create_invoice", 1) == 1
    stats = registry.list_run_stats("create_invoice", 1)
    assert [s.success for s in stats] == [True, False]
    assert stats[0].call_id == 7 and stats[1].error == "boom"
    assert [s.success for s in registry.run_stats_for_trace(t.id)] == [True, False]
    assert registry.run_stats_for_trace("none") == []
    event = registry.add_status_event("create_invoice", 1, Status.DRAFT, Status.VERIFIED, "checks passed")
    assert event.from_status is Status.DRAFT and event.to_status is Status.VERIFIED
    events = registry.list_status_events("create_invoice")
    assert [(e.from_status, e.to_status) for e in events] == [(None, Status.DRAFT), (Status.DRAFT, Status.VERIFIED)]


def test_rebuild_queue_dedupes_open_items(registry: Registry):
    make_version(registry)
    item = registry.enqueue_rebuild("create_invoice", 1, "avoided_tool_recovered", "billing_import_from_email")
    assert item is not None and item.resolved_at is None
    assert registry.enqueue_rebuild("create_invoice", 1, "avoided_tool_recovered", "billing_import_from_email") is None
    assert [i.id for i in registry.list_rebuild_queue()] == [item.id]
    registry.resolve_rebuild(item.id)
    assert registry.list_rebuild_queue() == []
    assert registry.list_rebuild_queue(include_resolved=True)[0].resolved_at is not None
    again = registry.enqueue_rebuild("create_invoice", 1, "avoided_tool_recovered", "billing_import_from_email")
    assert again is not None and again.id != item.id
    assert [i.id for i in registry.list_rebuild_queue(name="create_invoice")] == [again.id]
    with pytest.raises(RegistryError):
        registry.resolve_rebuild(item.id)  # already resolved


def test_exposed_versions_rules_and_order(registry: Registry):
    for name in ("alpha", "beta", "gamma", "delta", "epsilon"):
        make_version(registry, name)
    registry.set_status("alpha", 1, Status.ACTIVE, None)
    registry.set_status("beta", 1, Status.ACTIVE, None)
    registry.set_status("gamma", 1, Status.SUSPECT, "avoided_tool_recovered")
    registry.set_status("delta", 1, Status.SUSPECT, "runtime_failure")
    # epsilon stays draft
    registry.add_run_stat("alpha", 1, success=True, calls=4, ran_at="2026-01-01T00:00:01+00:00")
    registry.add_run_stat("alpha", 1, success=True, calls=4, ran_at="2026-01-01T00:00:02+00:00")
    registry.add_run_stat("alpha", 1, success=False, calls=1, ran_at="2026-01-01T00:00:09+00:00")
    registry.add_run_stat("beta", 1, success=True, calls=4, ran_at="2026-01-01T00:00:03+00:00")
    registry.add_run_stat("beta", 1, success=True, calls=4, ran_at="2026-01-01T00:00:04+00:00")
    registry.add_run_stat("beta", 1, success=True, calls=4, ran_at="2026-01-01T00:00:05+00:00")
    exposed = registry.exposed_versions(suspect_reasons=["avoided_tool_recovered"], limit=None)
    assert [v.tool_name for v in exposed] == ["beta", "alpha", "gamma"]
    limited = registry.exposed_versions(suspect_reasons=["avoided_tool_recovered"], limit=2)
    assert [v.tool_name for v in limited] == ["beta", "alpha"]
    assert [v.tool_name for v in registry.exposed_versions(suspect_reasons=[], limit=None)] == ["beta", "alpha"]


def test_exposed_versions_tie_breaks_on_last_success(registry: Registry):
    make_version(registry, "old")
    make_version(registry, "new")
    registry.set_status("old", 1, Status.ACTIVE, None)
    registry.set_status("new", 1, Status.ACTIVE, None)
    registry.add_run_stat("old", 1, success=True, calls=4, ran_at="2026-01-01T00:00:01+00:00")
    registry.add_run_stat("new", 1, success=True, calls=4, ran_at="2026-01-02T00:00:01+00:00")
    assert [v.tool_name for v in registry.exposed_versions(suspect_reasons=[], limit=None)] == ["new", "old"]


def test_two_registries_share_one_store(store: Path):
    a = Registry(store)
    b = Registry(store)
    try:
        t = a.create_trace("shared")
        assert b.get_trace(t.id) is not None
        va = make_version(a)
        vb = make_version(b)
        assert (va.version, vb.version) == (1, 2)
        assert b.list_versions("create_invoice")[-1].version == 2
    finally:
        a.close()
        b.close()


def test_transaction_rolls_back_on_error_and_nests(registry: Registry):
    t = registry.create_trace("t")
    with pytest.raises(RuntimeError):
        with registry.transaction() as conn:
            conn.execute("UPDATE traces SET task = 'changed' WHERE id = ?", (t.id,))
            raise RuntimeError("abort")
    assert registry.get_trace(t.id).task == "t"
    with registry.transaction():
        with registry.transaction():  # nested: still one transaction
            registry.set_outcome(t.id, True)
    assert registry.get_trace(t.id).outcome is True
