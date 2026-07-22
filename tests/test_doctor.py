"""Tests for `sutra doctor` preflight — deps/binaries/skills checks, no live services."""

from __future__ import annotations

import pytest

from friday.core import doctor
from friday.core.monitor import Status


def test_importable_true_for_stdlib_false_for_nonsense() -> None:
    assert doctor._importable("json") is True
    assert doctor._importable("totally_not_a_real_module_xyz") is False


def test_python_rows_core_ok_in_this_env() -> None:
    # The test environment has the base deps installed, so core must be OK and no
    # `pip install -e .` fix should be suggested for core.
    rows, fixes = doctor.python_rows()
    core = next(r for r in rows if r.name == "core")
    assert core.status is Status.OK
    assert "pip install -e ." not in fixes


def test_python_rows_flags_missing_group(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pretend the whole voice group is missing → it should warn and suggest the extra.
    voice_mods = set(doctor._DEP_GROUPS["voice"])
    real = doctor._importable
    monkeypatch.setattr(
        doctor, "_importable", lambda m: False if m in voice_mods else real(m)
    )
    rows, fixes = doctor.python_rows()
    voice = next(r for r in rows if r.name == "voice")
    assert voice.status is Status.WARN
    assert 'pip install -e ".[voice]"' in fixes


def test_binary_rows_present_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only xdotool present; everything else missing → apt fix lists the missing pkgs,
    # and never includes the non-apt "(nvidia driver)" entry.
    monkeypatch.setattr(doctor.shutil, "which", lambda b: "/usr/bin/x" if b == "xdotool" else None)
    rows, fixes = doctor.binary_rows()
    by_name = {r.name: r for r in rows}
    assert by_name["xdotool"].status is Status.OK
    assert by_name["tesseract"].status is Status.WARN
    assert len(fixes) == 1 and fixes[0].startswith("sudo apt install ")
    assert "tesseract-ocr" in fixes[0]
    assert "nvidia" not in fixes[0]  # GPU driver isn't apt-installable, must be excluded


def test_skill_rows_reports_loaded_count() -> None:
    rows = doctor.skill_rows()
    loaded = next(r for r in rows if r.name == "skills loaded")
    assert loaded.status is Status.OK
    assert "registered" in loaded.detail


async def test_run_doctor_returns_true_when_core_ok() -> None:
    # services=False avoids any network; core deps are present in the test env.
    ok = await doctor.run_doctor(services=False)
    assert ok is True


def test_engagement_rows_autonomous_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("friday.core.router.allow_paid", lambda: False)
    monkeypatch.setattr("friday.core.secrets.get_secret", lambda *a, **k: None)

    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    by_name = {r.name: r for r in doctor.engagement_rows()}
    assert by_name["SUTRA_AUTONOMOUS"].status is Status.OK

    monkeypatch.delenv("SUTRA_AUTONOMOUS", raising=False)
    by_name = {r.name: r for r in doctor.engagement_rows()}
    assert by_name["SUTRA_AUTONOMOUS"].status is Status.WARN


def test_engagement_rows_flags_paid_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The silent-fallback gotcha: ROUTER_ALLOW_PAID on but no key → the orchestrator is NOT Sonnet.
    monkeypatch.setattr("friday.core.router.allow_paid", lambda: True)
    monkeypatch.setattr("friday.core.secrets.get_secret", lambda *a, **k: "sk-ant-xxx")
    assert {r.name: r for r in doctor.engagement_rows()}["paid orchestrator"].status is Status.OK

    monkeypatch.setattr("friday.core.secrets.get_secret", lambda *a, **k: None)
    row = {r.name: r for r in doctor.engagement_rows()}["paid orchestrator"]
    assert row.status is Status.WARN and "ANTHROPIC_API_KEY" in row.detail


def test_nim_model_rows_flags_eol(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retired NIM id (404/410) is surfaced as an EOL warning; a live one reads OK — so a dead
    model can't silently rot in the reasoning tier again."""
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "test-key")
    monkeypatch.setattr(
        doctor, "_nim_alive",
        lambda model, key: (Status.WARN, f"{model} → HTTP 410 EOL — update the id")
        if "glm" in model.lower() else (Status.OK, f"{model} live"))
    by_name = {r.name: r for r in doctor.nim_model_rows()}
    assert by_name["NIM GLM"].status is Status.WARN and "EOL" in by_name["NIM GLM"].detail
    assert by_name["NIM Nemotron"].status is Status.OK


def test_nim_model_rows_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    rows = doctor.nim_model_rows()
    assert len(rows) == 1 and rows[0].status is Status.WARN and "unset" in rows[0].detail


def test_engagement_rows_recon_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("friday.core.router.allow_paid", lambda: False)
    monkeypatch.setattr("friday.core.secrets.get_secret", lambda *a, **k: None)
    monkeypatch.setattr(doctor.shutil, "which", lambda b: "/usr/bin/x" if b == "nmap" else None)
    by_name = {r.name: r for r in doctor.engagement_rows()}
    assert by_name["nmap"].status is Status.OK
    assert by_name["john"].status is Status.WARN  # missing recon tool → flagged, not fatal
