# Curriculum spec

The locked spec for writing **learn-deepseek-harness**: a build-it-yourself tutorial in the
[learn-agent-memory](https://github.com/hardness1020/learn-agent-memory) format, where the
reader rebuilds a minimal DeepSeek Harness (**mini-dsh**) in zero-dependency stdlib Python,
with the real system as the reference.

This spec is the output of the wayfinder map
([#1](https://github.com/hardness1020/learn-deepseek-harness/issues/1)); each decision links
its ticket. A writer produces sections mechanically from this document; nothing here is open
for redesign during section-writing.

## Vocabulary

- **dsh**: the real TypeScript harness under study.
- **Mini-dsh**: the stdlib-Python rebuild the reader constructs.
- **Section**: one tutorial unit adding exactly one Mechanism, carrying the prior `src/` forward.
- **Mechanism**: the single design idea a Section adds, answering its one design question.
- **Ceiling**: the mechanisms the rebuild excludes; pointed at in In-real-dsh, never rebuilt.
- **Model seam**: the swappable callable through which mini-dsh asks for a model response.
- **Studied version**: the single pinned dsh commit all real-source links reference.

## 1. Studied version

Decided in [#2](https://github.com/hardness1020/learn-deepseek-harness/issues/2).

- Subject: [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness),
  Cordis-based, "everything is a plugin", developer preview.
- Pin: [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)
  (`99f6f02fecdb7dff40c3fbc9470f5907c29f74ca`), version `0.1.0-rc.7`, 2026-08-17.
- Every link into dsh source is pinned to this SHA. Bumps are deliberate, never tracked live
  (re-pin policy is an open ticket on the map).
- Layer map with citations (10 layers, verified against source):
  [`docs/research/dsh-layers.md` on `research/dsh-layers`](https://github.com/hardness1020/learn-deepseek-harness/blob/research/dsh-layers/docs/research/dsh-layers.md).
  This is the writer's source for In-real-dsh pointers.

## 2. Scope and Ceiling

Decided in [#3](https://github.com/hardness1020/learn-deepseek-harness/issues/3).

All 10 dsh layers are rebuilt in mini-dsh. The Ceiling is per-mechanism, not per-layer.
Excluded mechanisms (pointed at, not rebuilt):

- continuable subagents
- HMR
- real sandbox confinement (argv-rewrite stub instead)
- non-JSONL persistence backends
- UI

Carrying rule, quoted across the tutorial: "Everything is a plugin, and every registration
is reversible."

## 3. Section list

Decided in [#3](https://github.com/hardness1020/learn-deepseek-harness/issues/3).
14 sections, 4 phases. Locked ordering rationale: skills sits after system-prompt
(prompt-plane pedagogy); inbox sits after scheduler (multi-step turns only exist once tools do).

### Foundation

| # | Slug | Mechanism | Design question |
|---|---|---|---|
| 00 | setup | repo skeleton, runner, model stand-in seam | why must every section's check run offline against a stand-in? |
| 01 | kernel | fiber/effect reversible registrations | why can the framework unload a plugin correctly, rather than per-plugin cleanup? |
| 02 | session-log | append-only log + surface + deriveMessages | why derive model history from a log instead of storing a message list? |
| 03 | compaction | surface `replace` op | if the log is append-only, how does compaction remove anything the model sees? |

### The Loop

| # | Slug | Mechanism | Design question |
|---|---|---|---|
| 04 | agent-loop | turn/step machine, log = only durable state | why re-assemble the prompt and re-derive history every step? |
| 05 | tools | scoped registry + pre/ask/guard/execute/post pipeline | why does a denied/errored call still produce a normal tool/result? |
| 06 | scheduler | 4-stage parallel tool scheduler | why do parallel-safe calls overlap, exclusive calls form barriers, and aborted-unstarted calls get synthetic results? |
| 07 | inbox | next-turn/next-step steering | why two inbox targets, and why claim only at step boundaries? |
| 08 | system-prompt | ordered providers -> system text + tool list + runtime-context snapshot | why is dynamic state a re-emitted user message rather than system text? |
| 09 | skills | layered provider registry; catalog injected, bodies on demand | why inject a catalog as context but load bodies through a tool call? |

### Capabilities

| # | Slug | Mechanism | Design question |
|---|---|---|---|
| 10 | capability-seams | Definition/Provider/Consumer ABCs (fs/shell/sandbox/llm) | when does a capability earn the three-way split? |
| 11 | jobs | owner-fenced background-work protocol | who owns cancellation once the job id is published? |
| 12 | subagent | named-provider delegation registry | why an interface over "establish a child, hand back a run", not a subclassed agent? |

### Composition

| # | Slug | Mechanism | Design question |
|---|---|---|---|
| 13 | composition | ordered patch layers over an empty entry list | why is a patch a whole-config replace, not a deep merge? |

## 4. Per-section template

Decided in [#5](https://github.com/hardness1020/learn-deepseek-harness/issues/5). Reference
implementation (locked as drafted): sections 00 and 01 on
[`prototype/section-01-kernel`](https://github.com/hardness1020/learn-deepseek-harness/tree/prototype/section-01-kernel/sections).

### Directory and naming

- `sections/NN-slug/`, two-digit dirs. README title: `# NN · Name`.
- Carry-forward: section N's `src/` = section N-1's `src/` copied **verbatim** plus this
  section's Mechanism, so the diff between adjacent sections is exactly the Mechanism.

### README slots, in order

1. Epigraph blockquote.
2. Opening prose + numbered requirements.
3. `## Mechanism`: moving parts, code excerpts, text flow diagram, ending with
   `### What changed` vs the prior section.
4. `## In real dsh`.
5. `## Failure modes`: bold-lead bullets.
6. `## Runnable`: "`src/` carries N-1 forward and adds: ...", the run line, and a demo note.
7. `## Sources`.

### In-real-dsh slot form

- A mini <-> real mapping table, per-symbol links pinned to the Studied version SHA.
- Followed by "what the real X adds" bullets, carrying Ceiling notes: excluded mechanisms
  are pointed at here, not rebuilt.

### Checks

- `test.py` inside `src/`, plain assert + print. Per-section scope: tests only this
  section's Mechanism; prior sections' checks stay runnable in their own dirs.
- Run line: `python sections/NN-slug/src/test.py`. Deterministic, no key, no network.
- Assertions target the session log, never the stand-in's internals.

## 5. Model strategy

Decided in [#4](https://github.com/hardness1020/learn-deepseek-harness/issues/4)
(ADR 0001, local).

- **Model seam**: a plain callable streaming chunks then one final message, in mini-dsh's
  own Message shape (provider-agnostic, like real dsh).
- **Offline (every section)**: section 00 ships a passive **scripted stand-in**: an ordered
  queue of canned responses, never inspecting the request, splitting each response into a
  few deterministic chunks so `assistant/chunk` events are real from day one.
- **Live (model-touching sections only, 04+)**: `demo.py` runs scripted turns against the
  real Anthropic API via the `anthropic` SDK + `python-dotenv`; skips politely without a
  key. `demo.py` is the only place the SDK and the ~20-line mini-Message -> Anthropic
  translation live. No interactive REPL anywhere.
- Root `requirements.txt` (anthropic, python-dotenv) and `.env.example`
  (`ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, optional base URL). The zero-dependency promise
  covers the mini-dsh the reader builds and checks offline; live-demo deps sit outside it.

## 6. Repo skeleton

```text
learn-deepseek-harness/
├── README.md            # anatomy in section 7 below
├── SPEC.md              # this file
├── LICENSE
├── requirements.txt     # live demos only: anthropic, python-dotenv
├── .env.example         # ANTHROPIC_API_KEY / ANTHROPIC_MODEL / optional base URL
└── sections/
    ├── 00-setup/
    │   ├── README.md
    │   └── src/         # message.py, standin.py, test.py
    ├── 01-kernel/
    │   ├── README.md
    │   └── src/         # 00's src verbatim + kernel.py, test.py
    ├── ...
    └── 13-composition/
        ├── README.md
        └── src/         # 12's src verbatim + this Mechanism, test.py, demo.py
```

`demo.py` appears inside `src/` only where the Mechanism touches the Model seam (04+).

## 7. Root README anatomy

Decided in [#7](https://github.com/hardness1020/learn-deepseek-harness/issues/7). Adapted
from learn-agent-memory; no dedicated system-under-study section (the whole repo is about
dsh: the badge is the pin surface, the pitch names the system).

1. Centered header: title, tagline, badges: `Studied: dsh 0.1.0-rc.7` (links to the pinned
   tree at `99f6f02`) and `License: MIT`. No language-links line until translations exist.
2. Pitch prose naming and linking dsh, then a one-line Contents nav.
3. Big picture: phase-pipeline text diagram (Foundation > The Loop > Capabilities >
   Composition, with section numbers) and the carrying-rule blockquote. The text diagram is
   interim; it swaps for an image when the visual-assets ticket resolves.
4. How to learn: the 4-part lens (Opening / Mechanism / In real dsh / Failure modes), read
   in order, run the offline checks, diff adjacent `src/`.
5. Sections table: 14 rows with 4 phase divider rows; columns # | Section (linked) |
   Design question | Mechanism, straight from section 3 above.
6. Repository structure tree.
7. Running: offline checks (stdlib, no key, no network) plus the live-demo block.
8. Contributing: deepen a section / correct the record. No add-a-system bullet.
9. References: dsh's own docs pinned to the SHA (cordis-primer, cordis-tutorial, subsystem
   docs) plus upstream [cordiverse/cordis](https://github.com/cordiverse/cordis).

Link-out policy: root References lists dsh's official docs; section-level links live only
in the In-real-dsh and Sources slots; plugin-authoring how-to defers entirely to dsh's own
cordis-tutorial.

## 8. Writing a section: checklist

1. Copy section N-1's `src/` verbatim into `sections/NN-slug/src/`.
2. Build the Mechanism (stdlib only) as new or changed files; the diff vs N-1 is the
   Mechanism, nothing else.
3. Write `test.py` per the check rules in section 4.
4. If the Mechanism touches the Model seam, write `demo.py` per section 5.
5. Write the README following the slot order in section 4; fill In-real-dsh from the layer
   map and pinned dsh source.
6. Run every section's `test.py` offline; all must pass.
7. Keep the root README sections table row consistent with section 3.
