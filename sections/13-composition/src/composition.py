"""Composition: ordered patch layers over an empty entry list.

A running mini-dsh is one flat list of entry rows {id, name, config},
pure data. The list is built by applying patch layers in order over an
empty list: bundles first, then the profile's patch, then the user's.
A patch row is judged by its id: an unknown id inserts the row, a
known id replaces the row's whole config (never a merge), and a
disabled row is removed. Mounting is driven by service availability,
not row position, and a settle-then-audit pass names every row still
waiting and the services it waits on.
"""

from agent_loop import agent_loop_plugin
from capabilities import (
    ArgvRewriteSandbox,
    EchoShellExecutor,
    MemoryFileSystem,
    SandboxedShellExecutor,
    capability_tools_plugin,
    llm_plugin,
    provider,
)
from jobs import JobOwner, job_tools, jobs_plugin
from session_log import session_log_plugin
from skills import skills_plugin
from standin import ScriptedModel
from subagent import subagent_plugin, subagent_tools
from system_prompt import system_prompt_plugin
from tools import tools_plugin


# -- the patch applier -----------------------------------------------------


def apply_layers(layers):
    """Ordered patch layers in, one flat entry list out.

    Later layers always win, and a replace takes the patch's config
    whole, so any row's final config is readable from exactly one
    layer: the last one that touched it.
    """
    entries = []
    for layer in layers:
        for patch in layer:
            row = next((r for r in entries if r["id"] == patch["id"]), None)
            if patch.get("disabled"):
                if row is not None:
                    entries.remove(row)
            elif row is None:
                if "name" not in patch:
                    raise ValueError(
                        f"patch for unknown id '{patch['id']}' names no plugin"
                    )
                entries.append(
                    {
                        "id": patch["id"],
                        "name": patch["name"],
                        "config": dict(patch.get("config", {})),
                    }
                )
            else:
                row["config"] = dict(patch.get("config", {}))  # whole, never a merge
    return entries


# -- the loader ------------------------------------------------------------


def mount_entries(ctx, entries, plugins=None):
    """Mount every row, driven by service availability, then audit.

    Row order carries no load semantics: a row mounts once every
    service its factory names exists, whatever position a layer gave
    it. When a pass mounts nothing and rows remain, the audit raises,
    naming each pending row and what it still waits on.
    """
    table = PLUGINS if plugins is None else plugins
    pending = list(entries)
    fibers = {}
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
    return fibers


def _missing(ctx, table, row):
    factory = table[row["name"]]
    return [name for name in getattr(factory, "requires", ()) if ctx.get(name) is None]


def _factory(make, *requires):
    """A table entry: config in, plugin out, mountable once `requires` exist."""
    make.requires = requires
    return make


# -- config-taking factories -----------------------------------------------


def _argv_sandbox(config):
    return provider("sandbox", ArgvRewriteSandbox(config["policies"]))


def _sandboxed_echo_shell(config):
    def apply(ctx):
        ctx.provide(
            "shell",
            SandboxedShellExecutor(
                EchoShellExecutor(), ctx.get("sandbox"), config["policy"]
            ),
        )

    return apply


def _scripted_llm(config):
    def apply(ctx):
        ctx.effect(
            ctx.get("llm").register(
                config["name"], ScriptedModel(config["responses"])
            ),
            "scripted adapter",
        )

    return apply


def _agent(config):
    def apply(ctx):
        session = ctx.get("sessions").create(config["session"])
        agent = ctx.get("agents").create(
            config["agent"],
            session,
            ctx.get("llm").model(config["model"]),
            ctx.get("tools").scope(config["agent"]),
            ctx.get("system_prompt"),
        )
        ctx.provide("agent", agent)
        ctx.provide("owner", JobOwner(config["agent"], agent))

    return apply


# -- the name table: how a data row finds code -----------------------------

PLUGINS = {
    "session-log": _factory(lambda config: session_log_plugin),
    "tools": _factory(lambda config: tools_plugin),
    "system-prompt": _factory(lambda config: system_prompt_plugin),
    "skills": _factory(lambda config: skills_plugin, "system_prompt", "tools"),
    "memory-fs": _factory(lambda config: provider("fs", MemoryFileSystem())),
    "argv-sandbox": _factory(_argv_sandbox),
    "sandboxed-echo-shell": _factory(_sandboxed_echo_shell, "sandbox"),
    "llm": _factory(lambda config: llm_plugin),
    "capability-tools": _factory(lambda config: capability_tools_plugin, "tools"),
    "jobs": _factory(lambda config: jobs_plugin),
    "agent-loop": _factory(lambda config: agent_loop_plugin),
    "subagents": _factory(lambda config: subagent_plugin),
    "scripted-llm": _factory(_scripted_llm, "llm"),
    "agent": _factory(_agent, "sessions", "agents", "llm", "tools", "system_prompt"),
    "job-tools": _factory(
        lambda config: lambda ctx: job_tools(ctx.get("owner"))(ctx),
        "jobs", "owner", "tools",
    ),
    "subagent-tools": _factory(
        lambda config: lambda ctx: subagent_tools(ctx.get("owner"))(ctx),
        "subagents", "owner", "tools",
    ),
}


# -- the base bundle: the whole mini-dsh as sixteen rows of data -----------

MINI_BASE = [
    {"id": "sessions", "name": "session-log"},
    {"id": "tools", "name": "tools"},
    {"id": "system-prompt", "name": "system-prompt"},
    {"id": "skills", "name": "skills"},
    {"id": "fs", "name": "memory-fs"},
    {"id": "sandbox", "name": "argv-sandbox", "config": {"policies": ["echo-only"]}},
    {"id": "shell", "name": "sandboxed-echo-shell", "config": {"policy": "echo-only"}},
    {"id": "llm", "name": "llm"},
    {"id": "capability-tools", "name": "capability-tools"},
    {"id": "jobs", "name": "jobs"},
    {"id": "agents", "name": "agent-loop"},
    {"id": "subagents", "name": "subagents"},
    # The design question, resident in the base: this row's value differs
    # by mode, so the base ships a stand-in with nothing to say, and every
    # profile restates the whole config, never a partial patch over it.
    {"id": "model", "name": "scripted-llm", "config": {"name": "scripted", "responses": []}},
    {
        "id": "agent",
        "name": "agent",
        "config": {"agent": "a1", "session": "s1", "model": "scripted"},
    },
    {"id": "job-tools", "name": "job-tools"},
    {"id": "subagent-tools", "name": "subagent-tools"},
]
