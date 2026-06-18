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
