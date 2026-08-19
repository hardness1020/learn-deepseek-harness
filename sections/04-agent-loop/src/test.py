"""Offline check for section 04: the turn/step machine, log as only durable state."""

import json

from agent_loop import Agent, agent_loop_plugin
from kernel import Context
from message import Message
from session_log import Session, session_log_plugin
from standin import ScriptedModel


def thaw(value):
    """Frozen event parts back to plain JSON data, for replaying appends."""
    return json.loads(json.dumps(value, default=dict))


def build(responses):
    ctx = Context()
    ctx.plugin(session_log_plugin)
    ctx.plugin(agent_loop_plugin)
    session = ctx.get("sessions").create("s1")
    agent = ctx.get("agents").create("a1", session, ScriptedModel(responses))
    return session, agent


def main():
    session, agent = build(["Hello."])

    # One turn: the log tells the whole story, boundaries included.
    agent.send("hi")
    types = [event["type"] for event in session.log]
    assert types == [
        "user/message",
        "turn/start",
        "step/start",
        "request/header",
        "assistant/chunk",
        "assistant/chunk",
        "assistant/chunk",
        "assistant/message",
        "step/end",
        "turn/end",
    ], types

    # The markers are log-only: the model's view is just the two messages.
    assert session.derive_messages() == [
        Message("user", "hi"),
        Message("assistant", "Hello."),
    ]

    # The step recorded what it sent (one derived message: the user's).
    header = next(e for e in session.log if e["type"] == "request/header")
    assert header["payload"]["messages"] == 1
    end = next(e for e in session.log if e["type"] == "step/end")
    assert end["payload"]["reason"] == "completed"

    check_rederivation()
    check_resume()
    check_crash_mid_step()
    check_busy_guard()

    print("section 04: all checks passed")


def header_counts(session):
    """Every request/header count, in order: what each step actually sent."""
    return [
        event["payload"]["messages"]
        for event in session.log
        if event["type"] == "request/header"
    ]


def check_rederivation():
    """Each step derives from the log as it is now, not as it was last turn."""
    session, agent = build(["Hello.", "Now this.", "The summary says 42."])
    agent.send("hi")
    agent.send("and now?")
    assert header_counts(session) == [1, 3]

    # Compact everything so far into one summary (section 03's Mechanism).
    session.append(
        "user/message",
        {"content": "Summary: greeted twice."},
        surface_op={"op": "replace", "start": 0, "end": len(session.log)},
    )

    # The next step sends 2 messages (summary + new user), not the 5 a cached
    # list from last turn would still hold. The loop derived, it never stored.
    agent.send("what did we say?")
    assert header_counts(session) == [1, 3, 2]


def check_resume():
    """An Agent holds nothing durable: replay the log, get the same future."""
    session, agent = build(["Hello.", "Now this."])
    agent.send("hi")

    # Rebuild the session from its own rows and hand it to a brand-new Agent.
    replayed = Session("replayed")
    for event in session.log:
        replayed.append(event["type"], thaw(event["payload"]), thaw(event["surface_op"]))
    resumed = Agent(replayed, ScriptedModel(["Now this."]))

    mark = len(session.log)
    agent.send("and now?")
    resumed.send("and now?")

    # Both futures are byte-for-byte the same rows: the log was the state.
    assert thaw(session.log[mark:]) == thaw(replayed.log[mark:])
    assert replayed.derive_messages() == session.derive_messages()


def check_crash_mid_step():
    """A crash mid-step leaves a readable log, and the next turn is clean."""

    def exploding(messages):
        yield ("chunk", "Hal")
        raise ConnectionError("wire dropped")

    session, agent = build(["Hello again."])
    agent.model = exploding
    try:
        agent.send("hi")
        assert False, "the model error must escape send()"
    except ConnectionError:
        pass

    # The log records exactly how far the step got: started, never ended.
    assert [e["type"] for e in session.log[-3:]] == [
        "step/start",
        "request/header",
        "assistant/chunk",
    ]

    # The orphan chunk is log-only, so the derived view needs no repair: the
    # next step derives both user messages and nothing from the dead step.
    agent.model = ScriptedModel(["Hello again."])
    agent.send("hi again")
    assert header_counts(session) == [1, 2]
    assert session.derive_messages() == [
        Message("user", "hi"),
        Message("user", "hi again"),
        Message("assistant", "Hello again."),
    ]


def check_busy_guard():
    """One log, one story: a send during a running turn refuses loudly."""
    session, agent = build([])

    def nested(messages):
        agent.send("again")
        yield ("chunk", "never reached")

    agent.model = nested
    try:
        agent.send("hi")
        assert False, "a mid-turn send must raise"
    except RuntimeError:
        pass
    # The refused send never touched the log: no second story interleaved.
    user_rows = [e["payload"]["content"] for e in session.log if e["type"] == "user/message"]
    assert user_rows == ["hi"]


if __name__ == "__main__":
    main()
