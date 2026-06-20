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


# --- PR2: decomposition (make_plan / _parse_steps) ---------------------------------

import types  # noqa: E402


def _resp(content):
    msg = types.SimpleNamespace(content=content)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def test_parse_steps_json_array():
    assert planner._parse_steps('["a", "b", "c"]') == ["a", "b", "c"]


def test_parse_steps_strips_markdown_fence():
    assert planner._parse_steps('```json\n["a", "b"]\n```') == ["a", "b"]


def test_parse_steps_numbered_and_bulleted_fallback():
    raw = "1. search the web\n2) write the file\n- check it\n* and verify"
    assert planner._parse_steps(raw) == [
        "search the web", "write the file", "check it", "and verify",
    ]


def test_parse_steps_empty():
    assert planner._parse_steps("") == []
    assert planner._parse_steps("   ") == []


def test_parse_steps_strips_reasoning_think_block():
    # qwen3 et al. emit <think>…</think> — the thoughts must NOT become steps (caught live)
    raw = '<think>\nlet me reason\nstep by step\n</think>\n["search the web", "write the file"]'
    assert planner._parse_steps(raw) == ["search the web", "write the file"]


def test_parse_steps_dangling_think_yields_nothing():
    # reasoning that ran out of tokens (open <think>, no close, no answer) → no fake steps
    raw = "<think>\nokay the user wants three rooms\nlet me think about the best approach"
    assert planner._parse_steps(raw) == []


async def test_make_plan_decomposes_goal(monkeypatch):
    async def fake_complete(messages, **kw):
        assert kw.get("local_tools_first") is False  # decompose uses the strong model, not local
        return _resp('["recon the box", "exploit the service", "grab the flag"]')

    monkeypatch.setattr(planner, "complete", fake_complete)
    p = await planner.make_plan("compromise the box")
    assert [s.description for s in p.steps] == [
        "recon the box", "exploit the service", "grab the flag",
    ]
    assert p.status == "active" and p.slug == "compromise-the-box"
    assert p.next_pending().n == 1


async def test_make_plan_numbered_reply(monkeypatch):
    async def fake_complete(messages, **kw):
        return _resp("1. search\n2. summarize\n3. save")

    monkeypatch.setattr(planner, "complete", fake_complete)
    p = await planner.make_plan("do a thing")
    assert [s.description for s in p.steps] == ["search", "summarize", "save"]


async def test_make_plan_caps_at_max_steps(monkeypatch):
    async def fake_complete(messages, **kw):
        return _resp(json.dumps([f"step {i}" for i in range(20)]))

    monkeypatch.setattr(planner, "complete", fake_complete)
    p = await planner.make_plan("big goal", max_steps=5)
    assert len(p.steps) == 5


async def test_make_plan_empty_goal_is_failed():
    p = await planner.make_plan("   ")
    assert p.status == "failed" and p.steps == []


async def test_make_plan_unusable_reply_is_failed(monkeypatch):
    async def fake_complete(messages, **kw):
        return _resp("[]")  # model returned an empty list → nothing to do

    monkeypatch.setattr(planner, "complete", fake_complete)
    p = await planner.make_plan("x")
    assert p.status == "failed" and p.steps == []


# --- PR3: execution (run_plan / resume_plan), persistence, budget + error pause -----

async def test_run_plan_executes_all_steps_and_persists(plans_dir, monkeypatch):
    prompts = []

    async def fake_run(prompt, confirm=None, observe=None, **kw):
        prompts.append(prompt)
        # an action step actually calls a tool — history carries the named tool round it ran
        return "did it", [{"role": "assistant", "tool_calls": [{"id": "1"}]},
                          {"role": "tool", "name": "write_file", "content": "ok"}]

    monkeypatch.setattr("friday.core.agent.run", fake_run)
    p = planner.new_plan("write a report", ["search", "write the file"])
    out = await planner.run_plan(p)
    assert out.status == "done"
    assert all(s.status == planner.DONE for s in out.steps)
    assert len(prompts) == 2
    assert "write a report" in prompts[0]                 # step grounded in the goal
    assert planner.load(p.slug).status == "done"          # persisted


