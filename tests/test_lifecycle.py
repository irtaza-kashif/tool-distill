import pytest
from helpers import (
    GOOD_INVOICE,
    INVOICE_NAME as NAME,
    acme_executor,
    acme_fixtures,
    add_version,
    sample_tools,
    sample_tools_with_currency,
)

from tooldistill.core.config import Settings
from tooldistill.core.errors import InvalidTransition, LifecycleError
from tooldistill.core.lifecycle import (
    ALLOWED_TRANSITIONS,
    active_tool_defs,
    apply_verification,
    demote,
    dependencies_changed,
    deprecate,
    exposed_versions,
    is_exposable,
    promote,
    reverify,
    runnable_version,
    transition,
)
from tooldistill.core.models import (
    REASON_AVOIDED_TOOL_RECOVERED,
    REASON_DEPENDENCY_CHANGED,
    REASON_RUNTIME_FAILURE,
    CheckName,
    Status,
)
from tooldistill.core.tools import ToolSet
from tooldistill.core.verifier import VerifyReport, verify

STATUSES = list(Status)


@pytest.fixture
def tools() -> ToolSet:
    return ToolSet(sample_tools())


def passing_report(version: int = 1) -> VerifyReport:
    return VerifyReport(NAME, version, passed=True, measured_calls=4, baseline_calls=31)


def failing_report(version: int = 1, check: CheckName = CheckName.REPLAY, *, needs_live: bool = False) -> VerifyReport:
    return VerifyReport(NAME, version, passed=False, failed_check=check, message="boom", needs_live_verification=needs_live)


def events(registry, version: int = 1) -> list[tuple[Status | None, Status, str]]:
    return [(e.from_status, e.to_status, e.reason) for e in registry.list_status_events(NAME, version)]


def status_of(registry, version: int = 1) -> tuple[Status, str | None]:
    record = registry.get_version(NAME, version)
    return record.status, record.status_reason


# ------------------------------------------------------------------------ transition table


def test_allowed_transitions_match_the_spec_table():
    assert ALLOWED_TRANSITIONS == {
        (Status.DRAFT, Status.VERIFIED),
        (Status.DRAFT, Status.DRAFT),
        (Status.DRAFT, Status.DEPRECATED),
        (Status.VERIFIED, Status.ACTIVE),
        (Status.VERIFIED, Status.DRAFT),
        (Status.VERIFIED, Status.DEPRECATED),
        (Status.ACTIVE, Status.SUSPECT),
        (Status.ACTIVE, Status.DEPRECATED),
        (Status.SUSPECT, Status.ACTIVE),
        (Status.SUSPECT, Status.SUSPECT),
        (Status.SUSPECT, Status.DEPRECATED),
        (Status.DEPRECATED, Status.ACTIVE),
    }


@pytest.mark.parametrize("to_status", STATUSES, ids=[s.value for s in STATUSES])
@pytest.mark.parametrize("from_status", STATUSES, ids=[s.value for s in STATUSES])
def test_every_pair_of_statuses(registry, from_status, to_status):
    add_version(registry)
    registry.set_status(NAME, 1, from_status, "setup")  # test setup bypasses the lifecycle on purpose
    before = len(registry.list_status_events(NAME, 1))
    if (from_status, to_status) in ALLOWED_TRANSITIONS:
        updated = transition(registry, NAME, 1, to_status, "because")
        assert (updated.status, updated.status_reason) == (to_status, "because")
        recorded = events(registry)
        assert len(recorded) == before + 1 and recorded[-1] == (from_status, to_status, "because")
    else:
        with pytest.raises(InvalidTransition, match=f"cannot move from {from_status} to {to_status}"):
            transition(registry, NAME, 1, to_status, "because")
        assert status_of(registry) == (from_status, "setup")
        assert len(registry.list_status_events(NAME, 1)) == before


def test_transition_on_unknown_version_raises(registry):
    from tooldistill.core.errors import RegistryError

    with pytest.raises(RegistryError):
        transition(registry, "ghost", 1, Status.VERIFIED, "x")


# ------------------------------------------------------------------------------ promote


