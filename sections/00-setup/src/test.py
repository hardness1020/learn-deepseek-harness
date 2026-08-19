"""Offline check for section 00: the stand-in speaks the Model seam contract."""

from message import Message
from standin import ScriptedModel


def drain(stream):
    events = list(stream)
    chunks = [text for kind, text in events[:-1] if kind == "chunk"]
    kind, final = events[-1]
    assert kind == "message"
    return chunks, final


def main():
    model = ScriptedModel(["Hello, reader.", "Second answer."])

    chunks, final = drain(model([Message("user", "hi")]))
    assert final == Message("assistant", "Hello, reader."), final
    assert "".join(chunks) == final.content, chunks
    assert len(chunks) > 1, "streaming must be real, not one blob"

    _, final = drain(model([Message("user", "again")]))
    assert final.content == "Second answer.", "queue must be ordered"

    print("section 00: all checks passed")


if __name__ == "__main__":
    main()
