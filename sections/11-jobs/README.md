# 11 · Jobs

English | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

> A slow command should not hold the whole turn hostage. But work that runs
> off on its own belongs to nobody, so the moment its id goes out, one
> owner holds the only stop button.

Eleven sections in, every piece of work mini-dsh starts still dies
with its turn. The scheduler's contract from section 06 is strict
about it: started work is never abandoned, and every call answers
before the step closes. Ask the shell seam to run something slow
and that contract holds the whole turn hostage; the model, the
inbox, and the user all wait on one command.

The obvious escape is a tool body that spawns a thread and returns.
But now the work belongs to nobody. The turn's abort signal points
at a call that already returned; the thread's output has no address;
and any session that guesses the id can read it, or kill it, or
wait on it. Backgrounding is easy. Ownership is the mechanism.

So: who owns cancellation once the job id is published?

The registry does, fenced to the owner, and the handoff must be
total:

1. Publish the id immediately: `start()` hands back a job id while
   the work runs on its own thread, and the producing call answers
   with a normal result carrying nothing but that id.
2. Take the whole protocol: the producer's `run()` returns
   `(cancel, done, read_output)`, and from then on the registry
   owns ids, snapshots, settlement, and delivery.
3. Fence every accessor: read, kill, and list answer only to the
   session that owns the job, and the caller's identity is ambient,
   fixed when its tools mount, never a model argument.
4. Settle first-wins, exactly once: `completed`, `failed`, or
   `killed`, whichever lands first, forever.
5. Deliver notices through the inbox: a `wakeup` job follows up an
   idle owner and injects into a busy one; a `quiet` job waits to
   be polled. Background work never appends a log row of its own.
6. Write the controls once: `job_output`, `job_kill`, and
   `job_list` serve every producer alike.

---

## Mechanism

One new file, `jobs.py`, and no carried file moves:

- **`JobRegistry`**: the jobs service, ctx key `"jobs"`, mounted
  by `jobs_plugin`. It owns the ids, the owner fence, snapshots,
  and first-wins settlement; a watcher thread per job awaits the
  work so completion lands even if nobody ever polls.
- **`JobOwner`**: the seam's vocabulary: the fence identity and
  the agent whose inbox receives notices.
- **`job_tools(owner)`**: a plugin factory mounting one producer
  (`shell_job`, which runs a command through the section 10 shell
  seam on its own thread) and the three controls into the owner's
  tool scope, with the owner baked in.

The handoff is the heart of it. A producer hands `start()` a
`run()` that starts the work and returns the protocol triple, and
gets back nothing but an id:

```python
def start(self, kind, label, owner, run, delivery="wakeup"):
    cancel, done, read_output = run()
    with self._lock:
        self._count += 1
        job = Job(
            f"job-{self._count}", kind, label, owner, delivery, cancel, read_output
        )
        self._jobs[job.id] = job
    threading.Thread(
        target=lambda: self._settle(job, *done()), ...
    ).start()
    return job.id
```

The instant that return happens, cancellation changes hands. The
turn that made the producing call can end, or abort, or be
cancelled outright; none of it reaches the job, because the turn's
signal was never wired to it. The only remaining door is
`job_kill`, and every door checks the fence first:

```python
def _fenced(self, job_id, caller_id):
    with self._lock:
        job = self._jobs.get(job_id)
    if job is None or job.owner.id != caller_id:
        # One message for a foreign id and a bogus one: a stranger
        # learns nothing, not even that the id exists.
        raise PermissionError(f"no job '{job_id}' owned by this session")
    return job
```

`caller_id` never comes from the model. `job_tools(owner)` bakes
the identity in when the tools mount into the owner's scope, so
whatever id agent B types, its tools answer to the registry as B,
and A's jobs stay invisible. The fence raises; the section 05
pipeline turns the refusal into a normal `is_error` result.

A job ends exactly once. The watcher settles `completed` or
`failed` when the work ends; `kill` settles `killed`; whichever
lands first is the outcome, forever:

```python
def _settle(self, job, status, detail=None):
    with self._lock:
        if job.outcome is not None:
            return  # the race already settled; a later voice changes nothing
        job.outcome = {"status": status, "detail": detail}
    self._notify(job)  # outside the lock: delivery may drive a whole turn
```

Settlement is also the moment the owner hears about it, and the
notice travels through section 07's inbox, never the log directly:

```python
if agent.status == "idle":
    agent.followup(notice)  # idle: the notice opens a turn of its own
else:
    agent.inject(notice)  # busy: park it for the next step boundary
```

