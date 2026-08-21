"""Jobs: the owner-fenced background-work protocol.

A tool call that outlives its turn cannot stay a tool call. The
scheduler's contract (section 06) is that started work is never
abandoned and every call answers before the step closes, so a
long-running command would hold the whole turn hostage. Jobs split
the call in two: the producing call starts the work and answers
immediately with a job id, and the work itself runs on its own
thread, owned by the registry from that moment on.

The handoff is the point. The producer hands start() a run() that
returns the protocol triple (cancel, done, read_output) and gets
back an id; the instant the id is published, the producing call's
abort signal stops mattering, and the only remaining kill switch is
job_kill, fenced to the owner. Every accessor (read, list, kill) is
denied unless the caller owns the job, and the caller's identity is
baked in when its tools mount, ambient, never a model argument.

Settlement is first-wins. A job ends exactly once, as "completed",
"failed", or "killed", whichever settles first; a kill racing a
completion cannot flip the outcome afterward. On settlement a
"wakeup" job sends its owner a notice through the section 07 inbox:
followup() when the owner is idle, so the notice opens a turn of
its own, inject() when a turn is in flight, so it parks until the
next step boundary. A "quiet" job says nothing and waits to be
polled. Either way the log stays boundary-clean: background work
never appends a row of its own.
"""

import shlex
import threading
from dataclasses import dataclass

from tools import ToolDefinition

DELIVERIES = ("quiet", "wakeup")


@dataclass(frozen=True)
class JobOwner:
    """Who a job answers to: the fence identity and the wakeup door."""

    id: str  # compared against the caller identity on every accessor
    agent: object  # the Agent whose inbox receives completion notices


@dataclass
class Job:
    """One background job: the registry's record, never handed out raw."""

    id: str
    kind: str
    label: str
    owner: JobOwner
    delivery: str
    cancel: callable  # ask the work to stop; cooperative, best effort
    read_output: callable  # the output so far, as text
    outcome: dict = None  # {"status", "detail"} once settled


class JobRegistry:
    """The jobs service: ids, fencing, snapshots, first-wins settlement."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}  # job_id -> Job
        self._count = 0

    def start(self, kind, label, owner, run, delivery="wakeup"):
        """Start background work; the job id, published immediately.

        run() starts the work and returns the protocol triple:
        cancel (ask it to stop), done (block until it ends, returning
        ("completed", None) or ("failed", detail)), and read_output
        (the output so far). The registry owns everything after: a
        watcher thread awaits done and settles, so completion lands
        even if nobody ever polls.
        """
        if delivery not in DELIVERIES:
            raise ValueError(f"unknown delivery '{delivery}'")
        cancel, done, read_output = run()
        with self._lock:
            self._count += 1
            job = Job(
                f"job-{self._count}", kind, label, owner, delivery, cancel, read_output
            )
            self._jobs[job.id] = job
        threading.Thread(
            target=lambda: self._settle(job, *done()),
            name=f"{job.id}-watcher",
            daemon=True,
        ).start()
        return job.id

    # -- the fence: every accessor answers only to the owner ---------------
    def _fenced(self, job_id, caller_id):
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.owner.id != caller_id:
            # One message for a foreign id and a bogus one: a stranger
            # learns nothing, not even that the id exists.
            raise PermissionError(f"no job '{job_id}' owned by this session")
        return job

    def output(self, job_id, caller_id):
        job = self._fenced(job_id, caller_id)
        return {"status": self._status(job), "output": job.read_output()}

    def kill(self, job_id, caller_id):
        """Ask the work to stop and settle "killed"; the post-race status.

        First-wins settlement decides what the kill meant: a job that
        already completed reports "completed", and the kill was a no-op.
        """
        job = self._fenced(job_id, caller_id)
        job.cancel()
        self._settle(job, "killed")
        return self._status(job)

    def list_for(self, caller_id):
        """Snapshots of the caller's jobs, oldest first. Never anyone else's."""
        with self._lock:
            jobs = [job for job in self._jobs.values() if job.owner.id == caller_id]
        return [
            {"id": job.id, "kind": job.kind, "label": job.label, "status": self._status(job)}
            for job in jobs
        ]

    # -- settlement: exactly once, first wins ------------------------------
    def _settle(self, job, status, detail=None):
        with self._lock:
            if job.outcome is not None:
                return  # the race already settled; a later voice changes nothing
            job.outcome = {"status": status, "detail": detail}
        self._notify(job)  # outside the lock: delivery may drive a whole turn

    def _notify(self, job):
        """Deliver the completion notice through the owner's inbox."""
        if job.delivery != "wakeup":
            return  # quiet: the owner polls with job_output when it cares
        notice = f"job {job.id} ({job.label}) finished: {job.outcome['status']}"
        agent = job.owner.agent
        if agent.status == "idle":
            agent.followup(notice)  # idle: the notice opens a turn of its own
        else:
            agent.inject(notice)  # busy: park it for the next step boundary

    @staticmethod
    def _status(job):
        return job.outcome["status"] if job.outcome else "running"


