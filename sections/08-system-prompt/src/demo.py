"""Live demo: section 08 prompt assembly against the real Anthropic API.

The request is assembled per step from ordered providers: sections
plus variables render the system text, the bridge supplies the tool
list, and context providers (a real clock, the cwd) render the
runtime-context snapshot. The lookup tool body sleeps long enough for
the clock to move, so the second step re-emits the snapshot as a
fresh user message while the system text stays byte-identical across
every request. This file is the only place the SDK and the
mini-Message to Anthropic translation live (ADR 0001). Scripted turns
only; skips politely when the key or the live-demo deps are missing.
"""

import os
import time
from datetime import datetime

from agent_loop import agent_loop_plugin
from kernel import Context
from message import Message
from session_log import session_log_plugin
from system_prompt import is_snapshot, system_prompt_plugin
from tools import ToolDefinition, tools_plugin


def live_model(messages, tools=(), system=""):
    """The Model seam, live: mini Messages in, chunks then one final Message."""
    import anthropic

    wire_tools = [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": {
                "type": "object",
                "properties": {
                    name: {"type": "string", "description": description}
                    for name, description in t["params"].items()
                },
                "required": sorted(t["params"]),
            },
        }
        for t in tools
    ]
    client = anthropic.Anthropic()
    extra = {"system": system} if system else {}
    with client.messages.stream(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
        max_tokens=500,
        messages=_wire(messages),
        tools=wire_tools,
        **extra,
    ) as stream:
        for piece in stream.text_stream:
            yield ("chunk", piece)
        final = stream.get_final_message()
    text = "".join(block.text for block in final.content if block.type == "text")
    calls = tuple(
        {"id": block.id, "name": block.name, "args": dict(block.input)}
        for block in final.content
        if block.type == "tool_use"
    )
    yield ("message", Message(role="assistant", content=text, tool_calls=calls))


def _wire(messages):
    """Mini history -> Anthropic wire shape; same-role runs share one entry."""
    wire = []
    for message in messages:
        role, blocks = _wire_blocks(message)
        if wire and wire[-1]["role"] == role:
            wire[-1]["content"].extend(blocks)
        else:
            wire.append({"role": role, "content": blocks})
    return wire


def _wire_blocks(message):
    if message.role == "assistant":
        blocks = [{"type": "text", "text": message.content}] if message.content else []
        blocks += [
            {"type": "tool_use", "id": c["id"], "name": c["name"], "input": dict(c["args"])}
            for c in message.tool_calls
        ]
        return "assistant", blocks
    if message.role == "tool":
        block = {
            "type": "tool_result",
            "tool_use_id": message.call_id,
            "content": message.content,
        }
        return "user", [block]
    return "user", [{"type": "text", "text": message.content}]


def print_story(session):
    """The log's own account of the run, consecutive chunks collapsed."""
    print("\nthe log tells the whole story:")
    log, i = session.log, 0
    while i < len(log):
        event = log[i]
        if event["type"] == "assistant/chunk":
            j = i
            while j < len(log) and log[j]["type"] == "assistant/chunk":
                j += 1
            print(f"  {event['seq']:>3}-{log[j - 1]['seq']:<3} assistant/chunk x {j - i}")
            i = j
            continue
        payload, extra = event["payload"], ""
        if event["type"] == "request/header":
            extra = (
                f"  (sent {payload['messages']} derived messages,"
                f" {len(payload['tools'])} tool schemas,"
                f" system {len(payload['system'])} chars)"
            )
        elif event["type"] == "user/message":
            label = "snapshot" if is_snapshot(event) else "claimed"
            extra = f"  ({label}: {payload['content'][:48]!r})"
        elif event["type"] == "tool/call":
            extra = f"  ({payload['name']})"
        elif event["type"] == "tool/result":
            extra = f"  (is_error={payload['is_error']})"
        elif event["type"] == "step/end":
            extra = f"  (reason={payload['reason']!r})"
        print(f"  {event['seq']:>3}     {event['type']}{extra}")
        i += 1


def main():
    try:
        import anthropic  # noqa: F401
        from dotenv import load_dotenv
    except ImportError as exc:
        print(f"{exc.name} is not installed; skipping the live demo"
              " (pip install -r requirements.txt)")
        return
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; skipping the live demo (see .env.example)")
        return

    ctx = Context()
    ctx.plugin(session_log_plugin)
    ctx.plugin(tools_plugin)
    ctx.plugin(system_prompt_plugin)
    ctx.plugin(agent_loop_plugin)

    prompt = ctx.get("system_prompt")
    prompt.variable("user", "Marcus")
    prompt.section("persona", "Answer briefly and warmly. The user's name is {{user}}.")
    prompt.context("time", lambda ac: "time: " + datetime.now().strftime("%H:%M:%S"))
    prompt.context("cwd", lambda ac: "cwd: " + os.getcwd(), order=10)

    notes = {"wifi": "hunter2", "coffee": "the beans live in the freezer"}

    def lookup_body(args):
        time.sleep(1.5)  # long enough for the time context to change
        return notes[args["key"]]

    tools = ctx.get("tools")
    tools.register(
        ToolDefinition(
            name="lookup",
            description="Read one of the user's saved notes. Known keys: wifi, coffee.",
            params={"key": "which note to read"},
            execute=lookup_body,
            is_concurrency_safe=True,
        )
    )

    session = ctx.get("sessions").create("live")
    agent = ctx.get("agents").create("a1", session, live_model, tools.scope("a1"), prompt)

    # Everything below prints straight off the bus (section 02): each event
    # the loop appends shows the moment it lands in the log. The last system
    # text seen is remembered only to report "byte-identical" per request.
    last_system = [None]

    def on_event(_session, event):
        payload = event["payload"]
        if event["type"] == "assistant/chunk":
            print(payload["text"], end="", flush=True)
        elif event["type"] == "user/message":
            if is_snapshot(event):
                print(f"\n  [runtime-context snapshot, re-emitted: {payload['content']!r}]")
            else:
                print(f"\n  [user/message claimed at the boundary: {payload['content']!r}]")
        elif event["type"] == "request/header":
            same = "byte-identical to the last one" if payload["system"] == last_system[0] \
                else f"{len(payload['system'])} chars"
            last_system[0] = payload["system"]
            print(f"  [request: {payload['messages']} messages,"
                  f" tools {list(payload['tools'])}, system {same}]")
        elif event["type"] == "tool/call":
            print(f"\n  [tool/call {payload['name']} {dict(payload['args'])}]")
        elif event["type"] == "tool/result":
            print(f"  [tool/result is_error={payload['is_error']}: {payload['content']}]")
        elif event["type"] == "turn/start":
            print(f"\n[turn opens at seq {event['seq']}]")
        elif event["type"] == "turn/end":
            print(f"\n[turn closes at seq {event['seq']}]")

    ctx.on("session/event", on_event)

    # Turn 1: the tool body sleeps past a clock tick, so the step after the
    # call re-emits the snapshot while the system text does not move.
    agent.send("What is my wifi password? Use the lookup tool, then tell me.")

    # Turn 2: same providers, same system text; the snapshot re-emits only
    # if the clock moved since the last emitted reading.
    agent.send("Say goodbye in one short line.")

    print_story(session)


if __name__ == "__main__":
    main()