def test_promote_activates_and_supersedes_active_and_suspect_versions(registry, tools):
    add_version(registry)  # v1
    add_version(registry)  # v2
    add_version(registry)  # v3
    add_version(registry)  # v4
    registry.set_status(NAME, 1, Status.ACTIVE, "promoted")
    registry.set_status(NAME, 2, Status.SUSPECT, REASON_RUNTIME_FAILURE)
    registry.set_status(NAME, 3, Status.VERIFIED, "checks_passed")
    registry.set_tool_description(NAME, "old description")
    result = promote(registry, tools, Settings(), NAME, 3)
    assert result.activated and result.version.status is Status.ACTIVE and result.report is None
    assert result.message == f"{NAME} v3 is now active"
    assert status_of(registry, 1) == (Status.DEPRECATED, "superseded_by_v3")
    assert status_of(registry, 2) == (Status.DEPRECATED, "superseded_by_v3")
    assert status_of(registry, 3) == (Status.ACTIVE, "promoted")
    assert status_of(registry, 4) == (Status.DRAFT, "pending_verification")  # drafts are left alone
    assert events(registry, 3)[-1] == (Status.VERIFIED, Status.ACTIVE, "promoted")
    assert events(registry, 1)[-1] == (Status.ACTIVE, Status.DEPRECATED, "superseded_by_v3")
    assert registry.get_tool(NAME).description.startswith("Create a draft invoice")
    assert registry.active_version(NAME).version == 3


def test_promote_without_version_takes_the_highest_verified(registry, tools):
    add_version(registry)
    add_version(registry)
    add_version(registry)
    registry.set_status(NAME, 1, Status.VERIFIED, "checks_passed")
    registry.set_status(NAME, 2, Status.VERIFIED, "checks_passed")
    result = promote(registry, tools, Settings(), NAME)
    assert result.activated and result.version.version == 2
    assert status_of(registry, 1) == (Status.VERIFIED, "checks_passed")  # an unpromoted verified version is not touched
    result = promote(registry, tools, Settings(), NAME)  # the remaining verified version is next
    assert result.activated and result.version.version == 1
    assert status_of(registry, 2) == (Status.DEPRECATED, "superseded_by_v1")
    with pytest.raises(LifecycleError, match="no verified version"):
        promote(registry, tools, Settings(), NAME)
    with pytest.raises(LifecycleError, match="unknown distilled tool"):
        promote(registry, tools, Settings(), "ghost")


def test_promote_active_version_is_a_noop(registry, tools):
    add_version(registry)
    registry.set_status(NAME, 1, Status.ACTIVE, "promoted")
    result = promote(registry, tools, Settings(), NAME, 1)
    assert not result.activated and "already active" in result.message
    assert events(registry) == [(None, Status.DRAFT, "pending_verification")]


def test_promote_draft_verifies_first(registry, tools):
    add_version(registry, fixtures=acme_fixtures())
    result = promote(registry, tools, Settings(), NAME, 1)
    assert result.activated and result.report is not None and result.report.passed
    assert result.message == f"{NAME} v1 is now active (verified: 4 base tool calls vs baseline 31)"
    assert events(registry) == [
        (None, Status.DRAFT, "pending_verification"),
        (Status.DRAFT, Status.VERIFIED, "checks_passed"),
        (Status.VERIFIED, Status.ACTIVE, "promoted"),
    ]
    add_version(registry, fixtures=acme_fixtures(with_since=False))  # v2 cannot replay
    result = promote(registry, tools, Settings(), NAME, 2)
    assert not result.activated and result.version.status is Status.DRAFT
    assert result.version.status_reason == "replay_failed"
    assert result.message.startswith(f"{NAME} v2 was not promoted (draft): rejected (replay): step 1")
    assert status_of(registry, 1) == (Status.ACTIVE, "promoted")  # the failed promotion left v1 alone


def test_promote_verified_version_with_changed_dependency_reverifies(registry, tools):
    add_version(registry, fixtures=acme_fixtures())
    verify(registry, tools, registry.get_version(NAME, 1), Settings())
    registry.set_status(NAME, 1, Status.VERIFIED, "checks_passed")
    assert not dependencies_changed(registry, tools, registry.get_version(NAME, 1))
    changed = ToolSet(sample_tools_with_currency())
    assert dependencies_changed(registry, changed, registry.get_version(NAME, 1))
    result = promote(registry, changed, Settings(), NAME, 1)
    assert not result.activated
    assert status_of(registry) == (Status.DRAFT, "static_failed")
    assert "missing required property 'currency'" in result.message
    assert events(registry)[-1] == (Status.VERIFIED, Status.DRAFT, "static_failed")


