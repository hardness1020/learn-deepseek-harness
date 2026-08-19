"""The Scripted stand-in: the offline Model seam implementation.

A passive queue of canned responses, handed out in order. It never
inspects the request. Each response streams as a few deterministic
chunks, then the final Message, so streaming is real from day one.
"""

from message import Message


def _chunks(text, n=3):
    size = max(1, -(-len(text) // n))
    return [text[i : i + size] for i in range(0, len(text), size)]


class ScriptedModel:
    def __init__(self, responses):
        self._queue = list(responses)

    def __call__(self, messages):
        """The Model seam: yields ("chunk", str)... then ("message", Message)."""
        text = self._queue.pop(0)
        for piece in _chunks(text):
            yield ("chunk", piece)
        yield ("message", Message(role="assistant", content=text))
