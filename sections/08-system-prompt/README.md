# 08 · System prompt

> The system text is a promise: the same words, in the same order,
> every single step. Anything that cannot keep that promise rides
> along as a message.

Section 07's request is honest but bare. `_step()` pulls schemas
straight off the tool registry and ships no system text at all: the
model is never told who it is, how it should behave, or what the
world looks like right now.

The missing text has many owners. Mini-dsh owns its identity line; a
persona plugin owns tone; the tool layer owns the schema list. Each wants to contribute its piece without coordinating with
the others, and every piece must land in the same place in every
request.

And some state is dynamic. A clock, a working directory: the model
needs the current reading, but bake it into the system text and no
two steps ship the same prompt. Providers cache on a stable prompt
prefix, so a timestamp in the system text buys a cache miss on every
step.

The other obvious build is worse: patch dynamic text into the
request out-of-band, and it never lands in the log. Replay could not
rebuild what the model actually saw, which is the whole point of
section 02.

So: why is dynamic state a re-emitted user message rather than
system text?

Because the system text must hold still and the log must stay the
whole story. For that, assembly must:

1. Keep one registry with four provider kinds: sections (static
   system text), contexts (dynamic state), variables (`{{name}}`
   values), and tool-schema providers. Every registration hands back
   its undo.
2. Render deterministically: one numeric order per entry,
   registration order breaking ties, so identical registrations
   always produce identical text.
3. Interpolate strictly: a `{{name}}` whose variable is unknown or
   unset refuses to ship the request, instead of shipping a hole.
4. Produce three artifacts from one assembly: the system text, the
   request's tool list, and a runtime-context snapshot.
5. Deliver the snapshot as a `user/message` row, re-emitted only
   when it changed. The retained snapshot is the last snapshot row
   in the log itself, never separate state.
6. Assemble per step, at the boundary, in the same place history is
   re-derived.

---

## Mechanism

One new file, `system_prompt.py`, and the request assembly rerouted
through it:

- **`SystemPrompt`**: the registry. `section()`, `context()`,
  `variable()`, and `tools()` file providers; each returns its undo,
  kernel-style. A built-in `harness:identity` section sits at order
  -100, so plugin text lands after it by default.
- **`assemble(assemble_context)`**: resolves every provider in
  `(order, registration)` order and returns the three artifacts:
  `system`, `tools`, and `runtime_context`.
- **The bridge**: the plugin registers one tool-schema provider that
  reads the agent's scoped view out of the assemble context, so the
  request's tool list is a prompt output too.
- **`latest_snapshot(session)`**: the dedupe. The retained snapshot
  is a projection of the log, the last `user/message` row whose
  payload carries `"kind": "runtime-context"`.

```python
def assemble(self, assemble_context):
    """Resolve every provider, in order: the request's three artifacts."""
    sections = [self._render(e["text"], assemble_context) for e in _ordered(self._sections)]
    contexts = [e["provider"](assemble_context) for e in _ordered(self._contexts)]
    return {
        "system": "\n\n".join(text for text in sections if text),
        "tools": [s for provider in self._tools for s in provider(assemble_context)],
        "runtime_context": "\n".join(text for text in contexts if text),
    }
```

Inside `_step()`, assembly happens right after the inbox claim, at
the boundary where section 04 already re-derives everything. The
snapshot enters the log only when it differs from the last snapshot
row, and the Model seam gains a third value:

```python
assembly = self.prompt.assemble({"tools": self.tools})
snapshot = assembly["runtime_context"]
if snapshot and snapshot != latest_snapshot(self.session):
    self.session.append("user/message", {"content": snapshot, "kind": "runtime-context"})
messages = self.session.derive_messages()  # re-derived, never cached
```

Providers render on one side; only the changed snapshot crosses into
the log:

```text
registered, ordered              assemble({"tools": scope}), every step

sections  -100 harness:identity ─┐
             0 persona           ├─► system text ────► request, byte-identical
variables  {{user}} = "Ada"     ─┘                     every step
tool providers  the bridge ──────► tool list ────────► request
contexts     0 time: 10:01      ─┐
            10 cwd: /home/ada    ├─► snapshot ─► same as the last snapshot
                                 ┘               row in the log?
                                                 ├─ yes: nothing appended
                                                 └─ no:  user/message row,
                                                         "kind": "runtime-context"
```

