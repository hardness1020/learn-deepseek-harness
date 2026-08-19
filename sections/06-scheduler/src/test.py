"""Offline check for section 06: the 4-stage parallel tool scheduler."""

import json
import threading

from agent_loop import agent_loop_plugin
from kernel import Context
from session_log import session_log_plugin
from standin import ScriptedModel
from tools import ToolDefinition, tools_plugin


def thaw(value):
    """Frozen event parts back to plain JSON data, for easy comparison."""
    return json.loads(json.dumps(value, default=dict))


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


def results_of(session):
    return [thaw(e["payload"]) for e in session.log if e["type"] == "tool/result"]


def reasons_of(session):
    return [e["payload"]["reason"] for e in session.log if e["type"] == "step/end"]


def main():
    check_batches()
    check_abort()
    print("section 06: all checks passed")


def check_batches():
    """Safe calls overlap, an exclusive call runs alone, rows keep model order."""
    lock = threading.Lock()
    in_flight = [0]
    pair = threading.Barrier(2)  # releases only if slow and quick overlap
    quick_done = threading.Event()
    solo_done = threading.Event()

    def gauged(body):
        """Count bodies in flight, so exclusivity is visible from inside one."""

        def run(args):
            with lock:
                in_flight[0] += 1
                seen = in_flight[0]
            try:
                return body(seen)
            finally:
                with lock:
                    in_flight[0] -= 1

        return run

    def slow(seen):
        pair.wait(timeout=5)  # proves quick is running at the same moment
        quick_done.wait(timeout=5)  # then deliberately finish after quick
        return "overlapped with quick"

    def quick(seen):
        pair.wait(timeout=5)
        quick_done.set()
        return "overlapped with slow"

    def solo(seen):
        alone = seen == 1  # the barrier drained every earlier body first
        solo_done.set()
        return f"ran alone: {alone}"

    def after(seen):
        return f"solo finished first: {solo_done.is_set()}, alone: {seen == 1}"

    ctx, tools, session, agent = build(
        [
            {
                "text": "",
                "tool_calls": [
                    {"id": "c1", "name": "slow", "args": {}},
                    {"id": "c2", "name": "quick", "args": {}},
                    {"id": "c3", "name": "solo", "args": {}},
                    {"id": "c4", "name": "after", "args": {}},
                ],
            },
            "All four answered.",
        ]
    )
    tools.register(
        ToolDefinition("slow", "a slow read", {}, gauged(slow), is_concurrency_safe=True)
    )
    tools.register(
        ToolDefinition("quick", "a quick read", {}, gauged(quick), is_concurrency_safe=True)
    )
    # No flag on solo: exclusive is the default, a tool must claim safety.
    tools.register(ToolDefinition("solo", "an exclusive write", {}, gauged(solo)))
    tools.register(
        ToolDefinition("after", "a read behind the barrier", {}, gauged(after), is_concurrency_safe=True)
    )
    agent.send("run everything at once")

    # All four tool/call rows land before the first result: log first, run second.
    types = [e["type"] for e in session.log]
    first_call = types.index("tool/call")
    assert types[first_call : first_call + 8] == ["tool/call"] * 4 + ["tool/result"] * 4

    # quick finished before slow did, yet the results sit in the model's order.
    # The barrier around solo is visible from inside the bodies themselves.
    assert results_of(session) == [
        {"call_id": "c1", "name": "slow", "is_error": False, "content": "overlapped with quick"},
        {"call_id": "c2", "name": "quick", "is_error": False, "content": "overlapped with slow"},
        {"call_id": "c3", "name": "solo", "is_error": False, "content": "ran alone: True"},
        {
            "call_id": "c4",
            "name": "after",
            "is_error": False,
            "content": "solo finished first: True, alone: True",
        },
    ]
    assert reasons_of(session) == [None, "completed"]


def check_abort():
    """Cancel mid-batch: started work finishes, unstarted calls answer anyway."""
    ctx, tools, session, agent = build(
        [
            {
                "text": "",
                "tool_calls": [
                    {"id": "c1", "name": "stop", "args": {}},
                    {"id": "c2", "name": "sibling", "args": {}},
                    {"id": "c3", "name": "late", "args": {}},
                    {"id": "c4", "name": "last", "args": {}},
                ],
            },
            "A fresh turn still works.",
        ]
    )

    def stop_body(args):
        agent.cancel()  # a worker thread presses cancel mid-batch
        return "stopping"

    tools.register(ToolDefinition("stop", "cancel the turn", {}, stop_body, is_concurrency_safe=True))
    tools.register(
        ToolDefinition(
            "sibling",
            "dispatched alongside stop",
            {},
            lambda args: "kept running",
            is_concurrency_safe=True,
        )
    )
    # late is exclusive, so it sits behind a barrier the abort reaches first.
    tools.register(ToolDefinition("late", "never starts", {}, lambda args: "must not run"))
    tools.register(
        ToolDefinition("last", "never starts", {}, lambda args: "must not run", is_concurrency_safe=True)
    )
    agent.send("stop everything")

    # The sibling was already dispatched, so it ran to its end; the two calls
    # behind the barrier never started and got synthetic results instead.
    assert results_of(session) == [
        {"call_id": "c1", "name": "stop", "is_error": False, "content": "stopping"},
        {"call_id": "c2", "name": "sibling", "is_error": False, "content": "kept running"},
        {"call_id": "c3", "name": "late", "is_error": True, "content": "aborted before dispatch"},
        {"call_id": "c4", "name": "last", "is_error": True, "content": "aborted before dispatch"},
    ]

    # The turn closed instead of going around, and the log stayed whole:
    # every question in the derived history has its answer, so replay works.
    assert reasons_of(session) == ["aborted"]
    assert [e["type"] for e in session.log[-2:]] == ["step/end", "turn/end"]
    tool_messages = [m for m in session.derive_messages() if m.role == "tool"]
    assert [m.call_id for m in tool_messages] == ["c1", "c2", "c3", "c4"]

    # cancel() marks one turn, not the agent: the next send starts fresh.
    agent.send("still there?")
    assert reasons_of(session) == ["aborted", "completed"]


if __name__ == "__main__":
    main()
