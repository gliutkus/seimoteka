"""The lifecycles of the background jobs, written down as data.

The page "Papildyti žinių bazę" (kb_page.py) starts three kinds of job, each kept
in a JSON file whose "state" field a worker process moves along:

- an interview (ingest.interviews), one file for each video;
- a Seimas update (ingest.update);
- a search for new YouTube videos (ingest.youtube_new).

Each module declares its LIFECYCLE below as a list of moves: from a state, to a
state, and the function that makes the move. A module changes a job's state only
through its move(), which refuses any move not on the list, so a stale page, a
second click or a command run at the wrong time gets a clear error instead of a
job in a state nothing expects. `None` stands for "no job": a move from None
creates the job, a move to None deletes it. Staying in a state is not a move.

Print the lifecycles as Mermaid diagrams, or check one move:

    uv run python -m ingest.lifecycle
    uv run python -m ingest.lifecycle interview review loading
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime


class IllegalMove(ValueError):
    """A job was asked to move between two states its lifecycle does not join."""


@dataclass(frozen=True)
class Move:
    source: str | None
    target: str | None
    by: str  # the function that makes the move, for people and agents reading it


@dataclass(frozen=True)
class Lifecycle:
    name: str
    moves: tuple[Move, ...]
    # States in which a worker process owns the job: nobody else may move it.
    running: tuple[str, ...]
    # States in which the job waits for a person.
    waiting: tuple[str, ...] = ()
    _next: dict = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        following: dict[str | None, set[str | None]] = {}
        for move in self.moves:
            following.setdefault(move.source, set()).add(move.target)
        object.__setattr__(self, "_next", following)
        unknown = set(self.running + self.waiting) - set(self.states)
        if unknown:
            raise ValueError(f"{self.name}: no move reaches {sorted(unknown)}")

    @property
    def states(self) -> tuple[str, ...]:
        """Every state, in the order the moves first name them."""
        seen: dict[str, None] = {}
        for move in self.moves:
            for state in (move.source, move.target):
                if state is not None:
                    seen.setdefault(state)
        return tuple(seen)

    def allows(self, source: str | None, target: str | None) -> bool:
        return source == target or target in self._next.get(source, ())

    def next(self, source: str | None) -> list[str | None]:
        """The states a job in `source` may move to."""
        return sorted(self._next.get(source, ()), key=lambda s: (s is None, s or ""))

    def check(self, source: str | None, target: str | None) -> None:
        if self.allows(source, target):
            return
        options = ", ".join(_name(s) for s in self.next(source)) or "nowhere"
        raise IllegalMove(
            f"a {self.name} job cannot move from {_name(source)} to "
            f"{_name(target)}; from {_name(source)} it moves to: {options}"
        )

    def mermaid(self) -> str:
        lines = ["stateDiagram-v2"]
        for move in self.moves:
            source = "[*]" if move.source is None else move.source
            target = "[*]" if move.target is None else move.target
            lines.append(f"    {source} --> {target}: {move.by}")
        return "\n".join(lines)


def moves(*rows: tuple[Iterable[str | None] | str | None, str | None, str]) -> tuple:
    """Moves from rows of (source or sources, target, by)."""
    out = []
    for sources, target, by in rows:
        if sources is None or isinstance(sources, str):
            sources = (sources,)
        out.extend(Move(source, target, by) for source in sources)
    return tuple(out)


def _name(state: str | None) -> str:
    return "no job" if state is None else repr(state)


def alive(pid: int | None) -> bool:
    """Whether a process with this pid is running."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def abandoned(job: dict, lifecycle: Lifecycle, now: datetime, stale: float) -> bool:
    """Whether a running job's worker died, or never reported in within `stale`
    seconds of the job being queued."""
    if job.get("state") not in lifecycle.running:
        return False
    if job.get("pid"):
        return not alive(job["pid"])
    waited = now - datetime.fromisoformat(job["updated_at"])
    return waited.total_seconds() >= stale


def mover(lifecycle: Lifecycle, write: Callable[..., dict]) -> Callable[..., dict]:
    """A module's move(job, state, **fields): check the move, then write it."""

    def move(job: dict, state: str, **fields) -> dict:
        lifecycle.check(job.get("state"), state)
        return write(job, state=state, **fields)

    move.__doc__ = f"Move a {lifecycle.name} job to `state`, if its lifecycle allows."
    return move


def all_lifecycles() -> dict[str, Lifecycle]:
    from . import interviews, update, youtube_new

    return {
        lc.name: lc
        for lc in (interviews.LIFECYCLE, update.LIFECYCLE, youtube_new.LIFECYCLE)
    }


def main(argv: list[str] | None = None) -> None:
    known = all_lifecycles()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("job", nargs="?", choices=sorted(known))
    ap.add_argument("source", nargs="?", help="a state, or 'none' for no job")
    ap.add_argument("target", nargs="?", help="a state, or 'none' for no job")
    args = ap.parse_args(argv)
    if args.source and args.target:
        lifecycle = known[args.job]
        source, target = (
            None if s.lower() == "none" else s for s in (args.source, args.target)
        )
        try:
            lifecycle.check(source, target)
        except IllegalMove as exc:
            raise SystemExit(str(exc)) from exc
        print("allowed")
        return
    for name, lifecycle in known.items():
        if args.job and name != args.job:
            continue
        print(f"## {name}\n")
        print(f"running: {', '.join(lifecycle.running)}")
        if lifecycle.waiting:
            print(f"waiting for a person: {', '.join(lifecycle.waiting)}")
        print(f"\n```mermaid\n{lifecycle.mermaid()}\n```\n")


if __name__ == "__main__":
    main()
