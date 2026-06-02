"""Monitor and budget tracker tests."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from friday.core.router import BudgetTracker, _fire_alerts, register_alert


def test_budget_persists_daily_spend(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_BUDGET_USD", "2.0")
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "30.0")
    import friday.core.router as router_mod
    monkeypatch.setattr(router_mod, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(router_mod, "_BUDGET_FILE", tmp_path / "budget.json")

    b = BudgetTracker(daily_limit=2.0, monthly_limit=30.0)
    # Simulate a Sonnet call: 1000 input + 200 output tokens
    cost = b.record("claude-sonnet-4-6", 1000, 200)
    assert cost == pytest.approx(0.003 + 0.003, rel=1e-3)
    assert b.daily_tokens == 1200
    assert b.monthly_tokens == 1200


def test_budget_alert_fires_at_threshold(tmp_path, monkeypatch):
    import friday.core.router as router_mod
    monkeypatch.setattr(router_mod, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(router_mod, "_BUDGET_FILE", tmp_path / "budget.json")
    monkeypatch.setattr(router_mod, "ALERT_THRESHOLD", 0.85)

    fired: list[tuple[str, float]] = []

    def _cb(scope: str, pct: float) -> None:
        fired.append((scope, pct))

    b = BudgetTracker(daily_limit=1.0, monthly_limit=100.0)
    # Wire up the global callback list temporarily
    original_cbs = list(router_mod._alert_callbacks)
    router_mod._alert_callbacks.clear()
    router_mod._alert_callbacks.append(_cb)

    try:
        # Spend 90 cents on a daily $1 limit — should trigger daily alert
        b._daily_spent = 0.90
        b._monthly_spent = 0.90
        b._check_thresholds()
        assert len(fired) == 1
        assert fired[0][0] == "daily"
        assert fired[0][1] >= 0.85

        # Second call should NOT re-fire (already flagged)
        b._check_thresholds()
        assert len(fired) == 1
    finally:
        router_mod._alert_callbacks.clear()
        router_mod._alert_callbacks.extend(original_cbs)


def test_budget_daily_reset(tmp_path, monkeypatch):
    import friday.core.router as router_mod
    monkeypatch.setattr(router_mod, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(router_mod, "_BUDGET_FILE", tmp_path / "budget.json")

    b = BudgetTracker(daily_limit=2.0, monthly_limit=30.0)
    b._daily_spent = 1.5
    b._day_start = time.time() - 90000  # 25 hours ago — past 24h window
    b._reset_day_if_needed()
    assert b._daily_spent == 0.0
    assert b._daily_alert_fired is False


@pytest.mark.asyncio
async def test_run_health_checks_returns_report():
    from friday.core.monitor import HealthReport, run_health_checks
    report = await run_health_checks()
    assert isinstance(report, HealthReport)
    check_names = {c.name for c in report.checks}
    assert "budget" in check_names
    assert "disk" in check_names
    assert "anthropic_key" in check_names