def test_promote_rollback_reverifies_a_deprecated_version(registry, tools):
    add_version(registry, fixtures=acme_fixtures())  # v1
    add_version(registry, fixtures=acme_fixtures())  # v2
    registry.set_status(NAME, 1, Status.DEPRECATED, "superseded_by_v2")
    registry.set_status(NAME, 2, Status.ACTIVE, "promoted")
    result = promote(registry, tools, Settings(), NAME, 1)
    assert result.activated and result.report.passed
    assert status_of(registry, 1) == (Status.ACTIVE, "rollback")
    assert status_of(registry, 2) == (Status.DEPRECATED, "superseded_by_v1")
    assert events(registry, 1)[-1] == (Status.DEPRECATED, Status.ACTIVE, "rollback")
    # a deprecated version that fails re-verification stays deprecated
    add_version(registry, fixtures=acme_fixtures(with_since=False))  # v3
    registry.set_status(NAME, 3, Status.DEPRECATED, "manual")
    result = promote(registry, tools, Settings(), NAME, 3)
    assert not result.activated and status_of(registry, 3) == (Status.DEPRECATED, "manual")
    assert status_of(registry, 1) == (Status.ACTIVE, "rollback")


def test_promote_suspect_dependency_changed_returns_on_pass(registry, tools):
    add_version(registry, fixtures=acme_fixtures())
    registry.set_status(NAME, 1, Status.SUSPECT, REASON_DEPENDENCY_CHANGED)
    result = promote(registry, tools, Settings(), NAME, 1)
    assert result.activated and status_of(registry) == (Status.ACTIVE, "reverified")
    assert events(registry)[-1] == (Status.SUSPECT, Status.ACTIVE, "reverified")


def test_promote_runtime_failure_suspect_needs_full_live_verification(registry, tools):
    add_version(registry, fixtures=acme_fixtures())
    registry.set_status(NAME, 1, Status.SUSPECT, REASON_RUNTIME_FAILURE)
    result = promote(registry, tools, Settings(), NAME, 1)
    assert not result.activated and result.report.passed
    assert status_of(registry) == (Status.SUSPECT, REASON_RUNTIME_FAILURE)
    assert "proves nothing after a runtime failure" in result.message and "verify --live" in result.message
    with pytest.raises(LifecycleError, match="allow_live_verification"):
        promote(registry, tools, Settings(), NAME, 1, live=True, executor=acme_executor)
    with pytest.raises(LifecycleError, match="needs an executor"):
        promote(registry, tools, Settings(allow_live_verification=True), NAME, 1, live=True)
    calls: list[str] = []

    def executor(name, args):
        calls.append(name)
        return acme_executor(name, args)

    result = promote(registry, tools, Settings(allow_live_verification=True), NAME, 1, live=True, executor=executor)
    assert result.activated and result.report.used_live
    assert calls == ["gmail_search", "gmail_get_message", "billing_find_customer", "billing_create_invoice"]
    assert status_of(registry) == (Status.ACTIVE, "reverified_live")
    fresh = registry.list_fixtures(NAME, 1)
    assert [f.base_tool for f in fresh] == calls and fresh[2].output == {"id": "c1"}


def test_reverify_live_failure_deprecates_suspect(registry, tools):
    add_version(registry, fixtures=acme_fixtures())
    registry.set_status(NAME, 1, Status.SUSPECT, REASON_RUNTIME_FAILURE)

    def broken(name, args):
        if name == "billing_create_invoice":
            raise RuntimeError("missing required property 'currency'")
        return acme_executor(name, args)

    updated, report = reverify(registry, tools, Settings(allow_live_verification=True), registry.get_version(NAME, 1), executor=broken, live=True)
    assert not report.passed and updated.status is Status.DEPRECATED and updated.status_reason == "replay_failed"
    assert [f.base_tool for f in registry.list_fixtures(NAME, 1)] == [f[0] for f in acme_fixtures()]  # old fixtures kept


# ------------------------------------------------------------------------- demote / deprecate


