"""Mini-dsh's own Message shape. Provider-agnostic, like real dsh."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
