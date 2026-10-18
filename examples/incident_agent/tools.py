"""Fake base tools for the incident demo: an alerting, deploy, logs, metrics, on-call and chat world, all in memory.

The world is deliberately hard to navigate the first time: paginated lists, display names that differ from ids, a
near-duplicate service, ids that must be followed hop by hop, a log query that rejects wide windows, a rollback
that needs an approval token, writes that reject wrong value kinds, and an advertised one-call rollback that may be
down. A distilled tool that knows the sequence needs none of that.

Switches (environment variables, read at call time; ``set_switch()`` overrides them in-process; the JSON file named
by INCIDENT_SWITCH_FILE overrides both, so a running MCP server can be flipped):

  INCIDENT_ROLLBACK_OUTAGE=1  deploys_rollback_latest raises "503 Service Unavailable"
  INCIDENT_SCHEMA_REASON=1    the deploy API now requires a ``reason`` on both rollback tools (the executor enforces it)
  INCIDENT_TOOLS_UPDATED=1    the tool definitions include the required ``reason`` field

Keeping the API change separate from the definition change reproduces an API that changed before its tool
definition did. "Now" is fixed so every timestamp is deterministic.
"""

import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = [
    "ALERTS",
    "GOOD_DEPLOY",
    "NOW",
    "SERVICES",
    "STATE",
    "TEAMS",
    "TOOLS",
    "USERS",
    "ToolError",
    "alert_in_task",
    "current_tools",
    "execute_tool",
    "probe",
    "reset",
    "set_switch",
    "switch",
    "task_for",
    "task_succeeded",
    "tool_definitions",
]


class ToolError(Exception):
    """Raised by a fake base tool to signal failure: the executor contract."""


# ------------------------------------------------------------------------------- switches

_SWITCH_ENV = {
    "rollback_outage": "INCIDENT_ROLLBACK_OUTAGE",
    "schema_reason": "INCIDENT_SCHEMA_REASON",
    "tools_updated": "INCIDENT_TOOLS_UPDATED",
}
_overrides: dict[str, bool] = {}


