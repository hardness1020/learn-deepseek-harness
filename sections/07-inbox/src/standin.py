"""The Scripted stand-in: the offline Model seam implementation.

A passive queue of canned responses, handed out in order. It never
inspects the request. Each response streams as a few deterministic
chunks, then the final Message, so streaming is real from day one.
A canned response may be a dict carrying tool_calls, so tool-using
turns are scriptable offline too.
"""

from message import Message


def _chunks(text, n=3):
    size = max(1, -(-len(text) // n))
    return [text[i : i + size] for i in range(0, len(text), size)]


class ScriptedModel:
    def __init__(self, responses):
        self._queue = list(responses)

    def __call__(self, messages, tools=()):
        """The Model seam: yields ("chunk", str)... then ("message", Message)."""
        item = self._queue.pop(0)
        if isinstance(item, str):
            item = {"text": item}
        text = item["text"]
        for piece in _chunks(text):
            yield ("chunk", piece)
        yield (
            "message",
            Message(
                role="assistant",
                content=text,
                tool_calls=tuple(item.get("tool_calls", ())),
            ),
        )
