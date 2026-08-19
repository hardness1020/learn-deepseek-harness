"""Offline check for section 05: the scoped tool registry and its pipeline."""

import json

from agent_loop import agent_loop_plugin
from kernel import Context
from message import Message
from session_log import session_log_plugin
from standin import ScriptedModel
from tools import ToolDefinition, tools_plugin

NOTES = {"wifi": "hunter2"}


def thaw(value):
    """Frozen event parts back to plain JSON data, for easy comparison."""
    return json.loads(json.dumps(value, default=dict))


def lookup_tool():
    return ToolDefinition(
        name="lookup",
        description="read one saved note by key",
        params={"key": "which note"},
        execute=lambda args: NOTES[args["key"]],
    )


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


def main():
    check_tool_turn()
    check_every_failure_is_a_result()
    check_ask()
    check_post_review()
    check_scoping()
    check_reversible()
    print("section 05: all checks passed")


def results_of(session):
    return [thaw(e["payload"]) for e in session.log if e["type"] == "tool/result"]


def headers_of(session):
    return [thaw(e["payload"]) for e in session.log if e["type"] == "request/header"]


def check_tool_turn():
    """A tool call makes the turn go around: two steps, one story in the log."""
    ctx, tools, session, agent = build(
        [
            {
                "text": "Checking.",
                "tool_calls": [{"id": "c1", "name": "lookup", "args": {"key": "wifi"}}],
            },
            "The wifi password is hunter2.",
        ]
    )
    tools.register(lookup_tool())
    agent.send("what is the wifi password?")

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
        "tool/call",
        "tool/result",
        "step/end",
        "step/start",
        "request/header",
        "assistant/chunk",
        "assistant/chunk",
        "assistant/chunk",
        "assistant/message",
        "step/end",
        "turn/end",
    ], types

    # The first step ended with more work (reason None); the second ended it.
    reasons = [e["payload"]["reason"] for e in session.log if e["type"] == "step/end"]
    assert reasons == [None, "completed"]

    # Each step recorded what it sent: messages derived fresh, tools by name.
    assert headers_of(session) == [
        {"messages": 1, "tools": ["lookup"]},
        {"messages": 3, "tools": ["lookup"]},
    ]

    # The call is log-only; the result joined the surface as a tool message.
    assert session.derive_messages() == [
        Message("user", "what is the wifi password?"),
        Message(
            "assistant",
            "Checking.",
            ({"id": "c1", "name": "lookup", "args": {"key": "wifi"}},),
        ),
        Message("tool", "hunter2", (), "c1"),
        Message("assistant", "The wifi password is hunter2."),
    ]
    assert results_of(session) == [
        {"call_id": "c1", "name": "lookup", "is_error": False, "content": "hunter2"}
    ]


def check_every_failure_is_a_result():
    """Denied, crashed, invalid, unknown: four failures, four normal results."""
    ctx, tools, session, agent = build(
        [
            {
                "text": "",
                "tool_calls": [
                    {"id": "c1", "name": "lookup", "args": {"key": "secret"}},
                    {"id": "c2", "name": "lookup", "args": {"key": "missing"}},
                    {"id": "c3", "name": "lookup", "args": {}},
                    {"id": "c4", "name": "shred", "args": {}},
                ],
            },
            "None of that worked.",
        ]
    )
    tools.register(lookup_tool())
    tools.guard(
        lambda call: "that key is off limits"
        if call.get("args", {}).get("key") == "secret"
        else None
    )

    # Nothing raises out of send(): every failure came back as data.
    agent.send("try everything")
    assert results_of(session) == [
        {
            "call_id": "c1",
            "name": "lookup",
            "is_error": True,
            "content": "denied: that key is off limits",
        },
        {
            "call_id": "c2",
            "name": "lookup",
            "is_error": True,
            "content": "KeyError: 'missing'",
        },
        {
            "call_id": "c3",
            "name": "lookup",
            "is_error": True,
            "content": "invalid args: missing ['key'], unknown []",
        },
        {"call_id": "c4", "name": "shred", "is_error": True, "content": "unknown tool 'shred'"},
    ]

    # The turn survived all four: the next step derived the failures as tool
    # messages (user + assistant + four results = 6) and closed normally.
    assert headers_of(session)[-1]["messages"] == 6
    assert [e["type"] for e in session.log[-2:]] == ["step/end", "turn/end"]
    tool_messages = [m for m in session.derive_messages() if m.role == "tool"]
    assert [m.call_id for m in tool_messages] == ["c1", "c2", "c3", "c4"]


ASK_SCRIPT = [
    {
        "text": "",
        "tool_calls": [{"id": "c1", "name": "lookup", "args": {"key": "wifi"}}],
    },
    "Done.",
]


