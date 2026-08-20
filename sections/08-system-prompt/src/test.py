"""Offline check for section 08: ordered providers, runtime-context snapshot."""

from agent_loop import agent_loop_plugin
from kernel import Context
from session_log import session_log_plugin
from standin import ScriptedModel
from system_prompt import IDENTITY, is_snapshot, system_prompt_plugin
from tools import ToolDefinition, tools_plugin


def build(responses):
    ctx = Context()
    ctx.plugin(session_log_plugin)
    ctx.plugin(tools_plugin)
    ctx.plugin(system_prompt_plugin)
    ctx.plugin(agent_loop_plugin)
    tools = ctx.get("tools")
    prompt = ctx.get("system_prompt")
    session = ctx.get("sessions").create("s1")
    agent = ctx.get("agents").create(
        "a1", session, ScriptedModel(responses), tools.scope("a1"), prompt
    )
    return ctx, tools, prompt, session, agent


def headers_of(session):
    return [e["payload"] for e in session.log if e["type"] == "request/header"]


def snapshots_of(session):
    """The runtime-context rows: user/message events carrying the snapshot."""
    return [e["payload"]["content"] for e in session.log if is_snapshot(e)]


def main():
    check_three_artifacts()
    check_stable_system_snapshot_reemit()
    check_snapshot_dedupe()
    check_strict_interpolation()
    check_reversible()
    print("section 08: all checks passed")


def check_three_artifacts():
    """One assembly, three artifacts: system text, tool list, snapshot row."""
    ctx, tools, prompt, session, agent = build(["hello"])
    tools.register(ToolDefinition("lookup", "read a note", {"key": "which note"}, lambda a: "x"))
    prompt.variable("user", "Ada")
    prompt.section("persona", "Answer {{user}} briefly.")
    prompt.section("guidance", "Prefer one sentence.")  # same order: ties break by registration
    clock = {"now": "10:00"}
    prompt.context("time", lambda ac: f"time: {clock['now']}")
    prompt.context("cwd", lambda ac: "cwd: /home/ada", order=10)

    agent.send("hi")

    # The system text: ordered sections, variables interpolated, built-in
    # identity first (order -100), then persona and guidance in registration
    # order. Identical text would come out again next step.
    (header,) = headers_of(session)
    assert header["system"] == IDENTITY + "\n\nAnswer Ada briefly.\n\nPrefer one sentence."

    # The tool list came out of the same assembly, through the bridge.
    # (The log froze the payload, so the list comes back as a tuple.)
    assert list(header["tools"]) == ["lookup"]

    # The snapshot: ordered contexts, one user/message row, claimed prompt
    # first, snapshot second, both derived as plain user history.
    assert snapshots_of(session) == ["time: 10:00\ncwd: /home/ada"]
    types = [e["type"] for e in session.log[:5]]
    assert types == ["turn/start", "step/start", "user/message", "user/message", "request/header"]
    assert header["messages"] == 2
    roles = [m.role for m in session.derive_messages()]
    assert roles == ["user", "user", "assistant"]


def check_stable_system_snapshot_reemit():
    """The clock moves mid-turn: system text holds still, the snapshot re-emits."""
    ctx, tools, prompt, session, agent = build(
        [
            {"text": "", "tool_calls": [{"id": "c1", "name": "tick", "args": {}}]},
            "done",
        ]
    )
    clock = {"now": "10:00"}
    prompt.context("time", lambda ac: f"time: {clock['now']}")

    def tick_body(args):
        clock["now"] = "10:01"
        return "ticked"

    tools.register(ToolDefinition("tick", "advance the clock", {}, tick_body))
    agent.send("go")

    # Two steps, two requests, one system text: dynamic state never got in.
    headers = headers_of(session)
    assert len(headers) == 2
    assert headers[0]["system"] == headers[1]["system"]

    # The snapshot changed between boundaries, so step 2 re-emitted it as a
    # fresh user/message row; both readings are durable history now.
    assert snapshots_of(session) == ["time: 10:00", "time: 10:01"]
    types = [e["type"] for e in session.log]
    second_step = types.index("step/start", types.index("step/end"))
    assert types[second_step : second_step + 3] == ["step/start", "user/message", "request/header"]


def check_snapshot_dedupe():
    """An unchanged snapshot is never re-emitted, within a turn or across turns."""
    ctx, tools, prompt, session, agent = build(
        [
            {"text": "", "tool_calls": [{"id": "c1", "name": "noop", "args": {}}]},
            "done",
            "again",
        ]
    )
    prompt.context("cwd", lambda ac: "cwd: /home/ada")
    tools.register(ToolDefinition("noop", "do nothing", {}, lambda args: "ok"))

    agent.send("go")  # a two-step turn: the second boundary finds no change
    agent.send("more")  # a later turn: the retained snapshot is the log itself

    assert len(headers_of(session)) == 3
    assert snapshots_of(session) == ["cwd: /home/ada"]


def check_strict_interpolation():
    """An unknown or unset {{variable}} refuses to ship the request."""
    ctx, tools, prompt, session, agent = build(["never sent"])
    prompt.section("broken", "Greet {{missing}} warmly.")

    try:
        agent.send("hi")
        raised = False
    except ValueError:
        raised = True
    assert raised

    # The claim landed, then assembly threw: the log shows no request/header,
    # so the transcript never claims a model call that was never made.
    assert [e["type"] for e in session.log] == ["turn/start", "step/start", "user/message"]

    # A registered-but-unset variable is the same hole, refused the same way.
    ctx, tools, prompt, session, agent = build(["never sent"])
    prompt.variable("user", None)
    prompt.section("broken", "Greet {{user}} warmly.")
    try:
        agent.send("hi")
        raised = False
    except ValueError:
        raised = True
    assert raised
    assert "request/header" not in [e["type"] for e in session.log]


def check_reversible():
    """Every provider registration hands back its undo, kernel-style."""
    ctx, tools, prompt, session, agent = build(["first", "second"])
    drop_section = prompt.section("persona", "Be verbose.")
    drop_context = prompt.context("time", lambda ac: "time: 10:00")

    agent.send("one")
    drop_section()
    drop_context()
    agent.send("two")

    headers = headers_of(session)
    assert headers[0]["system"] == IDENTITY + "\n\nBe verbose."
    assert headers[1]["system"] == IDENTITY
    assert snapshots_of(session) == ["time: 10:00"]  # gone: nothing new to emit


if __name__ == "__main__":
    main()