def test_demote_rules(registry):
    add_version(registry)
    assert demote(registry, NAME, 1, REASON_RUNTIME_FAILURE) is None  # drafts cannot be demoted
    registry.set_status(NAME, 1, Status.ACTIVE, "promoted")
    updated = demote(registry, NAME, 1, REASON_RUNTIME_FAILURE)
    assert (updated.status, updated.status_reason) == (Status.SUSPECT, REASON_RUNTIME_FAILURE)
    assert demote(registry, NAME, 1, REASON_DEPENDENCY_CHANGED) is None  # already hidden: left alone
    assert demote(registry, NAME, 1, REASON_AVOIDED_TOOL_RECOVERED, base_tool="x") is None
    assert registry.list_rebuild_queue() == []
    assert status_of(registry) == (Status.SUSPECT, REASON_RUNTIME_FAILURE)

    add_version(registry)  # v2
    registry.set_status(NAME, 2, Status.ACTIVE, "promoted")
    with pytest.raises(ValueError, match="base_tool"):
        demote(registry, NAME, 2, REASON_AVOIDED_TOOL_RECOVERED)
    updated = demote(registry, NAME, 2, REASON_AVOIDED_TOOL_RECOVERED, base_tool="billing_import_from_email")
    assert (updated.status, updated.status_reason) == (Status.SUSPECT, REASON_AVOIDED_TOOL_RECOVERED)
    queue = registry.list_rebuild_queue()
    assert [(i.tool_name, i.version, i.base_tool) for i in queue] == [(NAME, 2, "billing_import_from_email")]
    # a second recovered tool queues another item without a transition
    assert demote(registry, NAME, 2, REASON_AVOIDED_TOOL_RECOVERED, base_tool="billing_search_customers") is None
    assert len(registry.list_rebuild_queue()) == 2
    # a hiding reason replaces the exposed one
    updated = demote(registry, NAME, 2, REASON_RUNTIME_FAILURE)
    assert (updated.status, updated.status_reason) == (Status.SUSPECT, REASON_RUNTIME_FAILURE)
    assert events(registry, 2)[-1] == (Status.SUSPECT, Status.SUSPECT, REASON_RUNTIME_FAILURE)


def test_deprecate_from_every_status(registry):
    for index, status in enumerate(STATUSES, start=1):
        add_version(registry)
        registry.set_status(NAME, index, status, "setup")
    for index, status in enumerate(STATUSES, start=1):
        result = deprecate(registry, NAME, index)
        if status is Status.DEPRECATED:
            assert result is None
        else:
            assert result.status is Status.DEPRECATED and result.status_reason == "manual"
            assert events(registry, index)[-1] == (status, Status.DEPRECATED, "manual")


# ------------------------------------------------------------------------ apply_verification


def test_apply_verification_on_drafts(registry):
    add_version(registry)
    updated = apply_verification(registry, Settings(), registry.get_version(NAME, 1), failing_report())
    assert (updated.status, updated.status_reason) == (Status.DRAFT, "replay_failed")
    updated = apply_verification(registry, Settings(), updated, failing_report(check=CheckName.STATIC))
    assert updated.status_reason == "static_failed"
    updated = apply_verification(registry, Settings(), updated, failing_report(needs_live=True))
    assert updated.status_reason == "needs_live_verification"
    updated = apply_verification(registry, Settings(), updated, passing_report())
    assert (updated.status, updated.status_reason) == (Status.VERIFIED, "checks_passed")
    assert [e[1] for e in events(registry)] == [Status.DRAFT, Status.DRAFT, Status.DRAFT, Status.DRAFT, Status.VERIFIED]

    add_version(registry)  # v2, with auto_promote
    updated = apply_verification(registry, Settings(auto_promote=True), registry.get_version(NAME, 2), passing_report(2))
    assert (updated.status, updated.status_reason) == (Status.ACTIVE, "auto_promote")
    assert events(registry, 2)[1:] == [(Status.DRAFT, Status.VERIFIED, "checks_passed"), (Status.VERIFIED, Status.ACTIVE, "auto_promote")]


