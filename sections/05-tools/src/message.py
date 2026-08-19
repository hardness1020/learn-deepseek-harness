"""Mini-dsh's own Message shape. Provider-agnostic, like real dsh."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
    tool_calls: tuple = ()  # assistant only: the calls this message makes
    call_id: str = None  # tool only: the call this message answers