```text
the handoff, in time

producing call      shell_job body: run() starts the thread,
                    jobs.start() publishes "job-1"
                      │ the call's abort signal stops mattering here
turn ends           the work is still running; nobody waits
                      │
settlement          first of: watcher (completed | failed), kill (killed)
delivery            wakeup + idle owner  ──► followup(): a turn of its own
                    wakeup + busy owner  ──► inject(): next step boundary
                    quiet                ──► nothing; poll job_output
```

Here is a real run, as the log records it. The producing turn
closes on nothing but the id; the work finishes while the agent is
idle, and the notice comes back as a turn the model never asked
for:

```text
send("run echo hi in the background")     the gate holds the work open
  │   0  turn/start
  │   2  user/message   "run echo hi in the background"
  │   3  request/header tools [shell_job, job_output, job_kill, job_list]
  │   5  tool/call      shell_job {"command": "echo hi", "delivery": "wakeup"}
  │   6  tool/result    "started job-1"     ◄ the whole answer: an id
  │   8  step/start
  │  13  assistant/message "started it"
  │  15  turn/end                           ◄ the job is still running

the work finishes; the agent is idle; the watcher settles "completed"

  │  16  turn/start                         ◄ the notice's own turn
  │  18  user/message   "job job-1 (echo hi) finished: completed"
  │  21  tool/call      job_output {"job_id": "job-1"}
  │  22  tool/result    "completed; output: echo hi"
  │  29  assistant/message "all done"
  │  31  turn/end
```

The log stays boundary-clean across the whole story: the job's
thread never wrote a row. Its completion entered the same way any
input does, routed to the inbox and claimed at a boundary, so
replay reads an ordinary transcript.

### What changed

Compared with section 10:

- Every carried file is verbatim: `agent_loop.py`,
  `capabilities.py`, `inbox.py`, `kernel.py`, `message.py`,
  `scheduler.py`, `session_log.py`, `skills.py`, `standin.py`,
  `system_prompt.py`, `tools.py`. `jobs.py` is the only new source
  file, so the diff against 10 is this section's Mechanism,
  nothing else.
- The Mechanism is pure composition again: the producer consumes
  the section 10 shell seam, the notices ride section 07's
  `followup()` and `inject()` presets, the controls enter through
  the section 05 registry, and the fence reuses section 05's scope
  layers to make the caller's identity ambient.
- The log gained no new event type. A background job's entire
  public life is ordinary rows: a `tool/result` carrying its id,
  and a `user/message` carrying its notice.
- `demo.py`: the Live demo runs a genuinely slow command as a
  background job, lets the completion notice wake the real model
  in a turn it never asked for, and kills a second, quiet job in
  the same reply that started it.

---

## In real dsh

All pointers are into the pinned Studied version,
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca).
The layer is the package family
[`packages/jobs`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs).

| Mini-dsh | Real dsh | Notes |
| --- | --- | --- |
| `JobRegistry`, ctx key `"jobs"` | [`packages/jobs/jobs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/index.ts): `JobRegistry` | The real Definition is `abstract class JobRegistry extends Service` owning `ctx.jobs` (line 62): a section 10 seam in its own right, with the concrete registry mounted as a Provider. |
| `run()` returning `(cancel, done, read_output)` | [`packages/jobs/jobs/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/types.ts): `JobStart` | The same handoff: a producer's `run()` hands back `{cancel, done, readOutput?}` and gets a `JobId`; the registry owns everything after. `JobKindMap` (lines 23-26) names exactly two producer kinds, `bash` and `subagent`. |
| first-wins settlement; `completed` / `failed` / `killed` | [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/types.ts): `JobOutcome` | The same three-way outcome, settled once; a kill racing a completion cannot rewrite it. |
| `delivery="quiet" \| "wakeup"`, `followup()` / `inject()` | [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/types.ts): `CompletionDelivery`, [`packages/jobs/tool-jobs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/tool-jobs/src/index.ts) (lines 279-300) | Completion notices are delivered as `owner.followup()` when the owner is idle and `owner.inject()` when it is busy: the section 07 presets, used for exactly this. |
| `jobs_plugin` | [`packages/jobs/jobs-local/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs-local/src/index.ts): `LocalJobRegistry` | The shipped Provider: the in-process registry behind the abstract seam. |
| `job_output` / `job_list` / `job_kill` | [`packages/jobs/tool-jobs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/tool-jobs/src/index.ts) (lines 303, 343, 363, in that order) | The control tools, written once for all producers; every accessor is owner-fenced in the registry, not in the tools. |
| `shell_job` consuming the shell seam | [`packages/shell/tool-bash/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/tool-bash/src/index.ts) (lines 354-356) | The real bash tool acquires jobs by optional lookup, `ctx.get('jobs')`, not `inject`: with no registry mounted it degrades to foreground-only, and the schema keeps one tool either way. |

