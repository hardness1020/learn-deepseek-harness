"""Tools: a scoped registry and the fixed pre/ask/guard/execute/post pipeline.

The registry keeps ToolDefinitions in layers: one global layer plus one
layer per agent scope. A scope resolves names through its own layer
first (shadowing), and a name stays visible only if every restriction
that applies to the scope lists it (intersection). Every registration
hands back an undo callable, so plugins keep it reversible.

execute() runs one call through a fixed pipeline: pre votes, the ask
gate, deny-only guards, the tool body, post review. Every exit leaves
through the same door: a plain result dict with is_error and content.
A denial, a crash, bad args, or an unknown name never raises across
this boundary, because the model must see a turn-consistent transcript
and replay must reconstruct it.
"""

from dataclasses import dataclass

_RANK = {"allow": 0, "ask": 1, "deny": 2}


@dataclass(frozen=True)
class ToolDefinition:
    """What the model sees (name, description, params) plus the body."""

    name: str
    description: str
    params: dict  # param name -> description; every param is required
    execute: callable  # (args dict) -> content, coerced to str
    is_concurrency_safe: bool = False  # may this body overlap other calls?


class ToolRegistry:
    """The tools service: layered definitions plus the execution pipeline."""

    def __init__(self):
        self._layers = {}  # scope (None = global) -> name -> ToolDefinition
        self._restrictions = []  # (scope, frozenset of names allowed to stay)
        self._pre = []  # call -> "allow" | "ask" | "deny" | None
        self._guards = []  # call -> deny reason string | None
        self._post = []  # (call, result) -> replacement result | None
        self.asker = None  # call -> bool; with no asker, "ask" fails closed

    # -- registration: every method hands back its undo -------------------
    def register(self, tool, scope=None):
        layer = self._layers.setdefault(scope, {})
        if tool.name in layer:
            raise ValueError(f"tool '{tool.name}' already registered in this layer")
        layer[tool.name] = tool
        return lambda: layer.pop(tool.name)

    def restrict(self, names, scope=None):
        entry = (scope, frozenset(names))
        self._restrictions.append(entry)
        return lambda: self._restrictions.remove(entry)

    def pre(self, hook):
        self._pre.append(hook)
        return lambda: self._pre.remove(hook)

    def guard(self, check):
        self._guards.append(check)
        return lambda: self._guards.remove(check)

    def post(self, hook):
        self._post.append(hook)
        return lambda: self._post.remove(hook)

    def scope(self, scope_id):
        return ToolScope(self, scope_id)

    # -- resolution --------------------------------------------------------
    def _visible(self, scope):
        """The scope's layer shadows the global one; restrictions intersect."""
        merged = dict(self._layers.get(None, {}))
        if scope is not None:
            merged.update(self._layers.get(scope, {}))
        return {
            name: tool
            for name, tool in merged.items()
            if all(
                name in allowed
                for for_scope, allowed in self._restrictions
                if for_scope is None or for_scope == scope
            )
        }

    def schemas(self, scope=None):
        """What the request offers the model, in registration order."""
        return [
            {"name": t.name, "description": t.description, "params": dict(t.params)}
            for t in self._visible(scope).values()
        ]

    def is_safe(self, call, scope=None):
        """Parallel-safe by declaration only; unknown names count as exclusive."""
        tool = self._visible(scope).get(call.get("name"))
        return tool is not None and tool.is_concurrency_safe

    # -- the pipeline --------------------------------------------------------
    def execute(self, call, scope=None):
        """pre -> ask -> guard -> execute -> post. Every exit is a result."""
        return self._review(call, self._run(call, scope))

    def _run(self, call, scope):
        name = call.get("name")
        tool = self._visible(scope).get(name)
        if tool is None:
            return self._result(call, True, f"unknown tool '{name}'")
        decision = "allow"
        for hook in list(self._pre):
            vote = hook(call)
            if vote is not None and _RANK[vote] > _RANK[decision]:
                decision = vote  # votes only tighten, never loosen
        if decision == "deny":
            return self._result(call, True, "denied before execution")
        if decision == "ask":
            approved = self.asker is not None and self.asker(call)
            if not approved:
                return self._result(call, True, "approval was asked and not given")
        for check in list(self._guards):
            reason = check(call)
            if reason:
                return self._result(call, True, f"denied: {reason}")
        args = dict(call.get("args", {}))
        missing = sorted(set(tool.params) - set(args))
        unknown = sorted(set(args) - set(tool.params))
        if missing or unknown:
            return self._result(
                call, True, f"invalid args: missing {missing}, unknown {unknown}"
            )
        try:
            return self._result(call, False, str(tool.execute(args)))
        except Exception as exc:  # the body may fail; the pipeline may not
            return self._result(call, True, f"{type(exc).__name__}: {exc}")

    def _review(self, call, result):
        for hook in list(self._post):
            result = hook(call, result) or result
        return result

    @staticmethod
    def _result(call, is_error, content):
        return {
            "call_id": call.get("id"),
            "name": call.get("name"),
            "is_error": is_error,
            "content": content,
        }


class ToolScope:
    """One agent's view of the registry: its own layer over the global one."""

    def __init__(self, registry, scope_id):
        self._registry = registry
        self._scope = scope_id

    def register(self, tool):
        return self._registry.register(tool, self._scope)

    def restrict(self, names):
        return self._registry.restrict(names, self._scope)

    def schemas(self):
        return self._registry.schemas(self._scope)

    def execute(self, call):
        return self._registry.execute(call, self._scope)

    def is_safe(self, call):
        return self._registry.is_safe(call, self._scope)


def tools_plugin(ctx):
    ctx.provide("tools", ToolRegistry())
