"""Live demo: section 10 capability seams against the real Anthropic API.

The live model itself arrives through a seam: live_model registers as
an llm adapter and the agent consumes llm.model("anthropic"), the
folded Definition/Consumer service. The fs seam starts on one memory
backend; between turns its provider unmounts and another takes the
key, and the model reads different content through a byte-identical
schema. The shell seam runs through the sandbox stub, so the model
sees the fenced argv come back. This file is the only place the SDK
and the mini-Message to Anthropic translation live (ADR 0001).
Scripted turns only; skips politely when the key or the live-demo
deps are missing.
"""

import os

from agent_loop import agent_loop_plugin
from capabilities import (
    ArgvRewriteSandbox,
    EchoShellExecutor,
    MemoryFileSystem,
    SandboxedShellExecutor,
    capability_tools_plugin,
    llm_plugin,
    provider,
)
from kernel import Context
from message import Message
from session_log import session_log_plugin
from system_prompt import system_prompt_plugin
from tools import tools_plugin


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
            extra = f"  (claimed: {payload['content'][:48]!r})"
        elif event["type"] == "tool/call":
            extra = f"  ({payload['name']} {dict(payload['args'])})"
        elif event["type"] == "tool/result":
            extra = f"  (is_error={payload['is_error']}, {payload['content'][:48]!r})"
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
    ctx.plugin(capability_tools_plugin)
    ctx.plugin(llm_plugin)
    ctx.plugin(agent_loop_plugin)

    # Providers, mounted like any plugin. The live model is one of them:
    # an llm adapter behind the folded Definition/Consumer service.
    ctx.get("llm").register("anthropic", live_model)
    first_fs = ctx.plugin(
        provider("fs", MemoryFileSystem({"notes.txt": "The launch moved to Thursday."}))
    )
    ctx.plugin(provider("sandbox", ArgvRewriteSandbox({"read-only"})))
    ctx.plugin(
        provider(
            "shell",
            SandboxedShellExecutor(EchoShellExecutor(), ctx.get("sandbox"), "read-only"),
        )
    )

    session = ctx.get("sessions").create("live")
    agent = ctx.get("agents").create(
        "a1",
        session,
        ctx.get("llm").model("anthropic"),
        ctx.get("tools").scope("a1"),
        ctx.get("system_prompt"),
    )

    # Everything below prints straight off the bus (section 02): each event
    # the loop appends shows the moment it lands in the log. The last system
    # text seen is remembered only to report "byte-identical" per request.
    last_system = [None]

    def on_event(_session, event):
        payload = event["payload"]
        if event["type"] == "assistant/chunk":
            print(payload["text"], end="", flush=True)
        elif event["type"] == "user/message":
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

    # Turn 1: the model reads through the fs seam's first backend.
    agent.send("Read notes.txt and tell me in one sentence what it says.")

    # The swap: the first provider's undo runs, another takes the key. The
    # agent, the tools, and the schema the model sees never move.
    first_fs.dispose()
    ctx.plugin(
        provider("fs", MemoryFileSystem({"notes.txt": "The launch is back to Monday."}))
    )

    # Turn 2: same tool, same schema, different machine behind the wall.
    agent.send("Read notes.txt again. Did it change? One sentence.")

    # Turn 3: the shell seam runs through the sandbox stub; the fenced argv
    # comes back as the command's output for the model to report.
    agent.send("Run `uname -r` with the shell tool and report exactly what came back.")

    print_story(session)


if __name__ == "__main__":
    main()
