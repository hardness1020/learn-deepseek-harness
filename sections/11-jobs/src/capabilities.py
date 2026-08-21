"""Capability seams: one capability, three roles.

A capability (fs, shell, sandbox, llm) is anything the harness does to
the world outside the log. Each one splits three ways:

- Definition: an ABC, one ctx key, and the seam's vocabulary. It owns
  the contract and nothing else.
- Provider: a plugin mounting one concrete implementation under the
  key. The kernel's provide() already refuses a duplicate key, so an
  exclusive seam fails loud at mount by construction.
- Consumer: code that resolves the key and speaks only the ABC's
  verbs, usually a model-facing tool. A consumer never imports a
  provider, so swapping the backend never touches the schema the
  model sees.

Two seams bend the shape on purpose, and the bends answer this
section's question. The sandbox is not a tool triple: its one verb,
confine(argv, policy), is consumed by providers of other seams, and
the real confinement machinery sits above this rebuild's Ceiling, so
the provider here only rewrites argv and fails closed. And llm folds
Definition and Consumer into one service, because its consumer is the
agent loop itself: adapters stay plain callables in the Model seam
shape, plural under names, resolved at call time.
"""

import shlex
from abc import ABC, abstractmethod

from tools import ToolDefinition

SANDBOX_ARGV_MARKER = "mini-sandbox"


def provider(key, implementation):
    """The Provider role as a plugin: one implementation under one key.

    The kernel does the seam's bookkeeping: provide() hands back an
    undo collected on the plugin's fiber, and a second mount under an
    exclusive key raises before anything half-registers.
    """

    def apply(ctx):
        ctx.provide(key, implementation)

    return apply


# -- fs ------------------------------------------------------------------


class FileSystem(ABC):
    """Definition of the fs seam, ctx key "fs". Exclusive: one provider."""

    @abstractmethod
    def read(self, path):
        """The file's text; raises FileNotFoundError for a path not there."""

    @abstractmethod
    def write(self, path, text):
        """Create or replace one file."""


class MemoryFileSystem(FileSystem):
    """Provider: a dict of path -> text; keeps the Offline check off the disk."""

    def __init__(self, files=None):
        self._files = dict(files or {})

    def read(self, path):
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

    def write(self, path, text):
        self._files[path] = str(text)


# -- shell ---------------------------------------------------------------


class ShellExecutor(ABC):
    """Definition of the shell seam, ctx key "shell". Exclusive: one provider."""

    @abstractmethod
    def run(self, argv):
        """Run one argv list; the command's output as text."""


class EchoShellExecutor(ShellExecutor):
    """Provider and shell-plane stand-in: runs nothing, answers with the argv.

    The Offline check needs a shell that is deterministic and whose
    input is visible in the log; echoing the exact argv it was asked
    to run gives both, the way the Scripted stand-in does for the
    Model seam.
    """

    def run(self, argv):
        return " ".join(argv)


# -- sandbox -------------------------------------------------------------


class SandboxProvider(ABC):
    """Definition of the sandbox seam, ctx key "sandbox". One verb, no tool.

    This seam's consumers are providers of other seams, never the
    model: a sandboxed shell or fs provider asks confine() how to
    fence work the model already asked for through some other tool.
    """

    @abstractmethod
    def confine(self, argv, policy):
        """The argv to run instead, fenced by the named policy. Fails closed."""


class ArgvRewriteSandbox(SandboxProvider):
    """Provider: the Ceiling stub. Rewrites argv; enforces nothing.

    Real dsh chains platform runners (bwrap, landlock, seatbelt)
    here; that machinery is above this rebuild's Ceiling. The stub
    keeps the seam's two promises anyway: the rewrite is visible in
    the argv it returns, and an unknown policy raises instead of
    handing back the argv unfenced.
    """

    def __init__(self, policies):
        self._policies = frozenset(policies)

    def confine(self, argv, policy):
        if policy not in self._policies:  # fail closed: never run unfenced
            raise ValueError(f"unknown sandbox policy '{policy}'")
        return [SANDBOX_ARGV_MARKER, "--policy", policy, "--", *argv]


class SandboxedShellExecutor(ShellExecutor):
    """Provider built on another seam: run everything through the fence."""

    def __init__(self, inner, sandbox, policy):
        self._inner = inner
        self._sandbox = sandbox
        self._policy = policy

    def run(self, argv):
        return self._inner.run(self._sandbox.confine(argv, self._policy))


# -- llm -----------------------------------------------------------------


class LlmRuntime:
    """The llm seam, ctx key "llm": Definition and Consumer folded.

    No ABC and no tool. An adapter is any callable in the Model seam
    shape, and the consumer is the agent loop itself, so the roles
    that would earn their own homes live in this one service. Adapters
    are plural, registered under names; model(name) hands the loop a
    seam that resolves its adapter per call, so a backend swap reaches
    a live agent without touching it.
    """

    def __init__(self):
        self._adapters = {}

    def register(self, name, adapter):
        if name in self._adapters:
            raise ValueError(f"llm adapter '{name}' already registered")
        self._adapters[name] = adapter
        return lambda: self._adapters.pop(name)

    def model(self, name):
        """The Model seam bound to an adapter name, resolved per call."""

        def seam(messages, tools=(), system=""):
            adapter = self._adapters.get(name)
            if adapter is None:
                # The loop is this seam's consumer: a missing model is a
                # harness bug, so it fails loud instead of degrading.
                raise LookupError(f"no llm adapter registered under '{name}'")
            return adapter(messages, tools, system)

        return seam


def llm_plugin(ctx):
    ctx.provide("llm", LlmRuntime())


# -- consumers -----------------------------------------------------------


def capability_tools_plugin(ctx):
    """The Consumer role: model-facing tools over the fs and shell seams.

    Each body resolves its seam through the ctx key at execute time
    and speaks only the ABC's verbs. A missing provider raises here,
    and the section 05 pipeline answers with a normal is_error result,
    so a half-wired harness degrades instead of tearing the transcript.
    """

    def seam(key):
        implementation = ctx.get(key)
        if implementation is None:
            raise RuntimeError(f"no provider mounted under '{key}'")
        return implementation

    def read(args):
        return seam("fs").read(args["path"])

    def write(args):
        seam("fs").write(args["path"], args["text"])
        return f"wrote {args['path']}"

    def shell(args):
        return seam("shell").run(shlex.split(args["command"]))

    tools = ctx.get("tools")
    ctx.effect(
        tools.register(
            ToolDefinition(
                name="read",
                description="Read one file through the fs seam.",
                params={"path": "the file's path"},
                execute=read,
                is_concurrency_safe=True,  # reads may overlap
            )
        ),
        "read tool",
    )
    ctx.effect(
        tools.register(
            ToolDefinition(
                name="write",
                description="Create or replace one file through the fs seam.",
                params={"path": "the file's path", "text": "the file's new content"},
                execute=write,
            )
        ),
        "write tool",
    )
    ctx.effect(
        tools.register(
            ToolDefinition(
                name="shell",
                description="Run one command line through the shell seam.",
                params={"command": "the command line to run"},
                execute=shell,
            )
        ),
        "shell tool",
    )
