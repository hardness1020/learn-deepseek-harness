# 13 · Composition

English | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

> After twelve sections, what a product actually mounts is still a Python
> function somebody edits. Two builds are the same plugins in different
> lists, so the harness stops being code and becomes a list.

Every section so far ended the same way: a check that assembled the
harness by hand. Mount the session log, mount the tools, mount the
loop, create the agent, wire the owner tools; twelve sections of
mechanisms, and the thing deciding which of them a product mounts
is still a Python function somebody edits.

Products cannot ship that way. A web build and a headless build are
the same plugins in different lists; a user wants to swap one row
of somebody else's build without forking it. The description of the
harness has to become data: one flat entry list, built by layers,
each layer owned by whoever speaks for it, base vendor first, user
last.

The first instinct for the patch verb is a deep merge: let the base
carry the common keys and let each mode patch only the keys it
changes. Real dsh refuses. A patch targets a row by id and replaces
the row's whole config, never merging.

So: why is a patch a whole-config replace, not a deep merge?

Because a merge makes a row's effective config emergent: to know
what a row means you replay every layer that ever touched it, and a
base default leaks into a mode that never asked for it. A replace
keeps each row's truth in exactly one place: the last layer to
touch it holds the whole story. The cost lands on bundle authors,
by design: a row whose value differs by mode cannot live in base at
all, and every mode restates that row's complete config. The
section builds it as:

1. One flat entry list as the product's whole description: ordered
   rows `{id, name, config}`, data only, no callables.
2. The list built by ordered patch layers over an empty list:
   bundles first, profile and user layers after, later layers
   winning.
3. Three patch verbs keyed by the row id: an unknown id inserts,
   a known id replaces the whole config, `disabled` removes.
4. A name table turning a row's name into a plugin factory, so
   data finds code in exactly one place.
5. Mounting driven by service availability, never by row position.
6. A settle-then-audit pass: when nothing more can mount and rows
   remain, refuse the boot and name each row and what it waits on.

---

## Mechanism

One new file, `composition.py`, and no carried file moves:

- **`apply_layers(layers)`**: ordered patch layers in, one flat
  entry list out. Three verbs, all keyed by id.
- **`mount_entries(ctx, entries, plugins)`**: the loader. Mounts
  each row's plugin once the services its factory names exist;
  refuses with names when rows can never activate.
- **`PLUGINS`**: the name table, a row's `name` to a factory
  `config -> plugin`, each factory declaring the services it
  requires at mount.
- **`MINI_BASE`**: the base bundle: the whole harness sections 00
  through 12 built by hand, as sixteen rows of data.

The applier is small because the verbs are decided by what the id
already means, and the replace branch is one assignment:

```python
row = next((r for r in entries if r["id"] == patch["id"]), None)
if patch.get("disabled"):
    if row is not None:
        entries.remove(row)
elif row is None:
    entries.append({"id": patch["id"], "name": patch["name"],
                    "config": dict(patch.get("config", {}))})
else:
    row["config"] = dict(patch.get("config", {}))  # whole, never a merge
```

That one assignment is the design question answered in code. There
is no key-by-key walk to write, because a patch never meets the old
config at all; whatever the previous layers said about the row, the
last patch to touch it says everything.

The loader is the other half. Rows are data, so a row cannot say
"load me after the sessions row"; instead each factory names the
services it needs, and the loader keeps mounting whatever has
become mountable until the list settles:

```python
while pending:
    ready = [row for row in pending if not _missing(ctx, table, row)]
    if not ready:
        waits = "; ".join(
            f"'{row['id']}' waits on {', '.join(_missing(ctx, table, row))}"
            for row in pending
        )
        raise RuntimeError(f"rows never activated: {waits}")
    for row in ready:
        fibers[row["id"]] = ctx.plugin(table[row["name"]](row.get("config", {})))
        pending.remove(row)
```

Row order therefore carries no load semantics. The base bundle can
be listed backwards and the same product boots, because "when does
this mount" is answered by the services on the ctx, the section 01
substrate, not by position in a file. And when a row's service
never arrives, the audit refuses the whole boot and names the wait,
so a half-product cannot start quietly.

The base bundle makes the design question resident. The model row
ships in base because every mode has one, but its value differs by
mode, so base ships a stand-in with nothing to say and every
profile restates the whole config:

```python
{"id": "model", "name": "scripted-llm",
 "config": {"name": "scripted", "responses": []}},
{"id": "agent", "name": "agent",
 "config": {"agent": "a1", "session": "s1", "model": "scripted"}},
```

