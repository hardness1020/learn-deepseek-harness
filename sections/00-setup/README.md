# 00 · Setup

English | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

> Reach for a provider's SDK wherever an answer is needed and that provider's
> shape ends up in the prompt, the log, and the loop. Give the core one
> message shape and one swappable call, and the provider stays at the edge.

DeepSeek Harness (dsh) is a real agent harness: a large TypeScript codebase
in which tools, prompts, and whole subsystems are plugins mounted onto a
running kernel. This tutorial rebuilds a minimal version of it in stdlib
Python, one Mechanism per section.

Every one of those mechanisms orbits a single act: asking a model for a
response. History is derived for the model, tools are called by the model,
prompts are assembled for the model.

So the rebuild needs a way to ask, and the obvious way is to import a
provider's SDK and call it wherever an answer is needed.

That spreads the provider through the harness. Its request format reaches the
prompt builder, its response objects reach the log, its role names reach
compaction, and changing providers becomes an edit in every one of them.

The answer also arrives in pieces. A model writes its text as it goes, so a
caller that waits for one finished string can show nothing while it waits,
and the log has nothing to record until the end.

So: why does mini-dsh's core speak its own Message shape through a swappable
Model seam?

Because the harness's real subject is everything around the model call, and
none of that work should depend on whose model answers. One shape goes in,
one shape comes back, and the provider turns into a part you plug in. For
that to hold, section 00 must:

1. Give mini-dsh its own **Message shape**, provider-agnostic like real dsh,
   so no vendor wire format ever leaks into the core.
2. Fix the **Model seam**: a plain callable that takes the message list and
   streams chunk events, then exactly one final message.
3. Ship a **Scripted stand-in** speaking that contract, so the seam has a
   working implementation the moment it exists.
4. Chunk each response deterministically, so streaming is real from day one.

How the tutorial checks itself follows from that seam. The stand-in answers
from an ordered queue of canned responses and never reads the request, so
every section's check runs offline, without a key, and returns the same bytes
every time.

---

## Mechanism

Three moving parts, one per file:

- **`Message`** (`message.py`): the shape every model exchange uses, a frozen
  dataclass of `role` and `content`.
- **The Model seam**: not a class, a calling convention. `model(messages)`
  yields `("chunk", str)` events, then one `("message", Message)`.
- **`ScriptedModel`** (`standin.py`): the seam's first implementation, a
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

Three roles cover every exchange the harness has: what the user said, what
the model said, what a tool returned. Later sections add event types around
these messages rather than fields inside them.

The seam itself is a calling convention. Anything callable that takes a
message list and yields the two event kinds counts as a model, so an adapter
can be a function, a closure, or an object like the stand-in:

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
in order, no matter what was asked, and the whole script stays visible in the
check that wrote it: response one answers call one, always.

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
section 04's loop will forward both without buffering.

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
  in `demo.py` (sections 04 and later), outside the offline core.
- **A fold, not a triple.** Real dsh usually splits a capability three ways:
  a package defining the interface, packages providing it, packages
  consuming it. The llm seam folds definition and consumer into one package
  because its consumer is the agent loop itself, not a swappable tool
  surface. Section 10 rebuilds the seam and that folding rule.

---

## Failure modes

- **A vendor's response shape spreads.** Store what the provider returned
  and the log holds its JSON, compaction learns its role names, and the
  prompt builder is written against its request format. Changing providers
  then means editing all three. One Message shape confines the translation
  to an adapter.
- **A mutable message lets history be rewritten in place.** Sections 02 and
  03 treat a recorded message as a fact that happened, and let compaction
  shrink what the model sees only by going through the log. A message whose
  fields can be reassigned defeats both: the record and the view drift apart
  with no trace of the edit.
- **A seam that returns one finished string throws the stream away.** The
  caller has nothing to show while the model writes, section 02 has no chunk
  events to log, and a long answer looks like a hang. Chunks give the harness
  something to forward the moment the first bytes exist.
- **Chunks with no closing message push reassembly onto every caller.** The
  loop, the log, and every observer each concatenate their own copy, and each
  can get the joins subtly wrong. One final `("message", Message)` builds the
  durable record once, at the seam.
- **A seam defined as a base class drags the harness into every adapter.**
  Subclassing means a provider inherits whatever the harness's class already
  assumes, and a plain function or a closure that wraps another model no
  longer qualifies. A calling convention keeps the requirement at "yields
  these two event kinds", so swapping models is passing a different callable.

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

- [learn-agent-memory](https://github.com/hardness1020/learn-agent-memory):
  the tutorial family whose offline, keyless, deterministic check convention
  this section adopts.
