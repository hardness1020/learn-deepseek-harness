"""The 4-stage parallel tool scheduler: prepare, dispatch, finalize, finish.

The loop hands a reply's tool calls here instead of running them one
by one. Prepare gives every call its tool/call row and a safety
verdict before anything runs. Dispatch sends calls to worker threads
in batches: consecutive parallel-safe calls share a batch and overlap,
while an exclusive call is a batch of one. Finalize is the barrier:
the next batch starts only when the whole previous batch has finished,
and started work is never abandoned, even after a cancel. Finish
appends one tool/result per call in the model's order, and a call the
abort flag stopped before dispatch gets a synthetic error result, so
every question in the transcript keeps its answer and replay stays
valid.

Only the calling thread touches the session log. Worker threads run
the pipeline and hand back result dicts, nothing else.
"""

from concurrent.futures import ThreadPoolExecutor

ABORTED_BEFORE_DISPATCH = "aborted before dispatch"


def execute_tool_calls(session, tools, calls, aborted):
    """Drive one reply's calls through the stages; log rows in model order."""
    # prepare: a log row and a safety verdict per call, before anything runs
    plan = [(index, call, tools.is_safe(call)) for index, call in enumerate(calls)]
    for _index, call, _safe in plan:
        session.append("tool/call", call)  # log-only: before dispatch
    outcomes = {}  # index -> result dict, filled as batches finalize
    with ThreadPoolExecutor(max_workers=max(1, len(plan))) as pool:
        for batch in _batches(plan):
            # dispatch: a batch starts only if nothing has aborted the turn
            if aborted.is_set():
                break
            futures = [
                (index, pool.submit(tools.execute, call))
                for index, call, _safe in batch
            ]
            # finalize: the barrier; started work is never abandoned
            for index, future in futures:
                outcomes[index] = future.result()
    # finish: one result per call, model order; skipped calls answer too
    for index, call, _safe in plan:
        result = outcomes.get(index) or {
            "call_id": call.get("id"),
            "name": call.get("name"),
            "is_error": True,
            "content": ABORTED_BEFORE_DISPATCH,
        }
        session.append("tool/result", result)


def _batches(plan):
    """Consecutive safe calls share a batch; an exclusive call stands alone."""
    safe_run = []
    for entry in plan:
        if entry[2]:
            safe_run.append(entry)
            continue
        if safe_run:
            yield safe_run
            safe_run = []
        yield [entry]  # exclusive: a batch of one, a barrier on both sides
    if safe_run:
        yield safe_run