def switch(name: str) -> bool:
    """In-process override, else the JSON file named by INCIDENT_SWITCH_FILE, else the environment variable."""
    if name not in _SWITCH_ENV:
        raise KeyError(name)
    if name in _overrides:
        return _overrides[name]
    switch_file = os.environ.get("INCIDENT_SWITCH_FILE")
    if switch_file:
        try:
            data = json.loads(Path(switch_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict) and name in data:
            return bool(data[name])
    return os.environ.get(_SWITCH_ENV[name], "").strip().lower() in ("1", "true", "yes", "on")


def set_switch(name: str, value: bool | None) -> None:
    """Override a switch in-process (``None`` goes back to the environment variable)."""
    if name not in _SWITCH_ENV:
        raise KeyError(name)
    if value is None:
        _overrides.pop(name, None)
    else:
        _overrides[name] = bool(value)


# ----------------------------------------------------------------------------- fake world

NOW = datetime(2026, 10, 18, 14, 0, tzinfo=UTC)
PAGE_SIZE = 10


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(**delta: float) -> str:
    return _iso(NOW - timedelta(**delta))


def _short_hash(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:6]


TEAMS: dict[str, dict[str, Any]] = {
    "t_payments": {"id": "t_payments", "name": "Payments", "oncall_user_id": "u_1032", "channel": "#team-payments"},
    "t_platform": {"id": "t_platform", "name": "Platform", "oncall_user_id": "u_1077", "channel": "#team-platform"},
    "t_identity": {"id": "t_identity", "name": "Identity", "oncall_user_id": "u_1040", "channel": "#team-identity"},
    "t_catalog": {"id": "t_catalog", "name": "Catalog", "oncall_user_id": "u_1051", "channel": "#team-catalog"},
    "t_fulfillment": {"id": "t_fulfillment", "name": "Fulfillment", "oncall_user_id": "u_1063", "channel": "#team-fulfillment"},
    "t_data": {"id": "t_data", "name": "Data", "oncall_user_id": "u_1088", "channel": "#team-data"},
}

USERS: dict[str, dict[str, Any]] = {
    "u_1032": {"id": "u_1032", "name": "Maya Chen", "handle": "@maya", "team_id": "t_payments"},
    "u_1033": {"id": "u_1033", "name": "Tomás Reyes", "handle": "@tomas", "team_id": "t_payments"},
    "u_1040": {"id": "u_1040", "name": "Priya Nair", "handle": "@priya", "team_id": "t_identity"},
    "u_1051": {"id": "u_1051", "name": "Jonas Weber", "handle": "@jonas", "team_id": "t_catalog"},
    "u_1063": {"id": "u_1063", "name": "Aisha Bello", "handle": "@aisha", "team_id": "t_fulfillment"},
    "u_1077": {"id": "u_1077", "name": "Diego Alvarez", "handle": "@diego", "team_id": "t_platform"},
    "u_1078": {"id": "u_1078", "name": "Lena Fischer", "handle": "@lena", "team_id": "t_platform"},
    "u_1088": {"id": "u_1088", "name": "Sam Okafor", "handle": "@sam", "team_id": "t_data"},
}

CHANNELS = ["#incidents", "#deploys", *(team["channel"] for team in TEAMS.values())]

# (slug, display name, team, environment, tier). Order is the list order, 10 per page:
# checkout-worker (a decoy) is on page 1, payments-api (a decoy) on page 2, checkout-api on page 3,
# checkout-api-staging right after it, payments-worker on page 4.
_SERVICE_SPECS: list[tuple[str, str, str, str, int]] = [
    ("auth-api", "Auth API", "t_identity", "prod", 1),
    ("auth-worker", "Auth Worker", "t_identity", "prod", 2),
    ("checkout-worker", "Checkout Worker", "t_payments", "prod", 2),
    ("catalog-api", "Catalog API", "t_catalog", "prod", 1),
    ("catalog-indexer", "Catalog Indexer", "t_catalog", "prod", 3),
    ("search-api", "Search API", "t_catalog", "prod", 1),
    ("email-sender", "Email Sender", "t_platform", "prod", 2),
    ("feature-flags", "Feature Flags", "t_platform", "prod", 1),
    ("gateway-edge", "Gateway Edge", "t_platform", "prod", 1),
    ("image-resizer", "Image Resizer", "t_platform", "prod", 3),
    ("inventory-api", "Inventory API", "t_fulfillment", "prod", 1),
    ("inventory-sync", "Inventory Sync", "t_fulfillment", "prod", 2),
    ("ledger-api", "Ledger API", "t_payments", "prod", 1),
    ("notifications-api", "Notifications API", "t_platform", "prod", 2),
    ("payments-api", "Payments API", "t_payments", "prod", 1),
    ("pricing-engine", "Pricing Engine", "t_catalog", "prod", 1),
    ("recommendations", "Recommendations", "t_catalog", "prod", 2),
    ("reports-batch", "Reports Batch", "t_data", "prod", 3),
    ("reviews-api", "Reviews API", "t_catalog", "prod", 2),
    ("risk-scorer", "Risk Scorer", "t_payments", "prod", 1),
    ("search-indexer", "Search Indexer", "t_catalog", "prod", 3),
    ("session-store", "Session Store", "t_identity", "prod", 1),
    ("shipping-api", "Shipping API", "t_fulfillment", "prod", 1),
    ("tax-calculator", "Tax Calculator", "t_payments", "prod", 1),
    ("checkout-api", "Checkout API", "t_payments", "prod", 1),
    ("checkout-api-staging", "Checkout API (staging)", "t_payments", "staging", 1),
    ("webhooks-dispatcher", "Webhooks Dispatcher", "t_platform", "prod", 2),
    ("warehouse-router", "Warehouse Router", "t_fulfillment", "prod", 2),
    ("user-profiles", "User Profiles", "t_identity", "prod", 1),
    ("fraud-review", "Fraud Review", "t_payments", "prod", 2),
    ("data-export", "Data Export", "t_data", "prod", 3),
    ("metrics-collector", "Metrics Collector", "t_platform", "prod", 2),
    ("payments-worker", "Payments Worker", "t_platform", "prod", 1),
    ("returns-api", "Returns API", "t_fulfillment", "prod", 2),
]

# The two services with a bad deploy: what it changed, when, and what the logs say afterwards.
_INCIDENT_SPECS: dict[str, dict[str, Any]] = {
    "checkout-api": {
        "minutes_ago": 40,
        "message": "Lower upstream timeout: PAYMENT_TIMEOUT_MS=200 (was 2000)",
        "error": "TimeoutError: upstream payment gateway did not respond within 200 ms (PAYMENT_TIMEOUT_MS=200)",
        "alert": {"id": "A-1042", "title": "High 5xx rate on Checkout API (prod)", "metric": "http_5xx_rate", "threshold": "5%"},
    },
    "payments-worker": {
        "minutes_ago": 35,
        "message": "Set QUEUE_BATCH_SIZE=5000 (was 500)",
        "error": "QueueBacklog: batch of 5000 messages exceeded the visibility timeout (QUEUE_BATCH_SIZE=5000)",
        "alert": {"id": "A-1057", "title": "Queue latency on Payments Worker (prod)", "metric": "queue_latency_p95", "threshold": "30s"},
    },
}


def _build_deploys() -> dict[str, list[dict[str, Any]]]:
    """Per service, newest first. Incident services get 14 deploys (two pages); the rest three old ones."""
    history: dict[str, list[dict[str, Any]]] = {}
    for slug, _name, _team, _env, _tier in _SERVICE_SPECS:
        incident = _INCIDENT_SPECS.get(slug)
        count = 14 if incident else 3
        deploys: list[dict[str, Any]] = []
        for n in range(count, 0, -1):  # n = count is the newest
            deploy_id = f"dep_{_short_hash(slug, str(n))}"
            if incident and n == count:
                when = NOW - timedelta(minutes=incident["minutes_ago"])
                message = incident["message"]
            elif incident and n == count - 1:
                when = NOW - timedelta(hours=26)
                message = "Bump dependencies and refresh certificates"
            else:
                when = NOW - timedelta(days=3 + (count - n) * 2, hours=n % 5)
                message = f"Routine release {n}"
            deploys.append(
                {
                    "id": deploy_id,
                    "service_id": slug,
                    "version": f"v{when.strftime('%Y.%m.%d')}-{n}",
                    "commit": _short_hash(slug, "commit", str(n)) + _short_hash(str(n), slug)[:2],
                    "message": message,
                    "deployed_at": _iso(when),
                    "status": "live" if n == count else "superseded",
                    "previous_deploy_id": None,
                }
            )
        for older, newer in zip(deploys[1:], deploys[:-1]):
            newer["previous_deploy_id"] = older["id"]
        history[slug] = deploys
    return history


_DEPLOYS = _build_deploys()
DEPLOY_BY_ID: dict[str, dict[str, Any]] = {d["id"]: d for deploys in _DEPLOYS.values() for d in deploys}
GOOD_DEPLOY: dict[str, str] = {slug: _DEPLOYS[slug][1]["id"] for slug in _INCIDENT_SPECS}
BAD_DEPLOY: dict[str, str] = {slug: _DEPLOYS[slug][0]["id"] for slug in _INCIDENT_SPECS}

SERVICES: list[dict[str, Any]] = [
    {"id": slug, "name": name, "team_id": team, "env": env, "tier": tier, "current_deploy_id": _DEPLOYS[slug][0]["id"]}
    for slug, name, team, env, tier in _SERVICE_SPECS
]
SERVICE_BY_ID: dict[str, dict[str, Any]] = {s["id"]: s for s in SERVICES}


def _build_alerts() -> list[dict[str, Any]]:
    """40 alerts, newest first: the two firing ones for the incident services and 38 resolved historical ones."""
    alerts: list[dict[str, Any]] = []
    for slug, spec in _INCIDENT_SPECS.items():
        service = SERVICE_BY_ID[slug]
        alert = spec["alert"]
        alerts.append(
            {
                "id": alert["id"],
                "status": "firing",
                "title": alert["title"],
                "service_name": service["name"],
                "environment": service["env"],
                "fired_at": _ago(minutes=spec["minutes_ago"] - 6),
                "labels": {"metric": alert["metric"], "threshold": alert["threshold"], "severity": "high"},
                "summary": f"{alert['metric']} above {alert['threshold']} for 5 minutes on {service['name']} ({service['env']}).",
            }
        )
    metrics = ["cpu_high", "disk_usage", "http_5xx_rate", "latency_p95", "queue_latency_p95"]
    for n in range(38):
        service = SERVICES[(n * 7) % len(SERVICES)]
        metric = metrics[n % len(metrics)]
        alerts.append(
            {
                "id": f"A-{1001 + n}",
                "status": "resolved",
                "title": f"{metric} on {service['name']} ({service['env']})",
                "service_name": service["name"],
                "environment": service["env"],
                "fired_at": _ago(days=1 + n, hours=n % 7),
                "labels": {"metric": metric, "threshold": "n/a", "severity": "low"},
                "summary": f"{metric} alert on {service['name']}, resolved.",
            }
        )
    alerts.sort(key=lambda a: a["fired_at"], reverse=True)
    return alerts


ALERTS: list[dict[str, Any]] = _build_alerts()
ALERT_BY_ID: dict[str, dict[str, Any]] = {a["id"]: a for a in ALERTS}

_RUNBOOKS = [
    {"title": "Rolling back a deploy", "url": "https://runbooks.example/deploys/rollback", "summary": "Request an approval token with approvals_request(service_id, 'rollback'), then deploys_rollback."},
    {"title": "Opening an incident", "url": "https://runbooks.example/incidents/open", "summary": "Use severity sev1-sev3 and assign the team's on-call user id."},
    {"title": "Reading service logs", "url": "https://runbooks.example/logs", "summary": "logs_query accepts windows of up to 60 minutes."},
]

_DEPENDENCIES = {
    "checkout-api": ["payments-api", "inventory-api", "pricing-engine", "session-store"],
    "payments-worker": ["ledger-api", "payments-api", "notifications-api"],
}

STATE: dict[str, Any] = {}


def reset() -> None:
    """Forget every approval, rollback, incident and chat post, and put every service back on its newest deploy."""
    STATE.clear()
    STATE.update({"approvals": {}, "rollbacks": [], "incidents": [], "posts": [], "next_incident": 2041, "next_post": 1})
    for slug, deploys in _DEPLOYS.items():
        SERVICE_BY_ID[slug]["current_deploy_id"] = deploys[0]["id"]
        for deploy in deploys:
            deploy["status"] = "live" if deploy is deploys[0] else "superseded"


reset()


# ----------------------------------------------------------------------------- base tools


def _page(items: list[dict[str, Any]], page: int, what: str) -> dict[str, Any]:
    if not isinstance(page, int) or page < 1:
        raise ToolError("page must be an integer starting at 1")
    total_pages = max(1, -(-len(items) // PAGE_SIZE))
    if page > total_pages:
        raise ToolError(f"page {page} is past the end: {what} has {total_pages} pages of {PAGE_SIZE}")
    start = (page - 1) * PAGE_SIZE
    return {
        "page": page,
        "total_pages": total_pages,
        "total": len(items),
        "items": deepcopy(items[start : start + PAGE_SIZE]),
        "next_page": page + 1 if page < total_pages else None,
    }


def _service(service_id: str) -> dict[str, Any]:
    service = SERVICE_BY_ID.get(service_id)
    if service is None:
        raise ToolError(f"unknown service '{service_id}' (services are keyed by slug, e.g. checkout-api; see services_search)")
    return service


def _deploy(deploy_id: str) -> dict[str, Any]:
    deploy = DEPLOY_BY_ID.get(deploy_id)
    if deploy is None:
        raise ToolError(f"unknown deploy '{deploy_id}'")
    return deploy


def _parse_ts(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise ToolError(f"{field} must be an ISO 8601 timestamp with a timezone, e.g. 2026-10-18T13:00:00Z")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ToolError(f"{field} must be an ISO 8601 timestamp with a timezone, e.g. 2026-10-18T13:00:00Z") from None
    if parsed.tzinfo is None:
        raise ToolError(f"{field} must include a timezone, e.g. 2026-10-18T13:00:00Z")
    return parsed.astimezone(UTC)


def _require_reason(reason: str | None) -> str | None:
    if switch("schema_reason") and not reason:
        raise ToolError("missing required property 'reason'")
    return reason


def alerts_list(status: str = "firing", page: int = 1) -> dict[str, Any]:
    if status not in ("firing", "resolved", "all"):
        raise ToolError("status must be one of: firing, resolved, all")
    items = [a for a in ALERTS if status == "all" or a["status"] == status]
    return _page(items, page, "alerts")


def alerts_get(alert_id: str) -> dict[str, Any]:
    alert = ALERT_BY_ID.get(alert_id)
    if alert is None:
        raise ToolError(f"unknown alert '{alert_id}'")
    return deepcopy(alert)


def services_list(page: int = 1) -> dict[str, Any]:
    return _page(SERVICES, page, "services")


def services_search(query: str) -> list[dict[str, Any]]:
    """Matches service ids (slugs) only: 'checkout-api' finds the service, 'Checkout API' does not."""
    needle = query.strip().lower()
    if not needle:
        raise ToolError("query must not be empty")
    return [deepcopy(s) for s in SERVICES if needle in s["id"]]


def services_get(service_id: str) -> dict[str, Any]:
    return deepcopy(_service(service_id))


def services_dependencies(service_id: str) -> list[str]:
    _service(service_id)
    return list(_DEPENDENCIES.get(service_id, []))


def deploys_list(service_id: str, page: int = 1) -> dict[str, Any]:
    _service(service_id)
    return _page(_DEPLOYS[service_id], page, f"deploys of {service_id}")


def deploys_get(deploy_id: str) -> dict[str, Any]:
    return deepcopy(_deploy(deploy_id))


def approvals_request(service_id: str, action: str) -> dict[str, Any]:
    _service(service_id)
    if action not in ("rollback", "restart", "scale"):
        raise ToolError("action must be one of: rollback, restart, scale")
    token = f"apr_{_short_hash(service_id, action, str(len(STATE['approvals'])))}"
    STATE["approvals"][token] = {"service_id": service_id, "action": action, "used": False}
    return {"token": token, "service_id": service_id, "action": action, "expires_at": _iso(NOW + timedelta(minutes=15))}


def _apply_rollback(service: dict[str, Any], target: dict[str, Any], reason: str | None, how: str) -> dict[str, Any]:
    current = _deploy(service["current_deploy_id"])
    current["status"] = "rolled_back"
    target["status"] = "live"
    service["current_deploy_id"] = target["id"]
    record = {
        "service_id": service["id"],
        "current_deploy_id": target["id"],
        "rolled_back_from": current["id"],
        "rolled_back_to": target["id"],
        "reason": reason,
        "via": how,
        "at": _iso(NOW),
    }
    STATE["rollbacks"].append(record)
    return deepcopy(record)


def deploys_rollback(service_id: str, to_deploy_id: str, confirmation_token: str | None = None, reason: str | None = None) -> dict[str, Any]:
    service = _service(service_id)
    if not confirmation_token:
        raise ToolError("confirmation_token required: call approvals_request(service_id, 'rollback') first")
    approval = STATE["approvals"].get(confirmation_token)
    if approval is None or approval["service_id"] != service_id or approval["action"] != "rollback" or approval["used"]:
        raise ToolError("invalid or expired confirmation_token for this service: request a new one with approvals_request")
    target = _deploy(to_deploy_id)
    if target["service_id"] != service_id:
        raise ToolError(f"deploy '{to_deploy_id}' belongs to '{target['service_id']}', not '{service_id}'")
    if to_deploy_id == service["current_deploy_id"]:
        raise ToolError(f"deploy '{to_deploy_id}' is already live")
    reason = _require_reason(reason)
    approval["used"] = True
    return _apply_rollback(service, target, reason, "deploys_rollback")


def deploys_rollback_latest(service_id: str, reason: str | None = None) -> dict[str, Any]:
    """One call: roll the service back to the deploy before the current one. May be down (the outage switch)."""
    if switch("rollback_outage"):
        raise ToolError("503 Service Unavailable")
    service = _service(service_id)
    reason = _require_reason(reason)
    current = _deploy(service["current_deploy_id"])
    if not current["previous_deploy_id"]:
        raise ToolError(f"'{service_id}' has no earlier deploy to roll back to")
    return _apply_rollback(service, _deploy(current["previous_deploy_id"]), reason, "deploys_rollback_latest")


def logs_query(service_id: str, since: str, until: str, limit: int = 50) -> list[dict[str, Any]]:
    _service(service_id)
    start = _parse_ts(since, "since")
    end = _parse_ts(until, "until")
    if end <= start:
        raise ToolError("until must be after since")
    if end - start > timedelta(minutes=60):
        raise ToolError("window too large: at most 60 minutes between since and until")
    if not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ToolError("limit must be an integer between 1 and 200")
    incident = _INCIDENT_SPECS.get(service_id)
    bad_at = NOW - timedelta(minutes=incident["minutes_ago"]) if incident else None
    entries: list[dict[str, Any]] = []
    moment = start.replace(second=0, microsecond=0)
    while moment <= end and moment <= NOW:
        if moment >= start:
            entries.append({"ts": _iso(moment), "level": "INFO", "message": f"{service_id}: handled 1,2{moment.minute:02d} requests"})
            if bad_at is not None and moment >= bad_at and moment.minute % 2 == 0:
                entries.append({"ts": _iso(moment), "level": "ERROR", "message": incident["error"]})
        moment += timedelta(minutes=1)
    return entries[-limit:]


def metrics_query(service_id: str, metric: str, window: str) -> dict[str, Any]:
    _service(service_id)
    if metric not in ("error_rate", "latency_p95", "rps", "queue_latency_p95"):
        raise ToolError("metric must be one of: error_rate, latency_p95, rps, queue_latency_p95")
    match = re.fullmatch(r"(\d+)([mh])", window.strip())
    if not match:
        raise ToolError("window must look like 15m or 2h")
    minutes = int(match.group(1)) * (60 if match.group(2) == "h" else 1)
    incident = _INCIDENT_SPECS.get(service_id)
    bad_at = NOW - timedelta(minutes=incident["minutes_ago"]) if incident else None
    baseline = {"error_rate": 0.4, "latency_p95": 180.0, "rps": 240.0, "queue_latency_p95": 2.5}[metric]
    spike = {"error_rate": 12.5, "latency_p95": 2400.0, "rps": 231.0, "queue_latency_p95": 48.0}[metric]
    points = []
    for n in range(6, 0, -1):
        moment = NOW - timedelta(minutes=minutes * n / 6)
        value = spike if (bad_at is not None and moment >= bad_at and metric != "rps") else baseline
        points.append({"ts": _iso(moment), "value": value})
    return {"service_id": service_id, "metric": metric, "window": window, "points": points}


def teams_get(team_id: str) -> dict[str, Any]:
    team = TEAMS.get(team_id)
    if team is None:
        raise ToolError(f"unknown team '{team_id}'")
    return deepcopy(team)


def users_get(user_id: str) -> dict[str, Any]:
    user = USERS.get(user_id)
    if user is None:
        raise ToolError(f"unknown user '{user_id}' (user ids look like u_1032; handles such as @maya are not ids)")
    return deepcopy(user)


def oncall_schedule(team_id: str, day: str) -> list[dict[str, Any]]:
    team = teams_get(team_id)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ToolError("day must be YYYY-MM-DD")
    members = [u for u in USERS.values() if u["team_id"] == team_id]
    primary = team["oncall_user_id"]
    secondary = next((u["id"] for u in members if u["id"] != primary), primary)
    return [
        {"user_id": primary, "role": "primary", "from": f"{day}T00:00:00Z", "to": f"{day}T23:59:59Z"},
        {"user_id": secondary, "role": "secondary", "from": f"{day}T00:00:00Z", "to": f"{day}T23:59:59Z"},
    ]


def incidents_create(title: str, service_id: str, severity: str, assignee_id: str) -> dict[str, Any]:
    _service(service_id)
    if not isinstance(title, str) or not title.strip():
        raise ToolError("title must not be empty")
    if severity not in ("sev1", "sev2", "sev3"):
        raise ToolError("severity must be one of: sev1, sev2, sev3")
    if not isinstance(assignee_id, str) or not re.fullmatch(r"u_\d+", assignee_id):
        raise ToolError(
            f"assignee_id must be a user id such as u_1032 (got {assignee_id!r}); "
            "find it with teams_get(service.team_id).oncall_user_id"
        )
    if assignee_id not in USERS:
        raise ToolError(f"unknown user '{assignee_id}'")
    incident = {
        "id": f"INC-{STATE['next_incident']}",
        "title": title.strip(),
        "service_id": service_id,
        "severity": severity,
        "assignee_id": assignee_id,
        "status": "open",
        "notes": [],
        "created_at": _iso(NOW),
    }
    STATE["next_incident"] += 1
    STATE["incidents"].append(incident)
    return deepcopy(incident)


def incidents_update(incident_id: str, status: str, note: str = "") -> dict[str, Any]:
    incident = next((i for i in STATE["incidents"] if i["id"] == incident_id), None)
    if incident is None:
        raise ToolError(f"unknown incident '{incident_id}'")
    if status not in ("open", "mitigated", "resolved"):
        raise ToolError("status must be one of: open, mitigated, resolved")
    incident["status"] = status
    if note:
        incident["notes"].append(note)
    return deepcopy(incident)


def chat_post(channel: str, message: str) -> dict[str, Any]:
    if channel not in CHANNELS:
        raise ToolError(f"unknown channel {channel!r}; channels: {', '.join(CHANNELS)}")
    if not isinstance(message, str) or not message.strip():
        raise ToolError("message must not be empty")
    post = {"id": f"msg_{STATE['next_post']:04d}", "channel": channel, "message": message, "ts": _iso(NOW)}
    STATE["next_post"] += 1
    STATE["posts"].append(post)
    return deepcopy(post)


def runbooks_search(query: str) -> list[dict[str, Any]]:
    needle = query.lower()
    hits = [r for r in _RUNBOOKS if any(word in f"{r['title']} {r['summary']}".lower() for word in needle.split())]
    return deepcopy(hits or _RUNBOOKS)


_HANDLERS = {
    "alerts_list": alerts_list,
    "alerts_get": alerts_get,
    "services_list": services_list,
    "services_search": services_search,
    "services_get": services_get,
    "services_dependencies": services_dependencies,
    "deploys_list": deploys_list,
    "deploys_get": deploys_get,
    "deploys_rollback": deploys_rollback,
    "deploys_rollback_latest": deploys_rollback_latest,
    "approvals_request": approvals_request,
    "logs_query": logs_query,
    "metrics_query": metrics_query,
    "teams_get": teams_get,
    "users_get": users_get,
    "oncall_schedule": oncall_schedule,
    "incidents_create": incidents_create,
    "incidents_update": incidents_update,
    "chat_post": chat_post,
    "runbooks_search": runbooks_search,
}


def execute_tool(name: str, args: dict[str, Any]) -> Any:
    """The executor: raises on failure, returns JSON-serializable values."""
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"unknown tool '{name}'")
    return handler(**args)


def probe(tool_name: str) -> bool:
    """Health probe: is the base tool reachable right now?"""
    if tool_name == "deploys_rollback_latest":
        return not switch("rollback_outage")
    return tool_name in _HANDLERS


# ---------------------------------------------------------------------- tool definitions


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


def tool_definitions(*, reason: bool) -> list[dict[str, Any]]:
    """Anthropic-format definitions; ``reason=True`` is the deploy API after its schema change."""
    page = {"type": "integer", "description": "1-based page number; responses carry next_page and total_pages"}
    rollback_props: dict[str, Any] = {
        "service_id": {"type": "string"},
        "to_deploy_id": {"type": "string"},
        "confirmation_token": {"type": "string", "description": "from approvals_request(service_id, 'rollback')"},
    }
    rollback_required = ["service_id", "to_deploy_id", "confirmation_token"]
    latest_props: dict[str, Any] = {"service_id": {"type": "string"}}
    latest_required = ["service_id"]
    if reason:
        rollback_props["reason"] = {"type": "string", "description": "why the rollback is needed"}
        rollback_required.append("reason")
        latest_props["reason"] = {"type": "string", "description": "why the rollback is needed"}
        latest_required.append("reason")
    return [
        {
            "name": "alerts_list",
            "description": "List alerts by status (firing, resolved or all), 10 per page.",
            "input_schema": _obj({"status": {"type": "string", "enum": ["firing", "resolved", "all"]}, "page": page}, []),
        },
        {
            "name": "alerts_get",
            "description": "One alert in full: title, the service's display name, environment, labels and summary.",
            "input_schema": _obj({"alert_id": {"type": "string"}}, ["alert_id"]),
        },
        {
            "name": "services_list",
            "description": "List services, 10 per page, with id (slug), display name, team_id, env, tier and current_deploy_id.",
            "input_schema": _obj({"page": page}, []),
        },
        {
            "name": "services_search",
            "description": "Find services whose id (slug) contains the query, e.g. 'checkout'.",
            "input_schema": _obj({"query": {"type": "string"}}, ["query"]),
        },
        {
            "name": "services_get",
            "description": "One service by id: team_id, env, tier and current_deploy_id.",
            "input_schema": _obj({"service_id": {"type": "string"}}, ["service_id"]),
        },
        {
            "name": "services_dependencies",
            "description": "The upstream service ids a service depends on.",
            "input_schema": _obj({"service_id": {"type": "string"}}, ["service_id"]),
        },
        {
            "name": "deploys_list",
            "description": "Deploy history of a service, newest first, 10 per page.",
            "input_schema": _obj({"service_id": {"type": "string"}, "page": page}, ["service_id"]),
        },
        {
            "name": "deploys_get",
            "description": "One deploy: version, commit, message, deployed_at, status and previous_deploy_id.",
            "input_schema": _obj({"deploy_id": {"type": "string"}}, ["deploy_id"]),
        },
        {
            "name": "deploys_rollback",
            "description": "Roll a service back to an earlier deploy. Needs a confirmation token from approvals_request.",
            "input_schema": _obj(rollback_props, rollback_required),
        },
        {
            "name": "deploys_rollback_latest",
            "description": "The fastest way to undo a bad deploy: one call rolls the service back to the deploy before the current one, approval included.",
            "input_schema": _obj(latest_props, latest_required),
        },
        {
            "name": "approvals_request",
            "description": "Request an approval token for an action on a service (rollback, restart or scale).",
            "input_schema": _obj({"service_id": {"type": "string"}, "action": {"type": "string", "enum": ["rollback", "restart", "scale"]}}, ["service_id", "action"]),
        },
        {
            "name": "logs_query",
            "description": "Log entries of a service between two ISO 8601 timestamps (at most 60 minutes apart), oldest first.",
            "input_schema": _obj(
                {"service_id": {"type": "string"}, "since": {"type": "string"}, "until": {"type": "string"}, "limit": {"type": "integer"}},
                ["service_id", "since", "until"],
            ),
        },
        {
            "name": "metrics_query",
            "description": "Six data points of a metric over a window such as 15m or 2h.",
            "input_schema": _obj(
                {"service_id": {"type": "string"}, "metric": {"type": "string", "enum": ["error_rate", "latency_p95", "rps", "queue_latency_p95"]}, "window": {"type": "string"}},
                ["service_id", "metric", "window"],
            ),
        },
        {
            "name": "teams_get",
            "description": "A team: name, oncall_user_id and chat channel.",
            "input_schema": _obj({"team_id": {"type": "string"}}, ["team_id"]),
        },
        {
            "name": "users_get",
            "description": "A user by id (u_1032): name, handle and team_id.",
            "input_schema": _obj({"user_id": {"type": "string"}}, ["user_id"]),
        },
        {
            "name": "oncall_schedule",
            "description": "Who is on call for a team on a day (YYYY-MM-DD).",
            "input_schema": _obj({"team_id": {"type": "string"}, "day": {"type": "string"}}, ["team_id", "day"]),
        },
        {
            "name": "incidents_create",
            "description": "Open an incident for a service. severity is sev1, sev2 or sev3; assignee_id is a user id.",
            "input_schema": _obj(
                {"title": {"type": "string"}, "service_id": {"type": "string"}, "severity": {"type": "string", "enum": ["sev1", "sev2", "sev3"]}, "assignee_id": {"type": "string"}},
                ["title", "service_id", "severity", "assignee_id"],
            ),
        },
        {
            "name": "incidents_update",
            "description": "Change an incident's status (open, mitigated, resolved) and add a note.",
            "input_schema": _obj({"incident_id": {"type": "string"}, "status": {"type": "string", "enum": ["open", "mitigated", "resolved"]}, "note": {"type": "string"}}, ["incident_id", "status"]),
        },
        {
            "name": "chat_post",
            "description": "Post a message to a channel such as #incidents.",
            "input_schema": _obj({"channel": {"type": "string"}, "message": {"type": "string"}}, ["channel", "message"]),
        },
        {
            "name": "runbooks_search",
            "description": "Search the runbooks by keyword.",
            "input_schema": _obj({"query": {"type": "string"}}, ["query"]),
        },
    ]


def current_tools() -> list[dict[str, Any]]:
    """The definitions matching the INCIDENT_TOOLS_UPDATED switch right now."""
    return tool_definitions(reason=switch("tools_updated"))


TOOLS: list[dict[str, Any]] = current_tools()


# ------------------------------------------------------------------------------- the task


def task_for(alert_id: str) -> str:
    return (
        f"Alert {alert_id} fired. Find the cause, roll back the bad deploy if a deploy caused it, open an incident "
        "assigned to the on-call engineer, and post a summary in #incidents."
    )


def alert_in_task(task: str) -> dict[str, Any] | None:
    match = re.search(r"\bA-\d{4}\b", task)
    return ALERT_BY_ID.get(match.group(0)) if match else None


def task_succeeded(task: str) -> bool:
    """The developer's own success check: the alert's service is back on its good deploy, an incident for it is
    assigned to the team's on-call engineer, and #incidents has a post naming the service."""
    alert = alert_in_task(task)
    if alert is None:
        return False
    service = next((s for s in SERVICES if s["name"] == alert["service_name"] and s["env"] == alert["environment"]), None)
    if service is None or service["id"] not in GOOD_DEPLOY:
        return False
    if service["current_deploy_id"] != GOOD_DEPLOY[service["id"]]:
        return False
    oncall = TEAMS[service["team_id"]]["oncall_user_id"]
    if not any(i["service_id"] == service["id"] and i["assignee_id"] == oncall and i["status"] in ("open", "mitigated") for i in STATE["incidents"]):
        return False
    needles = (service["id"].lower(), service["name"].lower())
    return any(p["channel"] == "#incidents" and any(n in p["message"].lower() for n in needles) for p in STATE["posts"])
