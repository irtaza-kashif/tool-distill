"""The incident demo, driven by a scripted fake model against the fake incident world. No network.

Pins the shape the video relies on: a 28-call discovery that proposes a 10-call tool, a one-call reuse, and the
schema change that fails the tool safely and yields the next version under the same name.
"""

from typing import Any

import incident_tools as world
import pytest
from agent_loop import run_agent
from test_e2e_demo import Response, ScriptedModel, end_turn, last_results, propose, result_json
from test_e2e_demo import tool_use as _tool_use

from tooldistill import ToolDistill
from tooldistill.core.models import Status

NAME = "resolve_alert_by_rolling_back_the_cause"
SWITCHES = ("rollback_outage", "schema_reason", "tools_updated")
SCHEMA = {"type": "object", "properties": {"alert_id": {"type": "string"}}, "required": ["alert_id"]}


def tool_use(name: str, input: dict[str, Any]) -> Response:
    """One scripted model turn that calls one tool."""
    return Response([_tool_use(name, input)])


def tool_code(*, reason: bool) -> str:
    extra = ', reason="rolled back by the incident tool"' if reason else ""
    return f'''from tooldistill import distilled_tool, call


@distilled_tool(
    name="{NAME}",
    description="Handle a firing alert: find the upstream service whose recent deploy caused it, open an incident for the owning team's on-call engineer, roll that deploy back with an approval, acknowledge the alert and post a summary in #incidents.",
    input_schema={SCHEMA!r},
)
def {NAME}(alert_id: str) -> dict:
    alert = call("alerts_get", alert_id=alert_id)
    victim = call("services_get", service_id=alert["service_name"].lower().replace(" ", "-"))
    deps = call("services_dependencies", service_id=victim["id"])
    cause = max(deps, key=lambda d: d["current_deploy"]["deployed_at"])
    team = call("teams_get", team_id=cause["team_id"])
    incident = call("incidents_create", title=f"{{alert['title']}}: {{cause['id']}} rolled back", service_id=cause["id"], severity="sev2", assignee_id=team["oncall_user_id"])
    approval = call("approvals_request", service_id=cause["id"], action="rollback", incident_id=incident["id"])
    rollback = call("deploys_rollback", service_id=cause["id"], to_deploy_id=cause["current_deploy"]["previous_deploy_id"], confirmation_token=approval["token"]{extra})
    call("alerts_ack", alert_id=alert_id, incident_id=incident["id"])
    post = call("chat_post", channel="#incidents", message=f"{{incident['id']}}: rolled back {{cause['id']}} to {{rollback['rolled_back_to']}}; assigned to {{team['oncall_user_id']}}")
    final = call("incidents_update", incident_id=incident["id"], status="mitigated", note="rolled back")
    return {{"incident": final, "rollback": rollback, "post": post}}


def check(result: dict) -> bool:
    return result["incident"]["status"] == "mitigated" and bool(result["rollback"]["rolled_back_to"])
'''


def payload(*, reason: bool) -> dict[str, Any]:
    return {
        "name": NAME,
        "description": "Handle a firing alert end to end: find the upstream cause, incident, rollback, ack, summary.",
        "input_schema": SCHEMA,
        "code": tool_code(reason=reason),
        "test_inputs": {"alert_id": "A-1042"},
    }


def remember_verdict(memory: dict[str, Any]):
    def turn(messages):
        memory["verdict"] = last_results(messages)[0]["content"]
        return end_turn("Saved.")

    return turn


