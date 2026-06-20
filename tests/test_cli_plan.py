"""`sutra plan` CLI — thin glue over the (separately tested) planner functions."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import friday.cli as cli
from friday.core import planner

runner = CliRunner()


@pytest.fixture
def plans_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "_PLANS_DIR", tmp_path / "plans")
    return tmp_path / "plans"


def test_plan_list_empty(plans_dir):
    r = runner.invoke(cli.app, ["plan", "list"])
    assert r.exit_code == 0 and "no plans yet" in r.stdout


def test_plan_list_and_status(plans_dir):
    p = planner.new_plan("compromise the box", ["recon", "exploit"])
    p.steps[0].status = planner.DONE
    planner.save(p)

    r = runner.invoke(cli.app, ["plan", "list"])
    assert r.exit_code == 0
    assert "compromise-the-box" in r.stdout and "1/2" in r.stdout

    r2 = runner.invoke(cli.app, ["plan", "status", "compromise-the-box"])
    assert r2.exit_code == 0
    assert "recon" in r2.stdout and "exploit" in r2.stdout


def test_plan_status_missing(plans_dir):
    r = runner.invoke(cli.app, ["plan", "status", "nope"])
    assert r.exit_code == 0 and "no plan" in r.stdout


def test_plan_run_executes(plans_dir, monkeypatch):
    async def fake_make_plan(goal, **kw):
        return planner.new_plan(goal, ["step one", "step two"])

    async def fake_run_plan(plan, **kw):
        for s in plan.steps:
            s.status = planner.DONE
        plan.status = "done"
        return plan

    monkeypatch.setattr(planner, "make_plan", fake_make_plan)
    monkeypatch.setattr(planner, "run_plan", fake_run_plan)

    r = runner.invoke(cli.app, ["plan", "run", "do a thing", "-y"])
    assert r.exit_code == 0
    assert "Plan:" in r.stdout and "Result:" in r.stdout and "step one" in r.stdout


def test_plan_run_no_steps_is_graceful(plans_dir, monkeypatch):
    async def fake_make_plan(goal, **kw):
        p = planner.new_plan(goal, [])
        p.status = "failed"
        return p

    monkeypatch.setattr(planner, "make_plan", fake_make_plan)
    r = runner.invoke(cli.app, ["plan", "run", "x"])
    assert r.exit_code == 0 and "couldn't break that into steps" in r.stdout


def test_plan_resume_missing(plans_dir):
    r = runner.invoke(cli.app, ["plan", "resume", "nope"])
    assert r.exit_code == 0 and "no plan" in r.stdout
