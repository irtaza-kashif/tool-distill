"""The lifecycle: the only module that changes a distilled tool version's status.

Every change is checked against the transition table from CLAUDE.md and written to ``status_events``.
Exposure rules (what ``active_tools()`` returns) live here too.
"""

from dataclasses import dataclass
from typing import Any

from tooldistill.core.config import Settings
from tooldistill.core.errors import InvalidTransition, LifecycleError
from tooldistill.core.models import (
    REASON_AVOIDED_TOOL_RECOVERED,
    REASON_NEEDS_LIVE_VERIFICATION,
    REASON_RUNTIME_FAILURE,
    Status,
    VersionRecord,
)
from tooldistill.core.registry import Registry
from tooldistill.core.runtime import Executor
from tooldistill.core.tools import ToolSet
from tooldistill.core.verifier import HealthProbe, VerifyReport, verify

__all__ = [
    "ALLOWED_TRANSITIONS",
    "EXPOSED_SUSPECT_REASONS",
    "PromoteResult",
    "active_tool_defs",
    "apply_verification",
    "demote",
    "dependencies_changed",
    "deprecate",
    "exposed_versions",
    "is_exposable",
    "promote",
    "reverify",
    "runnable_version",
    "transition",
]

ALLOWED_TRANSITIONS: frozenset[tuple[Status, Status]] = frozenset(
    {
        (Status.DRAFT, Status.VERIFIED),  # all three checks pass
        (Status.DRAFT, Status.DRAFT),  # a check fails; results recorded, proposer may resubmit
        (Status.DRAFT, Status.DEPRECATED),  # manual
        (Status.VERIFIED, Status.ACTIVE),  # auto_promote or promote
        (Status.VERIFIED, Status.DRAFT),  # re-verification before promotion fails
        (Status.VERIFIED, Status.DEPRECATED),  # manual
        (Status.ACTIVE, Status.SUSPECT),  # dependency_changed / runtime_failure / avoided_tool_recovered
        (Status.ACTIVE, Status.DEPRECATED),  # newer version activated, manual, or re-verification failed
        (Status.SUSPECT, Status.ACTIVE),  # re-verification passes
        (Status.SUSPECT, Status.SUSPECT),  # a worse reason arrives (e.g. runtime_failure while avoided_tool_recovered)
        (Status.SUSPECT, Status.DEPRECATED),  # re-verification fails, or a newer version becomes active
        (Status.DEPRECATED, Status.ACTIVE),  # rollback after re-passing verification
    }
)

# Suspect versions with these reasons still work and stay exposed.
EXPOSED_SUSPECT_REASONS: tuple[str, ...] = (REASON_AVOIDED_TOOL_RECOVERED,)


# ------------------------------------------------------------------------------ transitions


def transition(registry: Registry, name: str, version: int, to: Status, reason: str) -> VersionRecord:
    """Move one version to ``to`` if the table allows it; writes the status and the audit event together."""
    with registry.transaction():
        current = registry.require_version(name, version)
        if (current.status, to) not in ALLOWED_TRANSITIONS:
            raise InvalidTransition(name, version, str(current.status), str(to))
        registry.set_status(name, version, to, reason)
        registry.add_status_event(name, version, current.status, to, reason)
    return registry.require_version(name, version)


def _activate(registry: Registry, version: VersionRecord, reason: str) -> VersionRecord:
    """Make ``version`` the active one: every other active or suspect version of the tool is superseded."""
    name, number = version.tool_name, version.version
    with registry.transaction():
        for other in registry.list_versions(name):
            if other.version != number and other.status in (Status.ACTIVE, Status.SUSPECT):
                transition(registry, name, other.version, Status.DEPRECATED, f"superseded_by_v{number}")
        activated = transition(registry, name, number, Status.ACTIVE, reason)
        registry.set_tool_description(name, activated.description)
    return activated