def check_ask():
    """Votes only tighten, and an ask with no approver fails closed."""
    ctx, tools, session, agent = build(list(ASK_SCRIPT))
    tools.register(lookup_tool())
    tools.pre(lambda call: "allow")  # a loose vote cannot loosen...
    tools.pre(lambda call: "ask")  # ...a tighter one
    agent.send("read the wifi note")
    (result,) = results_of(session)
    assert result["is_error"] is True
    assert result["content"] == "approval was asked and not given"

    # Same script, but now someone answers the ask.
    ctx, tools, session, agent = build(list(ASK_SCRIPT))
    tools.register(lookup_tool())
    tools.pre(lambda call: "ask")
    tools.asker = lambda call: True
    agent.send("read the wifi note")
    assert results_of(session) == [
        {"call_id": "c1", "name": "lookup", "is_error": False, "content": "hunter2"}
    ]


def check_post_review():
    """Post hooks review every result on its way to the log."""
    ctx, tools, session, agent = build(list(ASK_SCRIPT))
    tools.register(lookup_tool())
    tools.post(
        lambda call, result: dict(result, content="[redacted]")
        if "hunter2" in result["content"]
        else None
    )
    agent.send("read the wifi note")
    (result,) = results_of(session)
    assert result == {
        "call_id": "c1",
        "name": "lookup",
        "is_error": False,
        "content": "[redacted]",
    }


def check_scoping():
    """A scope layer shadows names; a restriction narrows what a scope sees."""
    ctx = Context()
    ctx.plugin(session_log_plugin)
    ctx.plugin(tools_plugin)
    ctx.plugin(agent_loop_plugin)
    tools = ctx.get("tools")
    tools.register(
        ToolDefinition("where", "which layer answers", {}, lambda args: "global")
    )
    tools.register(ToolDefinition("extra", "a second tool", {}, lambda args: "extra"))

    sessions, agents = ctx.get("sessions"), ctx.get("agents")
    scope_a, scope_b = tools.scope("a"), tools.scope("b")
    session_a = sessions.create("sa")
    agent_a = agents.create(
        "a",
        session_a,
        ScriptedModel(
            [{"text": "", "tool_calls": [{"id": "a1", "name": "where", "args": {}}]}, "Done."]
        ),
        scope_a,
    )
    session_b = sessions.create("sb")
    agent_b = agents.create(
        "b",
        session_b,
        ScriptedModel(
            [
                {
                    "text": "",
                    "tool_calls": [
                        {"id": "b1", "name": "where", "args": {}},
                        {"id": "b2", "name": "extra", "args": {}},
                    ],
                },
                "Done.",
            ]
        ),
        scope_b,
    )

    # Agent a's layer shadows the global "where"; agent b is restricted.
    scope_a.register(
        ToolDefinition("where", "which layer answers", {}, lambda args: "scoped for a")
    )
    tools.restrict(["where"], scope="b")

    agent_a.send("where am I?")
    agent_b.send("where am I?")

    # What each request offered, straight off the log.
    assert headers_of(session_a)[0]["tools"] == ["where", "extra"]
    assert headers_of(session_b)[0]["tools"] == ["where"]

    # a's call hit its own layer; b fell through to the global tool, and the
    # restricted "extra" does not exist as far as b is concerned.
    assert [r["content"] for r in results_of(session_a)] == ["scoped for a"]
    assert results_of(session_b) == [
        {"call_id": "b1", "name": "where", "is_error": False, "content": "global"},
        {"call_id": "b2", "name": "extra", "is_error": True, "content": "unknown tool 'extra'"},
    ]


def check_reversible():
    """Everything is a plugin, and every registration is reversible."""
    ctx = Context()
    ctx.plugin(session_log_plugin)
    ctx.plugin(tools_plugin)
    ctx.plugin(agent_loop_plugin)

    def clock_plugin(pctx):
        tool = ToolDefinition("clock", "tell the time", {}, lambda args: "10:00")
        pctx.effect(pctx.get("tools").register(tool), "tool(clock)")

    fiber = ctx.plugin(clock_plugin)
    session = ctx.get("sessions").create("s")
    agent = ctx.get("agents").create(
        "a",
        session,
        ScriptedModel(
            [
                {"text": "", "tool_calls": [{"id": "c1", "name": "clock", "args": {}}]},
                "It is 10:00.",
                {"text": "", "tool_calls": [{"id": "c2", "name": "clock", "args": {}}]},
                "The clock is gone.",
            ]
        ),
        ctx.get("tools").scope("a"),
    )

    agent.send("what time is it?")
    assert results_of(session)[0] == {
        "call_id": "c1",
        "name": "clock",
        "is_error": False,
        "content": "10:00",
    }

    # Unload the plugin: its registration goes with it, and even a call to
    # the now-missing tool still comes back as a normal result.
    fiber.dispose()
    agent.send("and now?")
    headers = headers_of(session)
    assert headers[0]["tools"] == ["clock"]
    assert headers[-1]["tools"] == []
    assert results_of(session)[-1] == {
        "call_id": "c2",
        "name": "clock",
        "is_error": True,
        "content": "unknown tool 'clock'",
    }


if __name__ == "__main__":
    main()
