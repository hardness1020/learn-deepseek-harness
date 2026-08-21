"""Offline check for section 12: the named-provider delegation registry."""

import threading
import time

from agent_loop import agent_loop_plugin
from jobs import JobOwner, job_tools, jobs_plugin
from kernel import Context
from session_log import session_log_plugin
from standin import ScriptedModel
from subagent import SubagentRun, in_process_provider, subagent_plugin, subagent_tools
from system_prompt import system_prompt_plugin
from tools import tools_plugin


class GateModel:
    """Test model: the child's reply waits on a gate the check opens by
    hand, so "the child is still thinking" and "the child answered" are
    decided here, never by timing."""

    def __init__(self, inner):
        self.gate = threading.Event()
        self._inner = inner

    def open(self):
        self.gate.set()

    def __call__(self, messages, tools=(), system=""):
        assert self.gate.wait(timeout=5), "the check never opened the gate"
        yield from self._inner(messages, tools, system)


def build(responses, child_factory=None, with_jobs=True):
    ctx = Context()
    ctx.plugin(session_log_plugin)
    ctx.plugin(tools_plugin)
    ctx.plugin(system_prompt_plugin)
    if with_jobs:
        ctx.plugin(jobs_plugin)
    ctx.plugin(agent_loop_plugin)
    ctx.plugin(subagent_plugin)
    session = ctx.get("sessions").create("s1")
    agent = ctx.get("agents").create(
        "a1", session, ScriptedModel(responses), ctx.get("tools").scope("a1"),
        ctx.get("system_prompt"),
    )
    owner = JobOwner("a1", agent)
    if with_jobs:
        ctx.plugin(job_tools(owner))
    ctx.plugin(subagent_tools(owner))
    if child_factory is not None:
        ctx.get("subagents").register("worker", in_process_provider(ctx, child_factory))
    return ctx, session, agent


def wait_until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the background work to land")


def settled(session, agent, turns):
    """True once `turns` turns closed and the drain loop has gone idle."""
    return turns_of(session) == turns and agent.status == "idle"


def turns_of(session):
    return sum(1 for e in session.log if e["type"] == "turn/end")


def results_of(session):
    return [e["payload"] for e in session.log if e["type"] == "tool/result"]


def users_of(session):
    return [e["payload"]["content"] for e in session.log if e["type"] == "user/message"]


def replies_of(session):
    return [
        e["payload"]["content"]
        for e in session.log
        if e["type"] == "assistant/message" and e["payload"]["content"]
    ]


def sub_call(call_id, provider, task, mode):
    return {
        "id": call_id,
        "name": "subagent",
        "args": {"provider": provider, "task": task, "mode": mode},
    }


def main():
    check_foreground_answers_with_the_childs_reply()
    check_the_contract_is_the_run_not_the_runner()
    check_unknown_provider_is_a_normal_error()
    check_background_delegation_is_a_job()
    check_background_without_jobs_refuses_loudly()
    check_a_crashed_child_is_a_normal_error()
    print("section 12: all checks passed")


def check_foreground_answers_with_the_childs_reply():
    """The child runs a whole turn in its own session; one answer crosses."""
    ctx, session, agent = build(
        [
            {
                "text": "",
                "tool_calls": [
                    sub_call("c1", "worker", "summarize the log", "foreground")
                ],
            },
            "the worker is done",
        ],
        child_factory=lambda: ScriptedModel(["the log has 12 rows"]),
    )

    agent.send("have the worker summarize the log")

    # The delegation happened inside one parent step: the tool call
    # waited on the run and answered with the child's reply.
    assert turns_of(session) == 1
    (result,) = results_of(session)
    assert result["is_error"] is False
    assert result["content"] == "the log has 12 rows"

    # The child's whole story lives in its own session log: the task
    # entered as its user/message, and its turn opened and closed there.
    child = ctx.get("sessions").sessions["sub-1"]
    assert users_of(child) == ["summarize the log"]
    assert replies_of(child) == ["the log has 12 rows"]
    assert turns_of(child) == 1
    # The parent log never gained a child row: one claimed prompt only.
    assert users_of(session) == ["have the worker summarize the log"]