A profile that wants a live model does not patch `{"model": "live"}`
into the agent row; it restates the agent row's complete config,
and inserts its own adapter row beside it. Nothing leaks up from
base, so reading the profile's layer is reading the truth.

```text
composition, top to bottom

MINI_BASE          sixteen rows over the empty list
profile layer      inserts its rows, replaces whole configs,
                   disables what the mode does without
user layer         the same three verbs, last word wins
  │
  ▼  apply_layers
one flat entry list: {id, name, config} rows, data only
  │
  ▼  mount_entries, via PLUGINS
plugins mounted as services become available; the audit
names any row still waiting when the list can settle no further
```

Here is the composed harness running, as the log records it. The
profile replaced the model row's config with a script that calls
the shell tool; the tool's answer crosses two other rows, the
sandbox fence wrapping the echo shell:

```text
send("run a command")                       the composed product
  │   0  turn/start
  │   2  user/message   "run a command"
  │   5  tool/call      shell {"command": "echo composed from rows"}
  │   6  tool/result    "mini-sandbox --policy echo-only --
  │                      echo composed from rows"
  │  13  assistant/message "the rows are alive"
  │  15  turn/end
```

Every mechanism in that transcript, the loop, the pipeline, the
sandbox, the prompt, arrived as a row. Disable one row, say the
skills row, and the same harness answers the same script with
`unknown tool 'skill'` through the section 05 door: a subsystem
removed by data, with no code edited anywhere.

### What changed

Compared with section 12:

- Every carried file is verbatim: `agent_loop.py`,
  `capabilities.py`, `inbox.py`, `jobs.py`, `kernel.py`,
  `message.py`, `scheduler.py`, `session_log.py`, `skills.py`,
  `standin.py`, `subagent.py`, `system_prompt.py`, `tools.py`.
  `composition.py` is the only new source file, so the diff
  against 12 is this section's Mechanism, nothing else.
- No mechanism below changed to become composable. The rows mount
  the same plugins every prior check mounted by hand, through the
  same section 01 `ctx.plugin()` door; the model row reaches the
  loop through the section 10 llm seam, adapters registered by
  name and resolved per call.
- The log gained no new event type. Composition happens before the
  first turn opens; the composed product's transcript is
  indistinguishable from a hand-built one, which is the point.
- `demo.py`: the Live demo boots the base bundle under a live
  profile layer: one inserted adapter row, one inserted worker
  row, the scripted model row disabled, and the agent row's config
  replaced whole so its model is the live one.
- This is the final section. The harness sections 00 through 12
  taught piece by piece is now sixteen rows over an empty list,
  and "everything is a plugin, and every registration is
  reversible" closes as data: a product is a diff over nothing.

---

## In real dsh

All pointers are into the pinned Studied version,
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca).
The layer is the boot plane:
[`apps/cli`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli),
[`packages/boot/app-boot`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot),
and the bundles under
[`packages/bundle`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle).

| Mini-dsh | Real dsh | Notes |
| --- | --- | --- |
| `apply_layers` over an empty list | [`apps/cli/src/profile-boot.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli/src/profile-boot.ts) (lines 142-171) | The locked layer order: bundles in `dsh.profile.bundles` order, then the profile's `cordis.patch.yml`, then `$DSH_HOME/cordis.patch.yml`, then `--patch` overlays. |
| the three verbs, replace whole | [`packages/bundle/base/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/base/cordis.patch.yml) (lines 6-10), applied by `applyEntryPatches` in [`vendor/include`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/include) | A patch targets a row by id and replaces its whole `config`, never merges, or inserts rows. |
| `MINI_BASE`, sixteen rows | [`packages/bundle/base/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/base/cordis.patch.yml) | `@deepseek-ai/dsh-base` is 78 rows; the headless mode adds 6 more in [`packages/bundle/headless/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/headless/cordis.patch.yml). Complete but not small. |
| the `PLUGINS` name table | [`packages/boot/app-boot/src/profile.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot/src/profile.ts): `resolveBundleDir` (line 344), `PROFILE_TEMPLATES` (lines 114-117) | Names resolve to real packages on disk; the shipped templates are `web = [dsh-base, dsh-web-app]` and `headless = [dsh-base, dsh-headless]`. |
| `mount_entries` on a fresh `Context()` | [`packages/boot/app-boot/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot/src/index.ts): `boot()` (line 757), rows mounted via [`vendor/loader/src/config/entry.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/loader/src/config/entry.ts) | `boot()` is `new Context()` then `ctx.plugin(Loader)`; each entry row becomes one plugin mount, each removal an unmount, the section 01 contract at product scale. |
| the settle-then-audit pass | [`packages/boot/app-boot/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot/src/index.ts): `assertEntriesActivated` (lines 700-725) | Row order carries no load semantics; activation is service-availability-driven, and the audit names rows still pending on missing services. |

