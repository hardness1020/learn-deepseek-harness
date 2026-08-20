"""The agent loop: a turn/step machine whose only durable state is the session log.

An Agent owns a Session, the Model seam, its scoped view of the tool
registry, the system-prompt registry, and an inbox of pending input,
nothing else worth saving. A turn is a while-step loop; each step
claims the inbox, assembles the request (system text, tool list, and
a runtime-context snapshot re-emitted as a user message only when it
changed), re-derives model history from the log, streams one model
call through the seam, appends everything back, and runs the reply's
tool calls through the pipeline.
A step whose reply carried calls ends with reason None: go around
again. Turn and step boundaries are themselves log-only events, so the
log alone tells the whole story.

Input never enters a turn directly. send() routes text to one of two
inbox targets; followup(), steer(), and inject() are its three preset
routings. The loop claims pending input only at step boundaries: all
next-step input every step, plus at most one queued next-turn prompt
when the step opens a turn. A drain loop keeps running turns until no
queued prompt remains, so a followup sent mid-turn gets a turn of its
own instead of raising.

A reply's calls are driven by the 4-stage scheduler, never executed
here one by one. The Agent owns one cancellation token per turn:
cancel() drops all pending input and asks the scheduler to stop at the
next barrier; a step cut short that way ends with reason "aborted" and
the turn closes.
"""

import threading

from inbox import Inbox
from scheduler import execute_tool_calls
from system_prompt import latest_snapshot


class Agent:
    """Drives one session through the Model seam, one turn at a time."""

    def __init__(self, session, model, tools, prompt):
        self.session = session
        self.model = model
        self.tools = tools  # this agent's scoped view of the tool registry
        self.prompt = prompt  # the system-prompt registry, assembled per step
        self.inbox = Inbox()  # pending input, routed but not yet claimed
        self.status = "idle"  # "idle" | "running", never durable
        self._abort = threading.Event()  # this turn's cancellation token

    def cancel(self):
        """Stop the turn and drop pending input. Started work still finishes."""
        self.inbox.clear()  # an aborted turn must not resurrect from stale input
        self._abort.set()

    # -- routing: one door, three presets ----------------------------------
    def send(self, text, target="next-turn", wakeup=True):
        """Route text to an inbox target; wake the drain loop when idle."""
        self.inbox.insert(target, {"content": text})
        # Only the driving thread ever observes "idle": a tool body or bus
        # listener sends mid-turn, so it queues and a later boundary claims.
        if wakeup and self.status == "idle":
            self._drain()

    def followup(self, text):
        """Queue a prompt that gets a turn of its own."""
        self.send(text, "next-turn", True)

    def steer(self, text):
        """Steer the nearest step: input for the work already underway."""
        self.send(text, "next-step", True)

    def inject(self, text):
        """Park model-facing context for the next step, without waking."""
        self.send(text, "next-step", False)

    # -- the machine --------------------------------------------------------
    def _drain(self):
        """Run turns until no queued prompt remains, then go idle."""
        self.status = "running"
        try:
            while True:
                self._abort.clear()  # cancel() marks one turn, not the agent
                self._turn()
                # Checked after every mid-turn sender has finished: worker
                # bodies join at the scheduler's barrier and bus listeners
                # run on this thread, so no insert can land between this
                # check and going idle.
                if not self.inbox.has("next-turn"):
                    break
        finally:
            self.status = "idle"

    def _turn(self):
        """One turn: steps until one ends with a reason and no steering waits."""
        self.session.append("turn/start", {})
        target = "next-turn"  # only a turn's first boundary consumes a queued prompt
        while True:
            reason = self._step(target)
            target = "next-step"
            if reason == "aborted":
                break  # cancelled: pending input is already gone
            if reason is not None and not self.inbox.has("next-step"):
                break  # fresh steering spends another step in this turn
        self.session.append("turn/end", {})

    def _step(self, target):
        """One step: claim the inbox, assemble the request, one model call, tools."""
        self.session.append("step/start", {})
        # The claim: pending input becomes user/message rows here, at the
        # boundary, exactly where the request is re-derived from the log.
        for message in self.inbox.claim(target):
            self.session.append("user/message", message)
        # Assembled fresh every step, like the history it travels with.
        assembly = self.prompt.assemble({"tools": self.tools})
        snapshot = assembly["runtime_context"]
        if snapshot and snapshot != latest_snapshot(self.session):
            # Dynamic state enters as a message, never as system text: the
            # system stays byte-identical across steps, and the log keeps a
            # durable record of exactly what the model saw.
            self.session.append("user/message", {"content": snapshot, "kind": "runtime-context"})
        messages = self.session.derive_messages()  # re-derived, never cached
        schemas = assembly["tools"]
        self.session.append(
            "request/header",
            {
                "messages": len(messages),
                "tools": [s["name"] for s in schemas],
                "system": assembly["system"],
            },
        )
        final = None
        for kind, value in self.model(messages, schemas, assembly["system"]):
            if kind == "chunk":
                self.session.append("assistant/chunk", {"text": value})
            else:
                final = value
                payload = {"content": value.content}
                if value.tool_calls:
                    payload["tool_calls"] = list(value.tool_calls)
                self.session.append("assistant/message", payload)
        if not final.tool_calls:
            self.session.append("step/end", {"reason": "completed"})
            return "completed"
        execute_tool_calls(self.session, self.tools, list(final.tool_calls), self._abort)
        if self._abort.is_set():
            self.session.append("step/end", {"reason": "aborted"})
            return "aborted"  # cancelled: the turn closes instead of going around
        self.session.append("step/end", {"reason": None})
        return None  # tool calls ran: go around again


class AgentRegistry:
    """The agents service: creates and holds the live Agents."""

    def __init__(self):
        self.agents = {}

    def create(self, agent_id, session, model, tools, prompt):
        if agent_id in self.agents:
            raise ValueError(f"agent '{agent_id}' already exists")
        agent = Agent(session, model, tools, prompt)
        self.agents[agent_id] = agent
        return agent


def agent_loop_plugin(ctx):
    ctx.provide("agents", AgentRegistry())
