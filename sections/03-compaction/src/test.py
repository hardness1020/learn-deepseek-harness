"""Offline check for section 03: compaction replaces surface entries, never log rows."""

import json

from kernel import Context
from message import Message
from session_log import Session, session_log_plugin


def thaw(value):
    """Frozen event parts back to plain JSON data, for replaying appends."""
    return json.loads(json.dumps(value, default=dict))


def expect_rejected(session, event_type, payload, surface_op):
    """A bad surface op must raise and leave the session untouched."""
    rows, surface = len(session.log), list(session.surface)
    try:
        session.append(event_type, payload, surface_op=surface_op)
        assert False, f"expected ValueError for {surface_op!r} on {event_type}"
    except ValueError:
        pass
    assert len(session.log) == rows, "a rejected append must not commit a log row"
    assert session.surface == surface, "a rejected append must not touch the surface"


def build_session(store):
    """One short conversation: two exchanges plus a tool result and a chunk."""
    session = store.create("s1")
    session.append("user/message", {"content": "hi"})            # seq 0
    session.append("assistant/chunk", {"text": "He"})            # seq 1, log-only
    session.append("assistant/message", {"content": "Hello."})   # seq 2
    session.append("tool/result", {"content": "42"})             # seq 3
    session.append("user/message", {"content": "and now?"})      # seq 4
    session.append("assistant/message", {"content": "Now this."})  # seq 5
    return session


def main():
    ctx = Context()
    ctx.plugin(session_log_plugin)
    store = ctx.get("sessions")

    session = build_session(store)
    assert session.surface == [0, 2, 3, 4, 5]

    # Compaction is one append: a summary message whose surface op replaces
    # the surface entries with seq in [0, 4). The log is never edited.
    rows_before = len(session.log)
    summary = session.append(
        "user/message",
        {"content": "Summary: greeted, tool said 42."},
        surface_op={"op": "replace", "start": 0, "end": 4},
    )

    # The model's view shrank to summary + tail.
    assert session.derive_messages() == [
        Message("user", "Summary: greeted, tool said 42."),
        Message("user", "and now?"),
        Message("assistant", "Now this."),
    ]
    assert session.surface == [summary["seq"], 4, 5]

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
    expect_rejected(  # covers no surface entry: seqs 0..3 were already replaced
        session, "user/message", {"content": "x"}, {"op": "replace", "start": 1, "end": 3}
    )
    expect_rejected(  # a log-only event type cannot join the surface
        session, "assistant/chunk", {"text": "x"}, "append"
    )
    expect_rejected(  # end is exclusive, so [4, 4) covers nothing
        session, "user/message", {"content": "x"}, {"op": "replace", "start": 4, "end": 4}
    )
    # After compaction the surface is [6, 4, 5]: no longer sorted by seq. A
    # seq range picking 5 and 6 but not 4 would cut a hole in the middle.
    expect_rejected(
        session, "user/message", {"content": "x"}, {"op": "replace", "start": 5, "end": 7}
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
