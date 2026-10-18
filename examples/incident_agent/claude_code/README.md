# The incident demo inside Claude Code

The same fake alerting, deploy, logs, metrics, on-call and chat tools as the SDK demo, served to Claude Code over
MCP, with the tooldistill plugin recording the session and turning a solved incident into a distilled tool.

| File | Purpose |
|---|---|
| `.mcp.json` | Claude Code's MCP config: the fake tools as the server `incidents` (runs `../mcp_server.py` through `uv`) |
| `tooldistill.toml` | tooldistill's config: `mcp_config = ".mcp.json"`, so the plugin reaches the same server under the same name |
| `switches.json` | the fake tools' switches, read live by every server process: outage, schema change, updated definitions |
| `.tooldistill/` | the store the plugin creates here (gitignored) |

Everything runs from this folder. The plugin's hooks and MCP server find `tooldistill.toml` through
`CLAUDE_PROJECT_DIR`, which Claude Code sets to the folder you start it in.

## Setup

- [uv](https://docs.astral.sh/uv/) on the PATH and Claude Code 2.1 or newer. Nothing else: the plugin runs the
  library from this checkout (`plugin-dev`), and the tools are fake.

```
cd examples/incident_agent/claude_code
claude --plugin-dir ../../../plugin-dev
```

Approve the project's `incidents` MCP server when asked. The session context mentions tooldistill and the session's
trace id.

## Walkthrough

1. **Discovery.** Set `"rollback_outage": true` in `switches.json`, so the one-call rollback fails and Claude has
   to find the long way: find the service behind the alert's display name, prove the deploy caused it, get an
   approval token, roll back, find the on-call engineer, open the incident, post the summary. Ask:

   > Alert A-1042 fired. Find the cause, roll back the bad deploy if a deploy caused it, open an incident assigned to the on-call engineer, and post a summary in #incidents.

   When it is done, run:

   > /tooldistill:distill handle a firing alert: find the cause, roll back, open an incident, post a summary

   Claude calls the plugin's `propose_distilled_tool`; the verdict comes back as the tool result and names the new
   version's status.

2. **Reuse.** Start a new session (`/clear` or a new `claude`). Ask the same for alert A-1057 (Payments Worker):
   one distilled tool call, about ten base calls inside it.

3. **The shortcut recovers.** Set `"rollback_outage": false`, then from this folder:

   ```
   uv run --project ../../.. tooldistill tool-updated incidents__deploys_rollback_latest
   uv run --project ../../.. --env-file ../../../.env tooldistill rebuild <the tool's name, typed out>
   ```

   The rebuild produces a shorter version that uses the one-call rollback; `uv run --project ../../.. tooldistill list`
   shows the name Claude chose.

4. **The API changes under the tool.** Set `"schema_reason": true` (rollbacks now require a `reason`; the tool
   definitions do not say so yet). Ask about alert A-1042 again in a fresh session: the distilled tool fails at the
   rollback step, Claude receives the structured error, finishes with base tools, and `/tooldistill:distill`
   proposes the next version under the same name.

Inspect at any point, from this folder:

```
uv run --project ../../.. tooldistill list
uv run --project ../../.. tooldistill traces list
```

Claude Code and the plugin each run their own copy of the `incidents` server, so a rollback Claude performed lives
in Claude's server's memory and the plugin's copy does not see it. The demo never depends on that.
