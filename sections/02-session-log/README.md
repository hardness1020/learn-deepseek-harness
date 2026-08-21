# 02 · Session log

English | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

> The model wants clean history, saving to disk wants every row, and
> compaction wants to shrink what the model sees. One list cannot serve all
> three, so record everything once and work out each view from that.

An agent turn produces far more than messages: streamed chunks, tool calls and results,
turn markers, request headers.

The same turn gets read three different ways. The model wants clean history,
saving to disk wants every row, and compaction wants to shrink what the model
sees without losing the record.

The naive way is one shared `messages` list, appended as the turn runs.

One list can only serve one consumer. Streamed chunks either pollute it or vanish,
compaction must destructively edit it, and after a crash you hold whatever the list
happened to contain, with no way to reconstruct how it got there.

The session log flips it: record everything that happened, once, append-only, and
*derive* what the model sees. For that to hold, the log must:

1. Keep one append-only log of frozen events per session; an event's **seq** is its
   log index, forever.
2. Maintain a **surface**: the ordered seqs of the message-producing events, nothing else.
3. Derive model history from the surface on demand (`derive_messages()`), never store it.
4. Validate and copy every payload at the append boundary, so history cannot be edited
   after the fact.
5. Feed every committed append to subscribers, so persistence and observers can be
   plugins rather than core code.

---

## Mechanism

Three moving parts:

- **Log**: an append-only list of frozen events. An event is `{seq, type, payload}`,
  and its seq equals its index.
- **Surface**: an ordered list of seqs, maintained at append time: exactly the
  `user/message`, `assistant/message`, and `tool/result` events.
- **`derive_messages()`**: projects the surface into `Message` objects, recomputed
  on every call.

Appending is the only write, and it does all the guarding:

```python
def append(self, event_type, payload):
    # Validate-and-copy at the boundary: the payload must be plain JSON
    # data, and the log keeps its own copy so no caller can edit history.
    payload = json.loads(json.dumps(payload))
    seq = len(self.log)
    event = _freeze({"seq": seq, "type": event_type, "payload": payload})
    self.log.append(event)
    if event_type in SURFACE_TYPES:
        self.surface.append(seq)
    if self._on_event is not None:
        self._on_event(self, event)
    return event
```

Deriving is a read that touches nothing:

```python
def derive_messages(self):
    """Project the surface into model history. Never stored, always derived."""
    return [
        Message(
            role=SURFACE_TYPES[event["type"]],
            content=event["payload"]["content"],
        )
        for event in (self.log[seq] for seq in self.surface)
    ]
```

The store mounts on section 01's kernel as the `sessions` service, so the whole
session log is a reversible registration like everything else:

```python
def session_log_plugin(ctx):
    ctx.provide("sessions", SessionStore(ctx))
```

```text
append(event_type, payload) ──► validate + copy ──► freeze ──► log[seq]
                                                │
                          surface type? ──► surface.append(seq)
                                                │
                                     emit("session/event", ...)

derive_messages() ──► for seq in surface ──► log[seq] ──► Message(role, content)
```

Notice what the split buys. An `assistant/chunk` is a real logged event, so streaming
is replayable, yet it never reaches the model: it is not a surface type.

And because the model's view is the surface, not the log, section 03 can shrink that
view by changing the surface while the log keeps every row.

### What changed

Compared with section 01:

- `message.py`, `standin.py`, and `kernel.py` are carried forward verbatim; the diff
  against 01 is this section's mechanism, nothing else.
- New `session_log.py`: `Session` (log, surface, `append`, `derive_messages`),
  `SessionStore`, and `session_log_plugin`.
- The session log is the first real service mounted on 01's kernel: `provide("sessions")`
  puts its undo on the plugin's fiber, so unmounting the session log is one `dispose()`.

---

## In real dsh

All pointers are into the pinned studied version,
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca).
The session log lives in
[`packages/core/session`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session).

