# 09 · Skills

English | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

> Instruction text is too big to ship every step and too useful to leave
> out. So the request carries only the names, and the text itself is
> fetched when something wants it.

Section 08's request carries stable system text and a changing
snapshot, but every word it carries still ships every step.
Instruction text does not fit that budget: a harness accumulates
how-to text for specialized work, and any one turn uses almost none
of it.

The two obvious homes are both wrong. Bake every instruction into
the system text and every request pays for all of them, used or
not. Leave them out entirely and the model cannot use what it never
hears about.

And the set is not fixed. Skill text has many owners: a built-in
set, a workspace, a plugin. Each mounts, unmounts, or overrides a
name while the session runs, and none may edit another's text to
do it.

So: why inject a catalog as context but load skill bodies through a
tool call?

Because what exists must be cheap and always visible, while what it
says must be paid for only on use. For that, the registry must:

1. Hold providers, not skills: each resolves names to instruction
   text through two verbs, `list()` for summaries and `get(name)`
   for one full body.
2. Layer providers: a later registration shadows an earlier one's
   names, and every registration hands back its undo.
3. Inject the catalog as context: names and one-line descriptions
   ride the runtime-context snapshot, re-emitted only when the
   catalog changes.
4. Load bodies on demand through one `skill` tool, so the text
   lands as an ordinary `tool/result` row.
5. Answer an unknown name with a normal error result, never a
   raise.
6. Ship nothing when the catalog is empty.

---

## Mechanism

One new file, `skills.py`, and no carried file moves:

- **`SkillRegistry`**: layers of providers, in registration order.
  `catalog()` merges every provider's `list()` summaries, one line
  per visible name, a later layer's line winning. `get(name)` walks
  the layers in reverse and returns the first body found.
  `register()` hands back its undo, kernel-style.
- **`MemorySkillProvider`**: the simplest provider, a dict of
  `name -> {"description", "body"}`. Any object with `list()` and
  `get(name)` is a provider; `list()` never volunteers a body.
- **`skills_plugin`**: wires the split. One section 08 context
  provider renders `catalog_text()` into the snapshot, one `skill`
  tool loads bodies, and the registry is provided as `skills`.

```python
def catalog(self):
    """One summary per visible name; a later layer's line wins."""
    merged = {}
    for provider in self._providers:
        for summary in provider.list():
            merged[summary["name"]] = summary
    return list(merged.values())

def get(self, name):
    """One full body, nearest layer first; None if no layer knows the name."""
    for provider in reversed(self._providers):
        body = provider.get(name)
        if body is not None:
            return body
    return None
```

The catalog needs no new delivery machinery. It is one more context
provider on the section 08 registry, so the snapshot dedupe already
decides when it re-enters the log: a provider change re-emits it, a
quiet catalog costs nothing.

```python
ctx.effect(
    ctx.get("system_prompt").context(
        "skills", lambda ac: skills.catalog_text(), order=100
    ),
    "skill catalog",
)
```

Bodies cross the other way, through the tool pipeline built in
section 05. An unknown name raises inside the body, and the
pipeline turns that into a normal `is_error` result, so the
transcript keeps its shape:

```text
registered, layered              every step (the section 08 plane)

built-in   greet, haiku ─┐ catalog() ─► skills, load with the      ─► same as the last
workspace  greet         ┘             skill tool before use:         snapshot row?
  (shadows the built-in)               - greet: <workspace's line>    ├─ yes: nothing
                                       - haiku: answer as a haiku     └─ no: user/message

on demand, mid-turn (the model asks)

tool/call    skill {"name": "haiku"}
               │  get("haiku"): reverse layer walk, first body wins
tool/result  the full instruction text, an ordinary row
```

Here is a real run, as the log records it. The catalog names two
skills; the model loads one body, follows it, and the second step
finds the catalog unchanged:

```text
send("hi")
  │   0  turn/start
  │   1  step/start
  │   2  user/message   "hi"                       ◄ claimed at the boundary
  │   3  user/message   "skills, load with ..."    ◄ the catalog: names and
  │   4  request/header tools ["skill"]              one line each, no bodies
  │   5  assistant/message {"tool_calls": [skill "haiku"]}
  │   6  tool/call     skill {"name": "haiku"}
  │   7  tool/result   "Answer with one haiku: ..." ◄ the body, on demand
  │   8  step/end      {"reason": null}
  │   9  step/start                                ◄ catalog unchanged:
  │  10  request/header                              no new snapshot row
  │  11  assistant/chunk "do"
  │  12  assistant/chunk "ne"
  │  13  assistant/message "done"
  │  14  step/end      {"reason": "completed"}
  │  15  turn/end
```

The body at seq 7 is derived history now, a plain `tool` message:
the model pays for it in every later request of the session, but
only because it asked. The `greet` body was never asked for and
never cost a token.

### What changed

Compared with section 08:

- Every carried file is verbatim: `agent_loop.py`, `inbox.py`,
  `kernel.py`, `message.py`, `scheduler.py`, `session_log.py`,
  `standin.py`, `system_prompt.py`, `tools.py`. `skills.py` is the
  only new source file, so the diff against 08 is this section's
  Mechanism, nothing else.
- The loop did not change because the Mechanism is pure plugin: the
  catalog enters through a section 08 context provider, and bodies
  enter through a section 05 tool. This is the first Section whose
  Mechanism lands without touching a carried file.
