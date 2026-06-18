"""Tests for the open_app desktop-launch skill — everything is mocked, nothing launches."""

from __future__ import annotations

from typing import Any

import pytest

from friday.skills import apps


class _FakePopen:
    """Records the args/kwargs Popen was called with, without spawning anything."""

    calls: list[tuple[Any, dict[str, Any]]] = []

    def __init__(self, args: Any, **kwargs: Any) -> None:
        _FakePopen.calls.append((args, kwargs))


@pytest.fixture(autouse=True)
def _patch_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakePopen.calls = []
    monkeypatch.setattr(apps.subprocess, "Popen", _FakePopen)


def _which_only(*available: str):
    """Return a fake shutil.which that resolves only the given commands."""
    paths = {cmd: f"/usr/bin/{cmd}" for cmd in available}
    return lambda cmd: paths.get(cmd)


@pytest.mark.asyncio
async def test_firefox_alias_resolves_and_launches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps.shutil, "which", _which_only("firefox"))
    result = await apps.open_app("Firefox")
    # browsers get --new-window so a window appears even if an instance is running
    assert result == {"status": "launched", "app": "Firefox", "command": "firefox --new-window"}
    assert len(_FakePopen.calls) == 1
    args, kwargs = _FakePopen.calls[0]
    assert args == ["/usr/bin/firefox", "--new-window"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] == apps.subprocess.DEVNULL
    assert kwargs["stderr"] == apps.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_unknown_app_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps.shutil, "which", _which_only())  # nothing on PATH
    result = await apps.open_app("definitely-not-real-app")
    assert "error" in result
    assert "definitely-not-real-app" in result["error"]
    assert _FakePopen.calls == []


@pytest.mark.asyncio
async def test_literal_name_when_alias_misses(monkeypatch: pytest.MonkeyPatch) -> None:
    # No alias for "inkscape" — falls back to the literal name, which `which` finds.
    monkeypatch.setattr(apps.shutil, "which", _which_only("inkscape"))
    result = await apps.open_app("inkscape")
    assert result == {"status": "launched", "app": "inkscape", "command": "inkscape"}
    args, _ = _FakePopen.calls[0]
    assert args == ["/usr/bin/inkscape"]


@pytest.mark.asyncio
async def test_gtk_launch_fallback_when_desktop_entry_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only gtk-launch exists, and a real .desktop entry resolves for the name.
    monkeypatch.setattr(apps.shutil, "which", _which_only("gtk-launch"))
    monkeypatch.setattr(apps, "_desktop_id_for", lambda name: "obscureapp")
    result = await apps.open_app("obscureapp")
    assert result["status"] == "launched"
    assert result["command"] == "gtk-launch obscureapp"
    args, kwargs = _FakePopen.calls[0]
    assert args == ["gtk-launch", "obscureapp"]
    assert kwargs["start_new_session"] is True


@pytest.mark.asyncio
async def test_gtk_launch_no_false_success_when_no_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gtk-launch exists but NO .desktop entry matches — must report an honest error,
    # never a phantom 'launched' (the live 'Opening notepad' that opened nothing).
    monkeypatch.setattr(apps.shutil, "which", _which_only("gtk-launch"))
    monkeypatch.setattr(apps, "_desktop_id_for", lambda name: None)
    result = await apps.open_app("notepad")
    assert "error" in result
    assert "status" not in result
    assert _FakePopen.calls == []


def test_open_app_registered_auto_confirm() -> None:
    from friday.skills import registry

    registry.autodiscover()
    meta = registry.get("open_app")
    assert meta.name == "open_app"
    assert meta.auto_confirm is True
