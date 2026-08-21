# 00 · Setup

English | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

> A model is just a callable that streams chunks and ends with a message.
> Anything speaking that contract can sit behind the seam, including a script.

This tutorial rebuilds a harness whose every mechanism orbits a model call:
history is derived for the model, tools are called by the model, prompts are
assembled for the model. And each of the 14 sections ends with a runnable
check that must prove its Mechanism works.

The obvious setup puts a real API behind those checks. Ask a live model,
assert on the answer.

But a live model needs a key, a network, and money, and it returns different
bytes for the same input. A failing assertion could mean broken code or a
moody model, and a check that can fail for two reasons proves nothing when it
passes. Worse, the flakiness lands in the wrong place: the thing under study
is the harness around the model, never the model itself.

So: why must every section's check run offline against a stand-in?

Because a check exists to prove this section's Mechanism, and the model is
the one moving part whose behavior the Mechanism does not own. Pin it, and
every check becomes deterministic: no key, no network, same bytes every run.
For that to hold, section 00 must:

1. Give mini-dsh its own **Message shape**, provider-agnostic like real dsh,
   so no vendor wire format ever leaks into the core.
2. Fix the **Model seam**: a plain callable that takes the message list and
   streams chunk events, then exactly one final message.
3. Ship a **Scripted stand-in** speaking that contract: an ordered queue of
   canned responses that never inspects the request.
4. Chunk each response deterministically, so streaming is real from day one
   and byte-identical on every run.

---

## Mechanism

Three moving parts, one per file:

- **`Message`** (`message.py`): the shape every model exchange uses, a frozen
  dataclass of `role` and `content`.
- **The Model seam**: not a class, a calling convention. `model(messages)`
  yields `("chunk", str)` events, then one `("message", Message)`.
- **`ScriptedModel`** (`standin.py`): the offline seam implementation, a
  queue of canned responses.

The Message shape is the whole vocabulary:

```python
@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
```

Frozen, because a message is a record of what was said, not a mutable draft.
Provider-agnostic, because the core must not care whose model answers;
translating this shape to a vendor's wire format is an adapter's job, and no
adapter lives in the core.

The stand-in is the seam's first implementation, and it is deliberately
passive:

```python
class ScriptedModel:
    def __init__(self, responses):
        self._queue = list(responses)

    def __call__(self, messages):
        """The Model seam: yields ("chunk", str)... then ("message", Message)."""
        text = self._queue.pop(0)
        for piece in _chunks(text):
            yield ("chunk", piece)
        yield ("message", Message(role="assistant", content=text))
```

`messages` arrives and is never read. The stand-in answers from its script,
in order, no matter what was asked. A stub that matched on the request would
grow rules, and rules grow into a second model that itself needs testing
(ADR 0001 rejected exactly that). An ordered queue keeps the whole script
visible in the check that wrote it: response one answers call one, always.

Each response streams as a few fixed-size chunks before the final message:

```python
def _chunks(text, n=3):
    size = max(1, -(-len(text) // n))
    return [text[i : i + size] for i in range(0, len(text), size)]
```

One call through the seam, end to end:

```text
check                                  ScriptedModel(["Hello, reader."])
  │
  │  model([Message("user", "hi")])
  ├──────────────────────────────────►  pop the next canned response
  │                                     (the request is never read)
  │   ("chunk", "Hello")   ◄──┐
  │   ("chunk", ", rea")   ◄──┼─────── split into fixed-size chunks
  │   ("chunk", "der.")    ◄──┘
  │   ("message", Message("assistant", "Hello, reader."))
  │◄──────────────────────────────────
```

The two phases matter more than the stand-in does. The chunks are the live
stream; the final `Message` is the durable record, and it always restates the
full text. Section 02's log will store them as different event types, and
section 04's loop will forward both without buffering. Because the stand-in
streams from day one, no later section meets streaming for the first time
against a live API.

### What changed

Section 00 has no predecessor, so this slot records the starting state every
later section inherits:

- `src/` is born: `message.py` and `standin.py` are the source, `test.py`
  the check.
- The carry-forward rule starts here. Section 01 copies this `src/` verbatim
  and adds only its kernel, so the diff between adjacent sections is exactly
  one Mechanism, nothing else.
- Nothing here knows about plugins, logs, or agents. The seam is a calling
  convention waiting for its callers.

---

## In real dsh

All pointers are into the pinned Studied version,
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca).
The Model seam's real home is
[`packages/llm`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm).

