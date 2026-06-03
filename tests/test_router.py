"""BudgetTracker unit tests — cost accounting, limits, resets, alerts, persistence."""

import time

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
        "gemini/gemini-2.5-flash-preview-04-17",
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
