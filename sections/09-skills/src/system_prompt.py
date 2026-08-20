"""The system prompt: ordered providers assembled into the request, every step.

No part of the request is written by hand. A registry holds four
provider kinds: sections (static system text), contexts (dynamic
runtime state), variables ({{name}} values), and tool-schema
providers. assemble() resolves them, in order, into the three
model-facing artifacts: the system text, the request's tool list,
and a runtime-context snapshot.

The section/context split is the point. System text must come out
byte-identical from step to step, or no prompt prefix is ever reused;
so dynamic state (a clock, a cwd) never renders there. The snapshot
travels as a user/message row instead, appended only when it differs
from the last snapshot row in the log. The retained snapshot is a log
projection, not separate state, so dedupe and replay agree by
construction.

Ordering is one numeric order per entry, registration order breaking
ties, so identical registrations always render identical text.
Interpolation is strict: a {{name}} whose variable is unknown or
unset raises, and the request never ships with a hole in it.
"""

import re

_VARIABLE = re.compile(r"\{\{(\w+)\}\}")

IDENTITY = "You are mini-dsh, a small agent harness."


class SystemPrompt:
    """Four ordered provider kinds; assemble() renders the three artifacts."""

    def __init__(self):
        self._count = 0  # registration counter: the deterministic tiebreak
        self._sections = []  # {"name", "order", "seq", "text": str | callable}
        self._contexts = []  # {"name", "order", "seq", "provider": callable}
        self._variables = {}  # name -> value for {{name}}
        self._tools = []  # providers: (assemble_context) -> [schema, ...]

    # -- registration: every method hands back its undo -------------------
    def section(self, name, text, order=0):
        """Static system text; a callable text renders per assembly."""
        return self._add(self._sections, {"name": name, "text": text, "order": order})

    def context(self, name, provider, order=0):
        """Dynamic runtime state; renders into the snapshot, never the system."""
        return self._add(self._contexts, {"name": name, "provider": provider, "order": order})

    def variable(self, name, value):
        if name in self._variables:
            raise ValueError(f"prompt variable '{name}' already set")
        self._variables[name] = value
        return lambda: self._variables.pop(name)

    def tools(self, provider):
        self._tools.append(provider)
        return lambda: self._tools.remove(provider)

    def _add(self, entries, entry):
        entry["seq"] = self._count
        self._count += 1
        entries.append(entry)
        return lambda: entries.remove(entry)

    # -- assembly ----------------------------------------------------------
    def assemble(self, assemble_context):
        """Resolve every provider, in order: the request's three artifacts."""
        sections = [self._render(e["text"], assemble_context) for e in _ordered(self._sections)]
        contexts = [e["provider"](assemble_context) for e in _ordered(self._contexts)]
        return {
            "system": "\n\n".join(text for text in sections if text),
            "tools": [s for provider in self._tools for s in provider(assemble_context)],
            "runtime_context": "\n".join(text for text in contexts if text),
        }

    def _render(self, text, assemble_context):
        if callable(text):
            text = text(assemble_context)
        return _VARIABLE.sub(self._value_of, text)

    def _value_of(self, match):
        name = match.group(1)
        value = self._variables.get(name)
        if value is None:  # unknown name or unset value: refuse to ship
            raise ValueError(f"prompt variable '{name}' is unknown or unset")
        return str(value)


def _ordered(entries):
    return sorted(entries, key=lambda entry: (entry["order"], entry["seq"]))


def is_snapshot(event):
    """Is this log event a runtime-context snapshot row?"""
    return event["type"] == "user/message" and event["payload"].get("kind") == "runtime-context"


def latest_snapshot(session):
    """The retained snapshot: the last runtime-context row in the log."""
    for event in reversed(session.log):
        if is_snapshot(event):
            return event["payload"]["content"]
    return None


def system_prompt_plugin(ctx):
    prompt = SystemPrompt()
    # Built-ins are ordinary registrations, reversible like everything else.
    ctx.effect(prompt.section("harness:identity", IDENTITY, order=-100), "identity")
    # The bridge: the request's tool list is a prompt output too. The
    # assemble context carries the agent's scoped view of the registry.
    ctx.effect(prompt.tools(lambda ac: ac["tools"].schemas()), "tool schemas")
    ctx.provide("system_prompt", prompt)
