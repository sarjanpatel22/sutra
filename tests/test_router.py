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


@pytest.fixture(autouse=True)
def _hermetic_tiers(monkeypatch):
    """Make the suite hermetic to the developer's real .env. router.py calls load_dotenv()
    at import, so a local NVIDIA_NIM_API_KEY inserts nvidia_nim/* models into the tiers and
    reorders the fallback chain these order-asserting tests rely on — green in CI (no key),
    red on the dev box. Strip any env-injected NIM models so every test sees the base config."""
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    monkeypatch.setattr(
        router,
        "TIER_MODELS",
        {tier: [m for m in models if not m.startswith("nvidia_nim/")]
         for tier, models in router.TIER_MODELS.items()},
    )


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


def test_record_accounts_for_cache_read_discount():
    # A mostly-cached Claude turn bills the cached reads at ~10%, so it costs far less
    # than the same token count at full price (the budget must match the real console).
    b = _tracker()
    cached = b.record("claude-sonnet-4-6", 10_000, 100, cache_read=9_000)
    full = _tracker().record("claude-sonnet-4-6", 10_000, 100)
    # full: 10k in @0.003 + 100 out @0.015 = 0.0315
    assert full == pytest.approx(0.0315)
    # cached: 1k normal @0.003 + 9k @10%*0.003 + 100 out @0.015 = (3 + 2.7 + 1.5)/1000
    assert cached == pytest.approx(0.0072)
    assert cached < full


def test_record_cache_write_billed_at_premium():
    b = _tracker()
    # cache WRITE bills at 125%: 2k normal @0.003 + 8k @1.25*0.003 = (6 + 30)/1000
    cost = b.record("claude-sonnet-4-6", 10_000, 0, cache_write=8_000)
    assert cost == pytest.approx(0.036)


def test_record_without_cache_args_is_unchanged():
    # Back-compat: the old (model, in, out) call path bills exactly as before.
    b = _tracker()
    assert b.record("claude-sonnet-4-6", 1000, 100) == pytest.approx(0.0045)


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


# --- _wire_nvidia_nim: free NIM brains, per-tier ordering ------------------

def test_wire_nvidia_nim_orders_brains_per_tier():
    """GLM leads the reasoning tiers (REASON/HEAVY); Nemotron leads the tool/agent tier
    (MEDIUM) for its ~1s tool calls; the local/paid model at index 0 always stays first.
    Locks the insert() index math so a future free-brain edit can't silently reorder the
    fallback chain (the bug this would catch is a $-relevant misroute)."""
    glm, nem = "nvidia_nim/test-glm", "nvidia_nim/test-nemotron"
    tiers = {
        Tier.REASON: ["ollama_chat/local", "gemini/flash"],
        Tier.HEAVY: ["claude-sonnet-4-6", "groq/llama"],
        Tier.MEDIUM: ["groq/a", "groq/b", "groq/c", "groq/d"],
    }
    router._wire_nvidia_nim(tiers, glm, nem)

    for t in (Tier.REASON, Tier.HEAVY):
        assert tiers[t][0] not in (glm, nem), "local/paid model must stay at index 0"
        assert tiers[t][1] == glm and tiers[t][2] == nem, "GLM ahead of Nemotron in reasoning tiers"

    med = tiers[Tier.MEDIUM]
    assert med.index(nem) < med.index(glm), "Nemotron ahead of GLM in the tool/agent tier"
    assert med[0] == "groq/a", "existing primary stays first"


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
    """In CLOUD-FIRST mode, on a tool turn with every CLOUD model down, the router must:
      * pass the tools array to the cloud models it tries,
      * fall back to qwen (LOCAL_TOOLS) and keep its tools,
      * reach qwen BEFORE any gemma model.
    qwen succeeds here, so gemma is never reached (correct: the action fired).
    """
    monkeypatch.setenv("ROUTER_LOCAL_TOOLS_FIRST", "0")  # this test covers cloud-first fallback
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


