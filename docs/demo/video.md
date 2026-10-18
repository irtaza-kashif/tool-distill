# The 3-minute demo video: shots, script, numbers

The demo is one program's output. `examples/incident_agent/compare.py` runs the same task twice without tooldistill
and twice with it, prints each run's calls, tokens and seconds as it goes, and ends with one table. Nothing to type
live beyond the one command. The Claude Code plugin gets one short segment of its own.

Dry run on 2026-10-18 with `claude-sonnet-5-5`. Replace the numbers with your take's; the script prints them.

```
uv run --env-file .env python examples/incident_agent/compare.py --alert A-1042
```

```
--- without tooldistill, run 1 ---
    18 tool calls, 114,194 tokens, 32.7s, done
--- without tooldistill, run 2 ---
    16 tool calls, 110,320 tokens, 28.7s, done
--- with tooldistill, run 1 ---
    18 tool calls, 136,052 tokens, 43.0s, done
    proposed a distilled tool: verified: 9 base tool calls vs baseline 18; the run spent 121,797 model tokens, the tool spends none; v1 is now active
--- with tooldistill, run 2 ---
    2 tool calls, 15,197 tokens, 5.0s, done
    called the distilled tool 1x (9 base calls inside)

                                                     run 1                               run 2
------------------------------------------------------------------------------------------------
without tooldistill                18 calls, 114k tok, 33s                 16 calls, 110k tok, 29s
with tooldistill                   18 calls, 136k tok, 43s      2 calls (+9 inside), 15k tok, 5s

with tooldistill, run 2 vs run 1: 9x fewer model tokens, 9x faster
```

The two rows are independent: the "without" row is a plain tool-use loop with no library and no store; the "with"
row has tooldistill wired in and starts from an empty store. Every run is a new conversation, the fake world is
reset before each run, and both rows use the same model and system prompt. Add `--verbose` to see every tool call
scroll by, which is the better footage for the time-skipped part.

## Shots

| # | Time | Clip | How |
|---|---|---|---|
| 1 | 0:00–0:15 | The problem: a terminal running `compare.py --verbose`, the "without" runs scrolling, sped up, with a tool-call counter overlay | start the recording at the command; keep the first "without" run |
| 2 | 0:15–0:40 | How it works: the diagram overlay `docs/demo/how-it-works.svg` | overlay over a dimmed terminal |
| 3 | 0:40–1:35 | The same recording continued: "with" run 1 scrolling, then the proposal and verdict line (freeze 2 s), then "with" run 2 (real time, it is 5 s), then the table (freeze 4 s) | one take of `compare.py --verbose`, time-skipped in the edit |
| 4 | 1:35–2:00 | Keeping tools current: a second terminal clip, `uv run --env-file .env python examples/incident_agent/run.py --alert A-1042 --schema-reason` after a `compare.py` run has left a tool in `examples/incident_agent/.tooldistill` (run `run.py --alert A-1042 --outage` once first to create it there); freeze on the structured error, then on `v2 is now active and replaces v1` | optional; cut if time is short |
| 5 | 2:00–2:40 | Claude Code: in `examples/incident_agent/claude_code/` with `"rollback_outage": true` in `switches.json`, `claude --plugin-dir ../../../plugin-dev`; ask for alert A-1042; `/tooldistill:distill handle a firing alert end to end`; new session; ask for A-1057; the plugin's tool answers in one call | two short sessions, time-skipped |
| 6 | 2:40–3:00 | Close card: `pip install tooldistill`, `claude plugin marketplace add irtaza-kashif/tool-distill`, the repo URL, "verified · reusable · kept current" | overlay |

Shots 1 and 3 are the same recording. `compare.py` writes its own store (`examples/incident_agent/.tooldistill_compare`)
and wipes it at the start of every run, so repeated takes need no reset. `scripts/demo_reset.py` clears the other
demo stores before shots 4 and 5.

## Overlay text

- Shot 1: a counter `tool calls: N` climbing with the scroll; at the end of the first run, `18 calls · 114k tokens · 33 s`.
- Shot 3, over the table: `same task, same model` and, under the last cell, `9x fewer tokens · 9x faster`.
- Shot 4: nothing over the structured error; hold it two seconds.
- Shot 5: three lines, `SessionStart hook → trace`, `PostToolUse hooks → recorded calls`, `/tooldistill:distill → proposal, MCP server → the tool`.
- Shot 6: the four lines of the close card.

## Narration (voiceover, ~140 words per minute; 330 words, leaves room for the freezes)

**0:00 (44 words).** An agent solving a real task takes twenty tool calls of trial and error to find a path that needed nine. Here, an alert fires and the agent hunts for the deploy that caused it. Run it again, and it starts from zero and hunts again.

**0:15 (62 words).** tooldistill sits beside your agent loop and records every tool call. When a run succeeds and was expensive, the model is asked to write the path down as a distilled tool: one Python function, one call. The library never trusts what the model claims. It checks the code before importing it, replays it against the run's recorded responses, and measures the saving. Only then can an agent use it.

**0:40 (84 words).** Same task, same model, two loops. Without tooldistill, both runs cost about the same: eighteen calls, a hundred thousand tokens, half a minute. With tooldistill, the first run costs the same, plus a proposal: the model writes the tool, the library verifies it, nine calls against eighteen. The second run is one call. Fifteen thousand tokens instead of a hundred and thirty. Five seconds instead of forty. The loop did not change; the tool list did.

**1:35 (62 words).** Tools rot when the APIs under them change, so the library keeps them current. Every version has its checks and its history. When the deploy API starts requiring a reason, the distilled tool fails safely: a structured error tells the agent which step broke, the agent falls back to the base tools, and that run becomes version two, verified the same way.

**2:00 (50 words).** The same library runs inside Claude Code as a plugin. A hook records the session, a slash command asks Claude to distill the task it just finished, and an MCP server offers the verified tool back. Next session, same kind of alert: one call. In your own loop, or in Claude Code.

**2:40 (28 words).** pip install tooldistill. The plugin installs from the repository as a marketplace. Verified, reusable, and kept current. tooldistill: your agent proposes, the library verifies.

## Recording checklist

1. `uv sync` once in the clone, `.env` with the API key in the repo root, `uv run pytest -q` once.
2. Record `compare.py --alert A-1042 --verbose` from the repo root, in a terminal about 120 columns wide so the table does not wrap. It takes two to four minutes and costs a dollar or two. If the "with" run 2 is not a single distilled call, run it again.
3. For shot 4, first `uv run --env-file .env python examples/incident_agent/run.py --alert A-1042 --outage` to create a tool in the normal store, then the `--schema-reason` run.
4. For shot 5, `uv run python scripts/demo_reset.py`, set `rollback_outage` to true in the Claude Code folder's `switches.json`, record the two sessions, set it back.
5. Narrate over the edited cut, one segment at a time.
