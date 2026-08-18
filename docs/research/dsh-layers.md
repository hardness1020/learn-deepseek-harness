# dsh architecture layers (pinned study version)

Research ticket: [#2](https://github.com/hardness1020/learn-deepseek-harness/issues/2)

## Pin

| Field | Value |
|---|---|
| Repo | `deepseek-ai/deepseek-harness` |
| Commit | `99f6f02fecdb7dff40c3fbc9470f5907c29f74ca` |
| Version | `0.1.0-rc.7` (root `package.json`, name `@deepseek-ai/dsh-root`) |
| Commit date | 2026-08-17 (merge of `release/dsh-0.1.0-rc.7`) |

All file paths below are relative to the dsh repo root at this commit. Every claim cites its owning source file.

## Layer map (verified, 10 layers)

| # | Layer | ONE mechanism | ctx key(s) |
|---|---|---|---|
| 1 | Cordis kernel | every registration is a disposer collected on a `Fiber`; `ctx.effect()` makes mount/unmount reversible | (framework) |
| 2 | Session log | append-only frozen `SessionEvent` log; ordered surface; `deriveMessages()` projection | `ctx.sessions` |
| 3 | Agent loop | `Agent` seam + swappable factory driving turn > step; log is the only durable state | `ctx.agents`, `ctx.agentLoop` |
| 4 | Tools | scoped registry + fixed pre/ask/guard/execute/post pipeline; every failure is a normal result | `ctx.tools` |
| 5 | System prompt & request context | ordered section/context/tool-schema/variable providers assembled per step | `ctx.systemPrompt` |
| 6 | Capability seams | Service Definition / Provider / Consumer split per capability (fs, shell, sandbox, llm) | `ctx.fs`, `ctx.shell`, `ctx.sandbox`, `ctx.llm` |
| 7 | Skills | layered provider registry resolving names to instruction text, injected pre-step | `ctx.skills` |
| 8 | Jobs | owner-fenced background-work protocol with `job_*` control tools | `ctx.jobs` |
| 9 | Subagent | named-provider registry establishing child agents; ownership transfer on fulfillment | `ctx.subagents` |
| 10 | Composition | ordered patch layers (bundles, profile patch, home patch) over an empty entry list, loaded by the Cordis Loader | (boot) |

Changes vs the candidate layering:

- **Split (g) subagent/jobs/skills into three layers (7, 8, 9).** Skills has zero coupling to jobs (`grep -rl "dsh-jobs\|ctx.jobs" packages/skill` returns nothing); it is a prompt/context-plane mechanism delivered via `agent/pre-step` (`packages/skill/tool-skill/src/index.ts:177,213`). Jobs is a generic runtime whose two shipped producers are bash and subagent as peers (`JobKindMap` in `packages/jobs/jobs/src/types.ts:23-26`); subagent uses jobs only in its one-shot background mode (`packages/subagent/subagent/src/run-settlement.ts:2-4`).
- **All other candidate layers confirmed** with corrections noted per layer below (kernel: `Fiber` not Scope; session: the surface middle layer; loop: no `agent/turn-start` bus event; tools: guard vs guard-plugins; prompt: three outputs not one; seams: sandbox is not a tool triple; composition: presets are a separate per-agent mechanism).

## Smallest runnable core

- **Product headless profile**: `dsh --profile headless` = `['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-headless']` (`packages/boot/app-boot/src/profile.ts:114-117`); dsh-base is 78 rows (`packages/bundle/base/cordis.patch.yml`), dsh-headless adds 6 (`packages/bundle/headless/cordis.patch.yml`). Complete but not small.
- **Smallest complete runnable tree in-repo**: the Python SDK runtime default, 8 rows (`python/sdk-runtime/src/deepseek_harness_runtime/runtime/cordis.yml`): `sdk-jsonrpc-server`, `agent-core` (`@deepseek-ai/dsh-agent-spine-demo`), `llm-deepseek`, `sessions` (jsonl), `session-checkpoints`, `subprocess`, `bash`, `fs-local`. Entry: `packages/examples/jsonrpc-demo/src/packaged-bin.ts`.
- **True minimum agent**: 1 entry/transport row + `@deepseek-ai/dsh-agent-spine-demo` (a code bundle mounting ~20 spine children) + 1 LLM adapter. What the spine leaves outside: LLM adapter, bash executor, non-local skill providers, entry point (`packages/examples/agent-spine-demo/README.md:44-50`).

## Per-layer detail

### Layer 1 — Cordis kernel (vendored framework)

**Mechanism.** Every registration a plugin makes (service, listener, timer, anything) is a disposer collected on the `Fiber` that owns its `Context`. Mounting a plugin runs a function; unmounting runs the fiber's collected disposers in reverse. One primitive, `ctx.effect()`, underlies all of it (`vendor/cordis/src/fiber.ts:418`).

**Key files.**
- `vendor/cordis/src/context.ts` — `Context` (a Proxy over itself); `extend` / `isolate` / `intercept`
- `vendor/cordis/src/fiber.ts` — `Fiber`, `effect()`, `FiberState` (`PENDING, LOADING, ACTIVE, FAILED, DISPOSED, UNLOADING`)
- `vendor/cordis/src/events.ts` — `EventsService`; `DispatchMode = 'emit' | 'parallel' | 'serial' | 'bail' | 'waterfall'`
- `vendor/cordis/src/registry.ts` — `RegistryService`, `Plugin` (Function / Constructor / Object forms), `Inject`
- `vendor/cordis/src/reflect.ts` — `ReflectService` (`provide` / `get` / `set` / `mixin`)
- `vendor/cordis/src/service.ts` — `Service` base class (constructor calls `ctx.reflect.provide`)
- `vendor/loader/src/config/entry.ts` — config row to plugin mount/unmount
- `docs/cordis-primer.md`, `vendor/README.md` (vendoring manifest + 18 local modifications)

**Core types/events.** `Context`, `Service`, `Plugin`, `Fiber`, `FiberState`; effects accept `Disposable | Promise | (Async)Iterable` shapes; built-in bus events `internal/plugin`, `internal/status`, `internal/service`, etc. (`vendor/cordis/src/events.ts:329-352`). Disposers run in reverse registration order; effects created while `UNLOADING` throw `CordisError('INACTIVE_EFFECT')`.

**Corrections vs candidate.** Candidate said "plugin/effect/service/event, reversible registrations" — accurate, with refinements: (1) the lifetime unit is `Fiber`, not Scope/EffectScope (those identifiers do not exist in `vendor/`); (2) add `inject`-based DI as a fifth element — a fiber reloads automatically when an injected service's provider changes (epoch of provider uids, `vendor/cordis/src/fiber.ts:620`), so reversibility is a consequence of service-driven re-entrancy; (3) `vendor/cordis` is a source-vendored, renamed (`@deepseek-ai/cordis`), locally patched copy of upstream `cordiverse/cordis` (upstream pin `56b3d4f` per `vendor/README.md`) — nine vendored dirs total (`cosmokit`, `schemastery`, `cordis`, `loader`, `include`, `group`, `timer`, `hmr`, `logger-console`).

**Minimal Python rebuild.** A `Fiber` with an ordered disposer list + `effect(fn, label)` returning a single-shot reverse-unwinding disposer (`contextlib.ExitStack` analogue); a `Context` resolving services by string key with `inject` gating and provider-epoch reload; an event bus with the five dispatch modes where `on()` returns a disposer (waterfall = around-middleware that pops the caller's `next`).

**Tutorial question.** Why is unloading a plugin something the framework can do correctly, rather than cleanup each plugin must remember? (Disposer-collecting registration vs register/unregister pairs; hot-reload, HMR, and dependent-cascade reload become one code path.)

### Layer 2 — Session log (event-sourced data plane)

**Mechanism.** A `Session` is an append-only array of frozen JSON `SessionEvent`s whose index is its `seq`; model history is never stored, it is derived: the log maintains an ordered "surface" of message-producing events, and `deriveMessages()` projects that surface into `Message[]`. Compaction rewrites the surface via a `replace` marker without mutating the log (`packages/core/session/src/index.ts:726`, `packages/core/session/src/surface.ts`).

**Key files.**
- `packages/core/session/src/types.ts` — `SessionEventMap`, `SessionEvent`, `SurfaceOp`, `SurfaceIntent`, `SessionHeader`
- `packages/core/session/src/index.ts` — `class Session`, `class SessionStore extends Service` (ctx key `ctx.sessions`), Cordis event declarations
- `packages/core/session/src/surface.ts` — `SurfaceManager`, `deriveEventMessage`
- `packages/core/session/src/known-event-types.ts` — generated list of all 45 in-repo session event types
- `packages/session/session-persistence/src/index.ts` — abstract `SessionPersistence extends Service` (ctx key `ctx.sessionPersistence`)
- `packages/session/session-persistence-jsonl/`, `packages/session/session-persistence-sqlite/` — shipped backends
- `docs/subsystems/session.md`, `packages/core/session/README.md`

**Core types/events.** Core `SessionEventMap` members: `turn/start`, `turn/end`, `step/start`, `step/end`, `user/message`, `assistant/chunk`, `assistant/message`, `tool/call`, `tool/result`, `todo/write`, `request/header`, `request/context`, `session/end-seed` (`packages/core/session/src/types.ts:236`); the map is extensible by declaration merging (plugins add `compaction/*`, `hook/*`, `llm/retry`, ...). Surface events are exactly `user/message | assistant/message | tool/result`; `SurfaceOp = 'append' | { op: 'replace', start, end }`. Bus events: `session/created` (emit, veto-by-throw), `session/disposed` (emit), `session/event` (emit, post-commit append feed), `session/flush` (parallel, awaited durability barrier) (`packages/core/session/src/index.ts:42-86`). `append()` validates (`snapshotJsonValue`), deep-freezes, validates the surface transition, then pushes; `seq == log.length` is an invariant.

**Corrections vs candidate.** "Append-only SessionEvent log + deriveMessages() projection" is correct on both halves, but misses the load-bearing middle layer: the **surface**. `deriveMessages()` projects `session.surface` (ordered seq list), not the raw log — that is how compaction removes model-visible messages while the log stays append-only. Also: `packages/session/session-projection` (`ctx.sessionProjections`) is NOT this projection — it folds committed events into client-facing UI read models; do not conflate it with `deriveMessages()`. Persistence attaches purely via the four bus events (`packages/session/session-persistence/src/coordinator.ts:1118-1132`), so backends are plugins, not part of the core class.

**Minimal Python rebuild.** Frozen-event append log with `seq == len(log)`; single-pass JSON validate-and-copy at the append boundary; a surface list of ints maintained by each append's `surface_op` + a `replace_generation` counter + `derive_messages()`; two subscriber hooks (unawaited per-append feed, awaited flush barrier) with per-listener exception containment — a JSONL writer subscribing to those two is a complete persistence backend.

**Tutorial question.** If the log is append-only, how does compaction ever remove anything from what the model sees? (Answer: a `replace` surface op shadows earlier surface nodes while raw rows stay on disk — forcing the log / surface / derived-messages distinction.)

### Layer 3 — Agent loop (turns and steps)

**Mechanism.** `ctx.agents` (`packages/core/agent/src/index.ts`) is a registry of opaque `Agent` handles; a swappable factory registered by `dsh-agent-loop` (`ctx.agents.setFactory()`) drives each agent as a `while (turn())` / `while (step)` machine whose only durable state is the session log. Every step re-assembles the prompt, re-derives messages from the log, streams one model call, executes its tool calls, and appends results back (`packages/core/agent-loop/src/agent.ts`).

**Key files.**
- `packages/core/agent/src/runtime-types.ts` — `Agent` interface, `AgentStatus`, the `agent/*` event declarations
- `packages/core/agent/src/index.ts` — `AgentRegistry` (ctx key `ctx.agents`), `AgentFactory`, `withInitiator`
- `packages/core/agent/src/dispatch.ts` — scope-filtered `agentEvents()` dispatcher
- `packages/core/agent/src/inbox.ts` — inbox with `InboxTarget = 'next-turn' | 'next-step'`
- `packages/core/agent-loop/src/agent.ts` — `ReactLoopAgent`: `kick` -> `turn()` -> `preStep()` -> `step()` -> `buildRequest()`
- `packages/core/agent-loop/src/tool-calls.ts` — `executeToolCalls`
- `packages/core/agent-default-model/src/index.ts` — deployment default model selection (`ctx.agentDefaultModel`)
- `docs/subsystems/core.md`, `docs/agent-lifecycle.md`

**Core types/events.** `Agent { id, session, inbox, status, ctx, cancel, send, followup, steer, inject, ... }`; `AgentStatus = 'idle' | 'running'`; `PreStepDecision = reject | enter(messages)`. Live bus events: `agent/created`, `agent/disposed`, `agent/status`, `agent/inbox/inserted|claimed|discarded`, `agent/session-start` (emit); `agent/pre-step`, `agent/request`, `agent/request-error` (waterfall); `agent/turn-stopping` (serial); `agent/error` (emit) (`packages/core/agent/src/runtime-types.ts`). Durable turn/step vocabulary is session events (`turn/start`, `step/start`, `step/end`, `turn/end`, appended at `packages/core/agent-loop/src/agent.ts:255,279,292,319`). Requests go through `ctx.llm.prepareCall()` then `preparedCall.stream(request)` (`agent.ts:345,449`).

**Corrections vs candidate.** (1) There is no `agent/turn-start` bus event — turn/step boundaries are durable session events appended by the driver; the `agent/*` bus carries only lifecycle, inbox, and interception points. (2) Termination is not "loop until no tool calls": a step ends `completed` (no tool calls), `max-tokens` (sticky), or `null` (tools ran); the turn closes only when an end reason exists AND `inbox.nextStep` is empty after the `agent/turn-stopping` serial re-check; a tool result with `concludesTurn` ends it early. (3) A step is richer than "model request + tool calls": claim inbox -> `systemPrompt.assemble` -> runtime-context projection -> `agent/pre-step` waterfall -> append `user/message`s -> `agent/request` waterfall + `request/header` logging -> stream (`assistant/chunk` per chunk) -> `assistant/message` -> tools. (4) `Agent` is a seam interface; the concrete `ReactLoopAgent` is package-internal, reached only via the factory, so the driver stays swappable.

**Minimal Python rebuild.** A per-agent state machine with two nested loops and a two-list inbox (`next_turn` / `next_step`) claimed at step boundaries; the append-only log as the only state with `derive_messages()` rebuilding each request; three hook points with real semantics (waterfall pre-step reject/replace, waterfall request-config, serial turn-stopping veto) plus one cancellation token threaded through everything.

**Tutorial question.** Why is the prompt re-assembled and history re-derived from the log at every step instead of appended to a live message list? (Replay/resume, `request/header` epochs, the `agent/pre-step` injection seam.)

### Layer 4 — Tools (scoped registry + guarded pipeline)

**Mechanism.** `ctx.tools` is a `ScopedLayers`-backed registry (global layer + per-agent scope layers with name shadowing and intersecting restrictions) whose execution runs a fixed pipeline: `tools/pre-execute` waterfall -> approval `ask` -> deny-only monotonic guards -> `tools/execute` around-waterfall -> tool body -> `tools/post-execute` waterfall -> `finalizeContent` -> frozen `tools/result` (`packages/core/tools/src/index.ts`, `docs/tool-execution-pipeline.md`).

**Key files.**
- `packages/core/tools/src/index.ts` — `ToolRuntime`, `ToolDefinition`, `tools/*` events, `register` / `restrict` / `guard` / `execute`, `TOOL_RUNTIME_SCHEDULER`
- `packages/core/tools/src/schema.ts` — `defineTool()` (typed args + output validation)
- `packages/core/scope/src/index.ts`, `packages/core/scope/src/store.ts` — `createScope`, `scopeOf`, `scopeTarget`, `ScopedLayers`
- `packages/guard/timeout-policy/src/index.ts` — `tools/execute` deadline wrapper (`TOOL_TIMEOUT`)
- `packages/guard/repeat-tool-reminder/src/index.ts` — advisory repeat-call reminders on `tools/post-execute`
- `packages/core/agent-loop/src/tool-calls.ts` — scheduler driver; results to session events
- `docs/subsystems/tools.md`, `docs/subsystems/scope.md`

**Core types/events.** `ToolDefinition extends ToolSchema` (`ToolSchema` lives in `packages/llm/llm/src/types.ts:333`) adding `output { schema, render }`, `execute(args, exec)`, optional `timeoutMs`, `isConcurrencySafe`, `finalizeContent`. Outcomes `ToolExecutionSuccess { isError:false, value, content, concludesTurn? }` / `ToolExecutionFailure { isError:true, ... }`; decisions `PreToolDecision = allow | deny | ask`, `PostToolDecision = accept | block`; `ToolGuard = (execution) => string | undefined` (deny-only, `packages/core/tools/src/index.ts:711`). Events: `tools/pre-execute`, `tools/execute`, `tools/post-execute`, `tools/code-dispatch-log` (waterfalls); `tools/result`, `tools/change` (emit) (`packages/core/tools/src/index.ts:152-207`). Abort codes `TOOL_ABORTED`, `TOOL_ABORTED_BEFORE_DISPATCH` (`packages/core/tools/src/index.ts:469,472`); `TOOL_TIMEOUT` is defined by the guard plugin, not core (`packages/guard/timeout-policy/src/index.ts:25`).

**Corrections vs candidate.** "Guarded pipeline" conflates two mechanisms: (1) `ctx.tools.guard()` registers monotonic synchronous deny-only checks (`packages/core/tools/src/index.ts:1110`) applied inside the pipeline after approval (`:1486-1499`); (2) the `packages/guard/*` plugins are ordinary event listeners, not `ToolGuard`s — the timeout policy wraps `tools/execute` cooperatively (never abandons the tool promise), and repeat reminders observe `tools/post-execute` and enrich via `additionalContexts`. Also: the loop does not call `ctx.tools.execute()` directly — `executeToolCalls` drives a 4-stage scheduler (`prepare / dispatch / finalize / finish`, `packages/core/agent-loop/src/tool-calls.ts:121-246`) so parallel-safe calls overlap while exclusive calls form barriers; and results become session events in the loop, not the registry (`tool/call` appended before dispatch, `tool/result` in model order with `sourceEventSeqs`; even aborted-unstarted calls get a synthetic pair so replay stays valid).

**Minimal Python rebuild.** A name-keyed registry with schema-validated execute + explicit output schema/render; one `execute()` with the pipeline as an explicit ordered list where every failure becomes an `is_error` result rather than an escaping exception; cancellation discipline (fused signals, never abandoning a started coroutine, distinct codes for skipped-before-dispatch vs aborted-after-start).

**Tutorial question.** Why does a denied or errored tool call still produce a normal `tool/result` in the log instead of raising? (The model must see a turn-consistent transcript; replay must reconstruct it.)

### Layer 5 — System prompt and request context

**Mechanism.** `ctx.systemPrompt` is a scoped registry of four provider kinds — ordered sections, ordered dynamic contexts, tool-schema providers, and variables — that `assemble(AssembleContext)` resolves into a `PromptAssembly`, passes through the `system-prompt/assemble` waterfall, then renders into three model-facing artifacts: the `system` string, the request's tool list, and a runtime-context snapshot delivered as a user-role message (`packages/core/system-prompt/src/index.ts`).

**Key files.**
- `packages/core/system-prompt/src/index.ts` — `SystemPrompt`, `PromptSection`, `PromptContext`, `PromptAssembly`, `renderPrompt`, `PERSONA_SECTION`, `TOOL_ORDER_REST`
- `packages/core/agent-loop/src/runtime-context.ts` — snapshot dedupe (`RuntimeContextProjection`)
- `packages/core/tools/src/index.ts:832-836` — tools register their schemas as one prompt provider (`ctx.systemPrompt.tools(...)`)
- `packages/context/agent-instructions/src/index.ts` — workspace instructions (AGENTS.md-style)
- `packages/context/time-context/src/index.ts` — time context
- `docs/subsystems/system-prompt.md`, `packages/context/README.md`

**Core types/events.** `PromptSection { name, order, text | (ctx) => string, complete? }`; `PromptContext { name, order, text }`; `PromptAssembly { sections, contexts, tools, variables }`; API `section() / context() / variable() / tools() / assemble()`, each returning a Cordis effect disposer. Events: `system-prompt/assemble` (waterfall, scope-filtered), `system-prompt/change` (emit). Built-in `'harness:identity'` section at order -100; persona at 0; tool guidance 100-199. Strict `{{variable}}` interpolation (unknown or undefined value throws).

**Corrections vs candidate.** "System-prompt assembly" is only a third of the output — assembly also produces the request tool list and a dynamic runtime-context snapshot emitted as a `user/message` (only when it differs from the retained snapshot), not system text. Assembly happens per step inside `preStep`, before `agent/pre-step` (`packages/core/agent-loop/src/agent.ts:230`). And most of `packages/context` bypasses this registry: `agent-instructions`, `time-context`, `tmux-context` append `UserMessage`s via `agent/pre-step` listeners; actual `systemPrompt.context()` callers are sandbox policy, approval policy, subagent delegation. Ordering is a single numeric `order` field, not a phase enum.

**Minimal Python rebuild.** A registry of named ordered contributors with `(assemble_context) -> str` callbacks + strict `{{var}}` interpolation; separation of static system text from dynamic context (re-emitted user message deduped against the last snapshot) so clock/cwd refreshes never break prompt-prefix caching; one tool-schema provider hook with a deterministic ordering rule.

**Tutorial question.** Why is time/cwd/sandbox state delivered as a re-emitted user message rather than regenerated into the system prompt each step? (Prompt-prefix caching, a durable record of what the model actually saw, replay fidelity.)

### Layer 6 — Capability seams (fs / shell / sandbox / llm)

**Mechanism.** A swappable capability splits into three roles: a Cordis `Service` subclass owning exactly one `ctx.<key>` plus the vocabulary types (Service Definition, never a bare TS interface), plugins registering a concrete implementation under that key (Service Provider), and plugins that `inject` the key and expose it, usually as a model-facing tool (Consumer). A backend swap never touches the schema the model sees (`.agents/notes/implemented/architecture/2026-06-13-capability-seams.md`, `docs/glossary.md`).

**Key files.**
- fs: Definition `packages/fs/fs/src/index.ts` (`abstract class FileSystem extends Service`, `ctx.fs`, line 86); Providers `packages/fs/fs-local/src/index.ts` (`LocalFileSystem`), `packages/fs/fs-sandbox/src/index.ts` (`SandboxedFileSystem`); Consumers `packages/fs/tool-fs/src/{read,write,edit,read-image}.ts` (tools `read`, `write`, `edit`, `read_image`; plus `glob`, `grep`, `str_replace_editor` elsewhere in `packages/fs/`)
- shell: Definition `packages/shell/shell/src/index.ts` (`abstract class ShellExecutor extends Service`, `ctx.shell`, line 65; one implementation per context, a second throws, lines 48-50); Providers `packages/shell/bash-local/src/index.ts` (`LocalBashExecutor`), `packages/shell/bash-sandbox/src/index.ts` (`SandboxBashExecutor`); Consumer `packages/shell/tool-bash/src/index.ts` (tool `bash`, line 243)
- sandbox: Definition `packages/sandbox/sandbox/src/index.ts` (`abstract class SandboxProvider extends Service`, `ctx.sandbox`, line 158; sole abstract method `confine(argv, policy): ConfinedArgv`); Provider `packages/sandbox/sandbox-local/src/index.ts` (runner chain `linux: ['bwrap','landlock']`, `darwin: ['seatbelt']`, line 160) + `packages/sandbox/sandbox-windows-acl/`
- llm: Definition+Consumer folded in `packages/llm/llm/src/index.ts` (`LlmRuntime extends Service`, `ctx.llm`, line 284; `abstract class LlmAdapter` line 180 with `stream(options): AsyncIterable<StreamChunk>`); Providers `packages/llm/llm-deepseek/src/index.ts`, `packages/llm/llm-pi-ai/src/index.ts` via `ctx.llm.registerAdapter(providers, adapter)`
- Seam-owned events: `fs/write-intent`, `fs/edit-intent` (waterfalls), `fs/observed` (emit) (`packages/fs/fs/src/index.ts:49-77`); `llm/stream` waterfall (`packages/llm/llm/src/index.ts:51-60`); shell and sandbox own no events.

**Corrections vs candidate.** "Definition/Provider/Consumer triples for fs/shell/sandbox" is directionally right, wrong on membership: (1) **sandbox is not a tool triple** — its Consumers are providers of other seams (`SandboxBashExecutor` calls `ctx.sandbox.confine(['bash','-c',command], policy)`, `packages/shell/bash-sandbox/src/index.ts:178`; `SandboxedFileSystem` fences via `ctx.sandboxPolicy`, `packages/fs/fs-sandbox/src/index.ts:127`); it is a process-confinement seam with no model-facing tool. (2) **llm belongs in this layer as the deliberate exception**: Definition and Consumer folded into `dsh-llm` because the Consumer is the loop itself (`packages/core/agent-loop/src/index.ts:297` injects `llm`), not a swappable schema surface. (3) Provider cardinality differs by seam: `ctx.shell`/`ctx.fs` are exclusive (second registration throws); `ctx.llm`/`ctx.subagents`/`ctx.skills` are plural registries.

**Minimal Python rebuild.** One ABC per capability + fail-loud duplicate registration for exclusive seams, a register-dict for plural ones; tools import only the ABC + vocabulary dataclasses, never a provider module (that import discipline IS the seam); sandbox as a pure argv-rewriting `confine(argv, policy)` that fails closed, so the sandboxed shell provider is a small decorator over the local one.

**Tutorial question.** When does a capability earn the three-package split versus folding roles? (The repo's rule: not preemptively — one provider + one consumer stays one package until a second appears; `dsh-llm` is the standing counterexample where the Consumer is the loop.)

### Layer 7 — Skills (moved out of "subagent/jobs/skills")

**Mechanism.** `ctx.skills` is a layered, scope-aware provider registry (`SkillLayer implements ScopeLayer`) resolving skill names to instruction text; consumers publish a catalog into the agent's context via `agent/pre-step` listeners and load skill bodies on demand with the `skill` tool (`packages/skill/skill/src/index.ts:357`, `packages/skill/tool-skill/src/index.ts:82,177,213`).

**Key files.** `packages/skill/skill/src/index.ts` (`SkillRegistry extends Service`; `SkillProvider` interface at line 248; provider registration is a factory taking a `SkillProviderControl`, line 391); `packages/skill/skill-filesystem/src/index.ts` (`FileSystemSkillProvider`, line 146); `packages/skill/tool-skill/src/index.ts`; event `skills/change` (line 297); `docs/subsystems/skills.md`.

**Corrections vs candidate.** Candidate grouped skills with subagent/jobs. Wrong: skills never touches `ctx.jobs` or background work (no hit for `dsh-jobs|ctx.jobs` under `packages/skill/`); its delivery mechanism is prompt/message injection, the same plane as layer 5. Kept adjacent to layer 5 in tutorial order.

**Minimal Python rebuild.** A resolver `list(cwd) -> [summary]` / `get(name) -> text` over skill directories; one pre-step hook appending a catalog block; cache invalidation on provider change.

**Tutorial question.** Why is a skill catalog injected as context while skill bodies load through a tool call? (Token economy: pay for summaries always, bodies only on use.)

### Layer 8 — Jobs (background-job runtime)

**Mechanism.** `ctx.jobs` is an owner-fenced background-work protocol: a producer hands `start()` a `run()` returning `{cancel, done, readOutput?}` and gets a `JobId`; the registry owns ids, authorization (every read/kill/wait is denied unless the caller session owns the job), snapshots, first-wins settlement, and completion notices delivered as `owner.followup()` (idle) or `owner.inject()` (busy) (`packages/jobs/jobs/src/index.ts:62`, `packages/jobs/tool-jobs/src/index.ts:279-300`).

**Key files.** `packages/jobs/jobs/src/index.ts` (`abstract class JobRegistry extends Service` — note: not "JobsService"), `packages/jobs/jobs/src/types.ts` (`JobId`, `JobStart`, `JobSnapshot`, `JobOutcome` with `status: 'completed' | 'killed' | 'failed'`, `CompletionDelivery = 'quiet' | 'wakeup'`, `JobKindMap` with exactly `bash` and `subagent`), `packages/jobs/jobs-local/src/index.ts` (`LocalJobRegistry`), `packages/jobs/tool-jobs/src/index.ts` (tools `job_output`, `job_list`, `job_kill`, lines 303, 343, 363), design notes `.agents/notes/implemented/architecture/2026-06-20-generic-long-running-tool-runtime.md`, `2026-07-26-job-registry-seam.md`.

**Corrections vs candidate.** Jobs is not subagent infrastructure: bash and subagent are peer producers (`JobKindMap`, `packages/jobs/jobs/src/types.ts:23-26`); both `tool-bash` and `tool-subagent` acquire jobs by optional lookup `ctx.get('jobs')`, not `inject` (`packages/shell/tool-bash/src/index.ts:354-356`, `packages/subagent/tool-subagent/src/index.ts:402-405`). No Cordis events — delivery is callback-based (`onJobDone` / `onJobsChanged`).

**Minimal Python rebuild.** `start(kind, label, owner, run) -> job_id` with `run()` returning `(cancel, done_awaitable, read_output)`; owner fencing on every accessor; first-wins settlement; then `job_output`/`job_list`/`job_kill` written once for all producers.

**Tutorial question.** When does a long-running tool need a job, and who owns cancellation once the id is published? (The producing call's signal stops mattering the instant `jobs.start()` returns; a pre-aborted background call must fail, not no-op.)

### Layer 9 — Subagent (delegation)

**Mechanism.** `ctx.subagents` (`SubagentRuntime extends Service`, concrete) is a named-provider registry: a `SubagentProvider` (a TS interface, not a Service — `packages/subagent/subagent/src/types.ts:285`) turns a resolved start request into a `SubagentRun`, typically by creating a child `Agent` through `parent.ctx.agents.create()` (`packages/subagent/subagent-in-process-driver/src/index.ts:132`); ownership transfers to the parent on fulfillment, and a continuation manager makes children durable across turns.

**Key files.** `packages/subagent/subagent/src/index.ts` (runtime, line 171; events `subagent/provider-added`, `subagent/provider-removed`, `subagent/start`, `subagent/end`, lines 134-167), `types.ts`, `continuation.ts`, `run-settlement.ts`; providers `subagent-spawn-in-process/`, `subagent-fork-in-process/`, `subagent-acp/`, `subagent-codex/`, `subagent-claude-code/`, `subagent-dsh-sdk/`; consumers `tool-subagent/src/index.ts` (tool `subagent`, name configurable), `tool-subagent-control/src/index.ts` (`send_message`, `interrupt_agent`, `list_agents`), `tool-subagent-report/src/index.ts` (`report`); `docs/subsystems/subagent.md`.

**Corrections vs candidate.** "Provider-registry contract + delegation tool" undercounts: the runtime also owns the continuation manager, child/descendant discovery, activation-setup registry, descriptor snapshots, and the child-to-parent report channel (three consumer packages, five tool names). Delegation has three modes and only one touches jobs: foreground (`await ctx.subagents.start()`), one-shot background (`jobs.start({kind:'subagent', ...})`, `packages/subagent/tool-subagent/src/index.ts:408-423`), continuable (`startContinuable()`, no job) (`packages/subagent/subagent/src/run-settlement.ts:2-4`).

**Minimal Python rebuild.** A name-to-factory dict returning a child agent + awaitable result; foreground awaits it, background hands it to jobs; skip continuable children in v1 (largest single piece of the package).

**Tutorial question.** Why is a subagent provider an interface over "establish a child and hand back a run" instead of a subclassed agent? (Providers range from an in-process child to another product over ACP; the parent-side contract must not know.)

### Layer 10 — Composition (profiles, bundles, patches, presets)

**Mechanism.** A running dsh is one flat Cordis Loader entry list (YAML rows `{id, name, config}`) built by applying an ordered stack of patch layers over an empty root list (`applyEntryPatches`, `vendor/include`), then mounted by the Loader; the same mechanism recurs per-agent as presets (`packages/boot/app-boot/src/index.ts:757` — `boot()`: `new Context()` then `ctx.plugin(Loader)`).

**Key files.** `apps/cli/src/bin.ts` (entry, bin `dsh`), `apps/cli/src/args.ts` (`--profile`), `apps/cli/src/profile-boot.ts` (layer stacking, lines 142-171), `packages/boot/app-boot/src/profile.ts` (`loadProfile`, `PROFILE_TEMPLATES` lines 114-117, `resolveBundleDir` line 344), `packages/bundle/base/cordis.patch.yml` (`@deepseek-ai/dsh-base`, 78 rows), `packages/bundle/headless/cordis.patch.yml` (6 rows), `packages/bundle/web-app/cordis.patch.yml`, `packages/preset/agent-presets/src/mount.ts`, `apps/cli/config/agent-presets/{minimal,standard,code,cordis}/agent.cordis.yml`, `docs/architecture.md` (Profiles and bundles).

**Core semantics.** Layer order: bundles in `dsh.profile.bundles` order, then profile `cordis.patch.yml`, then `$DSH_HOME/cordis.patch.yml`, then `--patch` overlays (`apps/cli/src/profile-boot.ts:142-171`). A patch targets a row by id and **replaces its whole `config`** (never merges) or inserts rows (`packages/bundle/base/cordis.patch.yml:6-10`). Row order carries no load semantics — activation is service-availability-driven, audited by `assertEntriesActivated` (`packages/boot/app-boot/src/index.ts:700-725`). Shipped templates: `web = [dsh-base, dsh-web-app]`, `headless = [dsh-base, dsh-headless]`.

**Corrections vs candidate.** `dsh-base` is real (`packages/bundle/base/package.json`, name `@deepseek-ai/dsh-base`). Two fixes: (1) presets are not a profile layer — `@deepseek-ai/dsh-agent-presets` mounts an `agent.cordis.yml` subtree once per process and each session joins by parenting its agent scope (`packages/preset/agent-presets/README.md:5-7`); profile composition is process-wide, presets per-agent. (2) `agent-spine-demo` lives at `packages/examples/agent-spine-demo` and is a code bundle plugin, not a runnable composition; runnable leaves live in repo-root `examples/` (`packages/examples/README.md:13,17`).

**Minimal Python rebuild.** An ordered-row registry keyed by id + a patch applier with three verbs (config replace, disable, insert); bundle-name module resolution; dependency-driven activation with a settle-then-audit pass that names rows still pending on missing services.

**Tutorial question.** Why is a patch a whole-config replace rather than a deep merge, and what does that force on bundle authors? (A row whose value differs by mode cannot live in base; each mode bundle restates complete config, keeping any row to one bundle layer plus the user's.)

## Candidate tutorial section questions

1. Kernel: why is unloading a plugin something the framework can do correctly, rather than cleanup each plugin must remember?
2. Session: if the log is append-only, how does compaction ever remove anything from what the model sees?
3. Agent loop: why re-assemble the prompt and re-derive history from the log at every step instead of keeping a live message list?
4. Tools: why does a denied or errored tool call still produce a normal `tool/result` in the log instead of raising?
5. System prompt: why is dynamic state (time, cwd, sandbox mode) delivered as a re-emitted user message rather than baked into the system prompt?
6. Capability seams: when does a capability earn the Definition/Provider/Consumer split versus folding roles into one package?
7. Skills: why inject a catalog as context but load skill bodies through a tool call?
8. Jobs: who owns cancellation of background work once the job id is published?
9. Subagent: why is a provider an interface over "establish a child and hand back a run" instead of a subclassed agent?
10. Composition: why is a patch a whole-config replace rather than a deep merge?

## Method note

Findings verified against the shallow clone at the pinned commit; layer claims cross-checked between package READMEs, `docs/architecture.md`, `docs/subsystems/*`, and the cited source files (identifiers and line numbers grepped directly at the pin).

