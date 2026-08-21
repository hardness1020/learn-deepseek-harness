# 12 · Subagent

English | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

> A side errand should not spend the parent's context. Subclassing the
> agent assumes whatever answers it lives in this process, and often it
> does not. So the parent asks a name and takes back a run.

Section 11 taught mini-dsh to put work in the background, but every
thought still happens in one context window. Send the agent on a
side errand, summarize a package, chase a failing test, and the
whole errand's transcript rides along in the parent's history
forever, crowding out the work the errand was supposed to serve.
Delegation is the escape: hand the task to a child with a session,
a tool scope, and a context of its own, and take back one answer.

The obvious build is a subclass. The section 04 `Agent` already
knows how to run a turn, so `class Subagent(Agent)` looks like a
head start. But what answers a delegation is not always an agent in
this process. Real dsh ships providers that fork the process, drive
another product over a wire protocol, or wrap a different harness
entirely; a base class would force every one of them to fake an
Agent's insides just to satisfy the registry.

So: why an interface over "establish a child, hand back a run",
not a subclassed agent?

Because the parent-side contract is four verbs, and a class is a
commitment to everything else. The section builds it as:

1. Hold providers by name: a registry under one ctx key, every
   registration handing back its undo.
2. Keep the provider a callable contract: the resolved start
   request goes in, a run comes out, and the registry never looks
   behind it.
3. Make the run the entire parent-side contract: `cancel`, `done`,
   `read_output`, deliberately section 11's protocol triple.
4. Foreground: the tool call waits on `done` and answers with the
   child's reply.
5. Background: hand the same triple to the job registry unchanged,
   so the subagent becomes the second producer, peer to shell, and
   the section 11 controls serve it with no new code.
6. Route every refusal through the section 05 door: an unknown
   name, a missing job registry, a crashed child, all normal
   `is_error` results.

---

## Mechanism

One new file, `subagent.py`, and no carried file moves:

- **`SubagentRuntime`**: the subagents service, ctx key
  `"subagents"`, mounted by `subagent_plugin`. A name-keyed
  registry of providers; `start()` resolves the name, builds the
  resolved request, and hands back whatever run the provider made.
- **`SubagentRun`**: the parent-side contract, one frozen triple.
- **`in_process_provider(ctx, model_factory)`**: one provider, not
  the contract: a child `Agent` built from the same services the
  parent came from.
- **`subagent_tools(owner)`**: a plugin factory mounting the one
  `subagent` tool into the owner's scope, with the owner baked in.

The registry is small because the contract is. A provider is any
callable that turns a resolved request into a run; nothing about it
says "agent":

```python
def start(self, name, task):
    """Resolve the name, hand the provider a resolved request, get a run."""
    provider = self._providers.get(name)
    if provider is None:
        raise LookupError(f"no subagent provider registered under '{name}'")
    self._count += 1
    return provider({"id": f"sub-{self._count}", "task": task})
```

And the run is the whole of what comes back. It is section 11's
protocol triple on purpose, because that shape already answers
every question a parent may ask of work it no longer holds:

```python
@dataclass(frozen=True)
class SubagentRun:
    cancel: callable  # ask the child to stop; cooperative, best effort
    done: callable  # block until it ends: ("completed", None) | ("failed", detail)
    read_output: callable  # the child's answer so far, as text
```

The in-process provider shows why the contract stays this thin. It
establishes a child through the same `sessions`, `agents`, and
`tools` services the parent came from, drives one `send()` on the
run's own thread, and reads the answer off the child's log. The
child's whole story stays in its own session; the only thing that
crosses back is the run. A provider with no agent behind it at all,
one that answers from a cache, a subprocess, or another product,
returns the same triple, and neither the registry nor the tool can
tell the difference.

The consumer folds both delegation modes into one tool body,
and the fold is the payoff of requirement 3:

```python
if mode == "foreground":
    started = subagents.start(name, task)
    status, detail = started.done()
    if status == "failed":
        raise RuntimeError(f"the subagent failed: {detail}")
    return started.read_output() or "(no reply)"
jobs = ctx.get("jobs")  # optional lookup: no registry, no background
if jobs is None:
    raise RuntimeError("no jobs registry mounted; use mode 'foreground'")

def run():
    started = subagents.start(name, task)
    return (started.cancel, started.done, started.read_output)

job_id = jobs.start("subagent", f"{name}: {task}", owner, run)
return f"started {job_id}"
```

