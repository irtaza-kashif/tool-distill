"""The incident demo's fake world: pagination, the upstream cause, the hidden rules, switches and the success check."""

import json

import incident_tools as world
import pytest

SWITCHES = ("rollback_outage", "schema_reason", "tools_updated")


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
    victim = world.services_get(alert["service_name"].lower().replace(" ", "-"))
    cause = max(world.services_dependencies(victim["id"]), key=lambda d: d["current_deploy"]["deployed_at"])
    team = world.teams_get(cause["team_id"])
    incident = world.incidents_create(f"{alert['title']}: {cause['id']} rolled back", cause["id"], "sev2", team["oncall_user_id"])
    token = world.approvals_request(cause["id"], "rollback", incident["id"])["token"]
    rollback = world.deploys_rollback(cause["id"], cause["current_deploy"]["previous_deploy_id"], token)
    world.alerts_ack(alert_id, incident["id"])
    world.chat_post("#incidents", f"{incident['id']}: rolled back {cause['id']} to {rollback['rolled_back_to']}")
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
    deploys = world.deploys_list("payments-api")
    assert deploys["total_pages"] == 2 and deploys["items"][0]["status"] == "live"
    assert world.deploys_list("auth-api")["total"] == 3


def test_search_matches_slugs_only_and_alerts_name_victims():
    assert world.services_search("Checkout API") == []
    assert [s["id"] for s in world.services_search("checkout")] == ["checkout-worker", "checkout-api", "checkout-api-staging"]
    alert = world.alerts_get("A-1042")
    assert alert["service_name"] == "Checkout API" and alert["environment"] == "prod" and alert["status"] == "firing"
    assert [a["id"] for a in world.alerts_list("firing")["items"]] == ["A-1057", "A-1042"]
    assert world.alerts_list("all")["total"] == 40 and world.alerts_list("resolved")["total"] == 38
    with pytest.raises(world.ToolError, match="unknown alert"):
        world.alerts_get("A-9999")
    with pytest.raises(world.ToolError, match="keyed by slug"):
        world.services_get("Checkout API")


def test_the_cause_is_upstream_of_the_victim():
    victim = world.services_get("checkout-api")
    assert victim["current_deploy"]["deployed_at"] < "2026-10-16"  # nothing recent on the alerted service itself
    deps = world.services_dependencies("checkout-api")
    assert [d["id"] for d in deps] == ["payments-api", "inventory-api", "pricing-engine", "session-store"]
    cause = max(deps, key=lambda d: d["current_deploy"]["deployed_at"])
    assert cause["id"] == "payments-api" and "PAYMENT_TIMEOUT_MS=200" in cause["current_deploy"]["message"]
    assert cause["current_deploy"]["previous_deploy_id"] == world.GOOD_DEPLOY["payments-api"]
    decoy = next(d for d in deps if d["id"] == "session-store")
    assert decoy["current_deploy"]["deployed_at"] > "2026-10-17"  # deployed yesterday: suspicious but innocent
    assert world.CAUSE_OF == {"checkout-api": "payments-api", "payments-worker": "ledger-api"}
    worker_deps = world.services_dependencies("payments-worker")
    assert max(worker_deps, key=lambda d: d["current_deploy"]["deployed_at"])["id"] == "ledger-api"


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
    assert any("payments-api" in e["message"] for e in after if e["level"] == "ERROR")  # the logs point upstream
    cause_logs = world.logs_query("payments-api", "2026-10-18T13:20:00Z", "2026-10-18T14:00:00Z")
    assert any("PAYMENT_TIMEOUT_MS=200" in e["message"] for e in cause_logs)
    assert len(world.logs_query("checkout-api", "2026-10-18T13:20:00Z", "2026-10-18T14:00:00Z", limit=5)) == 5
    rate = world.metrics_query("checkout-api", "error_rate", "1h")["points"]
    assert rate[0]["value"] == 0.4 and rate[-1]["value"] == 12.5
    assert all(p["value"] == 0.4 for p in world.metrics_query("auth-api", "error_rate", "1h")["points"])
    with pytest.raises(world.ToolError, match="metric must be"):
        world.metrics_query("checkout-api", "errors", "1h")
    with pytest.raises(world.ToolError, match="window must look like"):
        world.metrics_query("checkout-api", "error_rate", "one hour")


def test_rollback_needs_an_incident_an_approval_and_a_valid_token():
    good = world.GOOD_DEPLOY["payments-api"]
    with pytest.raises(world.ToolError, match="confirmation_token required"):
        world.deploys_rollback("payments-api", good)
    with pytest.raises(world.ToolError, match="must reference an open incident"):
        world.approvals_request("payments-api", "rollback")
    with pytest.raises(world.ToolError, match="unknown incident"):
        world.approvals_request("payments-api", "rollback", "INC-9999")
    incident = world.incidents_create("Checkout 5xx", "payments-api", "sev2", "u_1032")
    assert world.approvals_request("payments-api", "restart")["token"].startswith("apr_")  # only rollbacks need an incident
    with pytest.raises(world.ToolError, match="invalid or expired"):
        world.deploys_rollback("payments-api", good, "apr_nope")
    other = world.approvals_request("ledger-api", "rollback", incident["id"])["token"]
    with pytest.raises(world.ToolError, match="invalid or expired"):
        world.deploys_rollback("payments-api", good, other)
    token = world.approvals_request("payments-api", "rollback", incident["id"])["token"]
    with pytest.raises(world.ToolError, match="belongs to"):
        world.deploys_rollback("payments-api", world.GOOD_DEPLOY["ledger-api"], token)
    result = world.deploys_rollback("payments-api", good, token)
    assert result["rolled_back_to"] == good and world.services_get("payments-api")["current_deploy"]["id"] == good
    assert world.deploys_get(result["rolled_back_from"])["status"] == "rolled_back"
    with pytest.raises(world.ToolError, match="invalid or expired"):  # single use
        world.deploys_rollback("payments-api", result["rolled_back_from"], token)
    world.incidents_update(incident["id"], "resolved")
    with pytest.raises(world.ToolError, match="approvals need an open incident"):
        world.approvals_request("payments-api", "rollback", incident["id"])
    with pytest.raises(world.ToolError, match="action must be"):
        world.approvals_request("payments-api", "delete")