def check_the_contract_is_the_run_not_the_runner():
    """Two providers behind one name-keyed tool; one is not an agent at all."""
    ctx, session, agent = build(
        [
            {
                "text": "",
                "tool_calls": [
                    sub_call("c1", "worker", "add the numbers", "foreground"),
                    sub_call("c2", "shouter", "add the numbers", "foreground"),
                ],
            },
            "both answered",
        ],
        child_factory=lambda: ScriptedModel(["the sum is 7"]),
    )

    def shouting_provider(request):
        # Not an agent, not even a thread: the least a provider can be
        # and still honor the whole parent-side contract.
        return SubagentRun(
            cancel=lambda: None,
            done=lambda: ("completed", None),
            read_output=lambda: request["task"].upper(),
        )

    ctx.get("subagents").register("shouter", shouting_provider)

    agent.send("ask both of them")

    first, second = results_of(session)
    assert first["is_error"] is False and first["content"] == "the sum is 7"
    assert second["is_error"] is False and second["content"] == "ADD THE NUMBERS"
    # One provider made a child session; the other made nothing at all,
    # and the tool could not tell the difference.
    assert set(ctx.get("sessions").sessions) == {"s1", "sub-1"}


def check_unknown_provider_is_a_normal_error():
    """A name nobody registered answers through the section 05 door."""
    ctx, session, agent = build(
        [
            {
                "text": "",
                "tool_calls": [sub_call("c1", "nobody", "do a thing", "foreground")],
            },
            "nobody was home",
        ]
    )

    agent.send("delegate to a name that does not exist")

    (result,) = results_of(session)
    assert result["is_error"] is True
    assert "no subagent provider registered under 'nobody'" in result["content"]
    # The refusal never tore the transcript: the turn closed normally.
    types = [e["type"] for e in session.log]
    assert types[-3:] == ["assistant/message", "step/end", "turn/end"]


def check_background_delegation_is_a_job():
    """The same run handed to jobs: the second producer, peer to shell."""
    child = GateModel(ScriptedModel(["overnight answer"]))
    ctx, session, agent = build(
        [
            {
                "text": "",
                "tool_calls": [
                    sub_call("c1", "worker", "crunch the numbers", "background")
                ],
            },
            "started it",
            {
                "text": "",
                "tool_calls": [
                    {"id": "c2", "name": "job_output", "args": {"job_id": "job-1"}},
                    {"id": "c3", "name": "job_list", "args": {}},
                ],
            },
            "all done",
        ],
        child_factory=lambda: child,
    )

    agent.send("crunch the numbers in the background")

    # The turn closed on a job id while the child was still thinking.
    assert turns_of(session) == 1
    (result,) = results_of(session)
    assert result["is_error"] is False
    assert result["content"] == "started job-1"

    child.open()  # the child answers while the parent sits idle...
    wait_until(lambda: settled(session, agent, 2))

    # ...so the notice arrived as a followup, and the section 11
    # controls served the new producer with no new code: the output is
    # the child's reply, and the listing names the kind "subagent".
    assert users_of(session)[-1] == (
        "job job-1 (worker: crunch the numbers) finished: completed"
    )
    output, listing = results_of(session)[-2:]
    assert output["content"] == "completed; output: overnight answer"
    assert listing["content"] == "job-1 [subagent] worker: crunch the numbers: completed"


def check_background_without_jobs_refuses_loudly():
    """No job registry mounted: background refuses; it never blocks the turn."""
    ctx, session, agent = build(
        [
            {
                "text": "",
                "tool_calls": [sub_call("c1", "worker", "crunch", "background")],
            },
            "cannot background here",
        ],
        child_factory=lambda: ScriptedModel(["never used"]),
        with_jobs=False,
    )

    agent.send("run it in the background")

    (result,) = results_of(session)
    assert result["is_error"] is True
    assert "no jobs registry mounted" in result["content"]
    # No half-made child either: the refusal came before any start.
    assert set(ctx.get("sessions").sessions) == {"s1"}


def check_a_crashed_child_is_a_normal_error():
    """A child that dies mid-turn settles as an error result, not a raise."""

    def exploding_model(messages, tools=(), system=""):
        raise RuntimeError("the child exploded")
        yield  # never reached: makes this callable a generator, seam-shaped

    ctx, session, agent = build(
        [
            {
                "text": "",
                "tool_calls": [sub_call("c1", "worker", "try anyway", "foreground")],
            },
            "that went badly",
        ],
        child_factory=lambda: exploding_model,
    )

    agent.send("delegate to the doomed one")

    (result,) = results_of(session)
    assert result["is_error"] is True
    assert "the child exploded" in result["content"]
    # The parent's turn survived the child's death and closed normally.
    types = [e["type"] for e in session.log]
    assert types[-3:] == ["assistant/message", "step/end", "turn/end"]


if __name__ == "__main__":
    main()
