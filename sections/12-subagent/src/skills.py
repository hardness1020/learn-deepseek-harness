"""Skills: a layered provider registry; catalog injected, bodies on demand.

A skill is a named instruction text with a one-line description. The
registry never stores skills; it holds providers, each resolving names
to text through two verbs: list() yields {"name", "description"}
summaries, get(name) yields one full body. Providers stack in layers,
registration order, and a later layer shadows an earlier one's names,
so a workspace provider can override a built-in skill without touching
it. Every registration hands back its undo, kernel-style.

The catalog/body split is the token economy. The catalog (names and
descriptions only) renders through a section 08 context provider, so
it rides the runtime-context snapshot: injected into every request,
re-emitted only when it changes. Bodies never travel until asked for:
the model loads one through the skill tool, and the text arrives as an
ordinary tool/result row. Summaries are paid for always, bodies only
on use.
"""

from tools import ToolDefinition

CATALOG_HEADER = "skills, load with the skill tool before use:"


class SkillRegistry:
    """Layers of providers; the catalog and get() resolve through shadowing."""

    def __init__(self):
        self._providers = []  # layers, registration order; later shadows earlier

    def register(self, provider):
        """Add a provider layer on top. Hands back its undo."""
        self._providers.append(provider)
        return lambda: self._providers.remove(provider)

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

    def catalog_text(self):
        """The catalog block the context provider injects; "" when empty."""
        summaries = self.catalog()
        if not summaries:
            return ""
        lines = [f"- {s['name']}: {s['description']}" for s in summaries]
        return CATALOG_HEADER + "\n" + "\n".join(lines)


class MemorySkillProvider:
    """The simplest provider: a dict of name -> {"description", "body"}."""

    def __init__(self, skills):
        self._skills = dict(skills)

    def list(self):
        """Summaries only: a provider never volunteers a body."""
        return [
            {"name": name, "description": skill["description"]}
            for name, skill in self._skills.items()
        ]

    def get(self, name):
        skill = self._skills.get(name)
        return skill["body"] if skill else None


def skills_plugin(ctx):
    skills = SkillRegistry()
    # The catalog is context, never system text: it rides the section 08
    # snapshot, so a provider change re-emits it and a quiet catalog is free.
    ctx.effect(
        ctx.get("system_prompt").context(
            "skills", lambda ac: skills.catalog_text(), order=100
        ),
        "skill catalog",
    )

    def load(args):
        body = skills.get(args["name"])
        if body is None:  # the pipeline turns this into a normal is_error result
            raise ValueError(f"unknown skill '{args['name']}'")
        return body

    ctx.effect(
        ctx.get("tools").register(
            ToolDefinition(
                name="skill",
                description="Load the full instructions for one skill from the catalog.",
                params={"name": "the skill's name, exactly as the catalog lists it"},
                execute=load,
                is_concurrency_safe=True,  # reading instructions may overlap
            )
        ),
        "skill tool",
    )
    ctx.provide("skills", skills)