@pytest.mark.asyncio
async def test_local_tools_first_default_tries_qwen_before_cloud(monkeypatch):
    """DEFAULT (local-first): an ACTION/tool turn tries the local qwen FIRST and, when it
    succeeds, never touches a rate-limited cloud model — actions stay $0 and quota-proof."""
    monkeypatch.delenv("ROUTER_LOCAL_TOOLS_FIRST", raising=False)  # unset → default ON
    calls: list[dict] = []
    fake = _recording_acompletion(calls, fail_substrings=())  # everything succeeds
    monkeypatch.setattr(router.litellm, "acompletion", fake)

    resp = await router.complete(
        [{"role": "user", "content": "open youtube"}], tier=Tier.MEDIUM, tools=[_FAKE_TOOL]
    )
    assert resp.choices[0].message.content == "ok"
    # qwen2.5 (local) was the FIRST and ONLY call — no cloud model was reached.
    assert calls[0]["model"].startswith("ollama_chat/qwen2.5")
    assert not any(
        c["model"].startswith(("groq/", "gemini/")) or "claude" in c["model"] for c in calls
    ), "local-first action turn must not reach a rate-limited cloud model when local works"
    assert calls[0]["tools"] == [_FAKE_TOOL]  # local model still gets the tools


@pytest.mark.asyncio
async def test_local_tools_first_falls_back_to_cloud_when_local_down(monkeypatch):
    """Local-first is safe: if the local model is down (Ollama off), the action turn still
    completes by falling back to the cloud tier."""
    monkeypatch.delenv("ROUTER_LOCAL_TOOLS_FIRST", raising=False)  # default ON
    calls: list[dict] = []
    fake = _recording_acompletion(calls, fail_substrings=("ollama",))  # local down, cloud up
    monkeypatch.setattr(router.litellm, "acompletion", fake)

    resp = await router.complete(
        [{"role": "user", "content": "open youtube"}], tier=Tier.MEDIUM, tools=[_FAKE_TOOL]
    )
    assert resp.choices[0].message.content == "ok"
    assert calls[0]["model"].startswith("ollama_chat/qwen2.5"), "local is tried first"
    assert any(c["model"].startswith("groq/") for c in calls), "then falls back to cloud"


@pytest.mark.asyncio
async def test_local_tools_first_does_not_affect_chat_turns(monkeypatch):
    """Local-first is for ACTIONS only: a no-tools chat turn is unaffected — it still uses
    the cloud tier first (kept for chat quality)."""
    monkeypatch.delenv("ROUTER_LOCAL_TOOLS_FIRST", raising=False)  # default ON
    calls: list[dict] = []
    fake = _recording_acompletion(calls, fail_substrings=())
    monkeypatch.setattr(router.litellm, "acompletion", fake)

    await router.complete([{"role": "user", "content": "how are you?"}], tier=Tier.MEDIUM)
    assert calls[0]["model"].startswith("groq/"), "chat turns stay cloud-first (no tools)"


@pytest.mark.asyncio
async def test_local_tools_first_disabled_is_cloud_first(monkeypatch):
    """ROUTER_LOCAL_TOOLS_FIRST=0 restores the old cloud-first behavior for action turns."""
    monkeypatch.setenv("ROUTER_LOCAL_TOOLS_FIRST", "0")
    calls: list[dict] = []
    fake = _recording_acompletion(calls, fail_substrings=())
    monkeypatch.setattr(router.litellm, "acompletion", fake)

    await router.complete(
        [{"role": "user", "content": "open youtube"}], tier=Tier.MEDIUM, tools=[_FAKE_TOOL]
    )
    assert calls[0]["model"].startswith("groq/"), "cloud-first when the flag is off"


# --- prompt caching: cache Claude's static system+tools prefix (cost fix) ----------

def test_is_anthropic_detects_claude_only():
    assert router._is_anthropic("claude-sonnet-4-6")
    assert router._is_anthropic("claude-opus-4-8")
    assert router._is_anthropic("anthropic/claude-3-5-sonnet")
    for free in ("groq/llama-3.3-70b-versatile", "gemini/gemini-2.5-flash",
                 "ollama_chat/qwen2.5:7b"):
        assert not router._is_anthropic(free)


def test_apply_cache_control_marks_system_and_last_tool():
    messages = [
        {"role": "system", "content": "you are sutra"},
        {"role": "user", "content": "hi"},
    ]
    tools = [
        {"type": "function", "function": {"name": "a"}},
        {"type": "function", "function": {"name": "b"}},
    ]
    new_msgs, new_tools = router._apply_cache_control(messages, tools)
    # system content became a content-block list carrying cache_control
    block = new_msgs[0]["content"]
    assert isinstance(block, list) and block[0]["text"] == "you are sutra"
    assert block[0]["cache_control"] == {"type": "ephemeral"}
    assert new_msgs[1] == {"role": "user", "content": "hi"}  # other messages untouched
    # only the LAST tool is marked (Anthropic caches the whole block up to it)
    assert "cache_control" not in new_tools[0]
    assert new_tools[1]["cache_control"] == {"type": "ephemeral"}
    # the shared originals are NOT mutated (reused across the provider fallback loop)
    assert messages[0]["content"] == "you are sutra"
    assert "cache_control" not in tools[1]


