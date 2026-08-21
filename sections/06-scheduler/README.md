# 06 · Scheduler

English | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

> Running calls one at a time wastes real time. Letting them land in the
> log in whatever order they finish makes replay a race. So overlap the
> work, but fix the order.

Section 05 runs a reply's calls with a for-loop: one call, one answer,
next call. That was invisible while every reply carried one call. Real
replies batch: a model told to read three notes asks for all three at
once, and the serial loop turns three one-second reads into three
seconds of waiting.

The obvious build is to throw every call at a thread pool and append
each result as it lands. But now the log's order depends on thread
timing: run the same turn twice and get two different transcripts, so
replay stops reconstructing and starts racing. A write overlapping the
read that feeds it sees half a world. And when a turn is cancelled
mid-flight, the calls that never started are questions the assistant
message already asked: section 05's torn transcript, reached by a new
road.

So: why do parallel-safe calls overlap, exclusive calls form barriers,
and aborted-unstarted calls get synthetic results?

Because concurrency must pay for its speed without breaking section
05's contract: the transcript. For that, the scheduler must:

1. Log first, run second: every call's `tool/call` row is appended
   before anything dispatches, and every `tool/result` lands in the
   model's order, whatever order the threads finished in.
2. Treat safety as a declaration: a tool opts in with
   `is_concurrency_safe`, and the default is exclusive, because only
   the tool's author knows what its body touches.
3. Overlap only inside a batch: consecutive safe calls dispatch
   together; an exclusive call is a batch of one, a barrier:
   everything before it finishes first, everything after it waits.
4. Never abandon started work: cancellation takes effect between
   batches, and a body that was dispatched runs to its end.
5. Answer even the calls it skipped: an aborted-unstarted call gets a
   synthetic error result, because replay must reconstruct a
   transcript where every question has its answer.
6. Keep one writer: only the driving thread appends to the session
   log; worker threads run the pipeline and hand back results.

---

## Mechanism

One new file, `scheduler.py`, and the loop's tool arm rerouted through
it:

- **`execute_tool_calls(session, tools, calls, aborted)`**: the
  driver. Four stages: prepare, dispatch, finalize, finish.
- **`_batches(plan)`**: the grouping rule. Consecutive safe calls
  share a batch; an exclusive call stands alone.
- **`is_concurrency_safe`** on `ToolDefinition`, resolved through
  `is_safe()` on the registry and the scope, so shadowing applies to
  safety exactly as it applies to everything else.
- **`Agent.cancel()`**: one `threading.Event` per turn. The scheduler
  checks it before each batch; a step cut short ends with reason
  `"aborted"` and the turn closes.

Every call passes through the same four stages:

1. **prepare**: in model order, each call gets its `tool/call` row
   (log-only, before anything runs) and a safety verdict from
   `is_safe()`. Unknown names count as exclusive.
2. **dispatch**: batches go to worker threads. Before each batch, one
   question: has anything aborted the turn? If so, stop dispatching.
3. **finalize**: the barrier. The driver waits for every future in
   the batch; started work is never abandoned, even after a cancel.
4. **finish**: one `tool/result` row per call, in model order. A call
   that never dispatched gets a synthetic result:
   `{"is_error": true, "content": "aborted before dispatch"}`.

```text
reply: a (safe)   b (safe)   c (exclusive)   d (safe)

prepare   tool/call a, b, c, d   ◄ four rows, model order, nothing running
dispatch  batch [a b]   a ═══════════╗
                        b ═══════╗   ║   safe calls overlap
finalize                ── barrier ──┘
dispatch  batch [c]     c ═══════╗       exclusive: a batch of one
finalize                ── barrier
dispatch  batch [d]     d ═══╗
finalize                ── barrier
finish    tool/result a, b, c, d ◄ model order, though b finished before a
```

In code, the stages read in the same order:

