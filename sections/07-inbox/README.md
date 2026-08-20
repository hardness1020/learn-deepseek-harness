# 07 · Inbox

> Say it whenever you like. The loop hears it at the next boundary,
> in the next request, exactly once. Never mid-sentence.

Section 06's agent still has exactly one door. `send()` takes a
message, runs a whole turn, and returns; a second `send()` mid-turn
raised. Everything a user says must wait for the machine to go quiet.

Real input does not arrive on schedule. A user watching tool results
stream past wants to redirect the work now, not after it finishes
going down the wrong road. A finished background task wants to slip
its outcome into the next request. And a genuine follow-up question
should wait its turn, not barge into the one underway.

The obvious build is to append arriving text straight into the log as
a `user/message` row. But mid-step, the request in flight already
derived its history: the new row would claim the model saw words it
never received, and replay would rebuild a request that was never
sent. Worse, the sender is often a tool body on a worker thread, and
section 06 made the log single-writer. And a single list cannot say
what a message wants: to join the work underway, or to start its own.

So: why two inbox targets, and why claim only at step boundaries?

Because input must be routed, never applied, and the routing must
carry the sender's intent. For that, the inbox must:

1. Route, never apply: arriving text goes into a pending list, not
   the log. Insertion is lock-guarded, legal from any thread, and
   leaves no row.
2. Offer two targets for two intents: `next-turn` is a prompt that
   deserves a turn of its own; `next-step` is input for the work
   already underway. Only the sender knows which one it means.