Foreground waits in place. Background wraps the run into the
producer protocol and hands it to section 11, which owns everything
after: the id, the owner fence, first-wins settlement, and the
completion notice through the inbox. The job registry is acquired
by optional lookup, the way real dsh does it, so a harness with no
jobs mounted refuses background delegation loudly instead of
blocking the turn in secret.

```text
delegation, both ways

subagent {provider, task, mode}
  │  runtime.start(name, task): the name resolves, the provider
  │  establishes whatever it establishes, a run comes back
  │
foreground      done() waited on inside the tool call;
                the result is the child's reply
background      (cancel, done, read_output) handed to jobs;
                the result is a job id, and section 11 owns
                the fence, the settlement, and the notice
```

Here is a foreground delegation as the logs record it, both sides.
The parent's transcript carries two rows of the errand, the call
and the answer; the errand itself is a whole session elsewhere:

```text
send("have the worker summarize the log")        the parent, session s1
  │   0  turn/start
  │   2  user/message   "have the worker summarize the log"
  │   5  tool/call      subagent {"provider": "worker",
  │                               "task": "summarize the log",
  │                               "mode": "foreground"}
  │   6  tool/result    "the log has 12 rows"    ◄ one answer crosses back
  │  13  assistant/message "the worker says the log has 12 rows"
  │  15  turn/end

meanwhile, the child, session sub-1: an ordinary transcript

  │   0  turn/start
  │   2  user/message   "summarize the log"      ◄ the task, as its prompt
  │   7  assistant/message "the log has 12 rows"
  │   9  turn/end
```

In background mode the same run rides section 11 instead: the
parent's turn closes on `"started job-1"`, the child thinks while
the parent sits idle, and the notice arrives as a followup turn
where `job_output` answers with the child's reply and `job_list`
names the kind `subagent`. The controls did not change; the
producer did.

### What changed

Compared with section 11:

- Every carried file is verbatim: `agent_loop.py`,
  `capabilities.py`, `inbox.py`, `jobs.py`, `kernel.py`,
  `message.py`, `scheduler.py`, `session_log.py`, `skills.py`,
  `standin.py`, `system_prompt.py`, `tools.py`. `subagent.py` is
  the only new source file, so the diff against 11 is this
  section's Mechanism, nothing else.
- The Mechanism is pure composition: the child is established
  through the section 02 sessions, section 04 agents, and section
  05 tool services; the run is section 11's protocol triple; the
  background mode hands that triple to the job registry, making
  the subagent the second producer section 11 promised.
- The log gained no new event type. A delegation's entire public
  life in the parent's log is a `tool/call` and a `tool/result`;
  the rest of the story is an ordinary session of its own.
- `demo.py`: the Live demo delegates to a child running against
  the real API foreground and quotes its answer, then backgrounds
  a second child whose completion notice wakes the parent in a
  turn it never asked for.

---

## In real dsh

All pointers are into the pinned Studied version,
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca).
The layer is the package family
[`packages/subagent`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent).

| Mini-dsh | Real dsh | Notes |
| --- | --- | --- |
| `SubagentRuntime`, ctx key `"subagents"` | [`packages/subagent/subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/index.ts): `SubagentRuntime` | The runtime (line 171) is a concrete `Service`, unlike section 11's abstract `JobRegistry`: the seam it guards is the provider interface, not the registry itself. |
| a provider as a callable contract | [`packages/subagent/subagent/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/types.ts): `SubagentProvider` | The design question answered in the type system: `SubagentProvider` (line 285) is a TS interface, not a `Service` and not an `Agent` subclass; anything that turns a resolved start request into a `SubagentRun` qualifies. |
| `in_process_provider` | [`packages/subagent/subagent-in-process-driver/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent-in-process-driver/src/index.ts) | The same move at line 132: the child is created through `parent.ctx.agents.create()`, the ordinary section 04 door, not a private constructor. |
| the run handed to jobs in background mode | [`packages/subagent/subagent/src/run-settlement.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/run-settlement.ts), [`packages/subagent/tool-subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/tool-subagent/src/index.ts) (lines 408-423) | One-shot background delegation is `jobs.start({kind: 'subagent', ...})`: the second entry in `JobKindMap`, peer to `bash`, exactly the handoff this section rebuilds. |
| `jobs = ctx.get("jobs")`, optional lookup | [`tool-subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/tool-subagent/src/index.ts) (lines 402-405) | The real delegation tool acquires jobs by `ctx.get('jobs')`, not `inject`: no registry mounted means no background mode, never a silent foreground fallback. |
| the `subagent` tool | [`packages/subagent/tool-subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/tool-subagent/src/index.ts) | The shipped Consumer; even its tool name is configurable, because the schema the model sees belongs to the consumer, never to a provider. |

What the real subagent layer adds on top of this section's
Mechanism:

- **Continuable children.** `startContinuable()` plus a
  continuation manager make a child durable across turns,
  reachable from the parent between them. Per
  [`run-settlement.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/run-settlement.ts)
  (lines 2-4) only the one-shot background mode touches jobs;
  continuable children skip the registry entirely. Continuable
  subagents sit above this rebuild's Ceiling: pointed at here,
  not rebuilt.
