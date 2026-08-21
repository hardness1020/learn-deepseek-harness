"""Offline check for section 11: the owner-fenced background-work protocol."""

import threading
import time

from agent_loop import agent_loop_plugin
from capabilities import ShellExecutor, provider
from jobs import JobOwner, job_tools, jobs_plugin
from kernel import Context
from session_log import session_log_plugin
from standin import ScriptedModel
from system_prompt import system_prompt_plugin
from tools import ToolDefinition, tools_plugin


class GateShellExecutor(ShellExecutor):
    """Test provider: every run blocks on a gate the check opens by hand,
    so "the job is still running" and "the job finished" are decided
    here, never by timing."""

    def __init__(self, open_gate=False):
        self.gate = threading.Event()
        self.finished = threading.Event()
        if open_gate:
            self.gate.set()

    def open(self):
        self.gate.set()

    def run(self, argv):
        assert self.gate.wait(timeout=5), "the check never opened the gate"
        try:
            if argv and argv[0] == "boom":
                raise RuntimeError("the command failed")
            return " ".join(argv)
        finally:
            self.finished.set()


def build(responses, open_gate=False):
    ctx = Context()
    ctx.plugin(session_log_plugin)
    ctx.plugin(tools_plugin)
    ctx.plugin(system_prompt_plugin)
    ctx.plugin(jobs_plugin)
    ctx.plugin(agent_loop_plugin)
    gate = GateShellExecutor(open_gate)
    ctx.plugin(provider("shell", gate))
    session = ctx.get("sessions").create("s1")
    agent = ctx.get("agents").create(
        "a1", session, ScriptedModel(responses), ctx.get("tools").scope("a1"),
        ctx.get("system_prompt"),
    )
    ctx.plugin(job_tools(JobOwner("a1", agent)))
    return ctx, gate, session, agent


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


def start_call(call_id, command, delivery):
    return {
        "id": call_id,
        "name": "shell_job",
        "args": {"command": command, "delivery": delivery},
    }


def main():
    check_the_id_outlives_the_turn()
    check_busy_settlement_parks_at_the_boundary()
    check_the_owner_fence()
    check_turn_cancel_never_reaches_the_job()
    check_pre_aborted_call_fails_not_noops()
    check_kill_wins_the_race()
    check_completion_wins_the_race()
    check_failure_settles_failed()
    print("section 11: all checks passed")


def check_the_id_outlives_the_turn():
    """The producing turn closes on an id; the notice opens its own turn."""
    ctx, gate, session, agent = build(
        [
            {"text": "", "tool_calls": [start_call("c1", "echo hi", "wakeup")]},
            "started it",
            {"text": "", "tool_calls": [{"id": "c2", "name": "job_output", "args": {"job_id": "job-1"}}]},
            "all done",
        ]
    )

    agent.send("run echo hi in the background")

    # The gate is still shut, so the work is still running; yet the turn
    # is already over, closed on a normal result carrying only the id.
    assert turns_of(session) == 1
    (result,) = results_of(session)
    assert result["is_error"] is False
    assert result["content"] == "started job-1"

    gate.open()  # the work finishes while the owner is idle...
    wait_until(lambda: settled(session, agent, 2))

    # ...so the notice arrived as a followup: a turn of its own, whose
    # user/message is the notice, in which the model polled the output.
    assert users_of(session)[-1] == "job job-1 (echo hi) finished: completed"
    output = results_of(session)[-1]
    assert output["is_error"] is False
    assert output["content"] == "completed; output: echo hi"


