# tooldistill for Claude Code

Distilled tools for Claude Code: the session's MCP tool calls are recorded, `/tooldistill:distill` turns a solved task
into a tool, the library verifies it before Claude can use it, and keeps it current when the tools underneath change.
This directory is rendered by `scripts/render_plugin.py` from the library's texts and version; do not edit it by hand.

The plugin's MCP server and hooks run the released library, pinned to this plugin's version: `uvx --from tooldistill[plugin]==0.1.0 tooldistill serve`.

## What the project needs

A `tooldistill.toml` in the project root that lists the MCP servers tooldistill may call, in the same structure as
Claude Code's `.mcp.json` (or `mcp_config = ".mcp.json"` pointing at that file), for example:

```toml
store = ".tooldistill"
mcp_config = ".mcp.json"
auto_promote = true
allow_live_verification = true   # only with mock or sandboxed tools: live verification has real side effects
min_calls_to_distill = 4
```

The server and the hooks find it through `CLAUDE_PROJECT_DIR`; set `TOOLDISTILL_CONFIG=<path>` to point elsewhere.
Projects without the file are left alone.

## Install

```
claude plugin marketplace add irtaza-kashif/tool-distill      # or the path of a local checkout
claude plugin install tooldistill@tooldistill
```

Development loop, from the project you want to test in:

```
claude --plugin-dir <checkout>/plugin-dev
/reload-plugins                                                 # after editing the library
claude plugin validate <checkout>/plugin-dev --strict
```

## How it works

| Claude Code mechanism | What tooldistill does with it |
|---|---|
| `SessionStart` hook | creates the session's trace (its id is the session id) and tells Claude the trace id and the distilled tools that exist |
| `PostToolUse` / `PostToolUseFailure` hooks | record every tool call Claude makes (MCP tools as `<server>__<tool>`, built-ins as `builtin__<Name>`) |
| MCP server `tooldistill` | offers `propose_distilled_tool` and every verified distilled tool; runs them through its own connections to the project's MCP servers; announces changes with `tools/list_changed` |
| `/tooldistill:distill [what the task was]` | the user's confirmation that the task went well; asks Claude to propose a distilled tool for it |
| `tooldistill` CLI (`list`, `show`, `verify`, `promote`, `tool-updated`, `health`, `rebuild`) | the same store, from the terminal |

Built-in tools (Bash, Edit, Read, ...) are recorded but can never be part of a distilled tool; only calls to MCP tools can.
Hooks carry no token usage, so baselines in the plugin count tool calls and latency only.

A full walkthrough with fake Gmail and billing tools lives in `examples/invoice_agent/claude_code/` of the repository.
