# 01 · Kernel

> Every registration is an undo the framework collects. Unloading a plugin is running its undos in reverse.

This page continues the [mini-dsh track](../../README.md): phase Foundation, section 01,
the lifecycle kernel every later section mounts its pieces on.

dsh's slogan is "everything is a plugin": tools, session stores, prompt sections, whole
subsystems mount and unmount at runtime: on profile switches, hot reloads, test teardown,
subagent shutdown. The naive way to make that safe is a convention: every plugin writes a
`cleanup()` that unregisters whatever it registered. Conventions drift. One plugin adds a
listener in a new code path, forgets the matching unregister, and now unloading it leaks a
callback that fires against a dead plugin forever.

The kernel flips the ownership. Registering something *is* handing the framework the undo.
For that to hold, the kernel must:

1. Make every registration (listener, service, anything) produce an undo (a **disposer**).
2. Collect disposers on the **fiber** that owns the plugin, so unmount is one framework
   operation: run them in reverse.
3. Run each disposer at most once, no matter who calls it first.
4. Refuse registrations on an already-disposed fiber: an error, not a silent leak.

---

## Mechanism

Three moving parts:

- **Fiber**: the lifetime unit of one mounted plugin. An ordered list of undos plus a state
  (`loading → active → disposed`).
- **`effect()`**: the one primitive. Give it an undo; the fiber collects it and hands back a
  single-shot disposer.
- **Context**: a plugin's view of the app. Every registration API (`on`, `provide`) is built
  on `effect()`, so registrations made through a plugin's context land on that plugin's fiber.

A plugin is just a function that receives its context:

```python
def echo_plugin(ctx):
    ctx.on("say", heard.append)                       # registration #1
    ctx.provide("echo", lambda t: f"echo: {t}")       # registration #2
    # no cleanup code: the undos are already on this plugin's fiber

fiber = ctx.plugin(echo_plugin)   # mount
fiber.dispose()                   # unmount: listener and service both gone
```

The fiber is a disposer list with a state gate:

```python
def collect(self, dispose, label=""):
    if self.state == "disposed":
        raise InactiveEffectError(...)
    entry = {"dispose": dispose, "done": False}
    def run():
        if not entry["done"]:
            entry["done"] = True
            entry["dispose"]()
    self._disposers.append(run)
    return run          # single-shot: safe to call early, fiber won't re-run it

def dispose(self):
    self.state = "disposed"
    for run in reversed(self._disposers):
        run()
```

And every registration API is three lines: do the thing, hand `effect()` the undo:

```python
def on(self, event, callback):
    listeners = self._root._listeners.setdefault(event, [])
    listeners.append(callback)
    return self.effect(lambda: listeners.remove(callback), f"on({event})")
```

Mount and unmount are now symmetric by construction:

```text
mount:    plugin(apply) ──► new Fiber ──► apply(child ctx)
                                          each ctx.on / ctx.provide / ctx.effect
                                          pushes one undo onto the fiber
unmount:  fiber.dispose() ──► undos run in reverse ──► registrations gone
```

Reverse order matters: a plugin registers its foundations first and the things that depend
on them after, so teardown must dismantle the dependents before the foundations, the same
reason destructors and `defer` stacks unwind backwards.

### What changed

Compared with section 00:

- `message.py` and `standin.py` are carried forward verbatim; the diff against 00 is this
  section's mechanism, nothing else.
- New `kernel.py`: `Fiber`, `effect()`, and a `Context` whose registration APIs all route
  through `effect()`.
- Nothing uses the kernel yet. Section 02's session log mounts as a service on it.

---

## In real dsh

