# 10 · Capability seams

English | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

> Write a capability into the tool body and the model's view, the contract,
> and the machine come as one piece. Split all three on day one and nothing
> ever swaps. So split when some code must not know which machine answers.

Ten sections in, mini-dsh still touches nothing outside its own
log. The first real capability, reading a file or running a
command, has to live somewhere, and the obvious home is the tool
body itself.

That home welds three decisions into one function: what the model
sees, what the contract is, and which machine fulfills it. The
Offline check wants memory, a workstation wants the disk, a
locked-down host wants a fence, and every difference rewrites the
tool and wobbles the schema the model plans against.

The opposite ceremony fails too. Give every capability an
interface, a backend package, and a tool package on day one, and
the harness drowns in one-implementation abstractions nobody ever
swaps.

So: when does a capability earn the three-way split?

When some code must not know which machine answers: a consumer
facing the model, or a second backend waiting its turn. Where the
split is earned, the seams must:

1. Define each seam once: an ABC, one ctx key, and the seam's
   vocabulary, owning the contract and nothing else.
2. Mount providers as plugins: one implementation under the key,
   its undo on the fiber, and a second mount under an exclusive
   key (fs, shell, sandbox) failing loud.
3. Keep consumers provider-blind: a tool body resolves the key at
   execute time and speaks only the ABC's verbs, so a backend swap
   never changes what the model sees.
4. Shape the sandbox as a fence, not a tool: one verb,
   `confine(argv, policy)`, consumed by providers of other seams,
   refusing a policy it does not know.
5. Fold llm: Definition and Consumer in one service, adapters as
   plain callables in the Model seam shape, plural under names,
   resolved per call.
6. Degrade at the tool door: a missing provider or a refused
   policy answers as a normal `is_error` result, and the turn
   closes on its own feet.

---

## Mechanism

One new file, `capabilities.py`, and no carried file moves:

- **Definitions**: `FileSystem` (read, write), `ShellExecutor`
  (run), and `SandboxProvider` (confine) as ABCs, each naming one
  ctx key. The ABC, the key, and the vocabulary are the whole
  role; a Definition ships no code that does anything.
- **`provider()`**: the Provider role as a plugin factory. The
  kernel already does the bookkeeping: `provide()` hands back an
  undo and refuses a duplicate key, so an exclusive seam fails
  loud at mount for free.
- **`capability_tools_plugin`**: the Consumers. The `read`,
  `write`, and `shell` tools resolve their seam through
  `ctx.get()` at execute time and speak only the ABC's verbs;
  no tool imports a provider. That import discipline is the seam.
- **The two bends**: the sandbox seam has providers but no tool,
  and the llm seam has a service but no ABC. Each bend is the
  design question answered a different way.

The sandbox bend first. Its one verb rewrites an argv under a
named policy, and an unknown policy refuses rather than passing
the argv through unfenced:

```python
def confine(self, argv, policy):
    if policy not in self._policies:  # fail closed: never run unfenced
        raise ValueError(f"unknown sandbox policy '{policy}'")
    return [SANDBOX_ARGV_MARKER, "--policy", policy, "--", *argv]
```

Nobody offers `confine` to the model. The sandbox's consumers are
providers of other seams, so the fence wraps work the model
already asked for through some other schema:

```python
class SandboxedShellExecutor(ShellExecutor):
    """Provider built on another seam: run everything through the fence."""

    def run(self, argv):
        return self._inner.run(self._sandbox.confine(argv, self._policy))
```

The llm bend folds the other direction. Its consumer is the agent
loop itself, the `model` parameter every Agent has taken since
section 04, so a separate Consumer home would draw a boundary no
swap ever crosses. And the Model seam already is the contract: a
plain callable streaming chunks then one final Message needs no
ABC. What remains is plurality, a registry of named adapters, and
`model(name)` resolving late so a swap reaches a live agent:

```python
def model(self, name):
    """The Model seam bound to an adapter name, resolved per call."""

    def seam(messages, tools=(), system=""):
        adapter = self._adapters.get(name)
        if adapter is None:
            raise LookupError(f"no llm adapter registered under '{name}'")
        return adapter(messages, tools, system)

    return seam
```

```text
the three roles, one seam (fs)

Definition   FileSystem ABC: read, write; one ctx key "fs"
Provider     provide("fs", MemoryFileSystem({...}))   undo on the fiber;
                                                      a second mount raises
Consumer     read/write tools: ctx.get("fs") per call, the ABC's verbs only

the sandbox bend: consumed by a provider, never by a tool

shell tool ──► ctx.get("shell").run(["echo", "hi"])
                 SandboxedShellExecutor              a shell provider,
                   │ confine(["echo", "hi"], ...)    consuming the sandbox seam
                   │  ├─ known policy: prepend the fence marker
                   │  └─ unknown policy: raise; fail closed, nothing runs
                 EchoShellExecutor.run(fenced argv)  the inner provider
tool/result   "mini-sandbox --policy read-only -- echo hi"
```

