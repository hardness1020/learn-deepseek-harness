"""Offline check for section 07: next-turn/next-step steering."""

from agent_loop import agent_loop_plugin
from kernel import Context
from session_log import session_log_plugin
from standin import ScriptedModel
from tools import ToolDefinition, tools_plugin


def build(responses):
    ctx = Context()
    ctx.plugin(session_log_plugin)
    ctx.plugin(tools_plugin)
    ctx.plugin(agent_loop_plugin)
    tools = ctx.get("tools")
    session = ctx.get("sessions").create("s1")
    agent = ctx.get("agents").create(
        "a1", session, ScriptedModel(responses), tools.scope("a1")
    )
    return ctx, tools, session, agent


def turns_of(session):
    """The log split into turns, one list of events per turn/start."""
    turns = []
    for event in session.log:
        if event["type"] == "turn/start":
            turns.append([])
        turns[-1].append(event)
    return turns


def user_texts(events):
    return [e["payload"]["content"] for e in events if e["type"] == "user/message"]


def reasons_of(events):
    return [e["payload"]["reason"] for e in events if e["type"] == "step/end"]


def main():
    check_two_targets()
    check_inject_parks()
    check_turn_close_recheck()
    check_cancel_clears_inbox()
    print("section 07: all checks passed")


def check_two_targets():
    """Steering joins the turn underway; queued prompts each get their own."""
    ctx, tools, session, agent = build(
        [
            {"text": "", "tool_calls": [{"id": "c1", "name": "read", "args": {}}]},
            "Noted the steering.",
            "The first queued prompt got its own turn.",
            "And the second got another.",
        ]
    )

    def read_body(args):
        # A worker thread sends mid-turn: everything queues, nothing raises.
        agent.steer("while reading: also check the dates")
        agent.followup("queued: summarize everything")
        agent.followup("queued: then say goodbye")
        return "note contents"

    tools.register(ToolDefinition("read", "read a note", {}, read_body))
    agent.send("read my note")

    # Three turns from one send: the steer stayed inside turn 1, and each
    # queued prompt opened a turn of its own, in order. Prompts never merge.
    turns = turns_of(session)
    assert len(turns) == 3
    assert [user_texts(t) for t in turns] == [
        ["read my note", "while reading: also check the dates"],
        ["queued: summarize everything"],
        ["queued: then say goodbye"],
    ]
    assert [reasons_of(t) for t in turns] == [[None, "completed"], ["completed"], ["completed"]]
    assert all(t[-1]["type"] == "turn/end" for t in turns)

    # Claim happens at the step boundary, never mid-step: the steering text
    # entered the log only after the running step closed and the next opened.
    types = [e["type"] for e in turns[0]]
    result_at = types.index("tool/result")
    assert types[result_at : result_at + 4] == [
        "tool/result",
        "step/end",
        "step/start",
        "user/message",
    ]


def check_inject_parks():
    """inject() routes without waking; the next wake claims next-step first."""
    ctx, tools, session, agent = build(["hello, plaid-sky person"])
    agent.inject("context: the sky is plaid")

    # Routed, not applied: no driver woke and not one row reached the log.
    assert session.log == []

    agent.send("greet me")

    # One turn, and its first claim took all next-step input before the
    # queued prompt, so the parked context precedes the prompt in history.
    turns = turns_of(session)
    assert len(turns) == 1
    assert user_texts(turns[0]) == ["context: the sky is plaid", "greet me"]
    roles = [m.role for m in session.derive_messages()]
    assert roles == ["user", "user", "assistant"]


def check_turn_close_recheck():
    """A step may end "completed", but fresh steering keeps the turn open."""
    ctx, tools, session, agent = build(["first answer", "second answer"])

    def on_event(_session, event):
        # One shot: steering arrives while the completed step is still open.
        if event["type"] == "assistant/message" and event["payload"]["content"] == "first answer":
            agent.steer("wait, one more thing")

    ctx.on("session/event", on_event)
    agent.send("hi")

    # No tool calls anywhere, yet the turn ran two steps: the close re-check
    # found next-step input and spent another step in the same turn.
    turns = turns_of(session)
    assert len(turns) == 1
    assert reasons_of(turns[0]) == ["completed", "completed"]
    assert user_texts(turns[0]) == ["hi", "wait, one more thing"]
    assert turns[0][-1]["type"] == "turn/end"


def check_cancel_clears_inbox():
    """cancel() drops pending input, so an aborted turn cannot resurrect."""
    ctx, tools, session, agent = build(
        [
            {"text": "", "tool_calls": [{"id": "c1", "name": "stop", "args": {}}]},
            "A fresh turn still works.",
        ]
    )

    def stop_body(args):
        agent.steer("stale steering")
        agent.followup("stale prompt")
        agent.cancel()
        return "stopping"

    tools.register(ToolDefinition("stop", "cancel the turn", {}, stop_body))
    agent.send("stop everything")

    # The turn closed aborted, and the pending input queued before the cancel
    # never reached the log: no ghost step, no ghost turn.
    turns = turns_of(session)
    assert len(turns) == 1
    assert reasons_of(turns[0]) == ["aborted"]
    assert [e["type"] for e in turns[0][-2:]] == ["step/end", "turn/end"]
    assert all("stale" not in text for text in user_texts(turns[0]))

    # cancel() marks one turn, not the agent: the next send starts fresh.
    agent.send("still there?")
    turns = turns_of(session)
    assert len(turns) == 2
    assert user_texts(turns[1]) == ["still there?"]
    assert reasons_of(turns[1]) == ["completed"]


if __name__ == "__main__":
    main()
