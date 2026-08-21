"""Live demo: section 12 subagent against the real Anthropic API.

The model delegates a task to the "worker" provider foreground: a
child agent with a session of its own runs the whole errand against
the real API, and the parent's tool call answers with nothing but the
child's reply, which the parent quotes. Then it delegates a second
task in background mode: the turn closes on a job id while the child
is still thinking, and the completion notice wakes the parent in a
turn it never asked for, where it fetches the child's answer with
job_output. This file is the only place the SDK and the mini-Message
to Anthropic translation live (ADR 0001). Scripted turns only; skips
politely when the key or the live-demo deps are missing.
"""

import os
import time

from agent_loop import agent_loop_plugin
from jobs import JobOwner, job_tools, jobs_plugin
from kernel import Context
from message import Message
from session_log import session_log_plugin
from subagent import in_process_provider, subagent_plugin, subagent_tools
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

    ctx = Context()
    ctx.plugin(session_log_plugin)
    ctx.plugin(tools_plugin)
    ctx.plugin(system_prompt_plugin)
    ctx.plugin(jobs_plugin)
    ctx.plugin(agent_loop_plugin)
    ctx.plugin(subagent_plugin)

    def providers_plugin(inner):
        # One name, one provider: every child gets the live model too.
        inner.effect(
            inner.get("subagents").register(
                "worker", in_process_provider(inner, lambda: live_model)
            ),
            "worker provider",
        )

    ctx.plugin(providers_plugin)

    session = ctx.get("sessions").create("live")
    agent = ctx.get("agents").create(
        "live", session, live_model, ctx.get("tools").scope("live"),
        ctx.get("system_prompt"),
    )
    # The owner is fixed at mount: the delegation tool and the job
    # controls both answer to the registry as this agent.
    owner = JobOwner("live", agent)
    ctx.plugin(subagent_tools(owner))
    ctx.plugin(job_tools(owner))

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

    # Turn 1: foreground delegation; the parent quotes the child's reply.
    agent.send(
        "Use the subagent tool with provider 'worker' and mode 'foreground'"
        " to delegate this task: \"Reply with one short sentence: why is an"
        " append-only log a good place to keep an agent's state?\". Then"
        " quote the worker's answer exactly."
    )

    # Turn 2: background delegation; the turn closes on a job id.
    agent.send(
        "Now use the subagent tool with provider 'worker' and mode"
        " 'background' to delegate this task: \"Reply with a haiku about"
        " work finishing after the meeting ended.\". Report the job id and"
        " finish your reply immediately. Later, when a message tells you"
        " the job finished, fetch its output with job_output and quote it."
    )
    print("\n[the agent is idle; the child is still thinking on its own thread]")

    # The notice turn arrives on the settling thread; the main thread
    # only watches the log until that turn has closed.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        turns = sum(1 for e in session.log if e["type"] == "turn/end")
        if turns >= 3 and agent.status == "idle":
            break
        time.sleep(0.2)

    print_story(session, "the parent log tells its side")
    for child_id, child in ctx.get("sessions").sessions.items():
        if child_id != "live":
            print_story(child, f"and {child_id} has a whole session of its own")


if __name__ == "__main__":
    main()