Here is a real run, as the log records it. Two turns read the same
path; between them the first fs provider's undo runs and another
implementation takes the key. The agent is never touched:

```text
send("read it")                 provide("fs", A), notes.txt = "alpha"
  │   0  turn/start
  │   1  step/start
  │   2  user/message   "read it"
  │   3  request/header tools [read, write, shell]
  │   4  assistant/message {"tool_calls": [read "notes.txt"]}
  │   5  tool/call      read {"path": "notes.txt"}
  │   6  tool/result    "alpha"                  ◄ the machine's answer
  │   7  step/end       {"reason": null}
  │   8  step/start
  │   9  request/header tools [read, write, shell]
  │  10  assistant/chunk "do"
  │  11  assistant/chunk "ne"
  │  12  assistant/message "done"
  │  13  step/end       {"reason": "completed"}
  │  14  turn/end

A's undo runs; provide("fs", B), notes.txt = "beta"

send("read it again")
  │  15  turn/start
  │  ...
  │  18  request/header tools [read, write, shell] ◄ byte-identical offer,
  │  ...                                             same system text
  │  21  tool/result    "beta"                     ◄ only the machine changed
  │  ...
  │  29  turn/end
```

The seam's proof is that contrast: every `request/header` row in
the log is identical across the swap, and only the `tool/result`
rows tell the backends apart.

### What changed

Compared with section 09:

- Every carried file is verbatim: `agent_loop.py`, `inbox.py`,
  `kernel.py`, `message.py`, `scheduler.py`, `session_log.py`,
  `skills.py`, `standin.py`, `system_prompt.py`, `tools.py`.
  `capabilities.py` is the only new source file, so the diff
  against 09 is this section's Mechanism, nothing else.
- The Mechanism lands as pure plugin again: Consumers enter
  through the section 05 registry, Providers through the kernel's
  `provide()`, and the llm fold through the model parameter the
  loop has taken since section 04. The split needed no new
  framework, only discipline about who imports what.
- The Model seam gained a service home without changing shape:
  `llm.model(name)` is still a plain callable streaming chunks
  then one final Message, so `ScriptedModel` and `live_model`
  register as adapters unmodified.
- The log gained no new event type. A backend swap shows up only
  as differing `tool/result` rows under identical `request/header`
  rows.
- `demo.py`: the Live demo mounts the real Anthropic adapter
  through the llm runtime, swaps the fs backend between turns, and
  lets the model report the sandbox stub's fenced argv.

---

## In real dsh

All pointers are into the pinned Studied version,
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca).
Each seam is a package family:
[`packages/fs`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs),
[`packages/shell`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell),
[`packages/sandbox`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/sandbox),
[`packages/llm`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm).

| Mini-dsh | Real dsh | Notes |
| --- | --- | --- |
| `FileSystem` ABC, one `"fs"` key | [`packages/fs/fs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/fs/src/index.ts): `FileSystem` | The real Definition is `abstract class FileSystem extends Service` owning `ctx.fs` (line 86): subclassing `Service` ships the key and the contract together, never a bare interface. |
| `provider("fs", MemoryFileSystem(...))` | [`packages/fs/fs-local/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/fs-local/src/index.ts): `LocalFileSystem`, [`packages/fs/fs-sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/fs-sandbox/src/index.ts): `SandboxedFileSystem` | The shipped Providers. The sandboxed fs fences paths through `ctx.sandboxPolicy` (line 127), a second sandbox surface this rebuild folds into `confine`'s policy name. |
| the `read`/`write` tools | [`packages/fs/tool-fs/src/read.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/tool-fs/src/read.ts) and siblings | Consumers: `read`, `write`, `edit`, `read_image`, with `glob` and `grep` elsewhere in `packages/fs`. No tool schema names a backend. |
| `ShellExecutor`, exclusive mount | [`packages/shell/shell/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/shell/src/index.ts): `ShellExecutor` | `ctx.shell` (line 65) allows one implementation per context; a second registration throws (lines 48-50). The mini gets the same refusal from the kernel's `provide()`. |
| `SandboxedShellExecutor` | [`packages/shell/bash-sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/bash-sandbox/src/index.ts): `SandboxBashExecutor` | Calls `ctx.sandbox.confine(['bash', '-c', command], policy)` (line 178): a shell Provider consuming the sandbox seam, the mini's decorator with a real machine behind it. |
| `ArgvRewriteSandbox.confine` | [`packages/sandbox/sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/sandbox/sandbox/src/index.ts): `SandboxProvider` | `confine(argv, policy)` is the Definition's sole abstract method (line 158); the seam owns no tool and no events. |
| `LlmRuntime` | [`packages/llm/llm/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/index.ts): `LlmRuntime`, `LlmAdapter` | Definition and Consumer folded in one package: `ctx.llm` (line 284) is consumed by the loop, and adapters subclass `LlmAdapter` (line 180). Providers like [`llm-deepseek`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-deepseek/src/index.ts) register through `ctx.llm.registerAdapter`. |

