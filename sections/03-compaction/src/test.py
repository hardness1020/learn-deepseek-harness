"""Offline check for section 03: compaction replaces surface entries, never log rows."""

import json

from kernel import Context
from message import Message
from session_log import Session, session_log_plugin
from standin import ScriptedModel


def thaw(value):
    """Frozen event parts back to plain JSON data, for replaying appends."""
    return json.loads(json.dumps(value, default=dict))


def stream_turn(session, model):
    """One turn through the Model seam, every chunk and the message appended."""
    for kind, value in model(session.derive_messages()):
        if kind == "chunk":
            session.append("assistant/chunk", {"text": value})
        else:
            session.append("assistant/message", {"content": value.content})


def build_session(store):
    """Two streamed exchanges plus a tool result, all through the log."""
    model = ScriptedModel(["Hello.", "Now this."])
    session = store.create("s1")
    session.append("user/message", {"content": "hi"})         # seq 0
    stream_turn(session, model)                               # seqs 1-3 chunks, 4 message
    session.append("tool/result", {"content": "42"})          # seq 5
    session.append("user/message", {"content": "and now?"})   # seq 6
    stream_turn(session, model)                               # seqs 7-9 chunks, 10 message
    return session


def expect_rejected(session, event_type, payload, surface_op):
    """A bad surface op must raise ValueError and leave the session untouched."""
    rows, surface = len(session.log), list(session.surface)
    try:
        session.append(event_type, payload, surface_op=surface_op)
        assert False, f"expected ValueError for {surface_op!r} on {event_type}"
    except ValueError:
        pass
    assert len(session.log) == rows, "a rejected append must not commit a log row"
    assert session.surface == surface, "a rejected append must not touch the surface"


def main():
    ctx = Context()
    ctx.plugin(session_log_plugin)
    store = ctx.get("sessions")

    session = build_session(store)
    assert session.surface == [0, 4, 5, 6, 10]

    # Compaction is one append: a summary message whose surface op replaces
    # the surface entries with seq in [0, 6). The log is never edited.
    rows_before = len(session.log)
    summary = session.append(
        "user/message",
        {"content": "Summary: greeted, tool said 42."},
        surface_op={"op": "replace", "start": 0, "end": 6},
    )

    # The model's view shrank to summary + tail.
    assert session.derive_messages() == [
        Message("user", "Summary: greeted, tool said 42."),
        Message("user", "and now?"),
        Message("assistant", "Now this."),
    ]
    assert session.surface == [summary["seq"], 6, 10]

    # The log removed nothing: every old row still there, frozen, seq intact.
    assert len(session.log) == rows_before + 1
    assert [event["seq"] for event in session.log] == list(range(len(session.log)))
    assert session.log[0]["payload"]["content"] == "hi"
    assert session.log[1]["type"] == "assistant/chunk"

    # The op itself is on the record: the log alone explains the surface.
    assert session.log[summary["seq"]]["surface_op"]["op"] == "replace"
    assert session.log[0]["surface_op"] == "append"
    assert session.log[1]["surface_op"] is None

    # Proof: replaying every logged append rebuilds the exact same surface.
    replayed = Session("replayed")
    for event in session.log:
        replayed.append(event["type"], thaw(event["payload"]), thaw(event["surface_op"]))
    assert replayed.surface == session.surface
    assert replayed.derive_messages() == session.derive_messages()

    # Invalid ops are rejected before anything commits.
    expect_rejected(  # covers no surface entry: seqs 0..5 were already replaced
        session, "user/message", {"content": "x"}, {"op": "replace", "start": 1, "end": 5}
    )
    expect_rejected(  # a log-only event type cannot join the surface
        session, "assistant/chunk", {"text": "x"}, "append"
    )
    expect_rejected(  # end is exclusive, so [6, 6) covers nothing
        session, "user/message", {"content": "x"}, {"op": "replace", "start": 6, "end": 6}
    )
    expect_rejected(  # an op name the log cannot replay is rejected up front
        session, "user/message", {"content": "x"}, {"op": "delete", "start": 0, "end": 99}
    )
    expect_rejected(  # same for an unknown string op
        session, "user/message", {"content": "x"}, "prepend"
    )
    # After compaction the surface is [11, 6, 10]: no longer sorted by seq. A
    # seq range picking 10 and 11 but not 6 would cut a hole in the middle.
    expect_rejected(
        session, "user/message", {"content": "x"}, {"op": "replace", "start": 10, "end": 12}
    )

    # Compaction composes: a second replace can cover the first summary too.
    summary2 = session.append(
        "user/message",
        {"content": "Summary: whole conversation so far."},
        surface_op={"op": "replace", "start": 0, "end": len(session.log)},
    )
    assert session.surface == [summary2["seq"]]
    assert session.derive_messages() == [
        Message("user", "Summary: whole conversation so far.")
    ]
    assert session.log[0]["payload"]["content"] == "hi", "the log still forgets nothing"

    print("section 03: all checks passed")


if __name__ == "__main__":
    main()
