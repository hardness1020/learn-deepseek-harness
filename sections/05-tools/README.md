# 05 · Tools

English | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

> A tool call is a question already written in the log. Throwing instead of
> answering leaves the model staring at a question nobody replied to, so
> every call gets an answer.

Section 04's loop can only talk. Every step ends `"completed"` because the
model has nothing to do but reply. Tools change that: the model asks
mini-dsh to run something and needs the outcome back before it can go on.

The obvious build is a dict of functions. Look up the name, call it, append
what it returns. If the name is unknown, raise. If the arguments are bad,
raise. If a policy says no, raise before calling.

But every one of those raises lands in the middle of a turn. The assistant
message carrying the call is already in the log; the exception unwinds
`send()` and leaves a question with no answer. The next derivation shows the
model a transcript that ends mid-sentence, and replay rebuilds the same
broken story.

So: why does a denied or errored call still produce a normal `tool/result`?

Because the transcript is the contract. The model must see a turn-consistent
history, and replay must reconstruct it, so every call gets an answer row no
matter how it went. For that to hold, the tools layer must:

1. Keep tools in a **scoped registry**: one global layer plus one layer per
   agent scope, where a scope's layer shadows global names and every
   restriction that applies to the scope intersects what stays visible.
2. Run every call through one fixed pipeline: **pre -> ask -> guard ->
   execute -> post**, in that order, always.
3. Let every stage refuse, but let nothing raise across the boundary: a
   denial, a crash, bad args, or an unknown name all leave as the same
   result shape, `{call_id, name, is_error, content}`.
4. Fail closed at the ask gate: pre votes (`allow` / `ask` / `deny`) only
   tighten, and an `ask` with no approver is a deny.
5. Thread results back through the loop: `tool/call` appended log-only
   before dispatch, `tool/result` appended to the surface after, and a step
   whose reply carried calls ends with reason `None`: go around again.
6. Hand back an undo for every registration, because everything is a plugin,
   and every registration is reversible.

---

## Mechanism

One new file, `tools.py`, and the tool thread pulled through the files that
already existed:

- **`ToolDefinition`**: what the model sees (name, description, params) plus
  the body, `execute(args) -> content`.
- **`ToolRegistry`**: the `tools` service. Layers keyed by scope, restriction
  entries, hook lists, and the pipeline in `execute()`. Every `register` /
  `restrict` / `pre` / `guard` / `post` returns its undo.
- **`ToolScope`**: one agent's view of the registry, its own layer over the
  global one. The Agent holds this, never the registry.
- **The loop's new arm**: `_step()` now offers the schemas with the request,
  runs the reply's calls, and reports `None` when the turn must go around.

The pipeline is one funnel. Every stage can turn a call away, and every exit
leaves through the same door:

```text
call {"id", "name", "args"}
  │ resolve   unknown name ────────────────────────┐
  │ pre       votes tighten: allow < ask < deny ───┤
  │ ask       no approver, no approval ────────────┤
  │ guard     deny-only reasons ───────────────────┤
  │ execute   bad args, or the body raises ────────┤
  ▼                                                ▼
{"is_error": false, "content"}      {"is_error": true, "content"}
        └───────────────┬──────────────────────────┘
                      post (review, may replace)
                        ▼
             one shape, appended as tool/result
```

In code, the funnel is a straight line of early returns:

```python
def _run(self, call, scope):
    name = call.get("name")
    tool = self._visible(scope).get(name)
    if tool is None:
        return self._result(call, True, f"unknown tool '{name}'")
    decision = "allow"
    for hook in list(self._pre):
        vote = hook(call)
        if vote is not None and _RANK[vote] > _RANK[decision]:
            decision = vote  # votes only tighten, never loosen
    if decision == "deny":
        return self._result(call, True, "denied before execution")
    if decision == "ask":
        approved = self.asker is not None and self.asker(call)
        if not approved:
            return self._result(call, True, "approval was asked and not given")
    for check in list(self._guards):
        reason = check(call)
        if reason:
            return self._result(call, True, f"denied: {reason}")
    ...
    try:
        return self._result(call, False, str(tool.execute(args)))
    except Exception as exc:  # the body may fail; the pipeline may not
        return self._result(call, True, f"{type(exc).__name__}: {exc}")
```

The loop plugs the funnel into section 04's step. This is the `None` arm the
agent loop left as a socket:

```python
if not final.tool_calls:
    self.session.append("step/end", {"reason": "completed"})
    return "completed"
for call in final.tool_calls:
    self.session.append("tool/call", call)  # log-only: before dispatch
    result = self.tools.execute(call)
    self.session.append("tool/result", result)  # joins the surface
self.session.append("step/end", {"reason": None})
return None  # tool calls ran: go around again
```

