"""Offline check for section 01: mounting is reversible, without plugin cleanup code."""

from kernel import Context, InactiveEffectError


def main():
    ctx = Context()

    # Mount a plugin: two registrations, zero cleanup code inside it.
    heard = []

    def echo_plugin(ctx):
        ctx.on("say", heard.append)
        ctx.provide("echo", lambda text: f"echo: {text}")

    fiber = ctx.plugin(echo_plugin)
    ctx.emit("say", "one")
    assert heard == ["one"]
    assert ctx.get("echo")("hi") == "echo: hi"

    # Unmount: the framework undoes both registrations.
    fiber.dispose()
    ctx.emit("say", "two")
    assert heard == ["one"], "listener must be gone after dispose"
    assert ctx.get("echo") is None, "service must be gone after dispose"

    # Undos run in reverse registration order.
    order = []

    def ordered_plugin(ctx):
        for name in "abc":
            ctx.effect(lambda name=name: order.append(name), name)

    ctx.plugin(ordered_plugin).dispose()
    assert order == ["c", "b", "a"], order

    # A disposer is single-shot: early manual dispose, then fiber dispose.
    count = []

    def once_plugin(ctx):
        undo = ctx.effect(lambda: count.append(1))
        undo()

    ctx.plugin(once_plugin).dispose()
    assert count == [1], "undo must run at most once"

    # Double fiber-dispose is a no-op.
    fiber.dispose()

    # Registering on a disposed fiber is an error, not a silent leak.
    try:
        fiber.collect(lambda: None, "late")
        assert False, "expected InactiveEffectError"
    except InactiveEffectError:
        pass

    # Unmounting one plugin leaves its neighbors intact.
    a_fiber = ctx.plugin(lambda ctx: ctx.provide("a", 1))
    ctx.plugin(lambda ctx: ctx.provide("b", 2))
    a_fiber.dispose()
    assert ctx.get("a") is None and ctx.get("b") == 2

    print("section 01: all checks passed")


if __name__ == "__main__":
    main()
