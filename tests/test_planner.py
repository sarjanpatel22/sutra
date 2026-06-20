"""Planner data model + persistence — a plan is a durable checklist that resumes after a
token/budget pause, so the round-trip and progress logic must be rock-solid."""

import json

import pytest

from friday.core import planner


@pytest.fixture
def plans_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    monkeypatch.setattr(planner, "_PLANS_DIR", d)
    return d


def test_new_plan_numbers_steps_and_drops_blanks():
    p = planner.new_plan("  Research and write a report  ",
                         ["search the web", "  ", "write the file"])
    assert p.goal == "Research and write a report"
    assert [s.n for s in p.steps] == [1, 2]          # blank dropped, renumbered
    assert [s.description for s in p.steps] == ["search the web", "write the file"]
    assert all(s.status == planner.PENDING for s in p.steps)


def test_slugify_stable_and_safe():
    assert planner.slugify("Research the THM rooms!") == "research-the-thm-rooms"
    assert planner.slugify("   ") == "plan"          # empty → fallback
    assert len(planner.slugify("x" * 100)) == 40     # capped


def test_next_pending_and_completion():
    p = planner.new_plan("g", ["a", "b"])
    assert p.next_pending().n == 1 and not p.is_complete()
    p.steps[0].status = planner.DONE
    assert p.next_pending().n == 2                   # advances to the next undone step
    p.steps[1].status = planner.SKIPPED
    assert p.next_pending() is None and p.is_complete()  # skipped counts as finished


def test_progress_counts_doneish():
    p = planner.new_plan("g", ["a", "b", "c"])
    p.steps[0].status = planner.DONE
    p.steps[1].status = planner.SKIPPED
    assert p.progress() == (2, 3)                     # failed/pending are NOT finished


def test_empty_plan_is_not_complete():
    assert planner.new_plan("g", []).is_complete() is False


def test_save_load_roundtrip_preserves_progress(plans_dir):
    p = planner.new_plan("Compromise the box", ["recon", "exploit", "loot"])
    p.steps[0].status = planner.DONE
    p.steps[0].result = "nmap found 22,80"
    p.status = "paused"
    path = planner.save(p)
    assert path.is_file()

    back = planner.load(p.slug)
    assert back is not None
    assert back.goal == "Compromise the box" and back.status == "paused"
    assert back.steps[0].status == planner.DONE and back.steps[0].result == "nmap found 22,80"
    assert back.steps[1].status == planner.PENDING
    assert back.next_pending().n == 2                 # resumes exactly where it stopped


def test_save_is_atomic_no_tmp_left(plans_dir):
    planner.save(planner.new_plan("g", ["a"]))
    assert not list(plans_dir.glob("*.tmp"))          # atomic rename leaves no temp file


def test_load_missing_or_corrupt_returns_none(plans_dir):
    assert planner.load("does-not-exist") is None
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / "broken.json").write_text("{ not json")
    assert planner.load("broken") is None             # corrupt file never crashes a resume
    (plans_dir / "partial.json").write_text(json.dumps({"slug": "partial"}))  # missing 'goal'
    assert planner.load("partial") is None


def test_list_plans_newest_first(plans_dir):
    a = planner.new_plan("first goal", ["x"])
    a.created_at = 100.0
    b = planner.new_plan("second goal", ["y"])
    b.created_at = 200.0
    planner.save(a)
    planner.save(b)
    listed = planner.list_plans()
    assert [p.slug for p in listed] == ["second-goal", "first-goal"]  # newest first