Here is a real run, as the log records it. A `tick` tool moves a
fake clock mid-turn; the system text is 61 chars in both requests,
byte for byte the same, while the snapshot re-emits once:

```text
send("go")
  │   0  turn/start
  │   1  step/start
  │   2  user/message   "go"                  ◄ claimed at the boundary
  │   3  user/message   "time: 10:00"         ◄ snapshot, first reading
  │   4  request/header system 61 chars, tools ["tick"]
  │   5  assistant/message {"tool_calls": [tick]}
  │   6  tool/call     tick
  │   7  tool/result   "ticked"               ◄ the clock now says 10:01
  │   8  step/end      {"reason": null}
  │   9  step/start
  │  10  user/message   "time: 10:01"         ◄ changed: re-emitted
  │  11  request/header system 61 chars, tools ["tick"]
  │  12  assistant/chunk "do"
  │  13  assistant/chunk "ne"
  │  14  assistant/message "done"
  │  15  step/end      {"reason": "completed"}
  │  16  turn/end
```

Had the clock held still, seq 10 would not exist: the second step
would have found the snapshot equal to the last snapshot row and
appended nothing. Both readings the model ever saw are ordinary
`user` rows in derived history, durable and replayable.

### What changed

Compared with section 07:

- `inbox.py`, `kernel.py`, `message.py`, `scheduler.py`,
  `session_log.py`, `tools.py` are carried forward verbatim.
  `system_prompt.py` is the only new source file; the other changes
  are the assembly pulled through `agent_loop.py`, so the diff
  against 07 is this section's Mechanism, nothing else.
- `agent_loop.py`: `Agent` and `AgentRegistry.create()` gain a
  `prompt` parameter. `_step()` assembles per step, appends the
  snapshot row when it changed, takes the tool list from the
  assembly instead of the registry, and passes the system text
  through the Model seam.
- `standin.py`: the Model seam's signature gains `system=""`, one
  line. The Scripted stand-in stays passive: it never inspects the
  request, system text included.
- The log's shape changed: `request/header` now records the
  assembled system text, and a `user/message` payload may carry
  `"kind": "runtime-context"` to mark a snapshot row. Derived
  history treats both kinds as plain `user` messages.
- `demo.py`: the Live demo registers a persona section, a real clock
  and the cwd as contexts, and a tool slow enough for the clock to
  move mid-turn, so the re-emit happens on a real model call.

---

## In real dsh

All pointers are into the pinned Studied version,
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca).
The registry lives in the core system-prompt package; the snapshot
dedupe lives in the loop:
[`packages/core/system-prompt`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt).

| Mini-dsh | Real dsh | Notes |
| --- | --- | --- |
| `SystemPrompt` in `system_prompt.py` | [`packages/core/system-prompt/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts): `SystemPrompt` | The same four provider kinds behind `section() / context() / variable() / tools()`, each returning a Cordis effect disposer, the real form of the mini's undo callables. |
| `assemble()` returning three artifacts | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts): `PromptAssembly`, `renderPrompt` | Assembly resolves into a `PromptAssembly`, passes through the `system-prompt/assemble` waterfall, then renders the `system` string, the request's tool list, and the runtime-context snapshot. |
| `order=-100` built-in identity | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts): `'harness:identity'` | The built-in identity section sits at order -100, the exported `PERSONA_SECTION` at 0, tool guidance in 100-199. Ordering is a single numeric `order` field, not a phase enum. |
| `{{name}}` strict interpolation | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts) | Strict `{{variable}}` interpolation: an unknown name or an undefined value throws, exactly the mini's refuse-to-ship rule. |
| `latest_snapshot(session)` | [`packages/core/agent-loop/src/runtime-context.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/runtime-context.ts): `RuntimeContextProjection` | The retained snapshot is a projection; the snapshot is emitted as a `user/message` only when it differs from the retained one, never as system text. |
| assembly inside `_step()` | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts): `preStep` | Assembly happens per step inside `preStep`, before the `agent/pre-step` hook, the same boundary the mini uses (line 230). |
| the bridge in `system_prompt_plugin` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts): `ctx.systemPrompt.tools(...)` | Tools register their schemas as one prompt provider (lines 832-836). The mini folds the bridge into the prompt plugin; real dsh registers it from the tools package's side. |
| the checks' time context | [`packages/context/time-context/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/context/time-context/src/index.ts) | A whole package family contributes context this way; `agent-instructions` delivers workspace instructions in the same plane. |