async def test_run_plan_pauses_over_budget_leaving_step_pending(plans_dir, monkeypatch):
    async def fake_run(prompt, **kw):
        return "x", []

    monkeypatch.setattr("friday.core.agent.run", fake_run)
    monkeypatch.setattr(planner, "_budget_ok", lambda: False)
    p = planner.new_plan("g", ["a", "b"])
    out = await planner.run_plan(p)
    assert out.status == "paused"
    assert out.steps[0].status == planner.PENDING         # nothing ran — fully resumable
    assert planner.load(p.slug).status == "paused"


async def test_run_plan_retries_then_fails_step_and_continues(plans_dir, monkeypatch):
    # A persistently-failing step is retried up to the cap, then SKIPPED — the run keeps
    # going so one bad step can't freeze a long unattended plan.
    monkeypatch.setenv("PLAN_STEP_ATTEMPTS", "2")
    monkeypatch.setenv("PLAN_RETRY_BACKOFF", "0")
    tries = {"a": 0}

    async def fake_run(prompt, **kw):
        if "do step a" in prompt:          # step a always fails
            tries["a"] += 1
            raise RuntimeError("boom")
        return "ok", []                    # step b succeeds

    monkeypatch.setattr("friday.core.agent.run", fake_run)
    p = planner.new_plan("g", ["do step a", "do step b"])
    out = await planner.run_plan(p)
    assert tries["a"] == 2                            # retried up to the cap
    assert out.steps[0].status == planner.FAILED and out.steps[0].attempts == 2
    assert out.steps[1].status == planner.DONE        # the run CONTINUED past the failure
    assert out.status == "failed"                     # overall failed (a step couldn't finish)