What the real composition layer adds on top of this section's
Mechanism:

- **A live entry list.** The Loader is itself a plugin and the list
  stays live: editing a row mounts or unmounts exactly the
  difference in a running process, and HMR rides the same
  machinery. HMR sits above this rebuild's Ceiling: pointed at
  here, not rebuilt.
- **Profiles as products.** `dsh --profile web` and
  `dsh --profile headless`
  ([`apps/cli/src/args.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli/src/args.ts))
  pick a bundle stack from `PROFILE_TEMPLATES`: one binary, two
  products, differing by list alone.
- **Per-agent composition: presets.** Not a profile layer.
  [`@deepseek-ai/dsh-agent-presets`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/preset/agent-presets/src/mount.ts)
  mounts an `agent.cordis.yml` subtree once per process, and each
  session joins by parenting its agent scope; profile composition
  is process-wide, presets per-agent.
- **YAML rows and a real module system.** Rows live in
  `cordis.patch.yml` files a user can edit and diff, and names
  resolve to npm packages through `resolveBundleDir`; the mini's
  `PLUGINS` dict is that resolution with the filesystem removed.

---

## Failure modes

- **A deep merge makes every layer a suspect.** With merging, the
  effective config of a row exists nowhere; it is the fold of
  every layer that ever touched it, and debugging one wrong key
  means replaying the whole stack. Replace keeps the answer in one
  row in one layer: the last patch is the whole truth.
- **A factored-out default leaks into a mode that never asked.**
  Let base carry `{"name": "scripted", "responses": []}` and let a
  profile merge in one key, and the profile's model quietly keeps
  base's leftovers. The check proves the opposite shape: after a
  replace, no key of the old config survives.
- **Row position as load order breaks under patching.** Layers
  append inserted rows at the end of the list, so if position
  meant load order, one profile row would reshuffle the boot of
  everything after it. Availability-driven mounting makes position
  meaningless, which is what lets any layer insert anywhere.
- **A silently pending row boots half a product.** Without the
  audit, a row waiting on a service nobody provides just never
  mounts: the harness starts, the agent is missing, and nothing
  says so. The settle-then-audit pass turns the quiet hole into a
  refusal that names the row and the service it waits on.
- **Config that carries live objects cannot be patched.** A
  callable in a config row cannot be written in a file, diffed, or
  replaced whole by a later layer. The live demo's model enters
  through a name in the table and an adapter row; config carries
  only the name, so the row stays data a patch can own.

---

## Runnable

[`src/`](src/) carries 12 forward and adds:

- [`composition.py`](src/composition.py) (new): the patch applier,
  the availability-driven loader with its settle-then-audit pass,
  the `PLUGINS` name table, and the sixteen-row `MINI_BASE`
  bundle.
- [`test.py`](src/test.py): the Offline check proves the three
  verbs stack layers over an empty list, a replace takes the
  patch's config whole with nothing leaking from base, the
  sixteen rows boot the harness and a turn runs through the
  composed sandbox and shell rows, the reversed base boots
  identically, one disable row removes the skills subsystem, and
  a boot with a missing service refuses while naming every
  waiting row.
- [`demo.py`](src/demo.py): the Live demo composes a live profile
  over the base bundle, prints the rows it produced, then proves
  them live: a shell turn through the sandbox rows and a
  foreground delegation to a worker row's provider.

```bash
python sections/13-composition/src/test.py    # offline check, no key
```

The Live demo needs the root `requirements.txt` and a key; it
skips politely without one:

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/13-composition/src/demo.py
```

---

## Sources

- [`docs/architecture.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/architecture.md):
  the Profiles and bundles section: the entry list, the patch
  layers, and the shipped bundle stacks.
- [`apps/cli/src/profile-boot.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli/src/profile-boot.ts):
  the layer stacking itself: bundles, profile patch, home patch,
  and `--patch` overlays, in that order.