Here is a turn where the model calls a tool, as the log records it:

```text
send("what is the wifi password?")
  │   0  user/message
  │   1  turn/start
  ├─ step ────────────────────────────────────────────────
  │   2  step/start
  │   3  request/header    {"messages": 1, "tools": ["lookup"]}
  │   4  assistant/chunk   x 3
  │   7  assistant/message {"content": "Checking.", "tool_calls": [c1]}
  │   8  tool/call         c1: lookup {"key": "wifi"}     ◄ log-only
  │   9  tool/result       {"call_id": "c1", "is_error": false,
  │                         "content": "hunter2"}          ◄ surface
  │  10  step/end          {"reason": null}
  ├─ reason is None ► go around
  │  11  step/start
  │  12  request/header    {"messages": 3, "tools": ["lookup"]}
  │      ...
  │  17  step/end          {"reason": "completed"}
  │  18  turn/end
```

The result joined the surface, so the second derivation is `user`,
`assistant` (carrying its calls), `tool`: the model reads its own call and
the answer as ordinary history. Section 02 quietly prepared for this:
`tool/result` has been in `SURFACE_TYPES` since the surface existed.

Now rerun that turn with a guard that denies, a body that crashes, or a name
that does not exist. The log records exactly the same shape of story; only
`is_error` and `content` differ. The turn survives, the model reads what
went wrong, and the Offline check drives all four failures through one step
to prove no exception ever escapes `send()`.

Scoping is the other half of the Mechanism. `request/header` now records
which tools each request offered, so the log itself shows what a scope saw:
a scope layer shadowing a global name, and a restriction narrowing agent b
to `["where"]` while agent a still sees everything. A restricted tool is not
"denied", it simply does not exist for that scope, and calling it anyway
comes back as `unknown tool`, a normal result like every other.

### What changed

Compared with section 04:

- `kernel.py` is carried forward verbatim. `tools.py` is the only new source
  file; the other changes are the tool thread pulled through existing files,
  so the diff against 04 is this section's Mechanism, nothing else.
- `message.py`: `Message` gains `tool_calls` (assistant) and `call_id`
  (tool), both defaulted, so every section 04 Message still reads the same.
- `standin.py`: the Model seam gains a `tools` argument the Scripted
  stand-in ignores, and a canned response may be a dict with `tool_calls`,
  so tool-using turns are scriptable offline.
- `session_log.py`: `derive_messages()` thaws `tool_calls` and `call_id`
  from frozen payloads back onto Messages. `SURFACE_TYPES` is untouched.
- `agent_loop.py`: the Agent now takes its `ToolScope` alongside the session
  and the Model seam; the step offers schemas with the request, records them
  in `request/header`, runs calls through the pipeline, and fills in the
  `reason None` arm section 04 left as a socket.
- `demo.py`: the Live demo now does real tool use, including a guard denial
  the model has to read and explain.

---

## In real dsh

All pointers are into the pinned Studied version,
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca).
The tools layer lives in
[`packages/core/tools`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools),
with scoping in
[`packages/core/scope`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/scope).