def fixed_path(memory: dict[str, Any], *, reason: bool) -> list[Any]:
    """The successful part of a run, in the exact shape the distilled tool reproduces (fixtures must match)."""
    title = "High 5xx rate on Checkout API (prod): payments-api rolled back"
    extra = {"reason": "rolled back by the incident tool"} if reason else {}

    def create_incident(messages):
        return tool_use("incidents_create", {"title": title, "service_id": "payments-api", "severity": "sev2", "assignee_id": "u_1032"})

    def request_approval(messages):
        memory["incident"] = result_json(messages)
        return tool_use("approvals_request", {"service_id": "payments-api", "action": "rollback", "incident_id": memory["incident"]["id"]})

    def roll_back(messages):
        token = result_json(messages)["token"]
        return tool_use("deploys_rollback", {"service_id": "payments-api", "to_deploy_id": world.GOOD_DEPLOY["payments-api"], "confirmation_token": token, **extra})

    def ack(messages):
        memory["rollback"] = result_json(messages)
        return tool_use("alerts_ack", {"alert_id": "A-1042", "incident_id": memory["incident"]["id"]})

    def post(messages):
        message = f"{memory['incident']['id']}: rolled back payments-api to {memory['rollback']['rolled_back_to']}; assigned to u_1032"
        return tool_use("chat_post", {"channel": "#incidents", "message": message})

    def mitigate(messages):
        return tool_use("incidents_update", {"incident_id": memory["incident"]["id"], "status": "mitigated", "note": "rolled back"})

    return [create_incident, request_approval, roll_back, ack, post, mitigate]


def discovery_script(memory: dict[str, Any]) -> list[Any]:
    """28 base tool calls of trial and error, then the proposal after the nudge."""
    return [
        tool_use("alerts_get", {"alert_id": "A-1042"}),  # 1
        tool_use("services_search", {"query": "Checkout API"}),  # 2 by display name: nothing
        tool_use("services_search", {"query": "checkout"}),  # 3 three candidates
        tool_use("services_get", {"service_id": "checkout-api"}),  # 4 nothing deployed recently here
        tool_use("deploys_list", {"service_id": "checkout-api"}),  # 5
        tool_use("logs_query", {"service_id": "checkout-api", "since": "2026-10-18T10:00:00Z", "until": "2026-10-18T14:00:00Z"}),  # 6 too wide
        tool_use("logs_query", {"service_id": "checkout-api", "since": "2026-10-18T13:15:00Z", "until": "2026-10-18T14:00:00Z", "limit": 20}),  # 7 points upstream
        tool_use("metrics_query", {"service_id": "checkout-api", "metric": "error_rate", "window": "1h"}),  # 8
        tool_use("services_dependencies", {"service_id": "checkout-api"}),  # 9 payments-api deployed 40 minutes ago
        tool_use("services_get", {"service_id": "payments-api"}),  # 10
        tool_use("deploys_get", {"deploy_id": world.BAD_DEPLOY["payments-api"]}),  # 11
        tool_use("deploys_get", {"deploy_id": world.GOOD_DEPLOY["payments-api"]}),  # 12
        tool_use("runbooks_search", {"query": "rollback"}),  # 13 a distractor
        tool_use("deploys_rollback_latest", {"service_id": "payments-api"}),  # 14 the shortcut: 503
        tool_use("deploys_rollback", {"service_id": "payments-api", "to_deploy_id": world.GOOD_DEPLOY["payments-api"]}),  # 15 no token
        tool_use("approvals_request", {"service_id": "payments-api", "action": "rollback"}),  # 16 needs an incident
        tool_use("incidents_create", {"title": "Checkout 5xx", "service_id": "payments-api", "severity": "high", "assignee_id": "@maya"}),  # 17 severity
        tool_use("incidents_create", {"title": "Checkout 5xx", "service_id": "payments-api", "severity": "sev2", "assignee_id": "@maya"}),  # 18 handle
        tool_use("teams_get", {"team_id": "t_payments"}),  # 19
        tool_use("users_get", {"user_id": "u_1032"}),  # 20
        *fixed_path(memory, reason=False),  # 21-26
        tool_use("metrics_query", {"service_id": "payments-api", "metric": "error_rate", "window": "15m"}),  # 27 checking
        tool_use("oncall_schedule", {"team_id": "t_payments", "day": "2026-10-18"}),  # 28 a distractor
        end_turn("Rolled back payments-api, opened the incident for @maya, acknowledged A-1042 and posted the summary."),
        propose(payload(reason=False)),  # the nudge's turn
        remember_verdict(memory),
    ]


