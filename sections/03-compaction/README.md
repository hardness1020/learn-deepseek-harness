# 03 · Compaction

> The log never forgets. Compaction only changes which part of the story the
> model gets retold.

Conversations outgrow the context window. Sooner or later the model's history
must shrink: a long stretch of old exchanges gives way to a short summary.

But section 02 made the log append-only on purpose. Seqs are forever, the bus
feed and any persisted row point at them, and replay depends on them. Editing
or deleting rows would break everything the log promised.

So: if the log is append-only, how does compaction remove anything the model
sees?

Section 02 already built the way out. The model never sees the log; it sees
messages derived from the surface.

Compaction is therefore a surface edit, not a log edit: one new event,
appended like any other, whose surface op replaces a run of surface entries
with itself.

For that to hold, the session log must:

1. Carry a **surface op** on every append: `"append"` to join the surface,
   `None` to stay log-only, or `{"op": "replace", "start": s, "end": e}` to
   shadow the surface entries whose seq falls in `[start, end)`.
2. Record the op on the logged event, so the surface stays derivable from the
   log alone.
3. Validate the surface transition before the event commits: a bad op changes
   nothing, neither log nor surface.
4. Require the replacing event to derive a message itself: the summary the
   model reads instead of what it lost.
5. Never edit, move, or delete a log row: compaction shrinks the derived view
   only.

---

## Mechanism

Two moving parts, both inside `Session`:

- **Surface op**: the third argument to `append()`. Left out, `append()` picks
  the section 02 default: surface types join the surface, everything else is
  log-only. Passed explicitly, it can be the replace form.
- **`_surface_after()`**: computes the surface as it will be once the append
  commits, and raises if the op is invalid. Only after it returns does the
  event enter the log.

`append()` now validates the transition, then commits, and the op itself is
frozen into the event:

```python
def append(self, event_type, payload, surface_op=None):
    # Validate-and-copy at the boundary: the payload must be plain JSON
    # data, and the log keeps its own copy so no caller can edit history.
    payload = json.loads(json.dumps(payload))
    if surface_op is None and event_type in SURFACE_TYPES:
        surface_op = "append"
    seq = len(self.log)
    # Validate the surface transition before committing: a bad op must
    # leave both the log and the surface untouched.
    surface = self._surface_after(event_type, seq, surface_op)
    event = _freeze(
        {"seq": seq, "type": event_type, "payload": payload, "surface_op": surface_op}
    )
    self.log.append(event)
    self.surface = surface
    if self._on_event is not None:
        self._on_event(self, event)
    return event
```

The replace arm shadows a run of surface entries with the new event:

```python
def _surface_after(self, event_type, seq, surface_op):
    """The surface as it will be once this append commits. Raises if invalid."""
    if surface_op is None:
        return self.surface
    if event_type not in SURFACE_TYPES:
        raise ValueError(f"'{event_type}' derives no message; it cannot join the surface")
    if surface_op == "append":
        return self.surface + [seq]
    if not isinstance(surface_op, dict) or surface_op.get("op") != "replace":
        raise ValueError(f"unknown surface op: {surface_op!r}")
    # {"op": "replace", "start": s, "end": e}: this event shadows the
    # surface entries whose seq falls in [start, end), half-open.
    start, end = surface_op["start"], surface_op["end"]
    covered = [i for i, s in enumerate(self.surface) if start <= s < end]
    if not covered:
        raise ValueError(f"replace [{start}, {end}) covers no surface entry")
    if covered != list(range(covered[0], covered[-1] + 1)):
        raise ValueError(f"replace [{start}, {end}) covers a non-contiguous surface run")
    return self.surface[: covered[0]] + [seq] + self.surface[covered[-1] + 1 :]
```

Compaction, then, is not a new subsystem. It is one ordinary append: a
`user/message` holding the summary, carrying a replace op over the seqs it
retires.

```text
log      0:user  1:chunk  2:assistant  3:tool  4:user  5:assistant
surface  [0, 2, 3, 4, 5]

append("user/message", {"content": "Summary: ..."},
       surface_op={"op": "replace", "start": 0, "end": 4})

log      0:user  1:chunk  2:assistant  3:tool  4:user  5:assistant  6:user
surface  [6, 4, 5]

derive_messages() ──► "Summary: ..."   "and now?"   "Now this."
```

Every row is still in the log, at its old seq, frozen. Only the projection
changed: `derive_messages()` now starts at the summary.

Two details carry the weight:

- **Validate, then commit.** `_surface_after()` runs before `self.log.append`.
  A rejected op raises out of `append()` with the log and the surface exactly
  as they were: no ghost row whose recorded op never happened.
- **The op is on the record.** Because each event carries its surface op, the
  surface is a pure function of the log: replay every logged append and you
  rebuild it exactly. The Offline check proves this by rebuilding a second
  `Session` from the first one's rows.

One quirk is worth staring at: after a compaction, the surface is no longer
sorted by seq. In the diagram above it reads `[6, 4, 5]`, the summary sitting
before older seqs, because surface order is conversation order, not log order.

That is why a later replace must cover a contiguous *run of the surface*, and
why `_surface_after()` rejects a seq range whose covered entries have a hole.

### What changed

Compared with section 02:

