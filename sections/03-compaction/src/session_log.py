"""The session log: an append-only event log, an ordered surface, derived messages.

A Session never stores model history. It stores frozen events (an
event's seq is its log index) and maintains a surface: the ordered
seqs of the message-producing events. derive_messages() projects the
surface into Message objects on demand; everything else that happened
(chunks, markers) stays in the log for anyone who needs the full story.

Every append carries a surface op, recorded on the event: "append"
joins the surface, None stays log-only, and {"op": "replace", start,
end} shadows a run of surface entries with this event. The replace op
is how compaction shrinks what the model sees while the log keeps
every row.
"""

import json
from types import MappingProxyType

from message import Message

# The only event types the model ever sees, and the role each derives to.
# Everything else (assistant/chunk, turn markers, headers) is log-only.
SURFACE_TYPES = {
    "user/message": "user",
    "assistant/message": "assistant",
    "tool/result": "tool",
}


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


class Session:
    """An append-only log of frozen events plus the surface projected over it."""

    def __init__(self, session_id, on_event=None):
        self.id = session_id
        self.log = []  # frozen events; an event's seq is its index, forever
        self.surface = []  # ordered seqs of the surface events
        self._on_event = on_event

    def append(self, event_type, payload, surface_op=None):
        # Validate-and-copy at the boundary: the payload must be plain JSON
        # data, and the log keeps its own copy so no caller can edit history.
        payload = json.loads(json.dumps(payload))
        if surface_op is None and event_type in SURFACE_TYPES:
            surface_op = "append"
        seq = len(self.log)
        # Validate the surface transition before committing: a bad op must
        # leave both the log and the surface untouched.
        surface = self._surface_after(event_type, seq, surface_op)
        event = _freeze(
            {"seq": seq, "type": event_type, "payload": payload, "surface_op": surface_op}
        )
        self.log.append(event)
        self.surface = surface
        if self._on_event is not None:
            self._on_event(self, event)
        return event

    def _surface_after(self, event_type, seq, surface_op):
        """The surface as it will be once this append commits. Raises if invalid."""
        if surface_op is None:
            return self.surface
        if event_type not in SURFACE_TYPES:
            raise ValueError(f"'{event_type}' derives no message; it cannot join the surface")
        if surface_op == "append":
            return self.surface + [seq]
        if not isinstance(surface_op, dict) or surface_op.get("op") != "replace":
            raise ValueError(f"unknown surface op: {surface_op!r}")
        # {"op": "replace", "start": s, "end": e}: this event shadows the
        # surface entries whose seq falls in [start, end), half-open.
        start, end = surface_op["start"], surface_op["end"]
        covered = [i for i, s in enumerate(self.surface) if start <= s < end]
        if not covered:
            raise ValueError(f"replace [{start}, {end}) covers no surface entry")
        if covered != list(range(covered[0], covered[-1] + 1)):
            raise ValueError(f"replace [{start}, {end}) covers a non-contiguous surface run")
        return self.surface[: covered[0]] + [seq] + self.surface[covered[-1] + 1 :]

    def derive_messages(self):
        """Project the surface into model history. Never stored, always derived."""
        return [
            Message(
                role=SURFACE_TYPES[event["type"]],
                content=event["payload"]["content"],
            )
            for event in (self.log[seq] for seq in self.surface)
        ]


class SessionStore:
    """The sessions service: creates Sessions and feeds each append to the bus."""

    def __init__(self, ctx):
        self._ctx = ctx
        self.sessions = {}

    def create(self, session_id):
        if session_id in self.sessions:
            raise ValueError(f"session '{session_id}' already exists")
        session = Session(session_id, on_event=self._feed)
        self.sessions[session_id] = session
        return session

    def _feed(self, session, event):
        self._ctx.emit("session/event", session, event)


def session_log_plugin(ctx):
    ctx.provide("sessions", SessionStore(ctx))
