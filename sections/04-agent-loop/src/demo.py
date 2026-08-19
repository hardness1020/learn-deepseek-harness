"""Live demo: the section 04 turn/step machine against the real Anthropic API.

The loop does not change: the demo plugs a different callable into the
Model seam. This file is the only place the SDK and the mini-Message to
Anthropic translation live (ADR 0001). Scripted turns only; skips
politely when the key or the live-demo deps are missing.
"""

import os

from agent_loop import agent_loop_plugin
from kernel import Context
from message import Message
from session_log import session_log_plugin


def live_model(messages):
    """The Model seam, live: mini Messages in, chunks then one final Message."""
    import anthropic

    # The whole translation: mini roles collapse onto the two wire roles.
    wire = [
        {"role": "assistant" if m.role == "assistant" else "user", "content": m.content}
        for m in messages
    ]
    client = anthropic.Anthropic()
    with client.messages.stream(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
        max_tokens=300,
        messages=wire,
    ) as stream:
        for piece in stream.text_stream:
            yield ("chunk", piece)
        final = stream.get_final_message()
    text = "".join(block.text for block in final.content if block.type == "text")
    yield ("message", Message(role="assistant", content=text))


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
        extra = ""
        if event["type"] == "request/header":
            extra = f"  (sent {event['payload']['messages']} derived messages)"
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
    ctx.plugin(agent_loop_plugin)
    session = ctx.get("sessions").create("live")
    agent = ctx.get("agents").create("a1", session, live_model)

    # Streaming straight off the bus (section 02): every chunk the loop
    # appends is printed the moment it lands in the log.
    def on_event(_session, event):
        if event["type"] == "assistant/chunk":
            print(event["payload"]["text"], end="", flush=True)

    ctx.on("session/event", on_event)

    def turn(text):
        print(f"\nuser: {text}")
        print("assistant: ", end="", flush=True)
        agent.send(text)
        print()

    turn("Greet me in one short sentence.")
    turn("Now give the same greeting in exactly three words.")

    # The Foundation payoff: compact the whole exchange (section 03), keep
    # talking, and the next step derives only the summary plus the question.
    session.append(
        "user/message",
        {"content": "Summary of the conversation so far: you greeted me twice."},
        surface_op={"op": "replace", "start": 0, "end": len(session.log)},
    )
    turn("Based only on what you can see: how many times have you greeted me?")

    print_story(session)


if __name__ == "__main__":
    main()
