"""Subagent: the named-provider delegation registry.

Delegation hands a task to a child with a session, a tool scope, and
a context window of its own, and takes back one answer. The obvious
shape is a subclass: a Subagent extending the section 04 Agent. But
what answers a delegation is not always an agent in this process;
real dsh ships providers that fork the process, drive another product
over a wire protocol, or wrap a different harness entirely, and a
base class would force every one of them to fake an Agent's insides.

So the registry holds names, not classes. A provider is a callable
contract: it takes the resolved start request and hands back a run,
and the registry never looks behind it. The run is the entire
parent-side contract, and it is deliberately section 11's protocol
triple (cancel, done, read_output): a foreground delegation waits on
done inside its tool call and answers with the child's reply, and a
background delegation hands the same triple to the job registry
unchanged. That handoff makes the subagent the second job producer,
peer to shell, with the fence, the settlement, and the controls of
section 11 serving it with no new code.

The in-process provider is one implementation, not the contract: it
establishes a child Agent through the same services the parent came
from, drives one send() on the run's own thread, and reads the
answer off the child's session log. The child's whole story stays in
its own log; the only thing that crosses back is the run.
"""

import threading
from dataclasses import dataclass

from tools import ToolDefinition

MODES = ("foreground", "background")


@dataclass(frozen=True)
class SubagentRun:
    """The parent-side contract: everything a parent may do to a child.

    Deliberately the section 11 protocol triple, so a run can be
    waited on in place or handed to the job registry unchanged.
    """

    cancel: callable  # ask the child to stop; cooperative, best effort
    done: callable  # block until it ends: ("completed", None) | ("failed", detail)
    read_output: callable  # the child's answer so far, as text


class SubagentRuntime:
    """The subagents service: a name-keyed registry of providers."""

    def __init__(self):
        self._providers = {}
        self._count = 0

    def register(self, name, provider):
        """File a provider under a name; hands back the undo."""
        if name in self._providers:
            raise ValueError(f"subagent provider '{name}' already registered")
        self._providers[name] = provider
        return lambda: self._providers.pop(name)

    def start(self, name, task):
        """Resolve the name, hand the provider a resolved request, get a run."""
        provider = self._providers.get(name)
        if provider is None:
            raise LookupError(f"no subagent provider registered under '{name}'")
        self._count += 1
        return provider({"id": f"sub-{self._count}", "task": task})


def subagent_plugin(ctx):
    ctx.provide("subagents", SubagentRuntime())


# -- one provider, not the contract ----------------------------------------


def in_process_provider(ctx, model_factory):
    """A provider that establishes a child Agent in this process.

    The child is built from the same services the parent came from:
    its own session, its own tool scope, the shared system-prompt
    registry, and a model of its own from model_factory(). One send()
    drives the whole task on the run's thread, so foreground and
    background delegations share one shape; the answer is read off
    the child's log, never carried by the thread.
    """

    def start(request):
        session = ctx.get("sessions").create(request["id"])
        child = ctx.get("agents").create(
            request["id"],
            session,
            model_factory(),
            ctx.get("tools").scope(request["id"]),
            ctx.get("system_prompt"),
        )
        state = {"error": None}

        def work():
            try:
                child.send(request["task"])
            except Exception as exc:
                state["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=work, name=f"{request['id']}-run", daemon=True)
        thread.start()

        def done():
            thread.join()
            if state["error"]:
                return ("failed", state["error"])
            return ("completed", None)

        def read_output():
            replies = [
                event["payload"]["content"]
                for event in session.log
                if event["type"] == "assistant/message" and event["payload"]["content"]
            ]
            return replies[-1] if replies else ""

        return SubagentRun(child.cancel, done, read_output)

    return start


# -- the consumer: one delegation tool, two modes --------------------------


def subagent_tools(owner):
    """Plugin factory: this owner's delegation tool, mounted into its scope.

    Foreground waits on the run and answers with the child's reply.
    Background acquires the job registry by optional lookup, the way
    real dsh does, and hands it the run's triple unchanged; the owner
    baked in here is the same fence identity section 11's controls
    answer to, so job_output, job_kill, and job_list serve subagent
    jobs as they stand.
    """

    def apply(ctx):
        def seam(key):
            implementation = ctx.get(key)
            if implementation is None:
                raise RuntimeError(f"no provider mounted under '{key}'")
            return implementation

        def delegate(args):
            name, task, mode = args["provider"], args["task"], args["mode"]
            if mode not in MODES:
                raise ValueError(f"unknown mode '{mode}'")
            subagents = seam("subagents")
            if mode == "foreground":
                started = subagents.start(name, task)
                status, detail = started.done()
                if status == "failed":
                    raise RuntimeError(f"the subagent failed: {detail}")
                return started.read_output() or "(no reply)"
            jobs = ctx.get("jobs")  # optional lookup: no registry, no background
            if jobs is None:
                raise RuntimeError("no jobs registry mounted; use mode 'foreground'")

            def run():
                started = subagents.start(name, task)
                return (started.cancel, started.done, started.read_output)

            job_id = jobs.start("subagent", f"{name}: {task}", owner, run)
            return f"started {job_id}"

        tools = ctx.get("tools")
        ctx.effect(
            tools.register(
                ToolDefinition(
                    name="subagent",
                    description=(
                        "Delegate one task to a named subagent provider;"
                        " foreground answers with the child's reply,"
                        " background answers with a job id."
                    ),
                    params={
                        "provider": "the provider name to delegate to",
                        "task": "the task, as one prompt for the child",
                        "mode": (
                            "'foreground' to wait for the answer,"
                            " 'background' to run it as a wakeup job"
                        ),
                    },
                    execute=delegate,
                ),
                scope=owner.id,
            ),
            "subagent tool",
        )

    return apply