def test_apply_cache_control_handles_no_tools_and_no_system():
    msgs, tools = router._apply_cache_control([{"role": "user", "content": "x"}], None)
    assert tools is None
    assert msgs == [{"role": "user", "content": "x"}]  # no system message → unchanged


def _capturing_acompletion(seen):
    async def fake(*, model, messages, tools=None, **kw):  # noqa: ANN001
        seen.append({"model": model, "messages": messages, "tools": tools})
        return _fake_response()
    return fake


@pytest.mark.asyncio
async def test_complete_applies_cache_control_for_claude(monkeypatch):
    monkeypatch.setattr(router, "allow_paid", lambda: True)
    monkeypatch.setattr(router._budget, "can_afford", lambda t: True)
    seen: list[dict] = []
    monkeypatch.setattr(router.litellm, "acompletion", _capturing_acompletion(seen))
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    await router.complete(msgs, tier=Tier.HEAVY, tools=[_FAKE_TOOL])
    c = seen[0]
    assert router._is_anthropic(c["model"])  # Claude is tried first at HEAVY
    assert c["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert c["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert msgs[0]["content"] == "sys"  # caller's shared messages untouched


@pytest.mark.asyncio
async def test_complete_no_cache_control_for_free_tier(monkeypatch):
    monkeypatch.setenv("ROUTER_LOCAL_TOOLS_FIRST", "0")  # cloud-first → a groq model is first
    seen: list[dict] = []
    monkeypatch.setattr(router.litellm, "acompletion", _capturing_acompletion(seen))
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    await router.complete(msgs, tier=Tier.MEDIUM, tools=[_FAKE_TOOL])
    c = seen[0]
    assert not router._is_anthropic(c["model"])           # a free model
    assert c["messages"][0]["content"] == "sys"            # plain string, untouched
    assert "cache_control" not in c["tools"][-1]           # no caching for non-Claude


@pytest.mark.asyncio
async def test_prompt_cache_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ROUTER_PROMPT_CACHE", "0")
    monkeypatch.setattr(router, "allow_paid", lambda: True)
    monkeypatch.setattr(router._budget, "can_afford", lambda t: True)
    seen: list[dict] = []
    monkeypatch.setattr(router.litellm, "acompletion", _capturing_acompletion(seen))
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    await router.complete(msgs, tier=Tier.HEAVY, tools=[_FAKE_TOOL])
    # caching off → even Claude gets the plain string system, no cache_control
    assert seen[0]["messages"][0]["content"] == "sys"


# --- rate-limit cooldown: route around an exhausted model --------------------------

@pytest.fixture
def _clear_rl():
    router._RATE_LIMITED.clear()
    yield
    router._RATE_LIMITED.clear()


def test_is_rate_limit_error_detection():
    assert router._is_rate_limit_error(Exception("Error code: 429 - rate limit"))
    assert router._is_rate_limit_error(Exception("rate limit exceeded"))
    assert router._is_rate_limit_error(Exception("quota exceeded for today"))

    class RateLimitError(Exception):
        pass

    assert router._is_rate_limit_error(RateLimitError("x"))          # detected by class name
    assert not router._is_rate_limit_error(Exception("connection refused"))  # not a rate limit


def test_cooling_down_and_expiry(_clear_rl):
    router._mark_cooling_down("groq/x")
    assert router._is_cooling_down("groq/x")
    router._RATE_LIMITED["groq/x"] = 0.0                             # pretend the cooldown elapsed
    assert not router._is_cooling_down("groq/x")
    assert "groq/x" not in router._RATE_LIMITED                      # auto-cleaned


@pytest.mark.asyncio
async def test_rate_limited_model_skipped_on_next_call(monkeypatch, _clear_rl):
    monkeypatch.setenv("ROUTER_LOCAL_TOOLS_FIRST", "0")             # cloud-first → groq qwen3 first
    calls: list[str] = []

    async def fake(*, model, messages, tools=None, **kw):
        calls.append(model)
        if model.endswith("qwen3-32b"):
            raise Exception("litellm.RateLimitError: 429 too many requests")
        return _fake_response()

    monkeypatch.setattr(router.litellm, "acompletion", fake)
    msgs = [{"role": "user", "content": "hi"}]

    # Call 1: qwen3-32b 429s → marked cooling down → falls through to a working model.
    await router.complete(msgs, tier=Tier.MEDIUM, tools=[_FAKE_TOOL])
    assert calls[0].endswith("qwen3-32b")
    assert router._is_cooling_down("groq/qwen/qwen3-32b")

    # Call 2: the exhausted model is SKIPPED — no wasted 429 round-trip — yet a model still runs.
    calls.clear()
    await router.complete(msgs, tier=Tier.MEDIUM, tools=[_FAKE_TOOL])
    assert not any(m.endswith("qwen3-32b") for m in calls), "cooled-down model must be skipped"
    assert calls, "but it still routed to a working model"


@pytest.mark.asyncio
async def test_free_only_never_escalates_to_paid(monkeypatch):
    """A MEDIUM tool turn escalates to paid Claude when allow_paid + budget allow; a
    free_only call (cheap sub-agent workers) must NEVER reach a paid model — the $0
    guarantee under council fan-out, where parallel workers hammer the free tier."""
    monkeypatch.setattr(router, "allow_paid", lambda: True)
    monkeypatch.setattr(router._budget, "can_afford", lambda t: True)
    calls: list[dict] = []
    fake = _recording_acompletion(calls, fail_substrings=("groq/", "gemini/", "claude", "ollama"))
    monkeypatch.setattr(router.litellm, "acompletion", fake)
    msgs = [{"role": "user", "content": "hi"}]

    # Default MEDIUM tool turn DOES reach Claude (the escalation this guards).
    calls.clear()
    with pytest.raises(RuntimeError):
        await router.complete(msgs, tier=Tier.MEDIUM, tools=[_FAKE_TOOL])
    assert any("claude" in c["model"] for c in calls), "default should escalate to Claude"

    # free_only stays entirely on free models — no paid tier anywhere in the chain.
    calls.clear()
    with pytest.raises(RuntimeError):
        await router.complete(msgs, tier=Tier.MEDIUM, tools=[_FAKE_TOOL], free_only=True)
    assert calls and not any("claude" in c["model"] for c in calls), "free_only must stay $0"


@pytest.mark.asyncio
async def test_heavy_tier_stays_free_when_paid_disabled(monkeypatch):
    """$0 chokepoint: with paid disabled, even an EXPLICIT HEAVY tier (e.g. `ask --tier
    heavy`, or run_autonomous's HEAVY default) must never call a paid Claude model — the
    router drops it to the free MEDIUM tier itself, not trusting the caller to pre-pick."""
    monkeypatch.setattr(router, "allow_paid", lambda: False)
    monkeypatch.setattr(router._budget, "can_afford", lambda t: True)  # budget is fine
    calls: list[dict] = []
    fake = _recording_acompletion(calls, fail_substrings=())  # first model succeeds
    monkeypatch.setattr(router.litellm, "acompletion", fake)

    await router.complete([{"role": "user", "content": "hi"}], tier=Tier.HEAVY)

    assert calls, "expected a model call"
    assert not any("claude" in c["model"] for c in calls), \
        f"billed a paid model at the $0 default: {[c['model'] for c in calls]}"
    assert calls[0]["model"].startswith(("groq/", "gemini/")), "should use a free MEDIUM model"


@pytest.mark.asyncio
async def test_heavy_tier_uses_claude_when_paid_enabled(monkeypatch):
    """The opt-in path: with paid explicitly enabled AND budget available, HEAVY uses the
    paid model first — proving the gate isn't just always-off."""
    monkeypatch.setattr(router, "allow_paid", lambda: True)
    monkeypatch.setattr(router._budget, "can_afford", lambda t: True)
    calls: list[dict] = []
    fake = _recording_acompletion(calls, fail_substrings=())
    monkeypatch.setattr(router.litellm, "acompletion", fake)

    await router.complete([{"role": "user", "content": "hi"}], tier=Tier.HEAVY)
    assert "claude" in calls[0]["model"], \
        f"expected Claude first when paid is on, got {calls[0]['model']}"