What the real seams add on top of this section's Mechanism:

- **Real confinement.** `sandbox-local` chains platform runners:
  `bwrap` and `landlock` on linux, `seatbelt` on darwin (line
  160), plus a Windows ACL provider. That machinery is this
  rebuild's Ceiling: the argv-rewrite stub keeps the seam's shape
  and its fail-closed rule, but enforces nothing, and the
  confinement itself is pointed at here, never rebuilt.
- **Seam-owned events.** The fs Definition owns `fs/write-intent`
  and `fs/edit-intent` waterfalls plus an `fs/observed` emit, so a
  plugin can veto or rewrite a write before any provider sees it;
  llm owns an `llm/stream` waterfall for middleware. Shell and
  sandbox own no events: a Definition's surface is its verbs plus
  whatever events it alone declares.
- **Adapter routing.** `registerAdapter(providers, adapter)` binds
  model-name prefixes and the runtime routes each request by its
  model id. The mini binds one name per agent at creation and
  resolves it per call; the late binding is the same, the routing
  key is smaller.
- **The split as written policy.** dsh's architecture notes state
  the rule this section's question asks for: a capability is not
  split preemptively, one provider plus one consumer stays one
  package until a second appears, and `dsh-llm` is the standing
  exception because its consumer is the loop.

---

## Failure modes

- **A tool that imports a provider welds the seam shut.** If the
  `read` body constructs a backend or opens the disk itself,
  swapping machines means editing the tool, and every environment
  forks the schema. The body resolves `"fs"` per call and speaks
  the ABC's verbs; the import discipline is the seam.
- **A quiet second provider ships a config bug.** Let two shells
  mount silently and which machine runs a command depends on mount
  order nobody reads. An exclusive key refuses the second mount at
  mount time, before any call can pick wrong.
- **A sandbox that fails open is worse than none.** Hand back the
  argv unchanged for a policy you do not know and every
  misconfiguration runs unfenced, invisibly. `confine()` raises,
  the section 05 pipeline answers with a normal `is_error` result,
  and nothing runs.
- **A model-facing sandbox tool fences the wrong door.** Offer
  `confine` in the schema and fencing becomes the model's choice.
  The sandbox's consumers are providers of other seams: the fence
  wraps work already authorized, below the schema, where no one
  can ask it not to.
- **A preemptive split is dead weight.** An llm ABC with one
  consumer that can never change adds a boundary no swap will
  cross; adapters already swap as plain callables behind
  `model(name)`. The three-way split is earned by a consumer that
  must not know its provider, not by symmetry.

---

## Runnable

[`src/`](src/) carries 09 forward and adds:

- [`capabilities.py`](src/capabilities.py) (new): the three seam
  ABCs with their providers (`MemoryFileSystem`,
  `EchoShellExecutor`, `ArgvRewriteSandbox`,
  `SandboxedShellExecutor`), the `provider()` plugin factory, the
  folded `LlmRuntime`, and the consumer tools.
- [`test.py`](src/test.py): the Offline check proves a backend
  swap changes results under a byte-identical schema, an exclusive
  seam refuses a second mount, the sandbox's rewrite reaches the
  log through the shell provider, an unknown policy and a missing
  provider both answer as normal error results, and llm adapters
  coexist by name and swap under a live agent.
- [`demo.py`](src/demo.py): the Live demo consumes the real model
  through the llm runtime, swaps the fs backend between turns, and
  has the model report the fenced argv the sandbox stub produced.

```bash
python sections/10-capability-seams/src/test.py    # offline check, no key
```

The Live demo needs the root `requirements.txt` and a key; it
skips politely without one:

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/10-capability-seams/src/demo.py
```

---

## Sources

- [`docs/glossary.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/glossary.md):
  dsh's own definitions of Service Definition, Service Provider,
  and Service Consumer.
- [`.agents/notes/implemented/architecture/2026-06-13-capability-seams.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-06-13-capability-seams.md):
  the architecture note that decided the three-way split and the
  not-preemptively rule.