| Mini-dsh | Real dsh | Notes |
| --- | --- | --- |
| `Session` (log, `append`) | [`packages/core/session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts): `class Session` | `append()` validates (`snapshotJsonValue`), deep-freezes, validates the surface transition, then pushes; `seq == log.length` is an invariant. |
| `surface` + `derive_messages()` | [`packages/core/session/src/surface.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/surface.ts): `SurfaceManager`, `deriveEventMessage` | Surface events are exactly `user/message`, `assistant/message`, `tool/result`. `SurfaceOp` is `'append'` or `{op: 'replace', start, end}`; the replace arm is section 03. |
| event dict `{seq, type, payload}` | [`packages/core/session/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/types.ts): `SessionEvent`, `SessionEventMap` | 13 core event types (turn and step markers, user, assistant, tool traffic, request headers); 45 in-repo types total ([`known-event-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/known-event-types.ts)), extensible by declaration merging. |
| `SessionStore`, `ctx.get("sessions")` | [`packages/core/session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts): `class SessionStore extends Service` | Ctx key `ctx.sessions`; session creation emits `session/created`, vetoable by throw. |
| `emit("session/event", ...)` | `session/event` bus event in `index.ts` | The post-commit append feed. The real store also emits `session/disposed` and `session/flush`, an awaited durability barrier. |

What the real session log adds on top of this section's mechanism:

- **A durability barrier.** `session/flush` is a parallel, *awaited* bus event:
  persistence finishes writing before dsh moves on. Our kernel's `emit` is
  synchronous fire-and-forget, so the barrier is pointed at here, not rebuilt.
- **Persistence as plugins.** The abstract
  [`SessionPersistence`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence/src/index.ts)
  service (`ctx.sessionPersistence`) attaches purely via the bus events
  ([`coordinator.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence/src/coordinator.ts)):
  a backend tails `session/event` and `session/flush`, and the core `Session` never
  learns disks exist. dsh ships
  [JSONL](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence-jsonl)
  and
  [SQLite](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence-sqlite)
  backends. Ceiling: non-JSONL persistence backends are pointed at, not rebuilt.
- **A projection that is not this projection.**
  [`packages/session/session-projection`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-projection)
  (`ctx.sessionProjections`) folds committed events into client-facing UI read models.
  It is unrelated to `deriveMessages()`, and the UI itself sits above the Ceiling.
- **Surface rewriting.** The `replace` arm of `SurfaceOp` lets compaction shrink the
  model's view while the log stays append-only
  ([`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)):
  section 03 builds exactly that.

---

## Failure modes

- **A message-producing event the surface misses is invisible.** Add a new event type
  that should reach the model without registering it in `SURFACE_TYPES`, and
  `derive_messages()` silently drops it. Real dsh centralizes the mapping in
  `deriveEventMessage` for the same reason.
- **A throwing subscriber blocks the append.** The `session/event` feed is synchronous,
  so one bad listener raises through `append()`. Real dsh's persistence coordinator
  contains per-listener exceptions so one backend cannot wedge the log.
- **Validate-and-copy is JSON-shaped, not meaning-shaped.** The `json` round trip
  quietly turns tuples into lists and accepts `NaN`; a payload that survives it is
  guaranteed to be plain data, not to be the payload you meant.
- **Seq references make in-place pruning impossible, on purpose.** The surface, the
  feed, and any persisted row all point at seqs. Deleting or reordering log rows would
  break them all; removal must be a projection change (section 03), never log surgery.
- **State outside the log will not replay.** A consumer that caches a message list or
  keeps a mutable aggregate diverges the moment history is re-derived. The log is only
  the one durable truth if every write routes through `append()`.

---

## Runnable

[`src/`](src/) carries 01 forward and adds:

- [`session_log.py`](src/session_log.py): `Session` (append-only log, surface,
  `derive_messages()`), `SessionStore`, and `session_log_plugin` mounting it as the
  `sessions` service.
- [`test.py`](src/test.py): the seq invariant, surface selection, chunk invisibility,
  derived-not-stored history, frozen events, append-boundary rejection, the bus feed,
  and duplicate session ids.

```bash
python sections/02-session-log/src/test.py   # offline checks, no key
```

The mechanism never calls the model. The check drives the Scripted stand-in only to
stream one realistic turn into the log, so `assistant/chunk` events are real; there is
no `demo.py` until the loop exists (section 04).

---

## Sources

- [`docs/subsystems/session.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/session.md):
  dsh's own session subsystem doc.
- [`packages/core/session/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/README.md):
  the package's own README.