def demote(registry: Registry, name: str, version: int, reason: str, *, base_tool: str | None = None) -> VersionRecord | None:
    """Mark a version suspect. Returns the updated record, or None when nothing changed.

    An active version becomes suspect. A suspect version that is still exposed (``avoided_tool_recovered``)
    takes a hiding reason when one arrives; any other suspect, draft, verified or deprecated version is
    left alone. ``avoided_tool_recovered`` also queues a rebuild for ``base_tool``.
    """
    if reason == REASON_AVOIDED_TOOL_RECOVERED and not base_tool:
        raise ValueError("demote(avoided_tool_recovered) needs the recovered base_tool")
    current = registry.require_version(name, version)
    updated: VersionRecord | None = None
    if current.status is Status.ACTIVE:
        updated = transition(registry, name, version, Status.SUSPECT, reason)
    elif (
        current.status is Status.SUSPECT
        and current.status_reason == REASON_AVOIDED_TOOL_RECOVERED
        and reason != REASON_AVOIDED_TOOL_RECOVERED
    ):
        updated = transition(registry, name, version, Status.SUSPECT, reason)
    if reason == REASON_AVOIDED_TOOL_RECOVERED and (
        updated is not None or (current.status is Status.SUSPECT and current.status_reason == REASON_AVOIDED_TOOL_RECOVERED)
    ):
        assert base_tool is not None
        registry.enqueue_rebuild(name, version, reason, base_tool)
    return updated


def deprecate(registry: Registry, name: str, version: int, reason: str = "manual") -> VersionRecord | None:
    """Deprecate a version from any status; None when it already was."""
    current = registry.require_version(name, version)
    if current.status is Status.DEPRECATED:
        return None
    return transition(registry, name, version, Status.DEPRECATED, reason)


# --------------------------------------------------------------------------- verification


def apply_verification(
    registry: Registry,
    settings: Settings,
    version: VersionRecord,
    report: VerifyReport,
    *,
    live_ran: bool = False,
    rollback: bool = False,
) -> VersionRecord:
    """Turn a verifier report into the status change the lifecycle table prescribes.

    ``live_ran`` says every call ran live (``verify --live``): the only evidence that brings a
    ``runtime_failure`` suspect back. ``rollback`` lets a deprecated version that re-passed become active
    again (``promote --version N``); otherwise a deprecated version stays where it is.
    """
    name, number = version.tool_name, version.version
    current = registry.require_version(name, number)
    failure_reason = REASON_NEEDS_LIVE_VERIFICATION if report.needs_live_verification else f"{report.failed_check}_failed"
    match current.status:
        case Status.DRAFT:
            if not report.passed:
                return transition(registry, name, number, Status.DRAFT, failure_reason)
            verified = transition(registry, name, number, Status.VERIFIED, "checks_passed")
            return _activate(registry, verified, "auto_promote") if settings.auto_promote else verified
        case Status.VERIFIED:
            return current if report.passed else transition(registry, name, number, Status.DRAFT, failure_reason)
        case Status.ACTIVE:
            return current if report.passed else transition(registry, name, number, Status.DEPRECATED, failure_reason)
        case Status.SUSPECT:
            if not report.passed:
                return transition(registry, name, number, Status.DEPRECATED, failure_reason)
            if current.status_reason == REASON_RUNTIME_FAILURE and not live_ran:
                return current  # replay against old fixtures proves nothing after a runtime failure
            other = registry.active_version(name)
            if other is not None and other.version != number:
                return transition(registry, name, number, Status.DEPRECATED, f"superseded_by_v{other.version}")
            return _activate(registry, current, "reverified_live" if live_ran else "reverified")
        case Status.DEPRECATED:
            if report.passed and rollback:
                return _activate(registry, current, "rollback")
            return current
    raise AssertionError(f"unknown status {current.status}")  # pragma: no cover


def reverify(
    registry: Registry,
    tools: ToolSet,
    settings: Settings,
    version: VersionRecord,
    *,
    executor: Executor | None = None,
    live: bool = False,
    health_probe: HealthProbe | None = None,
    rollback: bool = False,
) -> tuple[VersionRecord, VerifyReport]:
    """Re-run verification on a stored version and apply the outcome.

    Live verification (fallback for uncovered calls) runs only when ``allow_live_verification`` is on
    and an executor is given. ``live=True`` (``verify --live``) runs every call live and needs both.
    """
    if live and not settings.allow_live_verification:
        raise LifecycleError("live verification is disabled: set allow_live_verification = true (it has real side effects)")
    if live and executor is None:
        raise LifecycleError("live verification needs an executor")
    allow_live = settings.allow_live_verification and executor is not None
    report = verify(
        registry,
        tools,
        version,
        settings,
        executor=executor,
        allow_live=allow_live,
        full_live=live,
        health_probe=health_probe,
    )
    updated = apply_verification(registry, settings, version, report, live_ran=live, rollback=rollback)
    return updated, report