def jobs_plugin(ctx):
    ctx.provide("jobs", JobRegistry())


# -- the tools: one producer, three controls, fenced to one owner ----------


def job_tools(owner):
    """Plugin factory: this owner's job tools, mounted into its scope.

    The fence identity is baked in here, at mount, so it is ambient:
    whatever job id the model types, these bodies answer to the
    registry as this owner and no other. The producer and the three
    control tools resolve "jobs" at execute time, like any consumer;
    the controls are written once, for every producer alike.
    """

    def apply(ctx):
        def seam(key):
            implementation = ctx.get(key)
            if implementation is None:
                raise RuntimeError(f"no provider mounted under '{key}'")
            return implementation

        def start_shell(args):
            shell = seam("shell")
            argv = shlex.split(args["command"])

            def run():
                """The producer's side of the protocol: start, hand over."""
                state = {"output": "", "error": None}
                cancelled = threading.Event()

                def work():
                    if cancelled.is_set():
                        return  # cancelled before it began: never runs
                    try:
                        state["output"] = shell.run(argv)
                    except Exception as exc:
                        state["error"] = f"{type(exc).__name__}: {exc}"

                thread = threading.Thread(target=work, daemon=True)
                thread.start()

                def done():
                    thread.join()
                    if state["error"]:
                        return ("failed", state["error"])
                    return ("completed", None)

                return (cancelled.set, done, lambda: state["output"])

            job_id = seam("jobs").start(
                "shell", args["command"], owner, run, delivery=args["delivery"]
            )
            return f"started {job_id}"

        def read_output(args):
            snapshot = seam("jobs").output(args["job_id"], owner.id)
            return f"{snapshot['status']}; output: {snapshot['output'] or '(none)'}"

        def kill(args):
            status = seam("jobs").kill(args["job_id"], owner.id)
            return f"job {args['job_id']}: {status}"

        def list_jobs(args):
            rows = seam("jobs").list_for(owner.id)
            if not rows:
                return "(no jobs)"
            return "\n".join(
                f"{row['id']} [{row['kind']}] {row['label']}: {row['status']}"
                for row in rows
            )

        tools = ctx.get("tools")
        ctx.effect(
            tools.register(
                ToolDefinition(
                    name="shell_job",
                    description=(
                        "Run one command line in the background through the"
                        " shell seam; answers immediately with a job id."
                    ),
                    params={
                        "command": "the command line to run",
                        "delivery": (
                            "'wakeup' to be notified when it finishes,"
                            " 'quiet' to poll with job_output"
                        ),
                    },
                    execute=start_shell,
                ),
                scope=owner.id,
            ),
            "shell_job tool",
        )
        ctx.effect(
            tools.register(
                ToolDefinition(
                    name="job_output",
                    description="The status and output so far of one of your jobs.",
                    params={"job_id": "the job's id"},
                    execute=read_output,
                    is_concurrency_safe=True,  # reads may overlap
                ),
                scope=owner.id,
            ),
            "job_output tool",
        )
        ctx.effect(
            tools.register(
                ToolDefinition(
                    name="job_kill",
                    description="Stop one of your jobs; reports the settled status.",
                    params={"job_id": "the job's id"},
                    execute=kill,
                ),
                scope=owner.id,
            ),
            "job_kill tool",
        )
        ctx.effect(
            tools.register(
                ToolDefinition(
                    name="job_list",
                    description="Snapshots of your jobs: id, kind, label, status.",
                    params={},
                    execute=list_jobs,
                    is_concurrency_safe=True,
                ),
                scope=owner.id,
            ),
            "job_list tool",
        )

    return apply
