"""The agent loop: a turn/step machine whose only durable state is the session log.

An Agent owns a Session, the Model seam, and its scoped view of the
tool registry, nothing else worth saving. A turn is a while-step loop;
each step re-derives model history from the log, streams one model
call through the seam, appends everything back, and runs the reply's
tool calls through the pipeline. A step whose reply carried calls ends
with reason None: go around again. Turn and step boundaries are
themselves log-only events, so the log alone tells the whole story and
a new Agent over the same log continues exactly where the old one
stopped.

A reply's calls are driven by the 4-stage scheduler, never executed
here one by one. The Agent owns one cancellation token per turn:
cancel() asks the scheduler to stop at the next barrier, and a step
cut short that way ends with reason "aborted" and closes the turn.
"""

import threading

from scheduler import execute_tool_calls


class Agent:
    """Drives one session through the Model seam, one turn at a time."""

    def __init__(self, session, model, tools):
        self.session = session
        self.model = model
        self.tools = tools  # this agent's scoped view of the tool registry
        self.status = "idle"  # "idle" | "running", never durable
        self._abort = threading.Event()  # this turn's cancellation token

    def cancel(self):
        """Ask the turn to stop. Batches already dispatched still finish."""
        self._abort.set()

    def send(self, text):
        """One turn: the user's message in, steps until one ends with a reason."""
        if self.status == "running":
            raise RuntimeError("agent is mid-turn; the log allows one story at a time")
        self.status = "running"
        self._abort.clear()  # cancel() marks one turn, not the agent
        try:
            self.session.append("user/message", {"content": text})
            self.session.append("turn/start", {})
            while self._step() is None:
                pass
            self.session.append("turn/end", {})
        finally:
            self.status = "idle"

    def _step(self):
        """One step: re-derive history, one model call, run its tool calls."""
        self.session.append("step/start", {})
        messages = self.session.derive_messages()  # re-derived, never cached
        schemas = self.tools.schemas()
        self.session.append(
            "request/header",
            {"messages": len(messages), "tools": [s["name"] for s in schemas]},
        )
        final = None
        for kind, value in self.model(messages, schemas):
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

    def create(self, agent_id, session, model, tools):
        if agent_id in self.agents:
            raise ValueError(f"agent '{agent_id}' already exists")
        agent = Agent(session, model, tools)
        self.agents[agent_id] = agent
        return agent


def agent_loop_plugin(ctx):
    ctx.provide("agents", AgentRegistry())