All pointers are into the pinned studied version,
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca).
The kernel is Cordis, source-vendored and locally patched under `vendor/`
([`vendor/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/README.md)).

| Mini-dsh | Real dsh | Notes |
| --- | --- | --- |
| `Fiber` | [`vendor/cordis/src/fiber.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/fiber.ts): `Fiber`, `effect()` | Six states (`PENDING, LOADING, ACTIVE, FAILED, DISPOSED, UNLOADING`) vs our three; effects also accept `Promise` and `(Async)Iterable` shapes. |
| `InactiveEffectError` | `CordisError('INACTIVE_EFFECT')` in `fiber.ts` | Thrown for effects created while `UNLOADING`. |
| `Context` | [`vendor/cordis/src/context.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/context.ts) | A `Proxy` over itself, with `extend` / `isolate` / `intercept` we skip entirely. |
| `on` / `emit` | [`vendor/cordis/src/events.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/events.ts) | Five dispatch modes (`emit / parallel / serial / bail / waterfall`); we build only `emit`, later sections add modes as the loop needs them. |
| `provide` / `get` | [`vendor/cordis/src/reflect.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/reflect.ts), [`service.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/service.ts) | The `Service` base class self-registers via `ctx.reflect.provide` in its constructor. |
| `plugin(apply)` | [`vendor/cordis/src/registry.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/registry.ts) | Plugins come in Function / Constructor / Object forms with `inject` declarations; ours is the Function form only. |

What the real kernel adds on top of this section's mechanism:

- **`inject`-driven reload**: a fiber declares the services it needs; when an injected
  service's provider changes, the fiber reloads automatically (provider-uid epochs in
  `fiber.ts`). Reversibility is what makes that cascade safe: reload is just
  dispose-then-mount.
- **HMR as the same code path**: hot module replacement (`vendor/hmr/`) is dispose + re-mount
  of the changed plugin's fiber. Excluded from the mini-dsh (Ceiling): it is file-watching
  plumbing over the mechanism this section already built.
- Config-driven mounting: [`vendor/loader/src/config/entry.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/loader/src/config/entry.ts)
  turns a config row into a mount/unmount; section 13's composition layer stands on it.

---

## Failure modes

- **Cleanup outside `effect()` is invisible.** A plugin that mutates global state directly
  (opens a file, spawns a thread) without wrapping the undo in `ctx.effect()` leaks on
  unmount, and the framework cannot even see it. The discipline is total or it is nothing:
  *every* side effect goes through `effect()`.
- **A throwing disposer halts the unwind.** One bad undo aborts the rest of the fiber's
  cleanup. The mini-dsh accepts this to stay small; real Cordis isolates disposer errors so
  one plugin's bug cannot wedge another's teardown.
- **Undo depends on already-undone state.** Reverse order protects dependents within one
  fiber, but nothing orders *across* fibers here. Real dsh layers `inject` on top so
  dependent fibers unload before their providers.
- **Registering during teardown.** A callback that fires mid-dispose and registers a new
  effect would leak it silently, which is why a disposed fiber throws
  `InactiveEffectError` instead of accepting the registration.
- **Holding a service reference across unmount.** `ctx.get("echo")` returns a live object;
  a caller that caches it keeps it working after its provider is gone. Real dsh's proxies
  and `inject` gating narrow this window; the mini-dsh just tells you the rule: resolve at
  use time, never cache.

---

## Runnable

[`src/`](src/) carries 00 forward and adds:

- [`kernel.py`](src/kernel.py): `Fiber`, `effect()`, and a `Context` with `plugin` /
  `on` / `emit` / `provide` / `get`, every registration built on `effect()`.
- [`test.py`](src/test.py): mount-then-unmount reversibility, reverse-order unwind,
  single-shot disposers, the disposed-fiber error, and neighbor isolation.

```bash
python sections/01-kernel/src/test.py   # offline checks, no key
```

This section never calls the model, so there is no `demo.py`.

---

## Sources

- [`docs/cordis-primer.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/cordis-primer.md):
  dsh's own introduction to the kernel.
- [`vendor/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/README.md):
  the vendoring manifest and dsh's 18 local modifications to upstream Cordis.
- [cordiverse/cordis](https://github.com/cordiverse/cordis): the upstream framework
  (dsh pins `56b3d4f`).