async def test_run_plan_retries_and_recovers(plans_dir, monkeypatch):
    # A transient failure (e.g. rate limit) recovers on retry — the cooldown rotates models.
    monkeypatch.setenv("PLAN_STEP_ATTEMPTS", "3")
    monkeypatch.setenv("PLAN_RETRY_BACKOFF", "0")
    state = {"n": 0}

    async def fake_run(prompt, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("transient rate limit")
        return "recovered", []

    monkeypatch.setattr("friday.core.agent.run", fake_run)
    out = await planner.run_plan(planner.new_plan("g", ["a"]))
    assert out.status == "done" and out.steps[0].status == planner.DONE
    assert out.steps[0].attempts == 2                 # failed once, succeeded on the retry


async def test_action_step_that_only_narrates_is_retried_then_failed(plans_dir, monkeypatch):
    # The live phantom: the model SAYS it wrote the file but calls no tool, so nothing lands on
    # disk. The step must NOT be banked as DONE — it's retried, then FAILED (honest + resumable).
    monkeypatch.setenv("PLAN_STEP_ATTEMPTS", "3")
    monkeypatch.setenv("PLAN_RETRY_BACKOFF", "0")
    calls = {"n": 0}

    async def fake_run(prompt, **kw):
        calls["n"] += 1
        return "Sure — I wrote ~/out.md with the summary.", []   # narration, NO tool call

    monkeypatch.setattr("friday.core.agent.run", fake_run)
    out = await planner.run_plan(planner.new_plan("g", ["write ~/out.md with the summary"]))
    assert calls["n"] == 3                              # retried up to the cap, never banked
    assert out.steps[0].status == planner.FAILED       # honest failure, not a phantom DONE
    assert out.status == "failed"


async def test_action_step_recovers_once_it_actually_calls_a_tool(plans_dir, monkeypatch):
    # Narrates on the first try, actually calls a tool on the retry → DONE (the guard lets a
    # real action through; it only rejects the narrate-only no-op).
    monkeypatch.setenv("PLAN_STEP_ATTEMPTS", "3")
    monkeypatch.setenv("PLAN_RETRY_BACKOFF", "0")
    state = {"n": 0}

    async def fake_run(prompt, **kw):
        state["n"] += 1
        if state["n"] == 1:
            return "I will write the file.", []                       # narrated → retried
        return "done", [{"role": "tool", "name": "write_file",
                         "content": '{"path": "~/out.md"}'}]           # actually wrote it

    monkeypatch.setattr("friday.core.agent.run", fake_run)
    out = await planner.run_plan(planner.new_plan("g", ["write ~/out.md"]))
    assert out.steps[0].status == planner.DONE
    assert out.steps[0].attempts == 2


async def test_write_step_that_runs_a_different_tool_then_narrates_is_not_done(
    plans_dir, monkeypatch
):
    # The subtlest live phantom: a "write skills.md" step where the model runs web_search (which
    # SUCCEEDS) and then narrates the list in text — never calling write_file. A succeeding tool
    # is not enough; a WRITE step needs a write tool (or the file on disk), else it's retried.
    monkeypatch.setenv("PLAN_STEP_ATTEMPTS", "2")
    monkeypatch.setenv("PLAN_RETRY_BACKOFF", "0")
    calls = {"n": 0}

    async def fake_run(prompt, **kw):
        calls["n"] += 1
        return "Here are the skills: 1. Linux 2. Networking", [
            {"role": "assistant", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "name": "web_search", "content": '{"results": ["x"]}'},  # not a write
        ]

    monkeypatch.setattr("friday.core.agent.run", fake_run)
    out = await planner.run_plan(planner.new_plan("g", ["write skills to /nope/skills.md"]))
    assert calls["n"] == 2                              # a non-write success isn't banked as DONE
    assert out.steps[0].status == planner.FAILED


async def test_reasoning_step_needs_no_tool(plans_dir, monkeypatch):
    # A pure-reasoning step (no action verb) legitimately answers in text — no tool required, so
    # the phantom guard must NOT catch it.
    async def fake_run(prompt, **kw):
        return "the three themes are A, B, C", []                  # text only, no tool

    monkeypatch.setattr("friday.core.agent.run", fake_run)
    out = await planner.run_plan(planner.new_plan("g", ["summarize the three themes"]))
    assert out.steps[0].status == planner.DONE


def test_expects_tool_classifies_action_vs_reasoning():
    assert planner._expects_tool("write ~/out.md with the rooms")
    assert planner._expects_tool("search the web for beginner rooms")
    assert planner._expects_tool("run tracker.py to verify output")
    assert not planner._expects_tool("summarize the findings")
    assert not planner._expects_tool("decide which approach is best")


async def test_action_step_with_only_failed_tool_calls_is_not_done(plans_dir, monkeypatch):
    # Second-layer phantom the 7-step live run exposed: the model DOES call write_file but with
    # an invented `command` arg, so the call errors and tracker.py never lands — yet the step was
    # marked DONE just because a tool was *attempted*. Now a step needs a tool that SUCCEEDS.
    monkeypatch.setenv("PLAN_STEP_ATTEMPTS", "2")
    monkeypatch.setenv("PLAN_RETRY_BACKOFF", "0")
    calls = {"n": 0}

    async def fake_run(prompt, **kw):
        calls["n"] += 1
        return "I tried to write it.", [
            {"role": "assistant", "tool_calls": [{"id": "1"}]},
            {"role": "tool",
             "content": '{"error": "write_file() got an unexpected keyword argument command"}'},
        ]

    monkeypatch.setattr("friday.core.agent.run", fake_run)
    out = await planner.run_plan(planner.new_plan("g", ["write ~/thm_kit/tracker.py"]))
    assert calls["n"] == 2                          # retried — a failed call isn't banked as DONE
    assert out.steps[0].status == planner.FAILED


def test_succeeded_tools_collects_only_non_error_calls():
    hist = [
        {"role": "tool", "name": "web_search", "content": '{"results": []}'},
        {"role": "tool", "name": "write_file", "content": '{"error": "bad arg"}'},
        {"role": "tool", "name": "edit_file", "content": '{"declined": true}'},
    ]
    assert planner._succeeded_tools(hist) == {"web_search"}
    assert planner._succeeded_tools([]) == set()
    assert planner._succeeded_tools(None) == set()


def test_step_progressed_requires_a_write_tool_for_write_steps():
    wrote = [{"role": "tool", "name": "write_file", "content": '{"path": "x"}'}]
    searched = [{"role": "tool", "name": "web_search", "content": '{"results": ["x"]}'}]
    # a non-write step is satisfied by any successful tool
    assert planner._step_progressed("search the web for rooms", searched)
    # a write step needs an actual write tool, not just any successful tool
    assert planner._step_progressed("write ~/out.md", wrote)
    assert not planner._step_progressed("write /nope/out.md", searched)
    # nothing succeeded → never progressed
    assert not planner._step_progressed("write ~/out.md", [])


def test_tool_result_ok_tolerates_truncated_success():
    # a long success result capped mid-JSON must NOT be misread as a failure
    assert planner._tool_result_ok('{"results": [{"title": "Vulnvers')     # truncated, no fail key
    assert planner._tool_result_ok("ok")                                   # plain non-JSON string
    assert not planner._tool_result_ok('{"error": "boom and a very long')  # leading error key


async def test_resume_retries_a_failed_step(plans_dir, monkeypatch):
    monkeypatch.setenv("PLAN_STEP_ATTEMPTS", "1")
    monkeypatch.setenv("PLAN_RETRY_BACKOFF", "0")
    down = {"on": True}

    async def fake_run(prompt, **kw):
        if down["on"]:
            raise RuntimeError("service down")
        return "ok now", []

    monkeypatch.setattr("friday.core.agent.run", fake_run)
    p = planner.new_plan("g", ["a"])
    assert (await planner.run_plan(p)).steps[0].status == planner.FAILED  # failed first run

    down["on"] = False                                # conditions recover
    resumed = await planner.resume_plan(p.slug)       # resume re-attempts the FAILED step
    assert resumed.status == "done" and resumed.steps[0].status == planner.DONE


async def test_run_plan_persists_progress_then_resume_finishes(plans_dir, monkeypatch):
    async def fake_run(prompt, **kw):
        return "ok", []

    monkeypatch.setattr("friday.core.agent.run", fake_run)
    # budget allows exactly one step, then pauses → proves per-step persistence
    gate = {"n": 0}

    def budget():
        gate["n"] += 1
        return gate["n"] <= 1

    monkeypatch.setattr(planner, "_budget_ok", budget)
    p = planner.new_plan("g", ["a", "b", "c"])
    out = await planner.run_plan(p)
    assert out.status == "paused"
    assert out.steps[0].status == planner.DONE            # first step ran + saved
    assert out.steps[1].status == planner.PENDING

    # ... a "token reset" later: resume continues from the persisted state
    monkeypatch.setattr(planner, "_budget_ok", lambda: True)
    resumed = await planner.resume_plan(p.slug)
    assert resumed.status == "done"
    assert [s.status for s in resumed.steps] == [planner.DONE] * 3


async def test_resume_missing_plan_is_none(plans_dir):
    assert await planner.resume_plan("nope") is None


def test_budget_ok_free_tier_always_true(monkeypatch):
    monkeypatch.setattr("friday.core.router.allow_paid", lambda: False)
    assert planner._budget_ok() is True                   # free tiers never run out


def test_budget_ok_paid_respects_affordability(monkeypatch):
    monkeypatch.setattr("friday.core.router.allow_paid", lambda: True)
    monkeypatch.setattr("friday.core.router._budget.can_afford", lambda t: False)
    assert planner._budget_ok() is False


def test_step_prompt_includes_prior_step_results():
    # The coherence fix: a later step must SEE what earlier steps produced, not just their
    # descriptions — otherwise it can't build on them (write the rooms the search found).
    p = planner.new_plan("write a THM report", ["search the web", "write the file"])
    p.steps[0].status = planner.DONE
    p.steps[0].result = "found rooms: Vulnversity, Blue, RootMe"
    prompt = planner._step_prompt(p, p.steps[1])
    assert "write a THM report" in prompt                     # the overall goal
    assert "found rooms: Vulnversity, Blue, RootMe" in prompt  # step 1's RESULT is shared
    assert "write the file" in prompt                         # the step to do now


def test_step_prompt_first_step_has_no_progress_block():
    p = planner.new_plan("do a thing", ["first step", "second"])
    prompt = planner._step_prompt(p, p.steps[0])
    assert "Progress so far" not in prompt                    # nothing done yet
    assert "do a thing" in prompt and "first step" in prompt
