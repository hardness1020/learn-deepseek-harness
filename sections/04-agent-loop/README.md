# 04 · Agent loop

English | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

> The loop holds no state worth saving. Every step it rereads the log, asks
> the model once, and writes the answer back.

Sections 00 to 03 built a session log that can derive model history, stream
chunks, and compact. But nothing drives it. Every check so far hand-cranked
the conversation, appending each message itself.

What is missing is the machine: take the user's text, call the model, record
the response, repeat until the work is done. That machine is the agent loop,
and mini-dsh calls one run of it a **turn**, made of one or more **steps**.

The obvious way to build it keeps a live message list in memory. Append the
user's text, append the reply, hand the list to the model each time. No
deriving, no projection, just a Python list that grows.

But that list would be a second copy of the truth. Compaction (section 03)
edits the surface behind its back. A crash loses it. Resuming a session means
rebuilding it and hoping it matches what the model actually saw.

So: why re-assemble the prompt and re-derive history every step?

Because the log is already the only durable state, and the loop should lean
on that instead of competing with it. For that to hold, the loop must:

1. Run a **turn** as a loop of **steps**: `send()` keeps stepping until a
   step reports an end reason instead of more work.
2. Start every step by deriving model history from the session log, fresh,
   then stream one model call through the Model seam and append every chunk
   and the final message back.
3. Record turn and step boundaries as log events (`turn/start`, `step/start`,
   `step/end`, `turn/end`), log-only, so the log alone tells the whole story.
4. Record a `request/header` row per step saying what was sent, so the log
   can prove what the model was shown.
5. Keep nothing durable on the Agent object: any Agent over the same log
   continues it exactly, so resume is replay plus a new Agent.

---

## Mechanism

Three moving parts in one new file, `agent_loop.py`:

- **`Agent.send()`**: one turn. Appends the user's message and `turn/start`,
  then steps until a step returns an end reason, then appends `turn/end`.
- **`Agent._step()`**: one step. Derives history, records what it is about to
  send, streams the model call, appends everything back, reports how it ended.
- **`AgentRegistry`**: the `agents` service a plugin provides, mirroring the
  `sessions` service from section 02.

The turn is a while loop with the step's answer as its exit condition:

```python
def send(self, text):
    """One turn: the user's message in, steps until one ends with a reason."""
    if self.status == "running":
        raise RuntimeError("agent is mid-turn; the log allows one story at a time")
    self.status = "running"
    try:
        self.session.append("user/message", {"content": text})
        self.session.append("turn/start", {})
        while self._step() is None:
            pass
        self.session.append("turn/end", {})
    finally:
        self.status = "idle"
```

And the step is where the design question is answered, in one line:

```python
def _step(self):
    """One step: re-derive history, one model call, append it all back."""
    self.session.append("step/start", {})
    messages = self.session.derive_messages()  # re-derived, never cached
    self.session.append("request/header", {"messages": len(messages)})
    for kind, value in self.model(messages):
        if kind == "chunk":
            self.session.append("assistant/chunk", {"text": value})
        else:
            self.session.append("assistant/message", {"content": value.content})
    reason = "completed"
    self.session.append("step/end", {"reason": reason})
    return reason
```

`derive_messages()` runs inside the step, after `step/start` commits. The
step owns no history. It borrows the log's, for exactly one model call.

Here is the second turn of a conversation, as the log records it:

```text
send("and now?")
  │  10  user/message      {"content": "and now?"}
  │  11  turn/start
  │
  ├─ step ─────────────────────────────────────────────
  │  12  step/start
  │      derive_messages()          ◄── read the log, fresh
  │  13  request/header    {"messages": 3}
  │  14  assistant/chunk   ┐
  │  15  assistant/chunk   │ streamed through the Model seam
  │  16  assistant/chunk   ┘
  │  17  assistant/message {"content": "Now this."}
  │  18  step/end          {"reason": "completed"}
  ├─ reason is "completed" ► leave the loop
  │
  │  19  turn/end
```

Every row is one `append()` on the section 02 session. The markers and the
header are log-only (`surface_op` is `None`), so the model never sees them;
`derive_messages()` still returns only real messages.

Because the step rereads the log, the other Mechanisms compose for free.
Compact between turns (section 03) and the next `request/header` records a
smaller number: the step derived the summary view, because that is what the
log projects now. Nothing told the loop about the compaction. Nothing had to.

The same move pays off when things go wrong. A model call that dies mid-step
leaves `step/start`, a `request/header`, and some orphan chunks, then nothing.
No repair step is needed: chunks are log-only, so the next derivation is
already clean, and the Offline check kills a model mid-stream to prove it.

And it pays off at resume. The Agent carries a session, a Model seam
callable, and a `status` flag that only means "mid-turn right now". Replay
the log into a fresh session, hand it to a brand-new Agent, and the next
turn appends byte-for-byte the same rows the original would have.

One honest caveat: this section re-derives history, but the "re-assemble the
prompt" half of the design question is still ahead. The mini's request is
just the derived messages until section 08 builds the system prompt.

### What changed

Compared with section 03:

- `kernel.py`, `message.py`, `session_log.py`, and `standin.py` are carried
  forward verbatim; `agent_loop.py` is the only new source file, so the diff
  against 03 is this section's Mechanism, nothing else.
- The hand-cranked `stream_turn()` helper from 03's check is gone. The loop
  is now real code under test, and the check drives it through `send()`.
- The while-step loop runs exactly once per turn today, because with no tools
  every step ends `"completed"`. The loop shape and the end reason are the
  socket section 05 plugs into.