```python
def execute_tool_calls(session, tools, calls, aborted):
    # prepare: a log row and a safety verdict per call, before anything runs
    plan = [(index, call, tools.is_safe(call)) for index, call in enumerate(calls)]
    for _index, call, _safe in plan:
        session.append("tool/call", call)  # log-only: before dispatch
    outcomes = {}  # index -> result dict, filled as batches finalize
    with ThreadPoolExecutor(max_workers=max(1, len(plan))) as pool:
        for batch in _batches(plan):
            # dispatch: a batch starts only if nothing has aborted the turn
            if aborted.is_set():
                break
            futures = [
                (index, pool.submit(tools.execute, call))
                for index, call, _safe in batch
            ]
            # finalize: the barrier; started work is never abandoned
            for index, future in futures:
                outcomes[index] = future.result()
    # finish: one result per call, model order; skipped calls answer too
    for index, call, _safe in plan:
        if index not in outcomes:  # never dispatched: answer anyway
            outcomes[index] = {
                "call_id": call.get("id"),
                "name": call.get("name"),
                "is_error": True,
                "content": ABORTED_BEFORE_DISPATCH,
            }
        session.append("tool/result", outcomes[index])
```

The pipeline from section 05 is untouched: workers call
`tools.execute(call)` and every exit is still a result. What moved is
who appends. The scheduler runs on the loop's thread and is the log's
only writer; worker threads compute result dicts and nothing else, so
the append-only log never needs a lock.

Here is a cancelled turn, as the log records it. The `stop` body calls
`agent.cancel()` from its worker thread, mid-batch:

```text
send("stop everything")
  │   7  assistant/message {"tool_calls": [stop, sibling, late, last]}
  │   8  tool/call    stop       ◄ all four rows before dispatch
  │   9  tool/call    sibling
  │  10  tool/call    late
  │  11  tool/call    last
  │  12  tool/result  stop     {"is_error": false, "content": "stopping"}
  │  13  tool/result  sibling  {"is_error": false, "content": "kept running"}
  │  14  tool/result  late     {"is_error": true,
  │                             "content": "aborted before dispatch"}
  │  15  tool/result  last     {"is_error": true,
  │                             "content": "aborted before dispatch"}
  │  16  step/end     {"reason": "aborted"}
  │  17  turn/end
```

The sibling was already dispatched, so it kept running to its end.
The two calls behind the barrier never started, and finish answered
them anyway. Derive the history and every question has its answer:
replay rebuilds the same story, cancel and all.

### What changed

Compared with section 05:

- `kernel.py`, `message.py`, `session_log.py`, `standin.py` are
  carried forward verbatim. `scheduler.py` is the only new source
  file; the other changes are the scheduler thread pulled through
  existing files, so the diff against 05 is this section's Mechanism,
  nothing else.
- `tools.py`: `ToolDefinition` gains `is_concurrency_safe` (default
  `False`), and the registry and scope gain `is_safe()`. The pipeline
  itself is untouched.
- `agent_loop.py`: the serial for-loop over a reply's calls becomes
  one call to `execute_tool_calls`. The Agent gains `cancel()` and the
  per-turn abort event, and a step can now end with reason
  `"aborted"`.
- The log's shape for a multi-call reply changed: all `tool/call` rows
  now land before the first `tool/result` (before dispatch), instead
  of interleaving call and result pair by pair.
- `demo.py`: the Live demo registers a parallel-safe read and an
  exclusive write, both deliberately slow, and prints each body's
  wall-clock window so the overlap is visible on the clock.

---

## In real dsh

All pointers are into the pinned Studied version,
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca).
The scheduler lives in the loop package, not the tool runtime:
[`packages/core/agent-loop`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop).

