"""A small chat agent with tooldistill wired in.

Run from the repo root:    uv run python examples/sandbox_agent.py
Type a task, watch the tool calls, say whether it worked. Type quit to stop.

Try this task:
    Sam emailed me about a meal. Find the invitation, look up the restaurant's street address on the web,
    check my calendar for that day, and add the meal to my calendar with the address as the location.
Then ask the same thing about Priya, and watch whether the model uses the distilled tool it made.

Afterwards:    uv run tooldistill list        uv run tooldistill show <tool name>        uv run tooldistill traces list

The file has five parts, top to bottom:
    1. settings and the API key
    2. the base tools: fake Gmail, fake Calendar, real web search
    3. the tooldistill object
    4. ask_model(): the API call, streaming, and the tool-call loop
    5. the chat loop
Every line that touches the library has a "tooldistill:" comment.
"""

import os
import sys

import anthropic
from ddgs import DDGS

from tooldistill import ToolDistill

# ======================================================================================= 1. settings and the API key

MODEL = "claude-sonnet-5-5"   # claude-haiku-5-5 is cheaper
STORE = ".tooldistill"        # the folder tooldistill writes to (gitignored)
TODAY = "2026-10-17"          # the fake inbox and calendar are written around this Saturday

# Read ANTHROPIC_API_KEY from the .env file in this folder. The anthropic client picks it up from the environment.
if os.path.exists(".env"):
    for line in open(".env"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value)

client = anthropic.Anthropic()
sys.stdout.reconfigure(encoding="utf-8")   # so the model's dashes and symbols print fine on Windows

SYSTEM = (
    "You are a personal assistant with tools for email, calendar and web search. Today is Saturday " + TODAY + ". "
    "Prefer the most direct path: if one tool completes the whole task in a single call, use it instead of several "
    "smaller tools. Days are YYYY-MM-DD and times are 24-hour HH:MM. When the task is done, reply with one line "
    "saying what you did."
)

# ======================================================================================= 2. the base tools

# A fake inbox and a fake calendar, kept in these lists. They start fresh every time the script starts.
INBOX = [
    {"id": "msg_1", "from": "Sam Rivera <sam@example.com>", "date": "2026-10-16", "subject": "Dinner next Thursday?",
     "body": "Hey! Dinner next Thursday?\nWhen: 2026-10-22 19:00-21:00\nWhere: Katz's Delicatessen, New York\n"
             "Say yes and put it on your calendar. - Sam"},
    {"id": "msg_2", "from": "Priya Nair <priya@example.com>", "date": "2026-10-15", "subject": "Lunch next week?",
     "body": "Hi! Lunch next week?\nWhen: 2026-10-26 12:30-13:30\nWhere: Pike Place Chowder, Seattle\n"
             "Add it to your calendar if that works. Priya"},
    {"id": "msg_3", "from": "Dana Okafor <dana@example.com>", "date": "2026-10-14", "subject": "Q4 planning doc",
     "body": "Draft attached. Comments by Friday please."},
]

CALENDAR = [
    {"id": "evt_1", "title": "Team sync", "day": "2026-10-22", "start": "17:00", "end": "18:00", "location": "Zoom"},
    {"id": "evt_2", "title": "Dentist", "day": "2026-10-26", "start": "09:00", "end": "10:00", "location": "Main St clinic"},
]


def gmail_search(query):
    # Every word of the query has to appear somewhere in the email. "from:sam" works too.
    words = query.lower().replace("from:", "").split()
    found = []
    for msg in INBOX:
        text = (msg["from"] + " " + msg["subject"] + " " + msg["body"]).lower()
        matches = True
        for word in words:
            if word not in text:
                matches = False
        if matches:
            found.append({"id": msg["id"], "from": msg["from"], "date": msg["date"], "subject": msg["subject"]})
    return found


def gmail_get_message(id):
    for msg in INBOX:
        if msg["id"] == id:
            return msg
    raise Exception("no message with id " + id)


def calendar_list_events(day):
    found = []
    for event in CALENDAR:
        if event["day"] == day:
            found.append(event)
    return found


def calendar_create_event(title, day, start, end, location):
    if len(day) != 10 or len(start) != 5 or len(end) != 5:
        raise Exception("day must be YYYY-MM-DD and times must be HH:MM")
    event = {"id": "evt_" + str(len(CALENDAR) + 1), "title": title, "day": day, "start": start, "end": end,
             "location": location}
    CALENDAR.append(event)
    return event


def web_search(query):
    # The only real tool: DuckDuckGo, no key, read-only.
    results = []
    for r in DDGS().text(query, max_results=5):
        results.append({"title": r["title"], "url": r["href"], "snippet": r["body"]})
    return results