What the real system-prompt layer adds on top of this section's
Mechanism:

- **Events around assembly.** `system-prompt/assemble` is a
  scope-filtered waterfall that can rewrite the assembly in flight,
  and `system-prompt/change` announces registry changes. The mini
  assembles without hooks.
- **A deterministic tool-order rule.** Real rendering orders the
  request's tool list with an explicit `TOOL_ORDER_REST` constant;
  the mini relies on registration order.
- **A context plane outside the registry.** Most of
  `packages/context` bypasses `systemPrompt.context()`:
  `agent-instructions`, `time-context`, and `tmux-context` append
  `UserMessage`s from `agent/pre-step` listeners. The registry's own
  `context()` callers are the sandbox policy, the approval policy,
  and subagent delegation. Real sandbox confinement sits above the
  Ceiling; the mini's argv-rewrite stub arrives with the capability
  seams in section 10.
- **Sections that can wait.** A real `PromptSection` may declare
  `complete?`, letting assembly proceed while a slow provider fills
  in later. The mini's providers are synchronous.

---

## Failure modes

- **A clock in the system text buys a cache miss per step.**
  Providers cache on a stable prompt prefix, and the system text is
  the first thing in it. One timestamp regenerated per step and no
  request ever reuses the prefix. The section/context split keeps
  every changing byte out of the system text by construction.
- **Out-of-band request text disappears from the record.** State
  patched into the request without a log row leaves replay unable to
  rebuild what the model saw. The snapshot is a `user/message` row,
  ordinary derived history; even the system text is recorded on
  `request/header`, so the log stays the whole story.
- **Re-emitting an unchanged snapshot floods history.** Appending
  the reading every step grows every later request by one row for no
  information. The boundary compares against the last snapshot row
  and appends only on change.
- **A retained snapshot kept in memory drifts from the log.** After
  a resume the memory is empty, the log is not, and the first step
  re-emits a snapshot the model already saw. The mini derives the
  retained snapshot from the log itself, so dedupe and replay agree
  by construction.
- **Loose interpolation ships a prompt with a hole.** A `{{typo}}`
  that renders as literal braces reaches the model as nonsense.
  Strict interpolation raises instead, and the log shows the step
  stopping before `request/header`: no request ever shipped.
- **Unordered providers shuffle the text.** If rendering follows
  dict or timing order, identical registrations can produce
  different prompts on different runs, and the prefix cache misses
  again. One numeric order, ties broken by registration order,
  renders the same text every time.

---

## Runnable

[`src/`](src/) carries 07 forward and adds:

- [`system_prompt.py`](src/system_prompt.py) (new): `SystemPrompt`,
  four provider kinds with undo per registration; `assemble()`;
  `latest_snapshot()`; the plugin with the identity built-in and the
  tool-schema bridge.
- [`agent_loop.py`](src/agent_loop.py): `_step()` assembles per
  step, appends the snapshot row when it changed, and passes the
  system text through the Model seam; `Agent` and `create()` gain
  the `prompt` parameter.
- [`standin.py`](src/standin.py): the seam signature gains
  `system=""`; the Scripted stand-in still never inspects it.
- [`test.py`](src/test.py): the Offline check proves the three
  artifacts land in one request, the system text stays byte-identical
  while a mid-turn tick re-emits the snapshot, dedupe holds within
  and across turns, an unknown or unset `{{variable}}` stops the step
  before any request ships, and every registration undoes.
- [`demo.py`](src/demo.py): the Live demo assembles a persona over
  the identity built-in, snapshots a real clock and the cwd, and
  lets a slow tool force a mid-turn re-emit on a real model call.

```bash
python sections/08-system-prompt/src/test.py    # offline check, no key
```

The Live demo needs the root `requirements.txt` and a key; it skips
politely without one:

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/08-system-prompt/src/demo.py
```

---

## Sources

- [`docs/subsystems/system-prompt.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/system-prompt.md):
  dsh's own tour of the four provider kinds and the three rendered
  artifacts.
- [`packages/context/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/context/README.md):
  the context package family, and which of its members go through
  the registry versus the pre-step plane.
