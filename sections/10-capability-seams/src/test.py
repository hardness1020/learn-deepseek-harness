"""Offline check for section 10: Definition/Provider/Consumer seams for fs, shell, sandbox, llm."""

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
from kernel import Context
from session_log import session_log_plugin
from standin import ScriptedModel
from system_prompt import system_prompt_plugin
from tools import tools_plugin

READ_NOTES = {"name": "read", "args": {"path": "notes.txt"}}


def build():
    ctx = Context()
    ctx.plugin(session_log_plugin)
    ctx.plugin(tools_plugin)
    ctx.plugin(system_prompt_plugin)
    ctx.plugin(capability_tools_plugin)
    ctx.plugin(llm_plugin)
    ctx.plugin(agent_loop_plugin)
    return ctx


def agent_of(ctx, model):
    session = ctx.get("sessions").create("s1")
    agent = ctx.get("agents").create(
        "a1", session, model, ctx.get("tools").scope("a1"), ctx.get("system_prompt")
    )
    return session, agent


def headers_of(session):
    return [e["payload"] for e in session.log if e["type"] == "request/header"]


def results_of(session):
    return [e["payload"] for e in session.log if e["type"] == "tool/result"]


def main():
    check_backend_swap_keeps_the_schema()
    check_exclusive_seam_fails_loud_at_mount()
    check_sandbox_rewrites_argv_through_the_shell()
    check_unknown_policy_fails_closed()
    check_missing_provider_is_normal_result()
    check_llm_adapters_are_plural_and_late_bound()
    print("section 10: all checks passed")


def check_backend_swap_keeps_the_schema():
    """Swapping the fs provider changes results, never what the model sees."""
    ctx = build()
    session, agent = agent_of(
        ctx,
        ScriptedModel(
            [
                {"text": "", "tool_calls": [dict(READ_NOTES, id="c1")]},
                "done",
                {"text": "", "tool_calls": [dict(READ_NOTES, id="c2")]},
                "done",
            ]
        ),
    )
    first = ctx.plugin(provider("fs", MemoryFileSystem({"notes.txt": "alpha"})))

    agent.send("read it")
    first.dispose()  # the provider's undo unmounts it; the agent never hears
    ctx.plugin(provider("fs", MemoryFileSystem({"notes.txt": "beta"})))
    agent.send("read it again")

    # Four requests across the swap: the offered schemas and the system text
    # are byte-identical, because no consumer names a backend.
    headers = headers_of(session)
    assert len(headers) == 4
    assert [list(h["tools"]) for h in headers] == [["read", "write", "shell"]] * 4
    assert len({h["system"] for h in headers}) == 1
    # Only the results tell the backends apart.
    results = results_of(session)
    assert [r["is_error"] for r in results] == [False, False]
    assert [r["content"] for r in results] == ["alpha", "beta"]


def check_exclusive_seam_fails_loud_at_mount():
    """A second provider under an exclusive key refuses at mount, not at use."""
    ctx = build()
    ctx.plugin(provider("shell", EchoShellExecutor()))
    try:
        ctx.plugin(provider("shell", EchoShellExecutor()))
        raise AssertionError("a second shell provider must fail loud")
    except ValueError as exc:
        assert "shell" in str(exc)


def check_sandbox_rewrites_argv_through_the_shell():
    """The sandbox's consumer is a shell provider; the fence shows in the log."""
    ctx = build()
    session, agent = agent_of(
        ctx,
        ScriptedModel(
            [
                {"text": "", "tool_calls": [{"id": "c1", "name": "shell", "args": {"command": "echo hi"}}]},
                "done",
            ]
        ),
    )
    ctx.plugin(provider("sandbox", ArgvRewriteSandbox({"read-only"})))
    ctx.plugin(
        provider(
            "shell",
            SandboxedShellExecutor(EchoShellExecutor(), ctx.get("sandbox"), "read-only"),
        )
    )

    agent.send("run it")

    # The tool body sent ["echo", "hi"]; the provider stack ran the fenced argv.
    (result,) = results_of(session)
    assert result["is_error"] is False
    assert result["content"] == "mini-sandbox --policy read-only -- echo hi"


def check_unknown_policy_fails_closed():
    """An unfenceable call runs nothing and answers as a normal error result."""
    ctx = build()
    session, agent = agent_of(
        ctx,
        ScriptedModel(
            [
                {"text": "", "tool_calls": [{"id": "c1", "name": "shell", "args": {"command": "echo hi"}}]},
                "done",
            ]
        ),
    )
    ctx.plugin(provider("sandbox", ArgvRewriteSandbox({"read-only"})))
    ctx.plugin(
        provider(
            "shell",
            SandboxedShellExecutor(EchoShellExecutor(), ctx.get("sandbox"), "network"),
        )
    )

    agent.send("run it")

    (result,) = results_of(session)
    assert result["is_error"] is True
    assert "network" in result["content"]
    # The turn still closed normally: the transcript keeps its shape.
    assert [e["type"] for e in session.log][-2:] == ["step/end", "turn/end"]


def check_missing_provider_is_normal_result():
    """A consumer with no provider mounted degrades through the section 05 door."""
    ctx = build()
    session, agent = agent_of(
        ctx,
        ScriptedModel([{"text": "", "tool_calls": [dict(READ_NOTES, id="c1")]}, "done"]),
    )

    agent.send("read it")

    (result,) = results_of(session)
    assert result["is_error"] is True
    assert "'fs'" in result["content"]
    assert [e["type"] for e in session.log][-2:] == ["step/end", "turn/end"]


def check_llm_adapters_are_plural_and_late_bound():
    """Two adapters coexist by name; a swap reaches a live agent per call."""
    ctx = build()
    llm = ctx.get("llm")
    session, agent = agent_of(ctx, llm.model("main"))
    drop = llm.register("main", ScriptedModel(["from the first backend"]))
    llm.register("spare", ScriptedModel(["never asked"]))  # plural: no conflict

    agent.send("hi")
    drop()  # the undo frees the name; the agent still holds llm.model("main")
    llm.register("main", ScriptedModel(["from the second backend"]))
    agent.send("hi again")

    texts = [e["payload"]["content"] for e in session.log if e["type"] == "assistant/message"]
    assert texts == ["from the first backend", "from the second backend"]
    # Within the registry a duplicate name fails loud, like an exclusive mount.
    try:
        llm.register("spare", ScriptedModel([]))
        raise AssertionError("a duplicate adapter name must fail loud")
    except ValueError as exc:
        assert "spare" in str(exc)


if __name__ == "__main__":
    main()