def test_apply_verification_on_other_statuses(registry):
    add_version(registry)
    registry.set_status(NAME, 1, Status.VERIFIED, "checks_passed")
    assert apply_verification(registry, Settings(), registry.get_version(NAME, 1), passing_report()).status is Status.VERIFIED
    assert apply_verification(registry, Settings(), registry.get_version(NAME, 1), failing_report()).status is Status.DRAFT

    registry.set_status(NAME, 1, Status.ACTIVE, "promoted")
    assert apply_verification(registry, Settings(), registry.get_version(NAME, 1), passing_report()).status is Status.ACTIVE
    updated = apply_verification(registry, Settings(), registry.get_version(NAME, 1), failing_report(check=CheckName.EFFICIENCY))
    assert (updated.status, updated.status_reason) == (Status.DEPRECATED, "efficiency_failed")

    registry.set_status(NAME, 1, Status.SUSPECT, REASON_DEPENDENCY_CHANGED)
    assert apply_verification(registry, Settings(), registry.get_version(NAME, 1), failing_report()).status is Status.DEPRECATED
    registry.set_status(NAME, 1, Status.SUSPECT, REASON_DEPENDENCY_CHANGED)
    assert apply_verification(registry, Settings(), registry.get_version(NAME, 1), passing_report()).status is Status.ACTIVE

    registry.set_status(NAME, 1, Status.SUSPECT, REASON_RUNTIME_FAILURE)
    stays = apply_verification(registry, Settings(), registry.get_version(NAME, 1), passing_report())
    assert (stays.status, stays.status_reason) == (Status.SUSPECT, REASON_RUNTIME_FAILURE)
    back = apply_verification(registry, Settings(), registry.get_version(NAME, 1), passing_report(), live_ran=True)
    assert (back.status, back.status_reason) == (Status.ACTIVE, "reverified_live")

    registry.set_status(NAME, 1, Status.DEPRECATED, "manual")
    assert apply_verification(registry, Settings(), registry.get_version(NAME, 1), passing_report()).status is Status.DEPRECATED
    assert apply_verification(registry, Settings(), registry.get_version(NAME, 1), passing_report(), rollback=True).status is Status.ACTIVE


def test_suspect_version_passing_while_a_newer_one_is_active_is_superseded(registry):
    add_version(registry)
    add_version(registry)
    registry.set_status(NAME, 1, Status.SUSPECT, REASON_DEPENDENCY_CHANGED)
    registry.set_status(NAME, 2, Status.ACTIVE, "promoted")
    updated = apply_verification(registry, Settings(), registry.get_version(NAME, 1), passing_report())
    assert (updated.status, updated.status_reason) == (Status.DEPRECATED, "superseded_by_v2")
    assert status_of(registry, 2) == (Status.ACTIVE, "promoted")


# ------------------------------------------------------------------------------ exposure


def test_exposure_rules_cap_and_tool_defs(registry):
    for name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta"):
        add_version(registry, GOOD_INVOICE.replace('name="create_invoice_from_email"', f'name="{name}"'), name=name)
    registry.set_status("alpha", 1, Status.ACTIVE, "promoted")
    registry.set_status("beta", 1, Status.ACTIVE, "promoted")
    registry.set_status("gamma", 1, Status.SUSPECT, REASON_AVOIDED_TOOL_RECOVERED)
    registry.set_status("delta", 1, Status.SUSPECT, REASON_RUNTIME_FAILURE)
    registry.set_status("epsilon", 1, Status.SUSPECT, REASON_DEPENDENCY_CHANGED)
    registry.set_status("zeta", 1, Status.DEPRECATED, "manual")
    registry.add_run_stat("beta", 1, success=True, calls=4)
    exposed = exposed_versions(registry, Settings())
    # beta has a successful run; gamma and alpha tie on runs, so the newer one (gamma) ranks first
    assert [v.tool_name for v in exposed] == ["beta", "gamma", "alpha"]
    assert [v.tool_name for v in exposed_versions(registry, Settings(max_exposed_tools=2))] == ["beta", "gamma"]
    assert exposed_versions(registry, Settings(max_exposed_tools=0)) == []
    defs = active_tool_defs(registry, Settings())
    assert [d["name"] for d in defs] == ["beta", "gamma", "alpha"]
    assert set(defs[0]) == {"name", "description", "input_schema"}
    assert defs[0]["input_schema"]["required"] == ["customer"]
    assert defs[0]["description"].startswith("Create a draft invoice")
    assert [is_exposable(registry.get_version(n, 1)) for n in ("alpha", "gamma", "delta", "epsilon", "zeta")] == [True, True, False, False, False]
    assert runnable_version(registry, "gamma").version == 1
    assert runnable_version(registry, "delta") is None and runnable_version(registry, "ghost") is None


def test_runnable_version_ignores_the_cap(registry):
    add_version(registry)
    add_version(registry)
    registry.set_status(NAME, 1, Status.DEPRECATED, "superseded_by_v2")
    registry.set_status(NAME, 2, Status.ACTIVE, "promoted")
    assert runnable_version(registry, NAME).version == 2
    assert exposed_versions(registry, Settings(max_exposed_tools=0)) == []
    assert runnable_version(registry, NAME).version == 2
