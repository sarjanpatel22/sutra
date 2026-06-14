"""BudgetTracker unit tests — cost accounting, limits, resets, alerts, persistence."""

import time
import types

import pytest

from friday.core import router
from friday.core.router import BudgetTracker, Tier


@pytest.fixture(autouse=True)
def tmp_budget(tmp_path, monkeypatch):
    """Redirect budget state to a temp dir and reset alert callbacks per test."""
    state_dir = tmp_path / "friday"
    monkeypatch.setattr(router, "_STATE_DIR", state_dir)
    monkeypatch.setattr(router, "_BUDGET_FILE", state_dir / "budget.json")
    router._alert_callbacks.clear()
    yield
    router._alert_callbacks.clear()


def _tracker(daily=2.0, monthly=30.0):
    return BudgetTracker(daily_limit=daily, monthly_limit=monthly)


# --- cost accounting -------------------------------------------------------

@pytest.mark.parametrize(
    "model",
    [
        "ollama/gemma3:4b",
        "ollama/some-other-local-model",  # prefix guard, not in cost table
        "groq/llama-3.3-70b-versatile",
        "gemini/gemini-2.5-flash",
    ],
)
def test_free_models_cost_zero(model):
    """Local + free-tier providers must never accrue spend (bug #1 regression)."""
    b = _tracker()
    cost = b.record(model, 100_000, 100_000)
    assert cost == 0.0
    assert b.daily_spent == 0.0
    assert b.monthly_spent == 0.0
    assert b.daily_tokens == 200_000


def test_known_paid_model_cost():
    b = _tracker()
    # sonnet = (0.003, 0.015) per 1k → 1k in + 1k out = 0.003 + 0.015
    cost = b.record("claude-sonnet-4-6", 1000, 1000)
    assert cost == pytest.approx(0.018)
    assert b.daily_spent == pytest.approx(0.018)
    assert b.monthly_spent == pytest.approx(0.018)


def test_unknown_paid_model_uses_default_rate():
    b = _tracker()
    # unknown, non-free model falls back to default (0.005, 0.015)
    cost = b.record("some/mystery-paid-model", 1000, 1000)
    assert cost == pytest.approx(0.020)


def test_model_name_typo_is_safe():
    """A model-name typo must never throw, and must never silently fake $0.

    Two failure modes we guard against:
      * free-provider typo (suffix wrong, prefix intact) -> still $0, never charged
      * paid-model typo ("claud-haiku" missing the 'e') -> NOT a free prefix, so it
        falls back to the conservative default rate instead of recording $0. The
        budget must over-, never under-count an unrecognised paid call.
    """
    b = _tracker()

    # free prefix intact, suffix garbled -> guard keeps it free
    assert b.record("groq/llama-TYPO-9000", 1000, 1000) == 0.0
    assert b.record("gemini/flash-TYPO", 1000, 1000) == 0.0

    # paid-model typo: no free prefix -> conservative default, not a fake $0
    cost = b.record("claud-haiku", 1000, 1000)  # missing the 'e'
    assert cost == pytest.approx(0.020)          # (0.005 + 0.015), didn't throw
    assert cost > 0.0                            # crucially NOT silently free


def test_cost_accumulates_across_calls():
    b = _tracker()
    b.record("claude-sonnet-4-6", 1000, 1000)
    b.record("claude-sonnet-4-6", 1000, 1000)
    assert b.daily_spent == pytest.approx(0.036)
    assert b.daily_tokens == 4000


# --- affordability ---------------------------------------------------------

def test_can_afford_within_budget():
    b = _tracker(daily=2.0, monthly=30.0)
    b.record("claude-sonnet-4-6", 1000, 1000)  # well under limits
    assert b.can_afford(Tier.HEAVY) is True


def test_cannot_afford_over_daily_limit():
    b = _tracker(daily=0.5, monthly=30.0)
    b.record("claude-sonnet-4-6", 0, 60_000)  # 0.9 spent > 0.5 daily
    assert b.can_afford(Tier.HEAVY) is False


def test_cannot_afford_over_monthly_limit():
    b = _tracker(daily=100.0, monthly=0.5)
    b.record("claude-sonnet-4-6", 0, 60_000)  # 0.9 spent > 0.5 monthly
    assert b.can_afford(Tier.HEAVY) is False


# --- period resets ---------------------------------------------------------

def test_daily_resets_but_monthly_persists():
    b = _tracker()
    b.record("claude-sonnet-4-6", 1000, 1000)  # 0.018 daily + monthly
    b._day_start = time.time() - 86_401  # roll past one day
    b.record("claude-sonnet-4-6", 1000, 1000)  # reset day, then add
    assert b.daily_spent == pytest.approx(0.018)   # only the new charge
    assert b.monthly_spent == pytest.approx(0.036)  # month kept accruing


def test_monthly_reset():
    b = _tracker()
    b.record("claude-sonnet-4-6", 1000, 1000)
    b._month_start = time.time() - (31 * 86_400)
    b.record("claude-sonnet-4-6", 1000, 1000)
    assert b.monthly_spent == pytest.approx(0.018)