def check_busy_settlement_parks_at_the_boundary():
    """A job finishing mid-turn injects; the notice waits for the boundary."""
    ctx, gate, session, agent = build(
        [
            {
                "text": "",
                "tool_calls": [
                    start_call("c1", "echo hi", "wakeup"),
                    {"id": "c2", "name": "waiter", "args": {}},
                ],
            },
            "saw the notice",
        ],
        open_gate=True,
    )

    def wait_for_notice(args):
        # Holds its step open until the settling thread has injected, so
        # "the job finished while the turn was in flight" is guaranteed.
        wait_until(lambda: agent.inbox.has("next-step"))
        return "held the step open"

    ctx.get("tools").register(
        ToolDefinition(
            name="waiter", description="", params={}, execute=wait_for_notice
        )
    )

    agent.send("run it and wait")

    # One turn, not two: the busy owner was never followed up, and the
    # parked notice entered as a user/message at the next step boundary,
    # after both results and before the reply that answers it.
    assert turns_of(session) == 1
    types = [e["type"] for e in session.log]
    notice_index = users_of(session).index("job job-1 (echo hi) finished: completed")
    user_seqs = [e["seq"] for e in session.log if e["type"] == "user/message"]
    result_seqs = [e["seq"] for e in session.log if e["type"] == "tool/result"]
    assert user_seqs[notice_index] > max(result_seqs)
    assert types[-3:] == ["assistant/message", "step/end", "turn/end"]


def check_the_owner_fence():
    """Another session's probes are denied; a stranger learns nothing."""
    ctx, gate, session, agent = build(
        [
            {"text": "", "tool_calls": [start_call("c1", "echo hi", "quiet")]},
            "ok",
            {"text": "", "tool_calls": [{"id": "c2", "name": "job_list", "args": {}}]},
            "mine is still there",
        ]
    )
    session_b = ctx.get("sessions").create("s2")
    agent_b = ctx.get("agents").create(
        "b1",
        session_b,
        ScriptedModel(
            [
                {
                    "text": "",
                    "tool_calls": [
                        {"id": "d1", "name": "job_output", "args": {"job_id": "job-1"}},
                        {"id": "d2", "name": "job_kill", "args": {"job_id": "job-1"}},
                        {"id": "d3", "name": "job_output", "args": {"job_id": "job-9"}},
                        {"id": "d4", "name": "job_list", "args": {}},
                    ],
                },
                "denied everywhere",
            ]
        ),
        ctx.get("tools").scope("b1"),
        ctx.get("system_prompt"),
    )
    ctx.plugin(job_tools(JobOwner("b1", agent_b)))

    agent.send("start something")  # the job stays running behind the shut gate
    agent_b.send("read and kill a1's job")

    probe, kill, bogus, listing = results_of(session_b)
    # Foreign id and bogus id: the same denial but for the id itself, so
    # a stranger cannot even tell which ids exist; and the kill never
    # reached the job.
    assert probe["is_error"] and kill["is_error"] and bogus["is_error"]
    assert probe["content"].replace("job-1", "job-9") == bogus["content"]
    assert "job-1" in probe["content"] and "owned by this session" in probe["content"]
    assert listing["is_error"] is False
    assert listing["content"] == "(no jobs)"

    # The owner still sees it, alive and untouched by the foreign kill.
    agent.send("list my jobs")
    assert results_of(session)[-1]["content"] == "job-1 [shell] echo hi: running"


def check_turn_cancel_never_reaches_the_job():
    """The producing call's signal stops mattering once the id is out."""
    ctx, gate, session, agent = build(
        [
            {
                "text": "",
                "tool_calls": [
                    start_call("c1", "echo hi", "wakeup"),
                    {"id": "c2", "name": "canceller", "args": {}},
                ],
            },
            "the job survived the cancel",
        ]
    )
    def cancel_the_turn(args):
        agent.cancel()
        return "cancelled"

    ctx.get("tools").register(
        ToolDefinition(
            name="canceller", description="", params={}, execute=cancel_the_turn
        )
    )

    agent.send("start it, then cancel the turn")

    # The turn died aborted, with the job id already published.
    reasons = [e["payload"]["reason"] for e in session.log if e["type"] == "step/end"]
    assert reasons == ["aborted"]
    assert turns_of(session) == 1

    gate.open()  # the cancelled turn is history; the job finishes anyway
    wait_until(lambda: settled(session, agent, 2))

    assert users_of(session)[-1] == "job job-1 (echo hi) finished: completed"


