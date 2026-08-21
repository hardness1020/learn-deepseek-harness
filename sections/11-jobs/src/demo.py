"""Live demo: section 11 jobs against the real Anthropic API.

The model starts a genuinely slow command as a background job and the
turn closes on nothing but the job id. While the agent sits idle the
work finishes on its own thread, and the completion notice arrives
through the inbox as a followup: a turn the model did not ask for,
where it reads the notice, polls job_output, and reports. A second,
quiet job is killed in the same breath it starts, showing the fence's
kill switch and first-wins settlement live. This file is the only
place the SDK and the mini-Message to Anthropic translation live
(ADR 0001). Scripted turns only; skips politely when the key or the
live-demo deps are missing.
"""

import os
import time

from agent_loop import agent_loop_plugin
from capabilities import ShellExecutor, provider
from jobs import JobOwner, job_tools, jobs_plugin
from kernel import Context
from message import Message
from session_log import session_log_plugin
from system_prompt import system_prompt_plugin
from tools import tools_plugin


class SlowEchoShellExecutor(ShellExecutor):
    """A shell whose every command takes real time, then echoes its argv."""

    def __init__(self, seconds):
        self._seconds = seconds

    def run(self, argv):
        time.sleep(self._seconds)
        return " ".join(argv)


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
    ctx.plugin(jobs_plugin)
    ctx.plugin(agent_loop_plugin)
    # Every command through this shell takes 4 real seconds, so the
    # first turn genuinely ends while the work is still running.
    ctx.plugin(provider("shell", SlowEchoShellExecutor(4)))

    session = ctx.get("sessions").create("live")
    agent = ctx.get("agents").create(
        "a1", session, live_model, ctx.get("tools").scope("a1"), ctx.get("system_prompt")
    )
    # The owner is fixed at mount: whatever ids the model types, these
    # tools answer to the registry as a1.
    ctx.plugin(job_tools(JobOwner("a1", agent)))

    def on_event(_session, event):
        payload = event["payload"]
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

    # Turn 1: the model starts the job and the turn closes on the id.
    agent.send(
        "Start the command `echo overnight build done` as a background job"
        " with wakeup delivery, then tell me its id. Later, when a message"
        " tells you it finished, fetch its output with job_output and quote"
        " the output exactly."
    )
    print("\n[the agent is idle; the job is still running on its own thread]")

    # The notice turn arrives on the settling thread; the main thread
    # only watches the log until that turn has closed.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        turns = sum(1 for e in session.log if e["type"] == "turn/end")
        if turns >= 2 and agent.status == "idle":
            break
        time.sleep(0.2)

    # Turn 3: a quiet job, killed in the same reply it was started in.
    agent.send(
        "Now start `echo never mind` as a quiet background job, kill it"
        " immediately with job_kill, then list your jobs and tell me what"
        " the kill reported."
    )

    print_story(session)


if __name__ == "__main__":
    main()
