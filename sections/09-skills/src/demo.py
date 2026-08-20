"""Live demo: section 09 skills against the real Anthropic API.

The catalog (skill names and one-line descriptions) rides the
runtime-context snapshot into every request; bodies stay out of the
prompt until the model loads one through the skill tool, where the
text arrives as an ordinary tool/result row. Between the two turns a
second provider registers, so the second boundary re-emits the catalog
while the system text stays byte-identical across every request. This
file is the only place the SDK and the mini-Message to Anthropic
translation live (ADR 0001). Scripted turns only; skips politely when
the key or the live-demo deps are missing.
"""

import os

from agent_loop import agent_loop_plugin
from kernel import Context
from message import Message
from session_log import session_log_plugin
from skills import MemorySkillProvider, skills_plugin
from system_prompt import is_snapshot, system_prompt_plugin
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
            label = "catalog snapshot" if is_snapshot(event) else "claimed"
            extra = f"  ({label}: {payload['content'][:48]!r})"
        elif event["type"] == "tool/call":
            extra = f"  ({payload['name']} {dict(payload['args'])})"
        elif event["type"] == "tool/result":
            extra = f"  (is_error={payload['is_error']}, {len(payload['content'])} chars)"
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
    ctx.plugin(skills_plugin)
    ctx.plugin(agent_loop_plugin)

    skills = ctx.get("skills")
    skills.register(
        MemorySkillProvider(
            {
                "haiku": {
                    "description": "answer as a haiku",
                    "body": "Answer with exactly one haiku: three lines of five,"
                            " seven, then five syllables. No other prose.",
                },
                "expand": {
                    "description": "explain a term for a beginner",
                    "body": "Define the term in one plain sentence, then give a"
                            " two-line everyday analogy. Avoid jargon.",
                },
            }
        )
    )

    tools = ctx.get("tools")
    session = ctx.get("sessions").create("live")
    agent = ctx.get("agents").create(
        "a1", session, live_model, tools.scope("a1"), ctx.get("system_prompt")
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
            if is_snapshot(event):
                print(f"\n  [catalog snapshot, re-emitted:\n{payload['content']}]")
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

    # Turn 1: the catalog names the haiku skill; the model loads the body on
    # demand and only then knows the exact instructions to follow.
    agent.send("Check your skills catalog, load the haiku skill, and describe the moon.")

    # A provider change between turns: the next boundary re-emits the catalog
    # while the system text does not move.
    skills.register(
        MemorySkillProvider(
            {
                "farewell": {
                    "description": "say goodbye warmly",
                    "body": "Say goodbye in one warm sentence, then wish the user"
                            " luck with whatever they mentioned last.",
                }
            }
        )
    )

    # Turn 2: the model sees the grown catalog and uses the new skill.
    agent.send("Load the farewell skill and use it to sign off.")

    print_story(session)


if __name__ == "__main__":
    main()
