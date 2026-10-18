"""The incident demo's fake world: pagination, traps, switches and the success check. No network."""

import json

import incident_tools as world
import pytest

SWITCHES = ("rollback_outage", "schema_reason", "tools_updated")
TASK = world.task_for("A-1042")


@pytest.fixture(autouse=True)
def clean_world():
    world.reset()
    for name in SWITCHES:
        world.set_switch(name, None)
    yield
    world.reset()
    for name in SWITCHES:
        world.set_switch(name, None)


def page_of(service_id: str) -> int:
    for page in range(1, 5):
        if any(s["id"] == service_id for s in world.services_list(page)["items"]):
            return page
    raise AssertionError(service_id)


def solve(alert_id: str) -> dict:
    """The sequence a distilled tool would make: the known-good path through the world."""
    alert = world.alerts_get(alert_id)
    slug = alert["service_name"].lower().replace(" ", "-")
    service = next(s for s in world.services_search(slug) if s["env"] == alert["environment"] and s["id"] == slug)
    current = world.deploys_get(service["current_deploy_id"])
    token = world.approvals_request(service["id"], "rollback")["token"]
    rollback = world.deploys_rollback(service["id"], current["previous_deploy_id"], token)
    team = world.teams_get(service["team_id"])
    user = world.users_get(team["oncall_user_id"])
    incident = world.incidents_create(f"{alert['title']}: rolled back", service["id"], "sev2", user["id"])
    world.chat_post("#incidents", f"{incident['id']}: rolled back {service['id']} to {rollback['rolled_back_to']}, assigned to {user['handle']}")
    return incident


def test_pagination_and_placement():
    first = world.services_list()
    assert first["page"] == 1 and first["total_pages"] == 4 and first["total"] == 34 and first["next_page"] == 2
    assert len(first["items"]) == 10 and world.services_list(4)["next_page"] is None
    assert page_of("checkout-worker") == 1 and page_of("payments-api") == 2
    assert page_of("checkout-api") == 3 and page_of("payments-worker") == 4
    with pytest.raises(world.ToolError, match="past the end"):
        world.services_list(5)
    with pytest.raises(world.ToolError, match="page must be"):
        world.services_list(0)
    deploys = world.deploys_list("checkout-api")
    assert deploys["total_pages"] == 2 and deploys["items"][0]["status"] == "live"
    assert world.deploys_list("auth-api")["total"] == 3


def test_search_matches_slugs_only_and_alerts_name_services():
    assert world.services_search("Checkout API") == []
    assert [s["id"] for s in world.services_search("checkout-api")] == ["checkout-api", "checkout-api-staging"]
    assert [s["id"] for s in world.services_search("checkout")] == ["checkout-worker", "checkout-api", "checkout-api-staging"]
    alert = world.alerts_get("A-1042")
    assert alert["service_name"] == "Checkout API" and alert["environment"] == "prod" and alert["status"] == "firing"
    assert [a["id"] for a in world.alerts_list("firing")["items"]] == ["A-1057", "A-1042"]
    assert world.alerts_list("all")["total"] == 40 and world.alerts_list("resolved")["total"] == 38
    with pytest.raises(world.ToolError, match="unknown alert"):
        world.alerts_get("A-9999")
    with pytest.raises(world.ToolError, match="keyed by slug"):
        world.services_get("Checkout API")


def test_deploy_history_points_at_the_bad_change():
    service = world.services_get("checkout-api")
    current = world.deploys_get(service["current_deploy_id"])
    assert "PAYMENT_TIMEOUT_MS=200" in current["message"] and current["status"] == "live"
    assert current["previous_deploy_id"] == world.GOOD_DEPLOY["checkout-api"]
    previous = world.deploys_get(current["previous_deploy_id"])
    assert previous["status"] == "superseded" and previous["deployed_at"] < current["deployed_at"]
    assert world.services_get("checkout-worker")["current_deploy_id"] not in world.BAD_DEPLOY.values()


def test_logs_and_metrics_traps():
    with pytest.raises(world.ToolError, match="window too large"):
        world.logs_query("checkout-api", "2026-10-18T10:00:00Z", "2026-10-18T14:00:00Z")
    with pytest.raises(world.ToolError, match="ISO 8601"):
        world.logs_query("checkout-api", "yesterday", "2026-10-18T14:00:00Z")
    with pytest.raises(world.ToolError, match="timezone"):
        world.logs_query("checkout-api", "2026-10-18T13:00:00", "2026-10-18T13:30:00Z")
    with pytest.raises(world.ToolError, match="after since"):
        world.logs_query("checkout-api", "2026-10-18T13:30:00Z", "2026-10-18T13:00:00Z")
    before = world.logs_query("checkout-api", "2026-10-18T12:30:00Z", "2026-10-18T13:10:00Z")
    after = world.logs_query("checkout-api", "2026-10-18T13:20:00Z", "2026-10-18T14:00:00Z")
    assert all(e["level"] == "INFO" for e in before)
    assert any("PAYMENT_TIMEOUT_MS=200" in e["message"] for e in after if e["level"] == "ERROR")
    assert len(world.logs_query("checkout-api", "2026-10-18T13:20:00Z", "2026-10-18T14:00:00Z", limit=5)) == 5
    rate = world.metrics_query("checkout-api", "error_rate", "1h")["points"]
    assert rate[0]["value"] == 0.4 and rate[-1]["value"] == 12.5
    assert all(p["value"] == 0.4 for p in world.metrics_query("auth-api", "error_rate", "1h")["points"])
    with pytest.raises(world.ToolError, match="metric must be"):
        world.metrics_query("checkout-api", "errors", "1h")
    with pytest.raises(world.ToolError, match="window must look like"):
        world.metrics_query("checkout-api", "error_rate", "one hour")