def check_pre_aborted_call_fails_not_noops():
    """A background call the abort catches first fails; no job ever starts."""
    ctx, gate, session, agent = build(
        [
            {
                "text": "",
                "tool_calls": [
                    {"id": "c1", "name": "canceller", "args": {}},
                    start_call("c2", "echo hi", "wakeup"),
                ],
            },
            {"text": "", "tool_calls": [{"id": "c3", "name": "job_list", "args": {}}]},
            "nothing is running",
        ]
    )

    def cancel_the_turn(args):
        agent.cancel()
        return "cancelled"

    ctx.get("tools").register(
        ToolDefinition(
            name="canceller", description="", params={}, execute=cancel_the_turn
        )
    )

    agent.send("cancel before the background call runs")

    # The shell_job call was never dispatched, but it still answered: a
    # synthetic error result, never a silent no-op with a half-made job.
    cancelled, skipped = results_of(session)
    assert skipped["name"] == "shell_job"
    assert skipped["is_error"] is True
    assert skipped["content"] == "aborted before dispatch"

    agent.send("what is running?")
    assert results_of(session)[-1]["content"] == "(no jobs)"


def check_kill_wins_the_race():
    """A killed job stays killed, even when the work later finishes."""
    ctx, gate, session, agent = build(
        [
            {"text": "", "tool_calls": [start_call("c1", "echo hi", "quiet")]},
            "started",
            {"text": "", "tool_calls": [{"id": "c2", "name": "job_kill", "args": {"job_id": "job-1"}}]},
            "killed it",
            {"text": "", "tool_calls": [{"id": "c3", "name": "job_output", "args": {"job_id": "job-1"}}]},
            "still killed",
        ]
    )

    agent.send("start it")
    agent.send("kill it")
    assert results_of(session)[-1]["content"] == "job job-1: killed"

    gate.open()  # the work finishes after the kill and loses the race
    wait_until(lambda: ctx.get("jobs").output("job-1", "a1")["output"] == "echo hi")
    time.sleep(0.05)  # grace: let the watcher's losing settle attempt run
    agent.send("how did it end?")

    # First-wins: the late completion changed nothing, and the output the
    # work produced is still readable; killed is a settlement, not amnesia.
    assert results_of(session)[-1]["content"] == "killed; output: echo hi"
    assert turns_of(session) == 3  # quiet: no notice turn ever arrived


def check_completion_wins_the_race():
    """A kill after completion reports what actually happened."""
    ctx, gate, session, agent = build(
        [
            {"text": "", "tool_calls": [start_call("c1", "echo hi", "wakeup")]},
            "started",
            "noted",  # the notice turn: no calls, just an acknowledgement
            {"text": "", "tool_calls": [{"id": "c2", "name": "job_kill", "args": {"job_id": "job-1"}}]},
            "too late to kill",
        ]
    )

    agent.send("start it")
    gate.open()  # the work finishes while the owner is idle
    wait_until(lambda: settled(session, agent, 2))  # the notice turn ran

    agent.send("kill it")
    # The kill lost: settlement had already happened, and the tool says so.
    assert results_of(session)[-1]["content"] == "job job-1: completed"


def check_failure_settles_failed():
    """A crashing body settles "failed"; the notice and the output say why."""
    ctx, gate, session, agent = build(
        [
            {"text": "", "tool_calls": [start_call("c1", "boom now", "wakeup")]},
            "started",
            {"text": "", "tool_calls": [{"id": "c2", "name": "job_output", "args": {"job_id": "job-1"}}]},
            "it failed",
        ]
    )

    agent.send("run the exploding one")
    gate.open()  # now the work runs, and explodes
    wait_until(lambda: settled(session, agent, 2))

    assert users_of(session)[-1] == "job job-1 (boom now) finished: failed"
    # The producing call still answered normally; only the job failed.
    first = results_of(session)[0]
    assert first["is_error"] is False and first["content"] == "started job-1"
    assert results_of(session)[-1]["content"] == "failed; output: (none)"


if __name__ == "__main__":
    main()
