"""The inbox: two pending lists routing new input into a running loop.

A message arriving mid-turn cannot go straight into the session log.
The step in flight already derived its request, so a row appended now
would claim the model saw text it never received; and the sender is
often a worker thread, which may never write to the log (section 06's
single-writer rule). So input is routed, never applied: insert() files
the message under a target, and the loop claims pending input only at
step boundaries, exactly where the next request is re-derived anyway.

Two targets because a message has two intents. "next-turn" is a prompt
that deserves a turn of its own; "next-step" is input for the work
already underway. claim() takes every pending next-step message and,
when the boundary also opens a turn, exactly one queued prompt, so
queued prompts never merge into a single turn.
"""

import threading

TARGETS = ("next-turn", "next-step")


class Inbox:
    """Two ordered pending lists; inserts from any thread, claims from the loop."""

    def __init__(self):
        self._lock = threading.Lock()  # tool bodies insert from worker threads
        self._pending = {target: [] for target in TARGETS}

    def insert(self, target, message):
        """File a message under a target. No log row: routed, not applied."""
        if target not in TARGETS:
            raise ValueError(f"unknown inbox target: {target!r}")
        with self._lock:
            self._pending[target].append(message)

    def claim(self, target):
        """The step-boundary operation: all next-step input, then at most one
        queued prompt when this boundary is also the start of a turn."""
        with self._lock:
            claimed = self._pending["next-step"]
            self._pending["next-step"] = []
            if target == "next-turn" and self._pending["next-turn"]:
                claimed.append(self._pending["next-turn"].pop(0))
            return claimed

    def has(self, target):
        with self._lock:
            return bool(self._pending[target])

    def clear(self):
        """Drop all pending input, next-step before next-turn."""
        with self._lock:
            self._pending["next-step"] = []
            self._pending["next-turn"] = []