| Mini-dsh | Real dsh | Notes |
| --- | --- | --- |
| `ToolRegistry` + `ToolScope` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts): `ToolRuntime`; [`packages/core/scope/src/store.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/scope/src/store.ts): `ScopedLayers` | `ctx.tools` is a `ScopedLayers`-backed registry: a global layer plus per-agent scope layers with name shadowing and intersecting restrictions, via `register` / `restrict`. |
| `ToolDefinition` | [`packages/core/tools/src/schema.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/schema.ts): `defineTool()` | `ToolDefinition extends ToolSchema` (the schema type lives in [`packages/llm/llm/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/types.ts)) and adds typed args, an output `{schema, render}`, `timeoutMs`, `isConcurrencySafe`, `finalizeContent`. |
| `pre()` votes | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts): `tools/pre-execute` | A waterfall event producing `PreToolDecision = allow \| deny \| ask`; the approval `ask` is answered by policy plugins and, above the Ceiling, the UI. |
| `guard()` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts): `ToolGuard` | `(execution) => string \| undefined`, deny-only and synchronous, applied inside the pipeline after approval. Distinct from the `packages/guard/*` plugins, which are ordinary event listeners. |
| `post()` review | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts): `tools/post-execute` | A waterfall producing `PostToolDecision = accept \| block`, plus enrichment (the repeat-tool reminder rides here). |
| the result dict | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts): `ToolExecutionSuccess` / `ToolExecutionFailure` | The same split, `isError: false \| true`, frozen before it becomes a `tools/result` event. |
| the loop's serial for-loop over calls | [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/tool-calls.ts): `executeToolCalls` | The real loop never calls `ctx.tools.execute()` directly; a 4-stage scheduler drives the calls. That scheduler is section 06's Mechanism. |

What the real tools layer adds on top of this section's Mechanism:

- **An around-waterfall for execution.** `tools/execute` wraps the body, so
  plugins can time-box it: the timeout policy
  ([`packages/guard/timeout-policy`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/guard/timeout-policy))
  defines `TOOL_TIMEOUT` itself and wraps cooperatively, never abandoning
  the tool's promise. The mini runs the body directly.
- **Typed schemas end to end.** `defineTool()` validates args against a real
  schema and validates the output too; `finalizeContent` shapes what the
  model reads. The mini validates only that param names match.
- **Parallel dispatch.** `executeToolCalls` runs a `prepare / dispatch /
  finalize / finish` scheduler: parallel-safe calls overlap, exclusive calls
  form barriers, and aborted-unstarted calls get synthetic results
  (`TOOL_ABORTED_BEFORE_DISPATCH`) so replay stays valid. All of that is
  section 06.
- **More result powers.** A result can carry `concludesTurn` to end the turn
  early, and `tools/result` events record `sourceEventSeqs`; the runtime
  also emits `tools/change` when the visible set moves.
- **A real answerer for `ask`.** The approval prompt a human sees is UI,
  which sits above the Ceiling; the mini keeps the seam as one `asker`
  callable and the Offline check answers it in code.

---

## Failure modes

- **A raised denial tears the transcript.** The assistant message carrying
  the call is already in the log when the pipeline says no. Raise instead of
  answering and the derived history ends with a question the model never
  hears back on; replay rebuilds the same hole. The result row is the
  answer, whatever the verdict was.
- **A silent skip teaches the model nothing.** Drop a denied call on the
  floor and the model waits forever or reissues it forever. `is_error` plus
  a reason is information: the check's model reads four different failures
  in one step and still finishes the turn.
- **An ask with no approver must deny.** Defaulting to allow would make an
  unconfigured mini-dsh the most permissive one. The gate fails closed,
  and the check proves the same call runs once someone answers.
- **Guards that could approve would fight.** Deny-only guards are monotonic:
  any guard can only shrink what runs, so their order never matters. A guard
  that could allow would override another's deny depending on registration
  order.
- **The body is the untrusted part.** A tool that raises is a normal event,
  caught and returned as a result. The pipeline itself must not raise, which
  is why bad args and unknown names are checked into results too, not
  asserted.
- **An irreversible registration outlives its plugin.** Every `register` /
  `restrict` / `guard` hands back its undo for the fiber to collect. The
  check unloads a tool plugin mid-conversation: the next `request/header`
  offers nothing, and calling the gone tool is just another normal result.
- **Without intersection, scopes only grow.** Shadowing can only add or
  replace; restrictions are what narrow. Intersecting every applicable
  restriction means any layer can fence a scope in, which is what section 12
  leans on when subagents get a subset of the parent's tools.

---

## Runnable

[`src/`](src/) carries 04 forward and adds:

- [`tools.py`](src/tools.py) (new): `ToolDefinition`, `ToolRegistry` with
  the pre/ask/guard/execute/post pipeline, `ToolScope`, and the plugin
  providing the `tools` service.
- [`agent_loop.py`](src/agent_loop.py): the step offers tool schemas with
  the request, appends `tool/call` and `tool/result` rows, and ends with
  reason `None` when the turn must go around.
- [`message.py`](src/message.py), [`standin.py`](src/standin.py),
  [`session_log.py`](src/session_log.py): the tool thread, as listed under
  What changed.
- [`test.py`](src/test.py): a tool turn goes around and lands the full story
  in order, four failure shapes come back as four normal results, the ask
  gate fails closed and tightens over loose votes, post review rewrites a
  result, scope shadowing and restriction show up in `request/header`, and
  unloading a tool plugin reverses its registration mid-conversation.
- [`demo.py`](src/demo.py): the Live demo does real tool use. The model
  reads a note through the pipeline, then hits a guard denial and reports
  what the tool told it, with the log's own story printed at the end.

```bash
python sections/05-tools/src/test.py        # offline check, no key
```

The Live demo needs the root `requirements.txt` and a key; it skips politely
without one:

```bash
pip install -r requirements.txt             # anthropic + python-dotenv
cp .env.example .env                        # then set ANTHROPIC_API_KEY
python sections/05-tools/src/demo.py
```

---

## Sources

- [`docs/subsystems/tools.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/tools.md):
  dsh's own doc for the tool runtime.
- [`docs/tool-execution-pipeline.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/tool-execution-pipeline.md):
  the fixed pipeline, stage by stage.
- [`docs/subsystems/scope.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/scope.md):
  scoped layers, shadowing, and restrictions.