3. Claim only at step boundaries: pending input becomes
   `user/message` rows exactly where the next request is re-derived
   from the log (section 04's rule), so the transcript never claims
   the model saw words it did not.
4. Give each prompt its own turn: a claim that opens a turn takes all
   pending `next-step` input plus at most one queued prompt, so
   queued prompts never merge.
5. Never close a turn over fresh steering: a step that ended with a
   reason re-checks `next-step`; anything there spends another step
   in the same turn.
6. Die with the turn it aimed at: `cancel()` empties the inbox, so an
   aborted turn cannot restart from input queued before the cancel.

---

## Mechanism

One new file, `inbox.py`, and the loop's front door rerouted through
it:

- **`Inbox`**: two ordered pending lists behind one lock.
  `insert(target, message)` files input from any thread;
  `claim(target)` empties `next-step` and, when the boundary opens a
  turn, pops exactly one `next-turn` prompt.
- **`send(text, target, wakeup)`**: the one routing door.
  `followup()`, `steer()`, and `inject()` are its three presets.
- **`_drain()`**: the driver. Runs turns until no queued prompt
  remains, then goes idle, so one wake serves every prompt queued
  behind it.
- **The turn-close re-check**: a turn ends only when a step ended
  with a reason and `next-step` is empty at that moment.

The three presets differ only in routing:

```python
def followup(self, text):
    """Queue a prompt that gets a turn of its own."""
    self.send(text, "next-turn", True)

def steer(self, text):
    """Steer the nearest step: input for the work already underway."""
    self.send(text, "next-step", True)

def inject(self, text):
    """Park model-facing context for the next step, without waking."""
    self.send(text, "next-step", False)
```

`send()` inserts, then wakes the drain loop only if the agent is
idle. A tool body or bus listener calling mid-turn just queues: the
driving thread is busy inside a step, and a later boundary claims.

```python
def _turn(self):
    self.session.append("turn/start", {})
    target = "next-turn"  # only a turn's first boundary consumes a queued prompt
    while True:
        reason = self._step(target)
        target = "next-step"
        if reason == "aborted":
            break  # cancelled: pending input is already gone
        if reason is not None and not self.inbox.has("next-step"):
            break  # fresh steering spends another step in this turn
    self.session.append("turn/end", {})
```

Inside `_step(target)`, the claim happens first, right where section
04 already re-derives everything:

```python
self.session.append("step/start", {})
for message in self.inbox.claim(target):
    self.session.append("user/message", message)
messages = self.session.derive_messages()  # re-derived, never cached
```

Inserts land any time; claims land only at boundaries:

```text
insert: any thread, any time          claim: loop thread, boundaries only

steer("s") ──► next-step [ s ]        every step: all of next-step
followup("B") ──► next-turn [ B ]     turn-opening step: plus one prompt

turn A    step 1             step 2             step 3
          claim: [A]         claim: [s]         claim: []
          user/message A     user/message s     model -> "done"
          model -> calls     model -> "ok"      completed, next-step
          tool rows   ▲      completed, but     empty: turn closes
                      │      next-step refilled
          s inserted here,   mid-step: another
          mid-step: parked   step, same turn
turn B    step 1  claim: [B]              one queued prompt, one turn
```

Here is a real run, as the log records it. The `read` body steers
once and queues two follow-up prompts, all from its worker thread:

```text
send("read my note")
  │   0  turn/start
  │   1  step/start
  │   2  user/message   "read my note"       ◄ claimed at the boundary
  │   3  request/header
  │   4  assistant/message {"tool_calls": [read]}
  │   5  tool/call     read
  │        ...the body steers and queues two prompts, mid-step...
  │   6  tool/result   read
  │   7  step/end      {"reason": null}
  │   8  step/start
  │   9  user/message   "while reading: also check the dates"  ◄ the steer
  │  10  request/header
  │  14  assistant/message
  │  15  step/end      {"reason": "completed"}
  │  16  turn/end                             ◄ next-step empty: close
  │  17  turn/start                           ◄ first queued prompt
  │  19  user/message   "queued: summarize everything"
  │  26  turn/end
  │  27  turn/start                           ◄ second queued prompt
  │  29  user/message   "queued: then say goodbye"
  │  36  turn/end
```

The steer entered the turn underway at seq 9, one boundary after it
was sent. The two follow-ups never merged: one prompt, one turn,
three turns from one wake. Derive the history at any point and every
`user/message` the model is said to have seen, it saw.

### What changed

Compared with section 06:

- `kernel.py`, `message.py`, `scheduler.py`, `session_log.py`,
  `standin.py`, `tools.py` are carried forward verbatim. `inbox.py`
  is the only new source file; the other changes are the inbox pulled
  through `agent_loop.py`, so the diff against 06 is this section's
  Mechanism, nothing else.
- `agent_loop.py`: `send()` routes through the inbox instead of
  appending `user/message` itself, and gains the `target` and
  `wakeup` parameters plus the `followup()` / `steer()` / `inject()`
  presets. The "agent is mid-turn" RuntimeError is gone: a mid-turn
  send queues instead of raising. One `send()` now drains every
  queued prompt before returning. `cancel()` also empties the inbox.
- The log's shape changed: a `user/message` row now lands inside the
  step that claims it, after `step/start`, instead of before
  `turn/start`. Input reaches the transcript only by being claimed.
- `demo.py`: the Live demo parks context with `inject()` while idle,
  then steers and queues a follow-up mid-turn from a bus listener, so
  one send shows all three routings on a real model.

---

## In real dsh

All pointers are into the pinned Studied version,
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca).
The inbox lives in the agent package; the claim sites live in the
loop:
[`packages/core/agent`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent).

