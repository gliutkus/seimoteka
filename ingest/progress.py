"""Progress of a background job's steps, kept in its job file for the app to show.

A job records when each step started and finished under "steps", and how far the
running step has got under "progress": done out of total, in a unit the page knows
how to show. Reports are throttled so a worker writes its job file at most every
few seconds, however often its tool calls back.
"""

from __future__ import annotations

import time
from collections.abc import Callable


def begin(job: dict, name: str, when: str) -> dict:
    """Fields that start step `name`, closing the step that ran before it."""
    steps = dict(job.get("steps") or {})
    for step in steps.values():
        step.setdefault("finished", when)
    steps[name] = {"started": when}
    return {"steps": steps, "progress": None}


def finish(job: dict, when: str) -> dict:
    """Fields that close the running step."""
    steps = dict(job.get("steps") or {})
    for step in steps.values():
        step.setdefault("finished", when)
    return {"steps": steps, "progress": None}


def throttled(
    save: Callable[[dict], object], unit: str, every: float = 2.0
) -> Callable[[float, float], None]:
    """A callback taking (done, total) that saves {"done", "total", "unit"} at most
    every `every` seconds, and always at the end."""
    last = {"at": 0.0}

    def report(done: float, total: float) -> None:
        moment = time.monotonic()
        if done < total and moment - last["at"] < every:
            return
        last["at"] = moment
        save({"done": done, "total": total, "unit": unit})

    return report
