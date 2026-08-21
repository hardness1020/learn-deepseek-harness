"""Live demo: section 13 composition against the real Anthropic API.

The harness every demo since section 04 assembled by hand boots here
from data: the base bundle's sixteen rows, then a live profile layer
that inserts an adapter row, inserts a worker-provider row, disables
the scripted model row, and replaces the agent row's whole config so
its model is the live one. The mounted product then proves the rows
are alive: one turn runs a command through the sandboxed shell rows,
and one delegates to the worker foreground and quotes its answer.
This file is the only place the SDK and the mini-Message to Anthropic
translation live (ADR 0001). Scripted turns only; skips politely when
the key or the live-demo deps are missing.
"""

import os

from composition import MINI_BASE, PLUGINS, apply_layers, mount_entries
from kernel import Context
from message import Message
from subagent import in_process_provider


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


def print_story(session, title):
    """The log's own account of one session, consecutive chunks collapsed."""
    print(f"\n{title}:")
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

    # Two demo-local names for the table: rows stay data, so the live
    # callable enters through a plugin, and config carries only names.
    def live_llm_factory(config):
        def apply(inner):
            inner.effect(inner.get("llm").register("live", live_model), "live adapter")

        return apply

    live_llm_factory.requires = ("llm",)

    def worker_factory(config):
        def apply(inner):
            # One name, one provider: every child gets the live model too.
            inner.effect(
                inner.get("subagents").register(
                    "worker", in_process_provider(inner, lambda: live_model)
                ),
                "worker provider",
            )

        return apply

    worker_factory.requires = ("subagents",)

    table = dict(PLUGINS)
    table["live-llm"] = live_llm_factory
    table["worker-provider"] = worker_factory

    live_profile = [
        {"id": "live-llm", "name": "live-llm"},
        {"id": "worker", "name": "worker-provider"},
        {"id": "model", "disabled": True},
        # The design question, lived: the agent's model differs by mode,
        # so this patch restates the row's whole config; nothing leaks
        # up from the base row it replaces.
        {"id": "agent", "config": {"agent": "live", "session": "live", "model": "live"}},
    ]

    entries = apply_layers([MINI_BASE, live_profile])
    print("the live profile, composed row by row:")
    for row in entries:
        config = f"  {row['config']}" if row["config"] else ""
        print(f"  {row['id']:<17} {row['name']}{config}")

    ctx = Context()
    mount_entries(ctx, entries, table)
    session = ctx.get("sessions").sessions["live"]
    agent = ctx.get("agent")

    def on_event(event_session, event):
        payload = event["payload"]
        if event_session.id != "live":
            # A child's whole story stays in its own log; here, just its
            # boundaries and its answer, prefixed with its session id.
            if event["type"] == "turn/start":
                print(f"\n  [{event_session.id}] turn opens")
            elif event["type"] == "assistant/message" and payload["content"]:
                print(f"  [{event_session.id}] answers: {payload['content']!r}")
            elif event["type"] == "turn/end":
                print(f"  [{event_session.id}] turn closes")
            return
        if event["type"] == "assistant/chunk":
            print(payload["text"], end="", flush=True)
        elif event["type"] == "user/message":
            print(f"\n  [user/message claimed at the boundary: {payload['content']!r}]")
        elif event["type"] == "tool/call":
            print(f"\n  [tool/call {payload['name']} {dict(payload['args'])}]")
        elif event["type"] == "tool/result":
            print(f"  [tool/result is_error={payload['is_error']}: {payload['content']}]")
        elif event["type"] == "turn/start":
            print(f"\n[turn opens at seq {event['seq']}]")
        elif event["type"] == "turn/end":
            print(f"\n[turn closes at seq {event['seq']}]")

    ctx.on("session/event", on_event)

    # Turn 1: the capability rows work; the sandbox row's rewrite is
    # visible in the shell tool's answer.
    agent.send(
        "Use the shell tool to run this command: echo the rows are alive."
        " Then report the tool's exact output."
    )

    # Turn 2: the delegation rows work; the worker provider came from
    # the profile layer, and the parent quotes its child's reply.
    agent.send(
        "Use the subagent tool with provider 'worker' and mode 'foreground'"
        " to delegate this task: \"Reply with one short sentence: why should"
        " an application be a list of rows instead of code that calls"
        " code?\". Then quote the worker's answer exactly."
    )

    print_story(session, "the parent log tells its side")
    for child_id, child in ctx.get("sessions").sessions.items():
        if child_id != "live":
            print_story(child, f"and {child_id} has a whole session of its own")


if __name__ == "__main__":
    main()
