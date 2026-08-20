"""The kernel: fiber/effect reversible registrations.

Every registration a plugin makes (a listener, a service, anything)
hands the framework an undo (a disposer), collected on the Fiber that
owns the plugin. Unmounting runs the undos in reverse. One primitive,
effect(), underlies all of it.
"""


class InactiveEffectError(RuntimeError):
    """Raised when registering an effect on a fiber that is already disposed."""


class Fiber:
    """The lifetime unit of one mounted plugin: an ordered list of undos."""

    def __init__(self, label):
        self.label = label
        self.state = "loading"  # loading -> active -> disposed
        self._disposers = []

    def collect(self, dispose, label=""):
        if self.state == "disposed":
            raise InactiveEffectError(
                f"cannot register {label or 'effect'} on disposed fiber '{self.label}'"
            )
        entry = {"dispose": dispose, "done": False}

        def run():
            if not entry["done"]:
                entry["done"] = True
                entry["dispose"]()

        self._disposers.append(run)
        return run  # single-shot: safe to call early, fiber won't re-run it

    def dispose(self):
        if self.state == "disposed":
            return
        self.state = "disposed"
        for run in reversed(self._disposers):
            run()
        self._disposers.clear()


class Context:
    """A plugin's view of the app. Registrations collect on this view's fiber."""

    def __init__(self, root=None, fiber=None):
        self._root = root if root is not None else self
        self.fiber = fiber if fiber is not None else Fiber("root")
        if self._root is self:
            self.fiber.state = "active"
            self._services = {}
            self._listeners = {}  # event name -> list of callbacks

    # -- the one primitive ------------------------------------------------
    def effect(self, dispose, label=""):
        return self.fiber.collect(dispose, label)

    # -- plugins ----------------------------------------------------------
    def plugin(self, apply):
        """Mount: run the plugin against a child view. Returns its fiber."""
        fiber = Fiber(getattr(apply, "__name__", "plugin"))
        apply(Context(self._root, fiber))
        fiber.state = "active"
        return fiber

    # -- registrations, all built on effect() -----------------------------
    def on(self, event, callback):
        listeners = self._root._listeners.setdefault(event, [])
        listeners.append(callback)
        return self.effect(lambda: listeners.remove(callback), f"on({event})")

    def emit(self, event, *args):
        for callback in list(self._root._listeners.get(event, [])):
            callback(*args)

    def provide(self, name, value):
        services = self._root._services
        if name in services:
            raise ValueError(f"service '{name}' already provided")
        services[name] = value
        return self.effect(lambda: services.pop(name), f"provide({name})")

    def get(self, name):
        return self._root._services.get(name)
