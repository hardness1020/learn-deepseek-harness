"""Offline check for section 02: model history is derived from the log, never stored."""

from kernel import Context
from message import Message
from session_log import session_log_plugin
from standin import ScriptedModel


def main():
    ctx = Context()
    ctx.plugin(session_log_plugin)
    store = ctx.get("sessions")

    feed = []
    ctx.on("session/event", lambda session, event: feed.append(event))

    # One streamed turn through the Model seam, every event appended.
    session = store.create("s1")
    session.append("user/message", {"content": "hi"})
    for kind, value in ScriptedModel(["Hello, reader."])(session.derive_messages()):
        if kind == "chunk":
            session.append("assistant/chunk", {"text": value})
        else:
            session.append("assistant/message", {"content": value.content})
    session.append("tool/result", {"content": "42"})

    # An event's seq is its log index, always.
    assert [event["seq"] for event in session.log] == list(range(len(session.log)))
    assert len(session.log) >= 6, "chunk events must be real, not skipped"

    # The surface picks out only the message-producing events, in order.
    surfaced = [session.log[seq]["type"] for seq in session.surface]
    assert surfaced == ["user/message", "assistant/message", "tool/result"], surfaced

    # Derived history: chunks are log-only, invisible to the model.
    assert session.derive_messages() == [
        Message("user", "hi"),
        Message("assistant", "Hello, reader."),
        Message("tool", "42"),
    ]

    # Derived, not stored: the next append shows up in the next projection.
    session.append("user/message", {"content": "more"})
    assert session.derive_messages()[-1] == Message("user", "more")

    # History cannot be edited: log events are frozen ...
    try:
        session.log[0]["payload"]["content"] = "rewritten"
        assert False, "expected TypeError on frozen event"
    except TypeError:
        pass

    # ... and the log keeps its own copy of every payload.
    payload = {"content": "original"}
    event = session.append("user/message", payload)
    payload["content"] = "mutated later"
    assert event["payload"]["content"] == "original"

    # Non-JSON payloads are rejected at the append boundary.
    try:
        session.append("user/message", {"content": object()})
        assert False, "expected TypeError on non-JSON payload"
    except TypeError:
        pass

    # Every committed append reached the bus feed, in log order.
    assert [event["seq"] for event in feed] == [event["seq"] for event in session.log]

    # One session id, one log.
    try:
        store.create("s1")
        assert False, "expected ValueError on duplicate session id"
    except ValueError:
        pass

    print("section 02: all checks passed")


if __name__ == "__main__":
    main()