# The tool definitions the model sees, in the format the Anthropic API wants.
TOOLS = [
    {"name": "gmail_search",
     "description": "Search the inbox. Returns id, from, date and subject for each match.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "gmail_get_message",
     "description": "Get one email in full, by id.",
     "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "calendar_list_events",
     "description": "List the calendar events on a day (YYYY-MM-DD).",
     "input_schema": {"type": "object", "properties": {"day": {"type": "string"}}, "required": ["day"]}},
    {"name": "calendar_create_event",
     "description": "Create a calendar event. Times are 24-hour HH:MM.",
     "input_schema": {"type": "object",
                      "properties": {"title": {"type": "string"}, "day": {"type": "string"}, "start": {"type": "string"},
                                     "end": {"type": "string"}, "location": {"type": "string"}},
                      "required": ["title", "day", "start", "end", "location"]}},
    {"name": "web_search",
     "description": "Search the web. Returns up to five results with title, url and snippet.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]


def execute_tool(name, args):
    # tooldistill: the executor. The library calls this to run a base tool. Raising an exception means "it failed".
    if name == "gmail_search":
        return gmail_search(args["query"])
    if name == "gmail_get_message":
        return gmail_get_message(args["id"])
    if name == "calendar_list_events":
        return calendar_list_events(args["day"])
    if name == "calendar_create_event":
        return calendar_create_event(args["title"], args["day"], args["start"], args["end"], args["location"])
    if name == "web_search":
        return web_search(args["query"])
    raise Exception("unknown tool " + name)


# ======================================================================================= 3. tooldistill

# tooldistill: one object for the whole program. It opens (or creates) the store and checks the tool definitions.
td = ToolDistill(
    STORE, execute_tool, TOOLS,
    auto_promote=True,          # a verified distilled tool becomes usable right away
    min_calls_to_distill=4,     # a run with fewer base tool calls is not worth distilling
    min_call_reduction=0.0,     # accept a distilled tool with the same number of calls (the default wants 30% fewer)
    fallback_distiller=False,   # True would make the library ask Claude itself when the model does not propose
)

# ======================================================================================= 4. one round with the model


def ask_model(messages, tools, trace):
    """Call the model. If it asks for tools, run them, send the results back, and call it again. Stop when it stops."""
    tool_calls = 0
    while True:
        # THIS IS THE API CALL. The "with" block opens a streaming connection and closes it at the end of the block.
        # Text is printed as it arrives; get_final_message() gives the complete response once the stream is done.
        with client.messages.stream(model=MODEL, max_tokens=4000, system=SYSTEM, tools=tools, messages=messages) as stream:
            printed_text = False
            for event in stream:
                if event.type == "text":
                    print(event.text, end="", flush=True)
                    printed_text = True
            response = stream.get_final_message()
        if printed_text:
            print()
        messages.append({"role": "assistant", "content": response.content})
        trace.record_usage(response.usage)   # tooldistill: token counts, so the run's cost is recorded

        if response.stop_reason != "tool_use":
            return tool_calls                # no tool calls in this response: the model is done

        results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls = tool_calls + 1
                print("  -> tool call:", block.name, block.input)
                # tooldistill: this is where the library sits. It runs the tool (base tool through execute_tool,
                # distilled tool through its own code, propose_distilled_tool through the verifier) and records
                # the call. It never raises: a failure comes back as is_error=True.
                result = trace.execute(block.name, block.input)
                if result.is_error:
                    print("  <- error:", result.content[:400])
                else:
                    print("  <- result:", result.content[:300])
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": result.content, "is_error": result.is_error})
        messages.append({"role": "user", "content": results})


# ======================================================================================= 5. the chat loop

print("Fake inbox: emails from Sam (a dinner invite), Priya (a lunch invite) and Dana (a document). Type quit to stop.")
messages = []
while True:
    task = input("\nyou: ").strip()
    if task == "quit":
        break
    if task == "":
        continue

    trace = td.start_trace(task)                                # tooldistill: one trace per task
    tools = TOOLS + td.active_tools() + [td.propose_tool()]    # tooldistill: base tools + distilled tools + propose tool
    names = []
    for t in td.active_tools():
        names.append(t["name"])
    if names:
        print("distilled tools available:", ", ".join(names))

    messages.append({"role": "user", "content": task})
    tool_calls = ask_model(messages, tools, trace)

    # tooldistill: the library never guesses whether the task worked. The developer's code decides. Here, you do.
    if tool_calls == 0:
        trace.set_outcome(True)
    else:
        answer = input("did that work? (y/n): ").strip().lower()
        trace.set_outcome(answer == "y")

    nudge = trace.distill_nudge()   # tooldistill: a message only when this run is worth distilling, at most once
    if nudge:
        print("(tooldistill: this run is worth distilling. Asking the model to propose a distilled tool.)")
        messages.append({"role": "user", "content": nudge})
        ask_model(messages, tools, trace)   # the model calls propose_distilled_tool; the verdict shows as its result

    trace.finish()                  # tooldistill: closes the trace

td.close()
