"""The background jobs' lifecycles: declared once, enforced, shown in Lithuanian."""

import os
import re
import sys
from pathlib import Path

import pytest

from ingest import interviews, lifecycle, update, youtube, youtube_new

ROOT = Path(__file__).resolve().parent.parent
LIFECYCLES = (interviews.LIFECYCLE, update.LIFECYCLE, youtube_new.LIFECYCLE)


@pytest.mark.parametrize("cycle", LIFECYCLES, ids=lambda c: c.name)
def test_every_state_is_reached_from_no_job_and_can_be_left(cycle):
    reached, frontier = set(), [None]
    while frontier:
        for state in cycle.next(frontier.pop()):
            if state is not None and state not in reached:
                reached.add(state)
                frontier.append(state)
    assert reached == set(cycle.states)
    for state in cycle.states:  # no job is ever stuck
        assert cycle.next(state), state


def test_a_move_off_the_list_is_refused_and_names_the_way_on():
    cycle = interviews.LIFECYCLE
    assert cycle.allows("review", "loading") and cycle.allows("loading", "loading")
    with pytest.raises(lifecycle.IllegalMove) as refused:
        cycle.check("loaded", "queued")
    assert "from 'loaded' it moves to: 'failed', 'loading', 'review'" in str(
        refused.value
    )
    job = {"state": "queued"}
    move = lifecycle.mover(cycle, lambda job, **fields: {**job, **fields})
    assert move(job, "downloading")["state"] == "downloading"
    with pytest.raises(ValueError):  # IllegalMove is a ValueError: pages catch it
        move(job, "loaded")


@pytest.mark.parametrize(
    "path", ["ingest/interviews.py", "ingest/update.py", "ingest/youtube_new.py"]
)
def test_states_change_only_through_the_lifecycle(path):
    source = (ROOT / path).read_text("utf-8")
    assert not re.findall(r"\bstate\s*=\s*['\"]", source)
    # a new job's file is written only after its lifecycle allowed the job
    for created in re.finditer(r"['\"]state['\"]:\s*['\"]queued['\"]", source):
        before = source[: created.start()]
        assert before.rfind("LIFECYCLE.check(") > before.rfind("\ndef ")


def test_the_page_never_writes_a_state_and_names_every_one(monkeypatch):
    if str(ROOT) not in sys.path:
        monkeypatch.syspath_prepend(str(ROOT))
    import kb_page

    assert "state=" not in (ROOT / "kb_page.py").read_text("utf-8")
    assert set(interviews.LIFECYCLE.states) <= set(kb_page.STATES)
    assert set(update.STEPS) <= set(kb_page.UPDATE_STEPS)
    assert set(youtube_new.STEPS) <= set(kb_page.NEW_STEPS)


def test_the_lifecycles_print_as_diagrams_and_moves_can_be_checked(capsys):
    lifecycle.main([])
    shown = capsys.readouterr().out
    assert shown.count("stateDiagram-v2") == 3
    assert "review --> loading: queue_load()" in shown
    assert "[*] --> queued: start()" in shown
    lifecycle.main(["interview", "review", "loading"])
    assert capsys.readouterr().out.strip() == "allowed"
    with pytest.raises(SystemExit, match="cannot move from 'loaded' to 'queued'"):
        lifecycle.main(["interview", "loaded", "queued"])


def test_a_search_another_live_worker_runs_is_not_taken_over(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "ROOT", tmp_path)
    for name in ("JOB", "LOCK"):
        monkeypatch.setattr(
            youtube_new, name, tmp_path / getattr(youtube_new, name).name
        )
    owner = os.getppid()  # a live process that is not this one
    youtube_new.update({"state": "running", "pid": owner, "created_at": "x"})
    with pytest.raises(SystemExit, match="already running"):
        youtube_new.run(search=lambda report: None)
    assert youtube_new.read()["pid"] == owner