| Mini-dsh | Real dsh | Notes |
| --- | --- | --- |
| `Message` | [`packages/llm/llm/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/types.ts) | The llm seam owns the vocabulary types, provider-agnostic like ours; `ToolSchema` (line 333) sits in this file, which is how tools later describe themselves to the model. Mini-dsh's whole vocabulary is one dataclass. |
| the Model seam contract | [`packages/llm/llm/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/index.ts): `LlmAdapter` (line 180) | The seam is a stream there too: `stream(options)` returns an `AsyncIterable<StreamChunk>`. The mini's chunks-then-message convention is the same idea with the final message made explicit. |
| `ScriptedModel` behind the seam | [`packages/llm/llm/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/index.ts): `LlmRuntime`, `ctx.llm` (line 284) | Adapters register through `ctx.llm.registerAdapter(providers, adapter)` and swap without the caller noticing. The stand-in is mini-dsh's first adapter. |
| the check calling `model(messages)` | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts) | The real consumer is the loop: `ctx.llm.prepareCall()` then `preparedCall.stream(request)` (lines 345, 449). Section 04 gives the mini the same caller. |
| chunks, then one final message | [`packages/core/session/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/types.ts) (line 236) | The stream's two phases become the session event types `assistant/chunk` and `assistant/message` once the log exists (section 02). |

What the real llm seam adds on top of this section's Mechanism:

- **An adapter registry with routing.** `ctx.llm` holds plural adapters
  keyed by provider name, and choosing a deployment's default model is its
  own plugin
  ([`packages/core/agent-default-model`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-default-model),
  `ctx.agentDefaultModel`). The mini has one callable at a time until
  section 10 gives the seam a service home.
- **Middleware on the stream.** An `llm/stream` waterfall (`index.ts` lines
  51 to 60) lets plugins wrap or observe every model call, and retries
  surface in the log as `llm/retry` session events.
- **Real wire adapters.** Shipped providers
  [`llm-deepseek`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-deepseek/src/index.ts)
  and
  [`llm-pi-ai`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-pi-ai/src/index.ts)
  speak vendor protocols. Ceiling: mini-dsh never rebuilds a wire adapter;
  its only real-API code is the Live demo's ~20-line Anthropic translation
  in `demo.py` (sections 04 and later), outside the offline core (ADR 0001).
- **A fold, not a triple.** Real dsh usually splits a capability three ways:
  a package defining the interface, packages providing it, packages
  consuming it. The llm seam folds definition and consumer into one package
  because its consumer is the agent loop itself, not a swappable tool
  surface. Section 10 rebuilds the seam and that folding rule.

---

## Failure modes

- **A live model makes every check a coin flip.** Same input, different
  bytes, so assertions either go vague (`"contains a word"`) or flake. The
  stand-in returns byte-identical output every run, so checks can assert
  exact content and mean it.
- **A stand-in that reads the request becomes a second model.** Matching
  rules accumulate, rules interact, and soon the fixture is clever enough to
  be wrong. The queue's contract is dumb on purpose: response one answers
  call one, and the script sits in plain sight inside the check.
- **A one-blob stand-in postpones streaming.** Yield only the final message
  and chunk handling first runs in section 04, against a live API, where
  failures are unreproducible. Deterministic chunking makes the stream real
  from the first check onward.
- **Assertions on the stand-in's internals check the scaffolding.** Reaching
  into `_queue` couples checks to a fixture that real adapters do not have.
  The rule, kept through all 14 sections: assert on what crosses the seam,
  and once the log exists, on the log, never on the stand-in.
- **A script that runs out must fail loudly.** One model call too many pops
  an empty queue and raises, so an over-asking check fails instead of
  silently recycling an answer that happens to pass.

---

## Runnable

[`src/`](src/) starts the carry-forward chain; every file is new:

- [`message.py`](src/message.py): the frozen `Message` dataclass.
- [`standin.py`](src/standin.py): `ScriptedModel` and its deterministic
  chunker.
- [`test.py`](src/test.py): the seam contract holds: chunks concatenate to
  exactly the final message's content, streaming is more than one blob, and
  the queue answers in order.

```bash
python sections/00-setup/src/test.py   # offline check, no key
```

The Model seam exists here, but no Mechanism drives it yet, so there is no
`demo.py`. The first Live demo lands with the agent loop in section 04.

---

## Sources

- [`docs/adr/0001-scripted-offline-model-live-anthropic-demos.md`](../../docs/adr/0001-scripted-offline-model-live-anthropic-demos.md):
  the local ADR that decided the scripted offline stand-in, the Live-demo
  split, and the options rejected on the way.
- [learn-agent-memory](https://github.com/hardness1020/learn-agent-memory):
  the tutorial family whose offline, keyless, deterministic check convention
  this section adopts.