- This is the first model-touching Section, so `demo.py` appears: the same
  loop, with the real Anthropic API plugged into the Model seam (ADR 0001).

---

## In real dsh

All pointers are into the pinned Studied version,
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca).
The loop lives in
[`packages/core/agent-loop`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop),
behind the registry in
[`packages/core/agent`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent).

| Mini-dsh | Real dsh | Notes |
| --- | --- | --- |
| `Agent.send()` and `_step()` | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts): `ReactLoopAgent` | The real driver runs `kick` -> `turn()` -> `preStep()` -> `step()` -> `buildRequest()`; each step re-derives messages from the log and re-assembles the prompt. |
| `AgentRegistry`, the `agents` service | [`packages/core/agent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/index.ts): `AgentRegistry` | `ctx.agents` holds opaque `Agent` handles; a swappable factory (`setFactory()`), registered by `dsh-agent-loop`, builds the concrete driver. |
| `status`: `"idle"` or `"running"` | [`packages/core/agent/src/runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts): `AgentStatus` | The same two states, on a much wider `Agent` seam interface (`cancel`, `send`, `followup`, `steer`, `inject`). |
| `turn/start`, `step/start`, `step/end`, `turn/end`, `request/header` rows | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts) | The durable turn/step vocabulary is session events appended by the driver, exactly as here; the `agent/*` bus carries only lifecycle, inbox, and interception points. |
| the Model seam call in `_step()` | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts): `ctx.llm.prepareCall()` | Real requests go through the llm capability seam and stream back chunk by chunk; the seam itself is section 10's Mechanism. |

What the real agent loop adds on top of this section's Mechanism:

- **A much richer step.** Before streaming, a real step claims the inbox,
  assembles the system prompt, projects runtime context, and runs the
  `agent/pre-step` and `agent/request` waterfalls. The mini's step is derive
  plus stream; sections 05 to 09 fill the rest in.
- **More ways for a step to end.** A real step ends `completed` (no tool
  calls), `max-tokens` (sticky), or `null` (tools ran, go around again), and
  the turn closes only when an end reason exists and `inbox.nextStep` is
  empty after the `agent/turn-stopping` re-check. A tool result marked
  `concludesTurn` ends the turn early. The mini has one arm until section 05.
- **A swappable driver.** `Agent` is a seam interface and `ReactLoopAgent` is
  package-internal, reached only through the factory, so the whole loop can
  be replaced without touching anything that holds an agent handle.
- **Lifecycle on the bus.** `agent/created`, `agent/disposed`,
  `agent/status`, and the inbox events let live observers follow along, and a
  cancellation token threads through everything. The mini's durable markers
  carry the story; cancellation arrives with the scheduler in section 06.

---

## Failure modes

- **A cached message list is a second copy of the truth.** Hold history in a
  live list and every other Mechanism becomes a sync problem: compaction
  edits the surface behind it, replay rebuilds sessions without it. Deriving
  from the log each step means there is nothing to keep in sync, ever.
- **A crash mid-step needs no repair.** A step that dies leaves `step/start`
  with no `step/end` and maybe orphan chunks. Because chunks are log-only,
  the next derivation is already clean; the check streams one chunk, kills
  the model, and shows the following turn sending exactly the right history.
- **A turn is not one model call.** Hard-wire "send, reply, done" and there
  is nowhere for tool execution to loop back into. The while-step shape with
  an explicit end reason is what lets section 05 add tools without touching
  the turn.
- **Without `request/header`, "the model saw X" is a guess.** The header row
  pins what each step sent into the log itself. The check compacts between
  turns and reads the counts straight off the log: 1, then 3, then 2 after
  compaction. No stand-in internals, just the record.
- **Two turns on one log would interleave the story.** A `send()` during a
  running turn raises instead of weaving two sets of turn/step markers into
  one sequence. Real dsh queues that message in the inbox and claims it at a
  step boundary; that is section 07's Mechanism.
- **Forgetting the markers makes replay ambiguous.** Without `turn/start` and
  `step/end` rows, a replayer cannot tell a finished turn from one that
  crashed halfway. The boundaries are data, not printf debugging: they are
  what makes the log a story instead of a pile of messages.

---

## Runnable

[`src/`](src/) carries 03 forward and adds:

- [`agent_loop.py`](src/agent_loop.py) (new): `Agent` with `send()` and
  `_step()`, `AgentRegistry`, and the plugin providing the `agents` service.
- [`test.py`](src/test.py): the full turn story lands in the log in order,
  `request/header` counts prove re-derivation across a compaction (1, 3, 2),
  a replayed log plus a new Agent continues byte-for-byte, a crash mid-step
  leaves a clean next derivation, and a mid-turn `send()` refuses.
- [`demo.py`](src/demo.py) (new): the first Live demo. The same loop with the
  real Anthropic API plugged into the Model seam, scripted turns with a
  compaction in the middle, and the log's own story printed at the end. The
  SDK and the mini-Message translation live only here (ADR 0001).

```bash
python sections/04-agent-loop/src/test.py   # offline check, no key
```

The Live demo needs the root `requirements.txt` and a key; it skips politely
without one:

```bash
pip install -r requirements.txt             # anthropic + python-dotenv
cp .env.example .env                        # then set ANTHROPIC_API_KEY
python sections/04-agent-loop/src/demo.py
```

---

## Sources

- [`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/core.md):
  dsh's own doc for the agent and agent-loop packages.
- [`docs/agent-lifecycle.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/agent-lifecycle.md):
  the turn and step lifecycle, from kick to turn end.
