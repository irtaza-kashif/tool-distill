"""The incident demo, driven by a scripted fake model against the fake incident world. No network.

Pins the shape the video relies on: a ~26-call discovery that proposes a 10-call tool, a one-call reuse, and the
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

NAME = "resolve_alert_with_rollback"


def tool_use(name: str, input: dict[str, Any]) -> Response:
    """One scripted model turn that calls one tool."""
    return Response([_tool_use(name, input)])


SWITCHES = ("rollback_outage", "schema_reason", "tools_updated")
SCHEMA = {"type": "object", "properties": {"alert_id": {"type": "string"}}, "required": ["alert_id"]}


def tool_code(*, reason: bool) -> str:
    extra = ', reason="rolled back by the incident tool"' if reason else ""
    return f'''from tooldistill import distilled_tool, call


@distilled_tool(
    name="{NAME}",
    description="Handle a firing alert: find its service, roll back the bad deploy with an approval, open an incident for the on-call engineer and post a summary in #incidents.",
    input_schema={SCHEMA!r},
)
def {NAME}(alert_id: str) -> dict:
    alert = call("alerts_get", alert_id=alert_id)
    slug = alert["service_name"].lower().replace(" ", "-")
    matches = call("services_search", query=slug)
    found = [s for s in matches if s["id"] == slug and s["env"] == alert["environment"]][0]
    service = call("services_get", service_id=found["id"])
    current = call("deploys_get", deploy_id=service["current_deploy_id"])
    approval = call("approvals_request", service_id=service["id"], action="rollback")
    rollback = call("deploys_rollback", service_id=service["id"], to_deploy_id=current["previous_deploy_id"], confirmation_token=approval["token"]{extra})
    team = call("teams_get", team_id=service["team_id"])
    user = call("users_get", user_id=team["oncall_user_id"])
    incident = call("incidents_create", title=f"{{alert['title']}}: rolled back", service_id=service["id"], severity="sev2", assignee_id=user["id"])
    post = call("chat_post", channel="#incidents", message=f"{{incident['id']}}: rolled back {{service['id']}} to {{rollback['rolled_back_to']}}; assigned to {{user['handle']}}")
    return {{"incident": incident, "rollback": rollback, "post": post}}


def check(result: dict) -> bool:
    return result["incident"]["status"] == "open" and bool(result["rollback"]["rolled_back_to"])
'''


def payload(*, reason: bool) -> dict[str, Any]:
    return {
        "name": NAME,
        "description": "Handle a firing alert end to end: rollback, incident, summary.",
        "input_schema": SCHEMA,
        "code": tool_code(reason=reason),
        "test_inputs": {"alert_id": "A-1042"},
    }


def remember_verdict(memory: dict[str, Any]):
    def turn(messages):
        memory["verdict"] = last_results(messages)[0]["content"]
        return end_turn("Saved.")

    return turn


def discovery_script(memory: dict[str, Any]) -> list[Any]:
    """26 base tool calls of trial and error, then the proposal after the nudge."""
    alert_id = "A-1042"

    def remember(key, fn):
        def turn(messages):
            memory[key] = fn(result_json(messages))
            return memory["next"](messages)

        return turn

    def rollback_with_token(messages):
        token = result_json(messages)["token"]
        memory["token"] = token
        return tool_use("deploys_rollback", {"service_id": "checkout-api", "to_deploy_id": memory["previous"], "confirmation_token": token})

    def create_incident_properly(messages):
        memory["user"] = result_json(messages)
        title = f"{memory['alert']['title']}: rolled back"
        return tool_use("incidents_create", {"title": title, "service_id": "checkout-api", "severity": "sev2", "assignee_id": memory["user"]["id"]})

    def post_summary(messages):
        incident = result_json(messages)
        memory["incident"] = incident
        message = f"{incident['id']}: rolled back checkout-api to {memory['rolled_back_to']}; assigned to {memory['user']['handle']}"
        return tool_use("chat_post", {"channel": "#incidents", "message": message})

    def after_alert(messages):
        memory["alert"] = result_json(messages)
        return tool_use("services_search", {"query": "Checkout API"})

    def after_current(messages):
        current = result_json(messages)
        memory["previous"] = current["previous_deploy_id"]
        return tool_use("deploys_get", {"deploy_id": current["previous_deploy_id"]})

    def after_rollback(messages):
        memory["rolled_back_to"] = result_json(messages)["rolled_back_to"]
        return tool_use("teams_get", {"team_id": "t_payments"})

    return [
        tool_use("alerts_get", {"alert_id": alert_id}),  # 1
        after_alert,  # 2 services_search by display name: nothing
        tool_use("services_list", {"page": 1}),  # 3
        tool_use("services_get", {"service_id": "checkout-worker"}),  # 4 the decoy
        tool_use("deploys_list", {"service_id": "checkout-worker"}),  # 5 no recent deploy there
        tool_use("services_list", {"page": 2}),  # 6
        tool_use("services_list", {"page": 3}),  # 7 found it
        tool_use("services_search", {"query": "checkout-api"}),  # 8 confirm the slug (staging shows up too)
        tool_use("services_get", {"service_id": "checkout-api"}),  # 9
        tool_use("services_dependencies", {"service_id": "checkout-api"}),  # 10 a distractor
        tool_use("deploys_list", {"service_id": "checkout-api"}),  # 11
        lambda m: tool_use("deploys_get", {"deploy_id": result_json(m)["items"][0]["id"]}),  # 12 current
        after_current,  # 13 previous
        tool_use("logs_query", {"service_id": "checkout-api", "since": "2026-10-18T10:00:00Z", "until": "2026-10-18T14:00:00Z"}),  # 14 too wide
        tool_use("logs_query", {"service_id": "checkout-api", "since": "2026-10-18T13:15:00Z", "until": "2026-10-18T14:00:00Z", "limit": 20}),  # 15
        tool_use("metrics_query", {"service_id": "checkout-api", "metric": "error_rate", "window": "1h"}),  # 16
        tool_use("runbooks_search", {"query": "rollback"}),  # 17 a distractor
        tool_use("deploys_rollback_latest", {"service_id": "checkout-api"}),  # 18 the shortcut: 503
        lambda m: tool_use("deploys_rollback", {"service_id": "checkout-api", "to_deploy_id": memory["previous"]}),  # 19 no token
        tool_use("approvals_request", {"service_id": "checkout-api", "action": "rollback"}),  # 20
        rollback_with_token,  # 21
        after_rollback,  # 22 teams_get
        tool_use("oncall_schedule", {"team_id": "t_payments", "day": "2026-10-18"}),  # 23 a distractor
        tool_use("incidents_create", {"title": "Checkout 5xx", "service_id": "checkout-api", "severity": "sev2", "assignee_id": "@maya"}),  # 24 wrong kind
        tool_use("users_get", {"user_id": "u_1032"}),  # 25
        create_incident_properly,  # 26
        post_summary,  # 27... counted below
        lambda m: tool_use("incidents_update", {"incident_id": memory["incident"]["id"], "status": "mitigated", "note": "rolled back"}),
        end_turn("Rolled back checkout-api, opened the incident for @maya and posted the summary."),
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
        assert memory["verdict"].startswith("verified: 10 base tool calls vs baseline 28")
        assert memory["verdict"].endswith("; v1 is now active")
        v1 = td.registry.get_version(NAME, 1)
        assert v1.status is Status.ACTIVE and v1.measured_calls == 10
        assert [a.base_tool for a in td.registry.list_avoided_tools(NAME, 1)] == ["deploys_rollback_latest"]
        assert td.registry.list_dependencies(NAME, 1)[0].base_tool == "alerts_get"

        # 2. reuse on a different alert: one distilled call, live
        world.reset()
        world.set_switch("rollback_outage", None)
        model = ScriptedModel([tool_use(NAME, {"alert_id": "A-1057"}), end_turn("Handled A-1057.")])
        report = run_agent(td, model, world.task_for("A-1057"), task_succeeded=world.task_succeeded)
        assert report.outcome is True and report.distilled_calls == 1 and report.inner_calls == 10 and report.base_calls == 0
        assert not report.nudged and world.services_get("payments-worker")["current_deploy_id"] == world.GOOD_DEPLOY["payments-worker"]
        assert td.registry.success_count(NAME, 1) == 1

        # 3. the deploy API now requires a reason: the tool fails safely, the fallback run yields v2
        world.reset()
        world.set_switch("schema_reason", True)
        memory = {}

        def fallback_rollback(messages):
            token = result_json(messages)["token"]
            return tool_use("deploys_rollback", {"service_id": "checkout-api", "to_deploy_id": world.GOOD_DEPLOY["checkout-api"], "confirmation_token": token, "reason": "rolled back by the incident tool"})

        def fallback_incident(messages):
            user = result_json(messages)
            memory["user"] = user
            return tool_use("incidents_create", {"title": "High 5xx rate on Checkout API (prod): rolled back", "service_id": "checkout-api", "severity": "sev2", "assignee_id": user["id"]})

        def fallback_post(messages):
            incident = result_json(messages)
            return tool_use("chat_post", {"channel": "#incidents", "message": f"{incident['id']}: rolled back checkout-api to {world.GOOD_DEPLOY['checkout-api']}; assigned to {memory['user']['handle']}"})

        def remember_error(messages):
            memory["error"] = last_results(messages)[0]
            return tool_use("alerts_get", {"alert_id": "A-1042"})

        current_bad = world.BAD_DEPLOY["checkout-api"]
        model = ScriptedModel([
            tool_use(NAME, {"alert_id": "A-1042"}),
            remember_error,
            tool_use("services_search", {"query": "checkout-api"}),
            tool_use("services_get", {"service_id": "checkout-api"}),
            tool_use("deploys_get", {"deploy_id": current_bad}),
            tool_use("approvals_request", {"service_id": "checkout-api", "action": "rollback"}),
            fallback_rollback,
            tool_use("teams_get", {"team_id": "t_payments"}),
            tool_use("users_get", {"user_id": "u_1032"}),
            fallback_incident,
            fallback_post,
            end_turn("Rolled back with the base tools."),
            propose(payload(reason=True)),
            remember_verdict(memory),
        ])
        report = run_agent(td, model, world.task_for("A-1042"), task_succeeded=world.task_succeeded)
        assert report.outcome is True and report.distilled_calls == 1 and report.distilled_failures == 1
        error = memory["error"]
        assert error["is_error"] and error["content"].startswith(f"Distilled tool '{NAME}' (v1) failed at step 6 of 10.\nBase tool: deploys_rollback\nError: missing required property 'reason'")
        assert report.nudged and memory["verdict"].startswith("verified: 10 base tool calls vs baseline 28")
        assert memory["verdict"].endswith("; v2 is now active and replaces v1")
        assert td.registry.get_version(NAME, 1).status is Status.DEPRECATED
        assert td.registry.get_version(NAME, 2).status is Status.ACTIVE
        assert [t["name"] for t in td.active_tools()] == [NAME]
    finally:
        td.close()
