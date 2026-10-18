# The 3-minute demo video: shot list, script, numbers

Everything below was dry-run on 2026-10-18 with `claude-sonnet-5-5` as the agent. The numbers are from that pass;
replace them with your take's numbers (the reports print them). Record each shot as its own clip, in order, and
run `uv run python scripts/demo_reset.py` before shot 3 and again before shot 9.

Terminal: Windows Terminal, dark theme, font 16–18 pt, window about 110×32 so the report lines never wrap.
Narration is recorded last, over the edited cut, segment by segment. Overlays show calls, tokens and seconds only.

## Shots

| # | Time | Clip | Command (from the repo root unless noted) | Freeze on |
|---|---|---|---|---|
| 1 | 0:00–0:18 | Discovery scrolling, sped up, call counter overlay | `uv run --env-file .env python examples/incident_agent/run.py --alert A-1042 --outage` | the report: `base tool calls: 17 (top level)` |
| 2 | 0:18–0:42 | Diagram overlay `docs/demo/how-it-works.svg` | none | the words "the agent proposes, the library verifies" |
| 3 | 0:42–1:20 | Split screen: left `examples/sandbox_agent.py` lines 196–220 (the loop with the three `tooldistill:` marks); right the end of shot 1's run: the nudge, the proposal, the verdict | same run as shot 1 | `verified: 9 base tool calls vs baseline 17; the run spent 168,427 model tokens, the tool spends none; v2 is now active` |
| 4 | 1:05–1:20 | Reuse | `uv run --env-file .env python examples/incident_agent/run.py --alert A-1057` | `distilled calls: 1 (0 failed, 9 base calls inside)` then the savings table overlay |
| 5 | 1:20–1:35 | CLI | in `examples/incident_agent/`: `uv run tooldistill list`, `uv run tooldistill show rollback_dependency_deploy_for_alert` | the checks table and `avoided: deploys_rollback_latest` |
| 6 | 1:35–1:48 | Recovery | in `examples/incident_agent/`: `uv run tooldistill tool-updated deploys_rollback_latest`, then `uv run --env-file ../../.env tooldistill rebuild rollback_dependency_deploy_for_alert` | `rebuilt ... as v3: verified: 8 base tool calls vs baseline 17; ...; v3 is now active and replaces v2` |
| 7 | 1:48–2:00 | The API changes | `uv run --env-file .env python examples/incident_agent/run.py --alert A-1042 --schema-reason` | the structured error block, 2 s, then `v4 is now active and replaces v3` |
| 8 | 2:00–2:42 | Claude Code | in `examples/incident_agent/claude_code/` with `"rollback_outage": true` in `switches.json`: `claude --plugin-dir ../../../plugin-dev`; ask for alert A-1042; `/tooldistill:distill handle a firing alert end to end`; new session; ask for A-1057 | the plugin's tool call in the second session |
| 9 | 2:42–3:00 | Close card | none | the two install lines and the repo URL |

The tool's name is whatever the model chose in your take (`uv run tooldistill list` shows it); in the dry run it was
`rollback_dependency_deploy_for_alert`. Type names out: PowerShell treats `<name>` as redirection.

## The numbers (dry run, Sonnet 5.5)

| | Discovery (shot 1) | Reuse (shot 4) |
|---|---|---|
| Base tool calls | 17 at the top level, 3 of them failures | 0 at the top level; 1 distilled call with 9 inside |
| Model tokens | 162,338 in + 6,089 out | 9,591 in + 324 out |
| Model turns | 15 | 2 |
| Wall time | 47.9 s | 3.7 s |

Lifecycle: v2 verified at 9 calls; the rebuild after the shortcut recovered gives v3 at 8; the schema change fails v3
at step 6 of 8 with a structured error, the fallback run takes 17 base calls, and v4 verifies at 10.

With `--model claude-haiku-5-5` the discovery took 24 base calls and 182,421 tokens; reuse was messier (the cheaper
model called base tools around the distilled call). Use Sonnet for the takes.

## Overlay text

- Shot 1 counter: `tool calls: N` climbing with the scroll; final card `17 calls · 168k tokens · 48 s`.
- Shot 4 table: `17 calls → 1 call (9 inside)`, `168k tokens → 10k`, `48 s → 3.7 s`.
- Shot 7: keep the structured error on screen for two seconds; nothing else on it.
- Shot 8: a three-line card: `SessionStart hook → trace`, `PostToolUse hooks → recorded calls`, `MCP server → the tools, /tooldistill:distill → the proposal`.
- Shot 9: `pip install tooldistill` / `claude plugin marketplace add irtaza-kashif/tool-distill` / `github.com/irtaza-kashif/tool-distill` / `verified · reusable · kept current`.

## Narration (voiceover, ~140 words per minute)

**0:00 (42 words).** An agent solving a real task takes twenty, thirty tool calls of trial and error to find a path that needed eight. The next run starts from zero and does it all again. Here, an alert fires, and the agent hunts for the deploy that caused it.

**0:18 (62 words).** tooldistill sits beside your agent loop and records every tool call. When a run succeeds and was expensive, the model is asked to write the path down as a distilled tool: one Python function, one call. The library never trusts what the model claims. It checks the code before importing it, replays it against the run's recorded responses, and measures the saving. Only then does the tool enter a lifecycle driven by evidence.

**0:42 (82 words).** The integration is a handful of lines: start a trace, route tool calls through it, say whether the task worked. First run: the one-call rollback is down, so the agent works around it. Seventeen calls, a hundred and sixty thousand tokens, forty-eight seconds. Then the nudge, the proposal, and the verdict: verified, nine calls against seventeen, and the tool spends no model tokens at all. Second run, a different alert, a different service: one call, four seconds, and the agent never touched a base tool.

**1:20 (76 words).** Tools rot when the APIs under them change, so the library keeps them current. The command line shows every version, its checks, its history, and the tool it avoided because it was down. When that tool comes back, one command flags the version and a rebuild produces an eight-call one. When the deploy API starts requiring a reason, the tool fails safely: a structured error, the agent falls back to base tools, and that run becomes version four.

**2:00 (58 words).** The same library runs inside Claude Code as a plugin. A hook records the session, a slash command asks Claude to distill the task it just finished, and an MCP server offers the verified tool back. Next session, same alert pattern: one call. Nothing the model says about a tool is believed. Everything is measured, in your own loop or in Claude Code.

**2:42 (30 words).** pip install tooldistill. The plugin installs from the repository as a marketplace. Verified, reusable, and kept current. tooldistill: your agent proposes, the library verifies.

Total: 350 words, about 2:30 of speech, leaving room for the pauses the freeze frames need.

## Recording checklist

1. `uv run python scripts/demo_reset.py`, then `uv run pytest -q` once, so nothing surprises you mid-take.
2. Shot 1 is a real model run; if it comes out under 15 calls or the agent never tries the shortcut, reset and run again. It costs a few cents.
3. Record shots 1, 4, 5, 6, 7 in that order without resetting in between: each depends on the store state the previous one left.
4. Reset before shot 8, set `rollback_outage` to true in `examples/incident_agent/claude_code/switches.json`, record the two Claude Code sessions, then set it back to false.
5. Edit: time-skip inside shot 1 (keep the first three calls, the 503 on the shortcut, the last two, the report), overlay the counter and the table, hold the structured error for two seconds.
6. Narrate over the cut; the segment word counts above fit their slots with pauses.