- **A provider zoo.** `subagent-spawn-in-process`,
  `subagent-fork-in-process`, `subagent-acp`, `subagent-codex`,
  `subagent-claude-code`, `subagent-dsh-sdk`: the interface
  argument made flesh. Several of these have no `Agent` anywhere
  behind them, which is why the registry never asked for one.
- **Ownership transfer on fulfillment.** When a start fulfills,
  the child's ownership transfers to the parent, so a parent that
  dies takes its delegations down with it; the mini's children
  live and die with the process instead.
- **A busier runtime.** Bus events (`subagent/provider-added`,
  `subagent/provider-removed`, `subagent/start`, `subagent/end`,
  lines 134-167 of the runtime), descriptor snapshots, descendant
  discovery, and a consumer surface of three packages carrying
  five tool names: `subagent`, this section's tool, plus
  `send_message`, `interrupt_agent`, `list_agents`, and `report`
  for children still alive. All of it lives in the runtime and
  its consumers, so providers stay as thin as the interface.

---

## Failure modes

- **A subclass as the contract caps the provider list.** Demand a
  `Subagent(Agent)` and every provider must be an agent in this
  process; the forked, remote, and other-product providers either
  cannot exist or must impersonate an Agent's internals to mount.
  Four verbs ask only for what the parent actually does: start,
  stop, wait, read.
- **A child sharing the parent's session is not delegation.** Write
  the errand's rows into the parent's log and its whole transcript
  rides in the parent's context forever, the exact cost delegation
  exists to avoid. Two logs, one answer crossing: the parent keeps
  a call and a result, the child keeps everything.
- **A run without cancel is a child nobody can stop.** Foreground
  can at least wait it out; a background child with no `cancel` in
  the triple makes `job_kill` a lie, settling "killed" while the
  work runs on. The triple carries the stop switch precisely so
  section 11's fence has something real behind it.
- **An unknown name that raises tears the transcript.** The
  `LookupError` for a name nobody registered must leave through
  the section 05 pipeline as a normal `is_error` result; let it
  escape and the model's question loses its answer, and replay
  breaks at the same row.
- **A silent foreground fallback makes background a lie.** With no
  job registry mounted, quietly running the task inline would
  block the turn for exactly as long as the model tried not to
  wait, with no id to kill. The tool refuses loudly instead, and
  the refusal is an ordinary result the model can route around.

---

## Runnable

[`src/`](src/) carries 11 forward and adds:

- [`subagent.py`](src/subagent.py) (new): the `SubagentRuntime`
  registry, the `SubagentRun` contract, the in-process provider,
  and the `subagent_tools(owner)` plugin factory mounting the
  delegation tool with both modes.
- [`test.py`](src/test.py): the Offline check proves a foreground
  delegation answers with the child's reply out of the child's own
  session, two providers behind one tool stay interchangeable when
  one is not an agent at all, an unknown name is a normal error
  result, a background delegation is an ordinary job whose notice
  and controls need no new code, background without a job registry
  refuses loudly, and a crashed child comes back as a normal error
  result.
- [`demo.py`](src/demo.py): the Live demo delegates to a child
  running against the real API and quotes its answer, then
  backgrounds a second child whose completion notice wakes the
  parent.

```bash
python sections/12-subagent/src/test.py    # offline check, no key
```

The Live demo needs the root `requirements.txt` and a key; it
skips politely without one:

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/12-subagent/src/demo.py
```

---

## Sources

- [`docs/subsystems/subagent.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/subagent.md):
  the subsystem doc for the delegation layer: the provider
  interface, the runtime, and the consumer tools.
- [`packages/subagent/subagent/src/run-settlement.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/run-settlement.ts):
  the three delegation modes (foreground, one-shot background,
  continuable) and the proof that only the background mode touches
  jobs.