| Mini-dsh | Real dsh | Notes |
| --- | --- | --- |
| `Inbox` in `inbox.py` | [`packages/core/agent/src/inbox.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/inbox.ts): `Inbox` | Two ordered pending lists per agent; `InboxTarget = 'next-turn' \| 'next-step'` is declared in [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/types.ts). |
| `claim(target)` | [`inbox.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/inbox.ts): `Inbox.claim` | Same rule: all next-step input, then one queued prompt when the boundary opens a turn. Documented as the loop's step-boundary operation, not a plugin extension point. |
| `send(text, target, wakeup)` | [`packages/core/agent/src/runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts): `Agent.send` | The unified routing door; `followup`, `steer`, and `inject` are fixed-preset aliases, exactly the mini's three one-liners. |
| the turn-close re-check | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts) | A turn closes only when a step ended with a reason and `inbox.nextStep` is empty, re-checked after the `agent/turn-stopping` serial hook gets one last chance to steer. |
| `cancel()` emptying the inbox | [`runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts): `CancelOptions` | `cancel(cause)` clears queued and steering work unless `keepInbox` asks to preserve it; `clear()` empties next-step before next-turn. |
| `_drain()` | [`agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts): `kick()` | The driver drains queued work before retiring, and `running` spans consecutive queued turns; it does not prove a turn is still open. |

What the real inbox adds on top of this section's Mechanism:

- **Durability.** Every mutation appends a normalized
  `agent/inbox/spliced` session event, and the live lists are a
  replay-once projection rebuilt from those rows on resume, so
  pending input survives a restart. The mini's inbox is memory only:
  its log has one writer (section 06) and inserts arrive on worker
  threads, so only claimed messages ever reach its log.
- **Identity and editing.** Real pending messages carry ids and can
  be `replace()`d or `remove()`d before their claim; every change is
  published live as `agent/inbox/inserted`, `claimed`, or
  `discarded`. The mini's payloads are anonymous; once filed, they
  wait.
- **A hook between claim and step.** The `agent/pre-step` waterfall
  can reject a proposed step or rewrite the claimed batch; a rejected
  step ends its claimed messages and closes the turn without a step.
  The mini's claims always enter.
- **A wake latch.** Real waking is decoupled from insertion: a wake
  landing during an aborted activity re-targets to `next-turn` and is
  latched, then replayed when the driver converges to idle. The
  mini's wake is one line, "drain if idle", safe because only the
  driving thread ever observes idle.
- **A human on the steer button.** In real dsh, steering usually
  arrives from the UI, which sits above the Ceiling; the mini presses
  `steer()` and `followup()` from a tool body and a bus listener, and
  `inject()` from the script.

---

## Failure modes

- **Applying input the moment it arrives makes the transcript lie.**
  The request in flight already derived its history, so a row
  appended mid-step says the model saw words it never received, and
  replay rebuilds a request that was never sent. Claims land at
  boundaries, where the next request is re-derived anyway; the log
  stays a record of what actually happened.
- **One list would flatten two intents.** Fold prompts into the
  running turn and a follow-up question hijacks the work underway;
  defer steering to the next turn and it arrives after the agent
  finished going down the wrong road. The sender declares the target,
  because only the sender knows which one it means.
- **Claiming every queued prompt at once merges conversations.** A
  turn-opening claim takes at most one `next-turn` message, so three
  queued prompts are three turns with three answers, not one
  mega-prompt with one muddled answer.
- **Closing the turn without the re-check strands last-second
  steering.** Input that lands while the final step is finishing
  would sit in `next-step` waiting for a wake that may never come.
  The close re-checks the list first: fresh steering spends another
  step in the same turn it aimed at.
- **An inbox that survives cancel resurrects the cancelled work.**
  `cancel()` empties both lists before aborting, so input queued
  before the cancel dies with the turn. Input sent after it queues
  normally, and the drain loop picks it up: a fresh mind, not a
  ghost.
- **Worker threads writing user rows would race the log.** Section
  06 made the log single-writer, and the inbox keeps it so: inserts
  are lock-guarded and memory-only, and only the driving thread turns
  claims into rows.

---

## Runnable

[`src/`](src/) carries 06 forward and adds:

- [`inbox.py`](src/inbox.py) (new): `Inbox`, two pending lists behind
  one lock; `insert`, `claim`, `has`, `clear`.
- [`agent_loop.py`](src/agent_loop.py): `send()` routes through the
  inbox and gains `target` and `wakeup`; `followup()`, `steer()`,
  `inject()`; the drain loop; the claim at each step boundary; the
  turn-close re-check; `cancel()` empties the inbox.
- [`test.py`](src/test.py): a tool body steers into its own turn and
  queues two prompts that each get a turn, an idle `inject()` leaves
  the log untouched until the next wake claims it first, steering
  that lands during a completed step keeps the turn open for one more
  step, and a cancel drops everything pending while the next send
  starts fresh.
- [`demo.py`](src/demo.py): the Live demo parks context while idle,
  then steers and queues a follow-up mid-turn off the bus, and prints
  the log's own story of all three routings.

```bash
python sections/07-inbox/src/test.py    # offline check, no key
```

The Live demo needs the root `requirements.txt` and a key; it skips
politely without one:

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/07-inbox/src/demo.py
```

---

## Sources

- [`docs/agent-lifecycle.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/agent-lifecycle.md):
  dsh's own diagram of a turn, claim points and inbox events included.
- [`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/core.md):
  the Agent surface, the three preset aliases, and the inbox as the
  delivery vocabulary.
- [`.agents/notes/implemented/architecture/2026-07-30-followup-enqueue-and-owned-runs.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-07-30-followup-enqueue-and-owned-runs.md):
  the design note on why `followup()` returns no handle.