def test_can_afford_triggers_monthly_reset():
    """Regression for bug #5: can_afford must roll the monthly window too."""
    b = _tracker(daily=100.0, monthly=0.5)
    b.record("claude-sonnet-4-6", 0, 60_000)  # 0.9 > 0.5 monthly
    assert b.can_afford(Tier.HEAVY) is False
    b._month_start = time.time() - (31 * 86_400)
    assert b.can_afford(Tier.HEAVY) is True
    assert b.monthly_spent == 0.0


# --- alerts ----------------------------------------------------------------

def test_alert_fires_once_at_threshold(monkeypatch):
    monkeypatch.setattr(router, "ALERT_THRESHOLD", 0.85)
    fired = []
    router.register_alert(lambda scope, pct: fired.append((scope, pct)))

    b = _tracker(daily=1.0, monthly=1000.0)  # huge monthly so only daily fires
    b.record("claude-sonnet-4-6", 0, 60_000)  # 0.9 / 1.0 = 90% > 85%
    b.record("claude-sonnet-4-6", 0, 1000)    # still over — must not re-fire

    assert len(fired) == 1
    assert fired[0][0] == "daily"
    assert fired[0][1] >= 0.85


def test_no_alert_below_threshold(monkeypatch):
    monkeypatch.setattr(router, "ALERT_THRESHOLD", 0.85)
    fired = []
    router.register_alert(lambda scope, pct: fired.append(scope))
    b = _tracker(daily=10.0, monthly=1000.0)
    b.record("claude-sonnet-4-6", 1000, 1000)  # tiny fraction of limit
    assert fired == []


# --- properties & persistence ---------------------------------------------

def test_pct_properties():
    b = _tracker(daily=2.0, monthly=30.0)
    b.record("claude-sonnet-4-6", 0, 100_000)  # 1.5 spent
    assert b.daily_pct == pytest.approx(75.0)
    assert b.monthly_pct == pytest.approx(5.0)


def test_zero_limit_pct_is_safe():
    b = _tracker(daily=0.0, monthly=0.0)
    assert b.daily_pct == 0.0
    assert b.monthly_pct == 0.0


def test_persistence_roundtrip():
    b1 = _tracker()
    b1.record("claude-sonnet-4-6", 1000, 1000)
    # A fresh tracker should load the persisted state from the same file.
    b2 = _tracker()
    assert b2.daily_spent == pytest.approx(0.018)
    assert b2.monthly_spent == pytest.approx(0.018)
    assert b2.daily_tokens == 2000


# ===========================================================================
# Local tool-capable fallback (qwen before gemma) — regression for the router
# fix: when every cloud LLM is rate-limited, a TOOL turn must fall back to a
# tool-CAPABLE local model (qwen2.5, Tier.LOCAL_TOOLS) BEFORE the chat-only
# local model (gemma3, Tier.LOCAL) — gemma can't accept a tools array at all,
# so reaching it on a tool turn would collapse the action into prose.
# ===========================================================================


# --- _tool_incapable: only local gemma can't take tools --------------------

@pytest.mark.parametrize(
    "model",
    [
        "ollama/gemma3:4b",
        "ollama_chat/gemma3:4b",
    ],
)
def test_tool_incapable_true_for_local_gemma(model):
    """gemma is the one local model Ollama hard-rejects tools on → strip them."""
    assert router._tool_incapable(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "ollama_chat/qwen2.5:7b",   # local but DOES emit tool_calls
        "ollama_chat/llama3.2:3b",  # local but DOES emit tool_calls
    ],
)
def test_tool_incapable_false_for_tool_capable_local(model):
    """Other local models keep their tools so the offline fallback can still act."""
    assert router._tool_incapable(model) is False


@pytest.mark.parametrize(
    "model",
    [
        "groq/llama-3.3-70b-versatile",
        "gemini/gemini-2.5-flash",
        "claude-sonnet-4-6",
    ],
)
def test_tool_incapable_false_for_cloud(model):
    """Cloud models all function-call — never strip their tools."""
    assert router._tool_incapable(model) is False


# --- _prefer_local_tools: inject qwen immediately before gemma -------------

def test_prefer_local_tools_inserts_before_local():
    """LOCAL_TOOLS (qwen) must land immediately BEFORE LOCAL (gemma)."""
    out = router._prefer_local_tools([Tier.MEDIUM, Tier.LOCAL])
    assert out == [Tier.MEDIUM, Tier.LOCAL_TOOLS, Tier.LOCAL]
    # qwen sits directly in front of gemma
    i = out.index(Tier.LOCAL_TOOLS)
    assert out[i + 1] is Tier.LOCAL


def test_prefer_local_tools_no_duplicate_when_already_present():
    """An existing LOCAL_TOOLS must not be added a second time."""
    out = router._prefer_local_tools([Tier.LOCAL_TOOLS, Tier.MEDIUM, Tier.LOCAL])
    assert out.count(Tier.LOCAL_TOOLS) == 1
    # ...and the one we keep is still ordered ahead of LOCAL
    assert out.index(Tier.LOCAL_TOOLS) < out.index(Tier.LOCAL)