What the real jobs layer adds on top of this section's Mechanism:

- **A second producer, as a peer.** `JobKindMap` names `bash` and
  `subagent`: the subagent tool's one-shot background mode hands
  its child to the same registry the bash tool uses, which is why
  the controls are written once. That producer is section 12's
  Mechanism; continuable subagents skip jobs entirely and sit
  above this rebuild's Ceiling.
- **A kill that kills.** The real bash producer's `cancel` signals
  a real process group; the mini's cancel is a cooperative flag
  the work may check. The seam's shape and the settlement race are
  identical; only the machinery behind `cancel` is bigger.
- **Callback delivery, no bus events.** Unlike every layer before
  it, jobs declares no Cordis events: change and completion flow
  through `onJobDone` / `onJobsChanged` callbacks, and the notice
  text the owner sees is composed in `tool-jobs`, not the
  registry.
- **Richer snapshots.** `JobSnapshot` carries timing, output
  cursors, and per-kind detail beyond the mini's four fields, and
  a `wait` accessor lets a caller block on settlement; both stay
  owner-fenced like every other accessor.

---

## Failure modes

- **A background thread with no registry is orphaned work.** A
  tool body that spawns a thread and returns leaves output with no
  address and a kill switch wired to nothing; the next slow
  command backgrounds the same way and now nobody can list what is
  running. `start()` is small, but the id, the fence, and the
  settlement it buys are the difference between background work
  and a leak.
- **An unfenced id is a cross-session leak.** Job ids travel
  through model text, so any session can type any id. If the
  registry answers whoever asks, one session reads another's
  output or kills its build. Every accessor checks the fence, and
  the caller's identity is baked in at mount, where no prompt can
  reach it.
- **A second settlement rewrites history.** Let a late completion
  overwrite `killed` and the owner who killed a job reads
  `completed` afterward, or the reverse; every consumer of the
  outcome now needs its own tiebreak. First-wins in the registry
  settles the race once, for everyone, and `job_kill` reports the
  race's true winner.
- **A notice appended mid-step tears the transcript.** The settling
  thread owns no boundary: a row written the moment work finishes
  lands between a request and its reply, claiming the model saw
  text it never received. Notices ride the inbox and enter at the
  next boundary, like every other mid-turn arrival since section
  07.
- **A cancel that reaches into the job makes background work a
  lie.** If the turn's abort killed published jobs, cancelling a
  turn would silently destroy work the model already reported as
  started. The turn's signal ends at the scheduler; a call the
  abort catches before dispatch still answers, as a synthetic
  error result (section 06), and a published job dies only by
  `job_kill`.

---

## Runnable

[`src/`](src/) carries 10 forward and adds:

- [`jobs.py`](src/jobs.py) (new): the `JobRegistry` service with
  the owner fence and first-wins settlement, the `JobOwner`
  vocabulary, and the `job_tools(owner)` plugin factory mounting
  the `shell_job` producer and the three control tools.
- [`test.py`](src/test.py): the Offline check proves the id
  outlives its turn and the notice opens a turn of its own, a
  busy owner's notice parks until the step boundary, a foreign
  session's probes are denied without learning which ids exist,
  cancelling the producing turn never reaches the job, a
  pre-aborted background call fails instead of no-opping, both
  sides of the settlement race stay settled, and a crashing body
  settles `failed`.
- [`demo.py`](src/demo.py): the Live demo backgrounds a genuinely
  slow command, lets the completion notice wake the real model in
  a turn it never asked for, and kills a quiet job in the reply
  that started it.

```bash
python sections/11-jobs/src/test.py    # offline check, no key
```

The Live demo needs the root `requirements.txt` and a key; it
skips politely without one:

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/11-jobs/src/demo.py
```

---

## Sources

- [`.agents/notes/implemented/architecture/2026-06-20-generic-long-running-tool-runtime.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-06-20-generic-long-running-tool-runtime.md):
  the design note that made jobs a generic runtime with bash and
  subagent as peer producers.
- [`.agents/notes/implemented/architecture/2026-07-26-job-registry-seam.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-07-26-job-registry-seam.md):
  the note that split the abstract `JobRegistry` from its local
  Provider, making jobs a capability seam.