@pytest.fixture
def incident_world():
    world.reset()
    for name in SWITCHES:
        world.set_switch(name, None)
    yield world
    world.reset()
    for name in SWITCHES:
        world.set_switch(name, None)


def make_td(store, *, check_schemas_on_start=True) -> ToolDistill:
    return ToolDistill(
        store,
        executor=world.execute_tool,
        tools=world.tool_definitions(reason=False),
        health_probe=world.probe,
        auto_promote=True,
        allow_live_verification=True,
        min_calls_to_distill=6,
        fallback_distiller=False,
        check_schemas_on_start=check_schemas_on_start,
    )


def test_incident_demo_discovery_reuse_and_schema_change(store, incident_world):
    td = make_td(store)
    try:
        # 1. discovery with the shortcut down
        world.set_switch("rollback_outage", True)
        memory: dict[str, Any] = {}
        model = ScriptedModel(discovery_script(memory))
        report = run_agent(td, model, world.task_for("A-1042"), task_succeeded=world.task_succeeded, max_steps=60)
        assert model.turns == [] and report.outcome is True and report.nudged
        assert report.base_calls == 28 and report.propose_calls == 1
        assert memory["verdict"].startswith("verified: 10 base tool calls vs baseline 28"), memory["verdict"]
        assert memory["verdict"].endswith("; v1 is now active")
        v1 = td.registry.get_version(NAME, 1)
        assert v1.status is Status.ACTIVE and v1.measured_calls == 10
        assert [a.base_tool for a in td.registry.list_avoided_tools(NAME, 1)] == ["deploys_rollback_latest"]
        assert td.registry.list_dependencies(NAME, 1)[0].base_tool == "alerts_ack"

        # 2. reuse on the other alert: one distilled call, live, a different victim, cause and team
        world.reset()
        world.set_switch("rollback_outage", None)
        model = ScriptedModel([tool_use(NAME, {"alert_id": "A-1057"}), end_turn("Handled A-1057.")])
        report = run_agent(td, model, world.task_for("A-1057"), task_succeeded=world.task_succeeded)
        assert report.outcome is True and report.distilled_calls == 1 and report.inner_calls == 10 and report.base_calls == 0
        assert not report.nudged and world.services_get("ledger-api")["current_deploy"]["id"] == world.GOOD_DEPLOY["ledger-api"]
        assert world.alerts_get("A-1057")["status"] == "acknowledged" and td.registry.success_count(NAME, 1) == 1

        # 3. the deploy API now requires a reason: the tool fails safely, the fallback run yields v2
        world.reset()
        world.set_switch("schema_reason", True)
        memory = {}

        def remember_error(messages):
            memory["error"] = last_results(messages)[0]
            return tool_use("alerts_get", {"alert_id": "A-1042"})

        model = ScriptedModel([
            tool_use(NAME, {"alert_id": "A-1042"}),
            remember_error,
            tool_use("services_get", {"service_id": "checkout-api"}),
            tool_use("services_dependencies", {"service_id": "checkout-api"}),
            tool_use("teams_get", {"team_id": "t_payments"}),
            *fixed_path(memory, reason=True),
            end_turn("Rolled back with the base tools, passing a reason."),
            propose(payload(reason=True)),
            remember_verdict(memory),
        ])
        report = run_agent(td, model, world.task_for("A-1042"), task_succeeded=world.task_succeeded)
        assert report.outcome is True and report.distilled_calls == 1 and report.distilled_failures == 1
        error = memory["error"]
        assert error["is_error"] and error["content"].startswith(
            f"Distilled tool '{NAME}' (v1) failed at step 7 of 10.\nBase tool: deploys_rollback\nError: missing required property 'reason'"
        )
        assert report.nudged and memory["verdict"].startswith("verified: 10 base tool calls vs baseline 28"), memory["verdict"]
        assert memory["verdict"].endswith("; v2 is now active and replaces v1")
        assert td.registry.get_version(NAME, 1).status is Status.DEPRECATED
        assert td.registry.get_version(NAME, 2).status is Status.ACTIVE
        assert [t["name"] for t in td.active_tools()] == [NAME]
    finally:
        td.close()