def test_prefer_local_tools_unchanged_without_local():
    """No LOCAL tier → nothing to guard → list returned as-is."""
    tiers = [Tier.HEAVY, Tier.MEDIUM]
    out = router._prefer_local_tools(tiers)
    assert out == tiers


# --- complete() per-model tool handling + fallback order -------------------

_FAKE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Look up the weather.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


def _fake_response(content="ok"):
    """Minimal litellm-shaped response with usage=None so budget recording skips."""
    message = types.SimpleNamespace(content=content, tool_calls=None)
    choice = types.SimpleNamespace(message=message)
    return types.SimpleNamespace(choices=[choice], usage=None)


def _recording_acompletion(calls, *, fail_substrings):
    """Build a fake litellm.acompletion that records (model, tools) per call and
    raises for any model whose id contains one of `fail_substrings` (simulating a
    rate-limited cloud model / a downed local model)."""

    async def fake(*, model, messages, tools=None, **kwargs):  # noqa: ANN001
        calls.append({"model": model, "tools": tools})
        if any(sub in model for sub in fail_substrings):
            raise RuntimeError(f"simulated failure for {model}")
        return _fake_response()

    return fake


@pytest.mark.asyncio
async def test_complete_strips_tools_per_model_and_prefers_qwen(monkeypatch):
    """On a tool turn with every CLOUD model down, the router must:
      * pass the tools array to the cloud models it tries,
      * fall back to qwen (LOCAL_TOOLS) and keep its tools,
      * reach qwen BEFORE any gemma model.
    qwen succeeds here, so gemma is never reached (correct: the action fired).
    """
    calls: list[dict] = []
    # Fail every cloud model (groq/gemini/claude); ollama models succeed.
    fake = _recording_acompletion(calls, fail_substrings=("groq/", "gemini/", "claude"))
    monkeypatch.setattr(router.litellm, "acompletion", fake)

    resp = await router.complete(
        [{"role": "user", "content": "weather in Toronto?"}],
        tier=Tier.MEDIUM,
        tools=[_FAKE_TOOL],
    )
    assert resp.choices[0].message.content == "ok"

    models = [c["model"] for c in calls]
    # at least one cloud model was attempted and got the tools list
    cloud = [
        c for c in calls
        if c["model"].startswith(("groq/", "gemini/")) or "claude" in c["model"]
    ]
    assert cloud, "expected cloud models to be attempted first"
    assert all(c["tools"] == [_FAKE_TOOL] for c in cloud), "cloud calls must carry tools"

    # qwen was reached and KEPT its tools (it can function-call locally)
    qwen = next(c for c in calls if c["model"].startswith("ollama_chat/qwen2.5"))
    assert qwen["tools"] == [_FAKE_TOOL]

    # ORDER: qwen (LOCAL_TOOLS) attempted before ANY gemma model (LOCAL)
    qwen_idx = next(i for i, m in enumerate(models) if m.startswith("ollama_chat/qwen2.5"))
    gemma_idxs = [i for i, m in enumerate(models) if "gemma" in m]
    if gemma_idxs:
        assert qwen_idx < min(gemma_idxs), "qwen must be tried before gemma"
    # qwen succeeded → gemma should not even be reached
    assert qwen["model"] == models[-1], "qwen success should end the fallback chain"


@pytest.mark.asyncio
async def test_complete_strips_tools_for_gemma_when_qwen_also_down(monkeypatch):
    """If qwen ALSO fails, the chat-only gemma is the last resort — and its tools
    MUST be stripped to None (Ollama rejects a tools array for gemma)."""
    calls: list[dict] = []
    # Everything cloud fails AND qwen fails → forces gemma as the final rung.
    fake = _recording_acompletion(
        calls, fail_substrings=("groq/", "gemini/", "claude", "qwen2.5")
    )
    monkeypatch.setattr(router.litellm, "acompletion", fake)

    resp = await router.complete(
        [{"role": "user", "content": "weather in Toronto?"}],
        tier=Tier.MEDIUM,
        tools=[_FAKE_TOOL],
    )
    assert resp.choices[0].message.content == "ok"  # gemma carried the turn (as prose)

    # qwen was attempted (and given tools) before gemma
    qwen = next(c for c in calls if c["model"].startswith("ollama_chat/qwen2.5"))
    assert qwen["tools"] == [_FAKE_TOOL]

    # gemma was reached and its tools were STRIPPED to None
    gemma = next(c for c in calls if "gemma" in c["model"])
    assert gemma["tools"] is None, "gemma must never receive a tools array"

    models = [c["model"] for c in calls]
    qwen_idx = models.index(qwen["model"])
    gemma_idx = models.index(gemma["model"])
    assert qwen_idx < gemma_idx, "qwen must be tried before gemma"