def test_rollback_needs_a_valid_token_and_changes_the_live_deploy():
    good = world.GOOD_DEPLOY["checkout-api"]
    with pytest.raises(world.ToolError, match="confirmation_token required"):
        world.deploys_rollback("checkout-api", good)
    with pytest.raises(world.ToolError, match="invalid or expired"):
        world.deploys_rollback("checkout-api", good, "apr_nope")
    other = world.approvals_request("payments-worker", "rollback")["token"]
    with pytest.raises(world.ToolError, match="invalid or expired"):
        world.deploys_rollback("checkout-api", good, other)
    token = world.approvals_request("checkout-api", "rollback")["token"]
    with pytest.raises(world.ToolError, match="belongs to"):
        world.deploys_rollback("checkout-api", world.GOOD_DEPLOY["payments-worker"], token)
    result = world.deploys_rollback("checkout-api", good, token)
    assert result["rolled_back_to"] == good and world.services_get("checkout-api")["current_deploy_id"] == good
    assert world.deploys_get(result["rolled_back_from"])["status"] == "rolled_back"
    with pytest.raises(world.ToolError, match="invalid or expired"):  # single use
        world.deploys_rollback("checkout-api", result["rolled_back_from"], token)
    with pytest.raises(world.ToolError, match="action must be"):
        world.approvals_request("checkout-api", "delete")


def test_shortcut_outage_and_schema_switches():
    assert world.probe("deploys_rollback_latest") and world.probe("alerts_get") and not world.probe("nope")
    world.set_switch("rollback_outage", True)
    assert not world.probe("deploys_rollback_latest")
    with pytest.raises(world.ToolError, match="503"):
        world.execute_tool("deploys_rollback_latest", {"service_id": "checkout-api"})
    world.set_switch("rollback_outage", None)
    result = world.deploys_rollback_latest("checkout-api")
    assert result["rolled_back_to"] == world.GOOD_DEPLOY["checkout-api"] and result["via"] == "deploys_rollback_latest"
    world.reset()
    world.set_switch("schema_reason", True)
    with pytest.raises(world.ToolError, match="missing required property 'reason'"):
        world.deploys_rollback_latest("checkout-api")
    token = world.approvals_request("checkout-api", "rollback")["token"]
    with pytest.raises(world.ToolError, match="missing required property 'reason'"):
        world.deploys_rollback("checkout-api", world.GOOD_DEPLOY["checkout-api"], token)
    assert world.deploys_rollback("checkout-api", world.GOOD_DEPLOY["checkout-api"], token, reason="bad config")["reason"] == "bad config"
    names = {t["name"]: t for t in world.tool_definitions(reason=False)}
    assert len(names) == 20 and "reason" not in names["deploys_rollback"]["input_schema"]["properties"]
    updated = {t["name"]: t for t in world.tool_definitions(reason=True)}
    for name in ("deploys_rollback", "deploys_rollback_latest"):
        assert "reason" in updated[name]["input_schema"]["required"]
    world.set_switch("tools_updated", True)
    assert "reason" in {t["name"]: t for t in world.current_tools()}["deploys_rollback"]["input_schema"]["required"]


def test_writes_reject_wrong_value_kinds():
    with pytest.raises(world.ToolError, match="severity must be"):
        world.incidents_create("x", "checkout-api", "high", "u_1032")
    with pytest.raises(world.ToolError, match="assignee_id must be a user id such as u_1032"):
        world.incidents_create("x", "checkout-api", "sev1", "@maya")
    with pytest.raises(world.ToolError, match="unknown user"):
        world.incidents_create("x", "checkout-api", "sev1", "u_9999")
    with pytest.raises(world.ToolError, match="unknown channel"):
        world.chat_post("#ops", "hello")
    incident = world.incidents_create("Checkout 5xx", "checkout-api", "sev1", "u_1032")
    assert incident["id"] == "INC-2041" and incident["status"] == "open"
    updated = world.incidents_update(incident["id"], "mitigated", "rolled back")
    assert updated["status"] == "mitigated" and updated["notes"] == ["rolled back"]
    with pytest.raises(world.ToolError, match="status must be"):
        world.incidents_update(incident["id"], "closed")
    assert world.teams_get("t_payments")["oncall_user_id"] == "u_1032"
    assert world.users_get("u_1032")["handle"] == "@maya"
    with pytest.raises(world.ToolError, match="handles such as @maya are not ids"):
        world.users_get("@maya")
    assert world.oncall_schedule("t_payments", "2026-10-18")[0]["user_id"] == "u_1032"
    assert world.services_dependencies("checkout-api")[0] == "payments-api"
    assert world.runbooks_search("rollback")[0]["title"] == "Rolling back a deploy"


@pytest.mark.parametrize("alert_id", ["A-1042", "A-1057"])
def test_task_succeeded_only_after_the_full_sequence(alert_id):
    task = world.task_for(alert_id)
    assert world.alert_in_task(task)["id"] == alert_id and not world.task_succeeded(task)
    assert not world.task_succeeded("Alert A-1001 fired.")  # a resolved alert on a healthy service
    solve(alert_id)
    assert world.task_succeeded(task)
    world.reset()
    assert not world.task_succeeded(task)


def test_outputs_are_json_and_copies():
    page = world.execute_tool("services_list", {"page": 3})
    json.dumps(page)
    page["items"][0]["name"] = "changed"
    assert world.services_list(3)["items"][0]["name"] != "changed"
    alert = world.execute_tool("alerts_get", {"alert_id": "A-1042"})
    alert["labels"].clear()
    assert world.alerts_get("A-1042")["labels"]
    with pytest.raises(world.ToolError, match="unknown tool"):
        world.execute_tool("nope", {})
