"""The session log: an append-only event log, an ordered surface, derived messages.

A Session never stores model history. It stores frozen events (an
event's seq is its log index) and maintains a surface: the ordered
seqs of the message-producing events. derive_messages() projects the
surface into Message objects on demand; everything else that happened
(chunks, markers) stays in the log for anyone who needs the full story.
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

    def append(self, event_type, payload):
        # Validate-and-copy at the boundary: the payload must be plain JSON
        # data, and the log keeps its own copy so no caller can edit history.
        payload = json.loads(json.dumps(payload))
        seq = len(self.log)
        event = _freeze({"seq": seq, "type": event_type, "payload": payload})
        self.log.append(event)
        if event_type in SURFACE_TYPES:
            self.surface.append(seq)
        if self._on_event is not None:
            self._on_event(self, event)
        return event

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
