"""The agent loop: a turn/step machine whose only durable state is the session log.

An Agent owns a Session and the Model seam, nothing else worth saving.
A turn is a while-step loop; each step re-derives model history from
the log, streams one model call through the seam, and appends every
chunk and the final message back. Turn and step boundaries are
themselves log-only events, so the log alone tells the whole story and
a new Agent over the same log continues exactly where the old one
stopped.
"""


class Agent:
    """Drives one session through the Model seam, one turn at a time."""

    def __init__(self, session, model):
        self.session = session
        self.model = model
        self.status = "idle"  # "idle" | "running", never durable

    def send(self, text):
        """One turn: the user's message in, steps until one ends with a reason."""
        if self.status == "running":
            raise RuntimeError("agent is mid-turn; the log allows one story at a time")
        self.status = "running"
        try:
            self.session.append("user/message", {"content": text})
            self.session.append("turn/start", {})
            while self._step() is None:
                pass
            self.session.append("turn/end", {})
        finally:
            self.status = "idle"

    def _step(self):
        """One step: re-derive history, one model call, append it all back."""
        self.session.append("step/start", {})
        messages = self.session.derive_messages()  # re-derived, never cached
        self.session.append("request/header", {"messages": len(messages)})
        for kind, value in self.model(messages):
            if kind == "chunk":
                self.session.append("assistant/chunk", {"text": value})
            else:
                self.session.append("assistant/message", {"content": value.content})
        # No tools yet, so every step completes. Section 05 adds the None
        # arm: tool calls ran, go around again.
        reason = "completed"
        self.session.append("step/end", {"reason": reason})
        return reason


class AgentRegistry:
    """The agents service: creates and holds the live Agents."""

    def __init__(self):
        self.agents = {}

    def create(self, agent_id, session, model):
        if agent_id in self.agents:
            raise ValueError(f"agent '{agent_id}' already exists")
        agent = Agent(session, model)
        self.agents[agent_id] = agent
        return agent


def agent_loop_plugin(ctx):
    ctx.provide("agents", AgentRegistry())