- `kernel.py`, `message.py`, and `standin.py` are carried forward verbatim;
  `session_log.py` is the only changed source file, so the diff against 02 is
  this section's Mechanism, nothing else.
- `append()` gains the `surface_op` argument, records the op on the frozen
  event, and commits only after the new `_surface_after()` validates the
  transition.
- The surface types are still exactly three. A compaction summary is a plain
  `user/message`; the op, not a new event type, does the replacing.
- There is no `compaction.py`. Compaction is one `append()` call, so the
  Mechanism lives where the surface lives.

---

## In real dsh

All pointers are into the pinned Studied version,
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca).
The surface and its ops live in
[`packages/core/session`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session).

| Mini-dsh | Real dsh | Notes |
| --- | --- | --- |
| `surface_op` argument: `"append"` or `{"op": "replace", "start", "end"}` | [`packages/core/session/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/types.ts): `SurfaceOp` | `SurfaceOp = 'append' \| { op: 'replace', start, end }`, the exact two-arm shape this section rebuilds. |
| validate-then-commit in `append()` | [`packages/core/session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts): `class Session` | `append()` validates (`snapshotJsonValue`), deep-freezes, validates the surface transition, then pushes; compaction rewrites the surface via a `replace` marker without mutating the log. |
| `_surface_after()` maintaining the surface | [`packages/core/session/src/surface.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/surface.ts): `SurfaceManager` | The real surface is a managed object with its own module; the mini folds it into two methods on `Session`. |
| summary as a plain `user/message` | [`packages/core/session/src/known-event-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/known-event-types.ts): `compaction/*` | Real dsh gives compaction its own event types, added to `SessionEventMap` by declaration merging; they appear among the 45 in-repo event types. |

What the real session log adds on top of this section's Mechanism:

- **Compaction as a plugin with its own vocabulary.** The core session package
  ships no `compaction/*` types; plugins add them by declaration merging, and
  they show up among the 45 in-repo event types in
  [`known-event-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/known-event-types.ts).
  The mini keeps `SURFACE_TYPES` fixed at three and reuses `user/message` for
  the summary, so the op stays the whole diff.
- **Someone to write the summary.** This section treats the summary text as
  caller-provided data; the replace op works the same whoever wrote it.
  Producing a summary with the model needs a request loop, and mini-dsh gets
  one in section 04.
- **A projection that is not this projection.**
  [`packages/session/session-projection`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-projection)
  folds committed events into client-facing UI read models, untouched by
  surface replaces. It is unrelated to `deriveMessages()`, and the UI itself
  sits above the Ceiling: pointed at, not rebuilt.

---

## Failure modes

- **`end` is exclusive.** `{"start": 0, "end": 4}` retires seqs 0 through 3
  and leaves seq 4 visible. Off by one and the summary sits next to a message
  it claims to have replaced. The check pins the edge: `[4, 4)` covers nothing
  and is rejected.
- **A replace that covers nothing would tell the story twice.** If an empty
  cover committed, the summary would join the surface while everything it
  summarizes stayed visible. `_surface_after()` rejects it instead.
- **Surface order is not seq order after the first compaction.** With surface
  `[6, 4, 5]`, the seq range `[5, 7)` picks 6 and 5 but skips 4: a hole in the
  middle of the run. Committing that would splice the summary over messages it
  never covered, so non-contiguous covers are rejected.
- **Committing before validating would break replay.** If `append()` pushed
  the row first and validated after, a failed compaction would leave a logged
  event whose recorded op never took effect, and rebuilding the surface from
  the log would diverge from the live one. Real dsh validates the surface
  transition before pushing for the same reason.
- **An op the log cannot replay is rejected up front.** `{"op": "delete"}` or
  `"prepend"` mean nothing to `_surface_after()`; silently accepting one would
  freeze a record onto the event that no replayer knows how to honor.
- **A log-only event cannot do the replacing.** An `assistant/chunk` carrying
  a replace op would delete part of the model's view and put nothing readable
  in its place. The op requires a surface type: whatever replaces messages
  must be a message.
- **What the model lost, the model cannot get back.** There is no un-replace
  op. After compaction the summary is the model's only memory of that span, so
  a bad summary is permanent for the conversation, even though the log still
  holds every row for replay and audit.

---

## Runnable

[`src/`](src/) carries 02 forward and adds:

- [`session_log.py`](src/session_log.py) (changed): the `surface_op` argument
  on `append()`, the op recorded on every frozen event, and `_surface_after()`
  validating each transition before it commits.
- [`test.py`](src/test.py): the derived view shrinks while the log keeps every
  row, ops are on the record, replaying the log rebuilds the surface exactly,
  invalid ops (empty cover, log-only replacer, exclusive-end edge, unknown op
  names, non-contiguous run) reject without touching the session, and a second
  compaction can cover the first.

```bash
python sections/03-compaction/src/test.py   # offline checks, no key
```

The Mechanism never touches the Model seam: the summary is caller-provided
data. The check drives the Scripted stand-in only to stream a realistic
conversation into the log before compacting it; there is no `demo.py` until
the loop exists (section 04).

---

## Sources

- [`docs/subsystems/session.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/session.md):
  dsh's own session subsystem doc.
- [`packages/core/session/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/README.md):
  the package's own README.
