"""Offline check for section 13: ordered patch layers over an empty entry list."""

from composition import MINI_BASE, apply_layers, mount_entries
from kernel import Context


def boot(layers):
    ctx = Context()
    mount_entries(ctx, apply_layers(layers))
    return ctx


def script(*responses):
    """A profile layer replacing the base model row's whole config."""
    return {
        "id": "model",
        "config": {"name": "scripted", "responses": list(responses)},
    }


def results_of(session):
    return [e["payload"] for e in session.log if e["type"] == "tool/result"]


def replies_of(session):
    return [
        e["payload"]["content"]
        for e in session.log
        if e["type"] == "assistant/message" and e["payload"]["content"]
    ]


def main():
    check_layers_stack_over_an_empty_list()
    check_a_patch_replaces_the_whole_config()
    check_the_composed_harness_runs()
    check_row_order_carries_no_load_semantics()
    check_disable_is_a_layer_verb()
    check_the_audit_names_what_still_waits()
    print("section 13: all checks passed")


def check_layers_stack_over_an_empty_list():
    """Three verbs, keyed by id: insert, whole-config replace, disable."""
    base = [
        {"id": "a", "name": "alpha", "config": {"x": 1}},
        {"id": "b", "name": "beta"},
    ]
    profile = [
        {"id": "c", "name": "gamma"},  # unknown id: insert
        {"id": "a", "config": {"x": 2}},  # known id: replace
        {"id": "b", "disabled": True},  # disable: the row is gone
    ]
    entries = apply_layers([base, profile])
    assert [row["id"] for row in entries] == ["a", "c"]
    assert entries[0] == {"id": "a", "name": "alpha", "config": {"x": 2}}
    assert entries[1] == {"id": "c", "name": "gamma", "config": {}}

    # A patch aimed at an id no layer inserted is a broken profile,
    # named at build time, never a row that half-exists.
    try:
        apply_layers([[{"id": "ghost", "config": {"x": 1}}]])
        raise AssertionError("a patch for an unknown id should refuse")
    except ValueError as exc:
        assert "ghost" in str(exc)


def check_a_patch_replaces_the_whole_config():
    """The design question: the patch's config is the row's config, entire."""
    base = [{"id": "a", "name": "alpha", "config": {"keep": 1, "drop": 2}}]
    patch = [{"id": "a", "config": {"keep": 9}}]
    (row,) = apply_layers([base, patch])
    # Not {"keep": 9, "drop": 2}: nothing leaks up from the base layer.
    assert row["config"] == {"keep": 9}


def check_the_composed_harness_runs():
    """Sixteen data rows boot the harness every prior section built by hand."""
    ctx = boot(
        [
            MINI_BASE,
            [
                script(
                    {
                        "text": "",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "name": "shell",
                                "args": {"command": "echo composed from rows"},
                            }
                        ],
                    },
                    "the rows are alive",
                )
            ],
        ]
    )

    ctx.get("agent").send("run a command")

    session = ctx.get("sessions").sessions["s1"]
    # The shell tool answered through the sandbox rows: the argv-rewrite
    # fence from one row wrapped the echo shell from another.
    (result,) = results_of(session)
    assert result["is_error"] is False
    assert result["content"] == (
        "mini-sandbox --policy echo-only -- echo composed from rows"
    )
    assert replies_of(session) == ["the rows are alive"]
    types = [e["type"] for e in session.log]
    assert types[-3:] == ["assistant/message", "step/end", "turn/end"]


def check_row_order_carries_no_load_semantics():
    """The reversed base boots identically: activation is availability-driven."""
    scrambled = list(reversed(MINI_BASE))
    ctx = boot([scrambled, [script("order never mattered")]])

    ctx.get("agent").send("say something")

    session = ctx.get("sessions").sessions["s1"]
    assert replies_of(session) == ["order never mattered"]


def check_disable_is_a_layer_verb():
    """One disable row unmakes a whole subsystem, no code edited anywhere."""
    ask = {
        "text": "",
        "tool_calls": [{"id": "c1", "name": "skill", "args": {"name": "greet"}}],
    }

    # With the base as shipped, the skill tool exists (an empty catalog
    # answers with a normal error naming the unknown skill).
    ctx = boot([MINI_BASE, [script(ask, "asked with skills")]])
    ctx.get("agent").send("load a skill")
    (result,) = results_of(ctx.get("sessions").sessions["s1"])
    assert result["is_error"] is True
    assert "unknown skill 'greet'" in result["content"]

    # One patch row removes the skills row; the same turn now finds no
    # such tool at all, and says so through the section 05 door.
    ctx = boot(
        [
            MINI_BASE,
            [{"id": "skills", "disabled": True}, script(ask, "asked without")],
        ]
    )
    ctx.get("agent").send("load a skill")
    (result,) = results_of(ctx.get("sessions").sessions["s1"])
    assert result["is_error"] is True
    assert "unknown tool 'skill'" in result["content"]


def check_the_audit_names_what_still_waits():
    """A row whose service never arrives is named, not silently skipped."""
    try:
        boot([MINI_BASE, [{"id": "sessions", "disabled": True}]])
        raise AssertionError("the audit should have refused the boot")
    except RuntimeError as exc:
        message = str(exc)
        assert "rows never activated" in message
        # The agent row waits on the missing sessions service, and the
        # tool rows wait on the owner the agent row would have provided.
        assert "'agent' waits on sessions" in message
        assert "'job-tools' waits on owner" in message
        assert "'subagent-tools' waits on owner" in message


if __name__ == "__main__":
    main()