def test_shortcut_outage_and_schema_switches():
    assert world.probe("deploys_rollback_latest") and world.probe("alerts_get") and not world.probe("nope")
    world.set_switch("rollback_outage", True)
    assert not world.probe("deploys_rollback_latest")
    with pytest.raises(world.ToolError, match="503"):
        world.execute_tool("deploys_rollback_latest", {"service_id": "payments-api"})
    world.set_switch("rollback_outage", None)
    result = world.deploys_rollback_latest("payments-api")
    assert result["rolled_back_to"] == world.GOOD_DEPLOY["payments-api"] and result["via"] == "deploys_rollback_latest"
    world.reset()
    world.set_switch("schema_reason", True)
    with pytest.raises(world.ToolError, match="missing required property 'reason'"):
        world.deploys_rollback_latest("payments-api")
    incident = world.incidents_create("x", "payments-api", "sev2", "u_1032")
    token = world.approvals_request("payments-api", "rollback", incident["id"])["token"]
    with pytest.raises(world.ToolError, match="missing required property 'reason'"):
        world.deploys_rollback("payments-api", world.GOOD_DEPLOY["payments-api"], token)
    assert world.deploys_rollback("payments-api", world.GOOD_DEPLOY["payments-api"], token, reason="bad config")["reason"] == "bad config"
    names = {t["name"]: t for t in world.tool_definitions(reason=False)}
    assert len(names) == 21 and "reason" not in names["deploys_rollback"]["input_schema"]["properties"]
    assert "confirmation_token" not in names["deploys_rollback"]["input_schema"]["required"]  # the API, not the schema, enforces it
    updated = {t["name"]: t for t in world.tool_definitions(reason=True)}
    for name in ("deploys_rollback", "deploys_rollback_latest"):
        assert "reason" in updated[name]["input_schema"]["required"]
    world.set_switch("tools_updated", True)
    assert "reason" in {t["name"]: t for t in world.current_tools()}["deploys_rollback"]["input_schema"]["required"]


def test_writes_reject_wrong_value_kinds():
    with pytest.raises(world.ToolError, match="severity must be one of: sev1, sev2, sev3"):
        world.incidents_create("x", "payments-api", "high", "u_1032")
    with pytest.raises(world.ToolError, match="assignee_id must be a user id such as u_1032"):
        world.incidents_create("x", "payments-api", "sev1", "@maya")
    with pytest.raises(world.ToolError, match="unknown user"):
        world.incidents_create("x", "payments-api", "sev1", "u_9999")
    with pytest.raises(world.ToolError, match="unknown channel"):
        world.chat_post("#ops", "hello")
    incident = world.incidents_create("Checkout 5xx", "payments-api", "sev1", "u_1032")
    assert incident["id"] == "INC-2041" and incident["status"] == "open"
    updated = world.incidents_update(incident["id"], "mitigated", "rolled back")
    assert updated["status"] == "mitigated" and updated["notes"] == ["rolled back"]
    with pytest.raises(world.ToolError, match="status must be"):
        world.incidents_update(incident["id"], "closed")
    with pytest.raises(world.ToolError, match="unknown incident"):
        world.alerts_ack("A-1042", "INC-9999")
    acked = world.alerts_ack("A-1042", incident["id"])
    assert acked["status"] == "acknowledged" and acked["incident_id"] == incident["id"]
    with pytest.raises(world.ToolError, match="not firing"):
        world.alerts_ack("A-1042", incident["id"])
    assert [a["id"] for a in world.alerts_list("acknowledged")["items"]] == ["A-1042"]
    assert world.teams_get("t_payments")["oncall_user_id"] == "u_1032"
    assert world.users_get("u_1032")["handle"] == "@maya"
    with pytest.raises(world.ToolError, match="handles such as @maya are not ids"):
        world.users_get("@maya")
    assert world.oncall_schedule("t_payments", "2026-10-18")[0]["user_id"] == "u_1032"
    assert world.runbooks_search("rollback")[0]["title"] == "Rolling back a deploy"


@pytest.mark.parametrize("alert_id", ["A-1042", "A-1057"])
def test_task_succeeded_only_after_the_full_sequence(alert_id):
    task = world.task_for(alert_id)
    assert world.alert_in_task(task)["id"] == alert_id and not world.task_succeeded(task)
    assert not world.task_succeeded("Alert A-1001 fired.")  # a resolved alert on a healthy service
    solve(alert_id)
    assert world.task_succeeded(task)
    world.reset()
    assert not world.task_succeeded(task) and world.alerts_get(alert_id)["status"] == "firing"


def test_rolling_back_the_victim_does_not_count():
    """The victim has nothing to roll back to that helps; only the cause's rollback satisfies the task."""
    task = world.task_for("A-1042")
    incident = world.incidents_create("x", "checkout-api", "sev2", "u_1032")
    token = world.approvals_request("checkout-api", "rollback", incident["id"])["token"]
    world.deploys_rollback("checkout-api", world.services_get("checkout-api")["current_deploy"]["previous_deploy_id"], token)
    world.alerts_ack("A-1042", incident["id"])
    world.chat_post("#incidents", "rolled back checkout-api")
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
