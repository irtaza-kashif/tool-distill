# The invoice demo inside Claude Code

The same fake Gmail and billing tools as the SDK demo, served to Claude Code over MCP, with the tooldistill
plugin recording the session and turning a solved task into a distilled tool.

What is in this folder:

| File | Purpose |
|---|---|
| `.mcp.json` | Claude Code's MCP config: the fake invoice tools as the server `invoice` (runs `../mcp_server.py` through `uv`) |
| `tooldistill.toml` | tooldistill's config: `mcp_config = ".mcp.json"`, so the plugin reaches the same server under the same name |
| `switches.json` | the fake tools' switches, read live by every server process: outage, schema change, updated definitions |
| `.tooldistill/` | the store the plugin creates here (gitignored) |

Everything runs from this folder. The plugin's hooks and MCP server find `tooldistill.toml` through
`CLAUDE_PROJECT_DIR`, which Claude Code sets to the folder you start it in.

## Setup

- [uv](https://docs.astral.sh/uv/) on the PATH. The MCP server, the hooks and the fake tools all start through it.
- Claude Code 2.1 or newer.
- Nothing else: the plugin runs the library from this checkout (`plugin-dev`), and the tools are fake.

Start Claude Code in this folder with the development variant of the plugin:

```
cd examples/invoice_agent/claude_code
claude --plugin-dir ../../../plugin-dev
```

Approve the project's `invoice` MCP server when asked. The session context should now mention tooldistill and the
session's trace id. `uv run --project ../../.. tooldistill traces list` shows that trace.

## Walkthrough

Mirrors the four demo steps of the SDK demo.

1. **Discovery.** Edit `switches.json` and set `"import_outage": true`, so the one-call shortcut fails and Claude
   has to find the long way. Ask:

   > Create a draft invoice for ACME from their most recent order email.

   Claude works through the `invoice` tools; every call is recorded by the PostToolUse hook. When it is done, run:

   > /tooldistill:distill create a draft invoice for ACME from their order email

   Claude calls the plugin's `propose_distilled_tool`; the verdict comes back as the tool result. With
   `auto_promote = true` the tool is active at once.

2. **Reuse.** Start a new session (`/clear` or a new `claude`). The session context lists the distilled tool, and the
   plugin's MCP server offers it as `mcp__plugin_tooldistill_tooldistill__<name>`. Ask the same for Globex:

   > Create a draft invoice for Globex from their most recent order email.

   One distilled tool call, four base calls inside it, recorded under this session's trace.

3. **The base tool recovers.** Set `"import_outage": false` in `switches.json`, then from this folder:

   ```
   uv run --project ../../.. tooldistill tool-updated invoice__billing_import_from_email
   uv run --project ../../.. --env-file ../../../.env tooldistill rebuild <name>
   ```

   The first marks the tool suspect because an avoided tool recovered; the second asks Claude for a shorter version
   that uses it, verifies it live, and activates v2. The running plugin server notices the store change and tells
   Claude Code the tool list changed.

4. **The API changes under the tool.** Set `"schema_currency": true` (the billing API now requires `currency`, the
   tool definitions do not say so yet). Ask for Initech's invoice: the distilled tool fails, Claude receives the
   structured error, finishes with base tools, and `/tooldistill:distill` then proposes v3 under the same name.
   With `"tools_updated": true` as well, the next plugin start catches the schema change first and re-verifies.

Inspect at any point, from this folder:

```
uv run --project ../../.. tooldistill list
uv run --project ../../.. tooldistill show <name>
uv run --project ../../.. tooldistill traces show <session id>
```

## Two things to know

- Claude Code and the plugin each run their own copy of the `invoice` server, so an invoice Claude created lives in
  Claude's server's memory and the plugin's copy does not see it. The demo never depends on that.
- Hooks carry no token counts, so baselines here are tool calls and latency only.