- The log's shape gained no new event type. A snapshot row may now
  carry the catalog block, and a `tool/result` row may carry a
  skill body; derived history treats both as the plain rows they
  are.
- `demo.py`: the Live demo hands a real model a catalog, lets it
  load a body on demand, and registers a second provider between
  turns so the re-emit happens on a real model call.

---

## In real dsh

All pointers are into the pinned Studied version,
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca).
The registry lives in the skill package family:
[`packages/skill`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill).

| Mini-dsh | Real dsh | Notes |
| --- | --- | --- |
| `SkillRegistry` in `skills.py` | [`packages/skill/skill/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill/src/index.ts): `SkillRegistry` | The real registry extends `Service` under `ctx.skills`, a plural seam like the mini's. Its layers are scope-aware (`SkillLayer implements ScopeLayer`); the mini layers by registration order alone. |
| the provider duck type (`list()` / `get(name)`) | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill/src/index.ts): `SkillProvider` | An interface resolving names to instruction text (line 248), not a Service. Registration takes a factory handed a `SkillProviderControl` (line 391), the real form of the mini's undo callable. |
| `MemorySkillProvider` | [`packages/skill/skill-filesystem/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill-filesystem/src/index.ts): `FileSystemSkillProvider` | The shipped provider resolves skill directories on disk (line 146); the mini's dict-backed provider keeps the Offline check free of the filesystem. |
| the catalog context provider | [`packages/skill/tool-skill/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/tool-skill/src/index.ts) | Real consumers publish the catalog from `agent/pre-step` listeners (lines 177, 213), the pre-step plane section 08 pointed at. The mini has no pre-step hook, so its catalog rides the snapshot context plane instead. |
| the `skill` tool | [`tool-skill/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/tool-skill/src/index.ts) | The body loads on demand through the tool (line 82): the same catalog/body split, delivered by the same two planes. |
| the snapshot dedupe as change signal | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill/src/index.ts): `skills/change` | The real registry announces provider changes on a bus event (line 297) and consumers invalidate caches; the mini re-resolves per assembly and lets the snapshot dedupe absorb the quiet steps. |

What the real skills layer adds on top of this section's Mechanism:

- **Scope-aware layers.** `SkillLayer implements ScopeLayer`, the
  same machinery the tool registry uses, so a subagent's scope can
  see a different catalog than its parent. The mini's layers are
  global; its shadowing rule is the same idea one axis smaller.
- **Providers with a control handle.** Registration is a factory
  taking a `SkillProviderControl`, so a provider can push change
  notifications and the `skills/change` event fans them out to
  caching consumers. The mini re-renders the catalog per assembly
  and needs no cache to invalidate.
- **A filesystem provider.** `FileSystemSkillProvider` walks skill
  directories and reads summaries without loading bodies, so the
  token economy holds at the I/O boundary too.
- **The pre-step delivery plane.** The real catalog arrives as
  `user/message`s appended by `agent/pre-step` listeners, the plane
  most of `packages/context` uses. The mini reaches the same log
  rows through its section 08 context registry.

---

## Failure modes

- **Bodies in the catalog pay for everything always.** Inline every
  instruction and each request carries all of them while a turn
  uses at most one. `list()` yields names and a line apiece;
  `get(name)` is the only door a body leaves through.
- **A catalog in the system text moves the prefix.** Section 08's
  promise is byte-identical system text; a provider mounting
  mid-session would rewrite it and bust the prompt-prefix cache. As
  context, a catalog change costs one `user/message` row and the
  prefix holds.
- **An unknown name that raises tears the transcript.** The model
  will misspell a skill eventually. The `skill` tool's body raises,
  the section 05 pipeline answers with a normal `is_error` result,
  and the turn keeps going instead of crashing the loop.
- **Layering by timing shuffles the catalog.** If resolution
  depends on dict order or thread timing, identical registrations
  render different catalogs, and every difference re-emits a
  snapshot row for nothing. Registration order layers, later wins:
  the same providers always render the same text.
- **A cached catalog drifts from its providers.** Cache the
  rendered block and an unregistered provider keeps advertising
  skills that no longer resolve. The mini re-resolves at every
  assembly; the snapshot dedupe, not a cache, keeps the quiet steps
  free.

---

## Runnable

[`src/`](src/) carries 08 forward and adds:

- [`skills.py`](src/skills.py) (new): `SkillRegistry` with layered
  providers and shadowing resolution; `MemorySkillProvider`; the
  plugin wiring the catalog context, the `skill` tool, and the
  `skills` service.
- [`test.py`](src/test.py): the Offline check proves the catalog
  rides the snapshot row without a body in it, a body arrives only
  as a `tool/result` after a `skill` call, a provider change
  re-emits the catalog while an unchanged one stays quiet, a later
  layer shadows a name until its undo uncovers the layer below, an
  unknown name stays a normal error result, and an empty catalog
  ships nothing.
- [`demo.py`](src/demo.py): the Live demo lets a real model read
  the catalog, load a body on demand, and sign off with a skill
  whose provider registered between turns.

```bash
python sections/09-skills/src/test.py    # offline check, no key
```

The Live demo needs the root `requirements.txt` and a key; it skips
politely without one:

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/09-skills/src/demo.py
```

---

## Sources

- [`docs/subsystems/skills.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/skills.md):
  dsh's own tour of the skill registry, its providers, and the
  catalog/body split.
