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
    assert "falkordb" in check_names  # added: memory-layer backend
    assert "ollama" in check_names    # added: critical local-LLM backend


def test_parse_redis_url():
    from friday.core.monitor import _parse_redis_url
    assert _parse_redis_url("redis://localhost:6380") == ("localhost", 6380)
    assert _parse_redis_url("redis://10.0.0.5:6379/0") == ("10.0.0.5", 6379)
    assert _parse_redis_url("garbage") == ("localhost", 6379)  # safe fallback


@pytest.mark.asyncio
async def test_missing_anthropic_key_is_warn_not_fail(monkeypatch):
    """Free-by-default: a missing Claude key must not trigger a FAIL (alert spam)."""
    from friday.core.monitor import Status, _check_api_key
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = await _check_api_key()
    assert result.status == Status.WARN


@pytest.mark.asyncio
async def test_watchdog_survives_a_crashing_check(monkeypatch):
    """A single check raising must NOT crash run_health_checks — it should
    surface as a FAIL result so the watchdog keeps running."""
    from friday.core import monitor

    async def _boom() -> monitor.CheckResult:
        raise RuntimeError("nvidia-smi exploded")

    # make the GPU check raise unexpectedly (no internal handling for this)
    monkeypatch.setattr(monitor, "_check_gpu", _boom)
    report = await monitor.run_health_checks()

    # report still produced; the crashing check is a FAIL, others unaffected
    gpu = next(c for c in report.checks if c.name == "gpu")
    assert gpu.status == monitor.Status.FAIL
    assert "exploded" in gpu.detail
    assert len(report.checks) == 11


@pytest.mark.asyncio
async def test_ollama_down_is_fail(monkeypatch):
    """Ollama is the critical local-LLM dep — down must be a FAIL, not a quiet OK."""
    from friday.core.monitor import Status, _check_ollama
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:1")  # nothing listening
    result = await _check_ollama()
    assert result.status == Status.FAIL


@pytest.mark.asyncio
async def test_free_keys_present_is_ok(monkeypatch):
    from friday.core.monitor import Status, _check_free_keys
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert (await _check_free_keys()).status == Status.OK


@pytest.mark.asyncio
async def test_mcp_check_skip_when_unconfigured(monkeypatch):
    from friday.core.monitor import Status, _check_mcp
    from friday.mcp import bridge as mcp
    monkeypatch.setattr(mcp, "load_config", lambda: {})
    assert (await _check_mcp()).status == Status.SKIP


@pytest.mark.asyncio
async def test_mcp_check_ok_when_configured(monkeypatch):
    from friday.core.monitor import Status, _check_mcp
    from friday.mcp import bridge as mcp
    monkeypatch.setattr(mcp, "load_config", lambda: {"git": {"command": "x"}})
    monkeypatch.setattr(mcp, "_bridge", None)
    r = await _check_mcp()
    assert r.status == Status.OK and "git" in r.detail


@pytest.mark.asyncio
async def test_free_keys_absent_is_warn(monkeypatch):
    from friday.core.monitor import Status, _check_free_keys
    for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert (await _check_free_keys()).status == Status.WARN