| Mini-dsh | Real dsh | Notes |
| --- | --- | --- |
| `execute_tool_calls` in `scheduler.py` | [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/tool-calls.ts): `executeToolCalls` | The loop never calls `ctx.tools.execute()` directly on a reply's calls; `executeToolCalls` drives the same 4-stage `prepare / dispatch / finalize / finish` scheduler. |
| `is_concurrency_safe` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts): `ToolDefinition` | `ToolDefinition.isConcurrencySafe`, declared per tool; exclusive unless the tool claims otherwise. |
| the synthetic result | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts): `TOOL_ABORTED_BEFORE_DISPATCH` | A distinct code from `TOOL_ABORTED`, so a transcript can tell a skipped call from an interrupted one. |
| `Agent.cancel()` + `threading.Event` | [`packages/core/agent/src/runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts): `Agent.cancel` | Real cancellation is fused abort signals threaded through the whole runtime; the mini keeps one event per turn, checked at batch boundaries. |
| finish appends in model order | [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/tool-calls.ts) | Results become session events in the loop, not the registry; `tool/result` events also carry `sourceEventSeqs` linking each answer to its rows, where the mini leans on `call_id`. |
| the `ThreadPoolExecutor` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts): `TOOL_RUNTIME_SCHEDULER` | The runtime reaches its scheduler through a named seam rather than a hard-coded pool. |

What the real scheduler adds on top of this section's Mechanism:

- **Aborting started calls, cooperatively.** `TOOL_ABORTED` exists for
  calls interrupted after dispatch: fused signals reach the body, and
  the timeout policy
  ([`packages/guard/timeout-policy`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/guard/timeout-policy))
  wraps `tools/execute` with a deadline while never abandoning the
  tool's promise. The mini never interrupts a started body at all, so
  its only abort code is the before-dispatch one.
- **More ways to end early.** A result can carry `concludesTurn`,
  ending the turn early. The mini's only early exit is `cancel()`.
- **Async all the way down.** dsh's tool bodies are async, so overlap
  is promise concurrency in one thread; the mini's bodies are plain
  Python callables, so it buys the same overlap with a thread pool.
- **A human on the cancel button.** In real dsh the cancel usually
  arrives from the UI, which sits above the Ceiling; the mini exposes
  `cancel()` as a plain method and the Offline check presses it from
  inside a tool body.

---

## Failure modes

- **Append-on-completion makes the log a race.** Let workers append
  results as they finish and the same turn produces a different
  transcript every run; replay stops being a reconstruction. Finish
  appends in model order from one thread, so concurrency never shows
  up in the story, only in the clock.
- **Opt-out safety would invert the burden.** If tools were safe
  unless marked exclusive, every author who forgot the flag would be
  gambling with shared state. Exclusive-by-default means the worst
  a forgetful author loses is speed, and `solo` in the check proves an
  unmarked tool really runs alone.
- **Abandoning started work corrupts more than it saves.** Killing a
  body mid-write leaves half a file and a result nobody can trust.
  The scheduler only refuses to start new batches; whatever was
  dispatched finishes and reports. Cancellation is a barrier decision,
  never a bullet.
- **A skipped call with no row is a hole in the transcript.** The
  assistant message already carries all four calls; drop the unstarted
  two and the derived history asks questions it never answers. The
  synthetic result is section 05's rule surviving cancellation:
  every call gets an answer row, no matter how it went.
- **Workers writing to the log would need locks everywhere.** The
  session log is single-writer by construction: prepare and finish run
  on the loop's thread, workers only compute. Concurrency stays inside
  one stage instead of leaking into every data structure.
- **A stale safety verdict is still a safe one.** Verdicts are fixed
  at prepare, so a tool unloaded mid-batch keeps its slot; its body
  already ran or its result says what happened. Re-resolving mid-run
  would let the plan change under the barrier.

---

## Runnable

[`src/`](src/) carries 05 forward and adds:

- [`scheduler.py`](src/scheduler.py) (new): `execute_tool_calls`, the
  4-stage driver, and `_batches`, the grouping rule.
- [`tools.py`](src/tools.py): `is_concurrency_safe` on
  `ToolDefinition`, `is_safe()` on the registry and the scope.
- [`agent_loop.py`](src/agent_loop.py): the tool arm routes through
  the scheduler; the Agent gains `cancel()` and the per-turn abort
  event; a step can end with reason `"aborted"`.
- [`test.py`](src/test.py): two safe calls prove they overlapped
  through a barrier only concurrency can pass, an unmarked tool runs
  alone between them, results land in model order even though the
  quick call finished first, and a cancel pressed mid-batch lets
  started work finish while the unstarted calls come back synthetic
  and the next turn starts fresh.
- [`demo.py`](src/demo.py): the Live demo asks for two parallel
  lookups and then an exclusive save, and prints each body's
  wall-clock window plus the log's own story.

```bash
python sections/06-scheduler/src/test.py    # offline check, no key
```

The Live demo needs the root `requirements.txt` and a key; it skips
politely without one:

```bash
pip install -r requirements.txt             # anthropic + python-dotenv
cp .env.example .env                        # then set ANTHROPIC_API_KEY
python sections/06-scheduler/src/demo.py
```

---

## Sources

- [`docs/tool-execution-pipeline.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/tool-execution-pipeline.md):
  dsh's own doc for the execution pipeline the scheduler drives.
- [`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/core.md):
  the loop package that owns `executeToolCalls`.
- [`docs/agent-lifecycle.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/agent-lifecycle.md):
  where tool execution sits inside a turn, cancellation included.
