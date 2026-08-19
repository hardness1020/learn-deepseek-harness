"""Live demo: the section 05 tool pipeline against the real Anthropic API.

The loop does not change: the demo plugs a live callable into the
Model seam and registers one mini tool. The model reads the schemas
off the request, calls the tool, and the pipeline answers, including
a guard denial delivered as a normal tool/result the model has to
work with. This file is the only place the SDK and the mini-Message
to Anthropic translation live (ADR 0001). Scripted turns only; skips
politely when the key or the live-demo deps are missing.
"""

import os

from agent_loop import agent_loop_plugin
from kernel import Context
from message import Message
from session_log import session_log_plugin
from tools import ToolDefinition, tools_plugin


def live_model(messages, tools=()):
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
    with client.messages.stream(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
        max_tokens=500,
        messages=_wire(messages),
        tools=wire_tools,
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
                f" {len(payload['tools'])} tool schemas)"
            )
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
    ctx.plugin(agent_loop_plugin)

    notes = {"wifi": "hunter2", "coffee": "the beans live in the freezer"}
    tools = ctx.get("tools")
    tools.register(
        ToolDefinition(
            name="lookup",
            description="Read one of the user's saved notes."
            " Known keys: wifi, coffee, launch-codes.",
            params={"key": "which note to read"},
            execute=lambda args: notes[args["key"]],
        )
    )
    tools.guard(
        lambda call: "launch codes stay classified"
        if call.get("args", {}).get("key") == "launch-codes"
        else None
    )

    session = ctx.get("sessions").create("live")
    agent = ctx.get("agents").create("a1", session, live_model, tools.scope("a1"))

    # Streaming and tool traffic straight off the bus (section 02): every
    # event the loop appends is printed the moment it lands in the log.
    def on_event(_session, event):
        payload = event["payload"]
        if event["type"] == "assistant/chunk":
            print(payload["text"], end="", flush=True)
        elif event["type"] == "tool/call":
            print(f"\n  [tool/call {payload['name']} {dict(payload['args'])}]")
        elif event["type"] == "tool/result":
            print(f"  [tool/result is_error={payload['is_error']}: {payload['content']}]")

    ctx.on("session/event", on_event)

    def turn(text):
        print(f"\nuser: {text}")
        print("assistant: ", end="", flush=True)
        agent.send(text)
        print()

    turn("Look up my wifi note and tell me what it says.")
    turn("Now try the launch-codes note and report exactly what the tool tells you.")

    print_story(session)


if __name__ == "__main__":
    main()
