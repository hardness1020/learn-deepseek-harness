"""Offline check for section 09: layered provider registry, catalog injected, bodies on demand."""

from agent_loop import agent_loop_plugin
from kernel import Context
from session_log import session_log_plugin
from skills import CATALOG_HEADER, MemorySkillProvider, skills_plugin
from standin import ScriptedModel
from system_prompt import is_snapshot, system_prompt_plugin
from tools import ToolDefinition, tools_plugin

HAIKU = {"description": "answer as a haiku", "body": "Answer with one haiku: 5-7-5."}


def build(responses):
    ctx = Context()
    ctx.plugin(session_log_plugin)
    ctx.plugin(tools_plugin)
    ctx.plugin(system_prompt_plugin)
    ctx.plugin(skills_plugin)
    ctx.plugin(agent_loop_plugin)
    tools = ctx.get("tools")
    prompt = ctx.get("system_prompt")
    skills = ctx.get("skills")
    session = ctx.get("sessions").create("s1")
    agent = ctx.get("agents").create(
        "a1", session, ScriptedModel(responses), tools.scope("a1"), prompt
    )
    return ctx, tools, prompt, skills, session, agent


def headers_of(session):
    return [e["payload"] for e in session.log if e["type"] == "request/header"]


def snapshots_of(session):
    """The runtime-context rows: user/message events carrying the snapshot."""
    return [e["payload"]["content"] for e in session.log if is_snapshot(e)]


def results_of(session):
    return [e["payload"] for e in session.log if e["type"] == "tool/result"]


def main():
    check_catalog_injected_body_on_demand()
    check_catalog_reemits_on_provider_change()
    check_layering_shadows_and_undoes()
    check_unknown_skill_is_normal_result()
    check_empty_catalog_ships_nothing()
    print("section 09: all checks passed")


def check_catalog_injected_body_on_demand():
    """The catalog rides the snapshot row; the body arrives only as a tool/result."""
    ctx, tools, prompt, skills, session, agent = build(
        [
            {"text": "", "tool_calls": [{"id": "c1", "name": "skill", "args": {"name": "haiku"}}]},
            "done",
        ]
    )
    skills.register(
        MemorySkillProvider({"haiku": HAIKU, "greet": {"description": "greet by name", "body": "Say hello."}})
    )
    prompt.context("cwd", lambda ac: "cwd: /home/ada")  # ad-hoc state, same plane

    agent.send("hi")

    # One snapshot row: ordered contexts, then the catalog block. Names and
    # one-line descriptions only; no body ever rides along.
    (snapshot,) = snapshots_of(session)
    assert snapshot == (
        "cwd: /home/ada\n"
        + CATALOG_HEADER
        + "\n- haiku: answer as a haiku\n- greet: greet by name"
    )
    assert HAIKU["body"] not in snapshot

    # The skill tool is offered like any other tool.
    assert "skill" in list(headers_of(session)[0]["tools"])

    # The body entered the log only when the model asked, as an ordinary
    # tool/result row, and derived history carries it as a plain tool message.
    (result,) = results_of(session)
    assert result["is_error"] is False
    assert result["content"] == HAIKU["body"]
    roles = [m.role for m in session.derive_messages()]
    assert roles == ["user", "user", "assistant", "tool", "assistant"]
    assert session.derive_messages()[3].content == HAIKU["body"]


def check_catalog_reemits_on_provider_change():
    """A provider registered mid-turn re-emits the catalog; unchanged stays quiet."""
    ctx, tools, prompt, skills, session, agent = build(
        [
            {"text": "", "tool_calls": [{"id": "c1", "name": "install", "args": {}}]},
            "done",
            "again",
        ]
    )
    skills.register(MemorySkillProvider({"haiku": HAIKU}))

    def install_body(args):
        skills.register(
            MemorySkillProvider({"deploy": {"description": "ship it safely", "body": "Check twice."}})
        )
        return "installed"

    tools.register(ToolDefinition("install", "add a skill provider", {}, install_body))

    agent.send("go")  # two steps: the second boundary sees the changed catalog
    agent.send("more")  # a later turn: catalog unchanged, nothing re-emitted

    assert len(headers_of(session)) == 3
    assert snapshots_of(session) == [
        CATALOG_HEADER + "\n- haiku: answer as a haiku",
        CATALOG_HEADER + "\n- haiku: answer as a haiku\n- deploy: ship it safely",
    ]


def check_layering_shadows_and_undoes():
    """A later layer shadows a name; undoing it uncovers the layer below."""
    ctx, tools, prompt, skills, session, agent = build(
        [
            {"text": "", "tool_calls": [{"id": "c1", "name": "skill", "args": {"name": "greet"}}]},
            "done",
            {"text": "", "tool_calls": [{"id": "c2", "name": "skill", "args": {"name": "greet"}}]},
            "done",
        ]
    )
    skills.register(
        MemorySkillProvider({"greet": {"description": "greet plainly", "body": "Say hello."}})
    )
    drop = skills.register(
        MemorySkillProvider({"greet": {"description": "greet grandly", "body": "Proclaim a hello."}})
    )

    agent.send("one")
    drop()
    agent.send("two")

    # One catalog line per visible name, from the shadowing layer while it
    # stands; the undo uncovers the base layer and the catalog re-emits.
    assert snapshots_of(session) == [
        CATALOG_HEADER + "\n- greet: greet grandly",
        CATALOG_HEADER + "\n- greet: greet plainly",
    ]
    assert [r["content"] for r in results_of(session)] == ["Proclaim a hello.", "Say hello."]


def check_unknown_skill_is_normal_result():
    """An unknown name is a normal is_error result, never a raise (section 05's door)."""
    ctx, tools, prompt, skills, session, agent = build(
        [
            {"text": "", "tool_calls": [{"id": "c1", "name": "skill", "args": {"name": "nope"}}]},
            "done",
        ]
    )
    skills.register(MemorySkillProvider({"haiku": HAIKU}))

    agent.send("hi")

    (result,) = results_of(session)
    assert result["is_error"] is True
    assert "nope" in result["content"]
    # The turn still closed normally: the transcript keeps its shape.
    assert [e["type"] for e in session.log][-2:] == ["step/end", "turn/end"]


def check_empty_catalog_ships_nothing():
    """No providers, no catalog block, no snapshot row at all."""
    ctx, tools, prompt, skills, session, agent = build(["hello"])

    agent.send("hi")

    assert snapshots_of(session) == []
    assert [m.role for m in session.derive_messages()] == ["user", "assistant"]


if __name__ == "__main__":
    main()