def dependencies_changed(registry: Registry, tools: ToolSet, version: VersionRecord) -> bool:
    """True when any stored dependency hash differs from the current tool list (a missing tool counts)."""
    for dep in registry.list_dependencies(version.tool_name, version.version):
        if dep.base_tool not in tools or tools.schema_hash(dep.base_tool) != dep.schema_hash:
            return True
    return False


# ------------------------------------------------------------------------------- promote


@dataclass(slots=True)
class PromoteResult:
    version: VersionRecord
    activated: bool
    message: str
    report: VerifyReport | None = None


def promote(
    registry: Registry,
    tools: ToolSet,
    settings: Settings,
    name: str,
    version: int | None = None,
    *,
    executor: Executor | None = None,
    live: bool = False,
    health_probe: HealthProbe | None = None,
) -> PromoteResult:
    """Activate a version. Without ``version`` the highest-numbered verified version is taken.

    A verified version whose dependencies are unchanged is activated directly. Anything else is
    re-verified first: a draft becomes verified then active, a suspect or deprecated version (rollback)
    returns to active only when the checks pass.
    """
    if version is None:
        verified = registry.list_versions(name, statuses=[Status.VERIFIED])
        if not verified:
            if registry.get_tool(name) is None:
                raise LifecycleError(f"unknown distilled tool '{name}'")
            raise LifecycleError(f"'{name}' has no verified version to promote; pass a version number to re-verify one")
        current = verified[-1]
    else:
        current = registry.require_version(name, version)
    if current.status is Status.ACTIVE:
        return PromoteResult(current, False, f"{current.label} is already active")
    if current.status is Status.VERIFIED and not dependencies_changed(registry, tools, current):
        activated = _activate(registry, current, "promoted")
        return PromoteResult(activated, True, f"{activated.label} is now active")
    updated, report = reverify(
        registry, tools, settings, current, executor=executor, live=live, health_probe=health_probe, rollback=True
    )
    if updated.status is Status.VERIFIED:
        updated = _activate(registry, updated, "promoted")
    if updated.status is Status.ACTIVE:
        return PromoteResult(updated, True, f"{updated.label} is now active ({report.verdict})", report)
    if updated.status is Status.SUSPECT and updated.status_reason == REASON_RUNTIME_FAILURE and report.passed:
        message = (
            f"{updated.label} stays suspect: replay against its old fixtures passed but proves nothing after a "
            "runtime failure; run verify --live (with allow_live_verification) or distill a new version"
        )
        return PromoteResult(updated, False, message, report)
    return PromoteResult(updated, False, f"{updated.label} was not promoted ({updated.status}): {report.verdict}", report)


# ------------------------------------------------------------------------------- exposure


def is_exposable(version: VersionRecord) -> bool:
    """Whether a version may be run by an agent (ignores the ``max_exposed_tools`` cap)."""
    if version.status is Status.ACTIVE:
        return True
    return version.status is Status.SUSPECT and version.status_reason in EXPOSED_SUSPECT_REASONS


def exposed_versions(registry: Registry, settings: Settings) -> list[VersionRecord]:
    """The versions ``active_tools()`` returns: active, plus suspect(avoided_tool_recovered), capped and ranked."""
    return registry.exposed_versions(suspect_reasons=EXPOSED_SUSPECT_REASONS, limit=settings.max_exposed_tools)


def active_tool_defs(registry: Registry, settings: Settings) -> list[dict[str, Any]]:
    """Exposed versions as Anthropic tool dicts."""
    return [
        {"name": v.tool_name, "description": v.description, "input_schema": v.input_schema}
        for v in exposed_versions(registry, settings)
    ]


def runnable_version(registry: Registry, name: str) -> VersionRecord | None:
    """The one version of ``name`` an agent may run right now, if any."""
    for candidate in registry.list_versions(name):
        if is_exposable(candidate):
            return candidate
    return None
