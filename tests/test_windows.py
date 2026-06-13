"""Tests for window-management skills (xdotool).

No real xdotool is invoked: the module's `_run` helper and `shutil.which` are
mocked. We lock that window scanning parses ids+titles, title matching is a
case-insensitive substring done in Python, focus/close activate the right id,
non-matches return errors, missing-xdotool returns a clean error, and the
confirmation gating (close requires confirm) is registered correctly.
"""

from friday.skills import registry
from friday.skills import windows as w


def _fake_runner(calls: list[list[str]], responses: dict[str, tuple[int, str, str]]):
    """Build an async _run that records calls and replies based on the subcommand."""

    async def _run(cmd, stdin=None):
        calls.append(cmd)
        # key on the xdotool subcommand + (for getwindowname) the target id
        if cmd[:3] == ["xdotool", "search", "--onlyvisible"]:
            return responses.get("search", (0, "", ""))
        if cmd[1] == "getwindowname":
            return responses.get(f"name:{cmd[2]}", (0, "", ""))
        return responses.get(cmd[1], (0, "", ""))

    return _run


def _present(monkeypatch):
    monkeypatch.setattr(w.shutil, "which", lambda _: "/usr/bin/xdotool")


def test_registrations_and_gating():
    registry.autodiscover()
    assert registry.get("list_windows").auto_confirm is True
    assert registry.get("focus_window").auto_confirm is True
    assert registry.get("close_window").auto_confirm is False


async def test_list_windows_parses_ids_and_names(monkeypatch):
    calls: list[list[str]] = []
    responses = {
        "search": (0, "111\n222\n333\n", ""),
        "name:111": (0, "Firefox\n", ""),
        "name:222": (0, "   \n", ""),  # blank title -> skipped
        "name:333": (0, "Terminal\n", ""),
    }
    monkeypatch.setattr(w, "_run", _fake_runner(calls, responses))
    _present(monkeypatch)
    out = await w.list_windows()
    assert out == {
        "windows": [
            {"id": "111", "title": "Firefox"},
            {"id": "333", "title": "Terminal"},
        ]
    }


async def test_focus_window_matches_substring_and_activates(monkeypatch):
    calls: list[list[str]] = []
    responses = {
        "search": (0, "111\n222\n", ""),
        "name:111": (0, "Mozilla Firefox\n", ""),
        "name:222": (0, "GNU Emacs\n", ""),
    }
    monkeypatch.setattr(w, "_run", _fake_runner(calls, responses))
    _present(monkeypatch)
    out = await w.focus_window("firefox")  # case-insensitive
    assert out == {"status": "focused", "title": "Mozilla Firefox"}
    assert ["xdotool", "windowactivate", "111"] in calls


async def test_focus_window_no_match_returns_error(monkeypatch):
    calls: list[list[str]] = []
    responses = {
        "search": (0, "111\n", ""),
        "name:111": (0, "Terminal\n", ""),
    }
    monkeypatch.setattr(w, "_run", _fake_runner(calls, responses))
    _present(monkeypatch)
    out = await w.focus_window("spotify")
    assert "no window matching" in out["error"]
    assert all(c[1] != "windowactivate" for c in calls)


async def test_close_window_closes_matched_id(monkeypatch):
    calls: list[list[str]] = []
    responses = {
        "search": (0, "900\n", ""),
        "name:900": (0, "Document - LibreOffice\n", ""),
    }
    monkeypatch.setattr(w, "_run", _fake_runner(calls, responses))
    _present(monkeypatch)
    out = await w.close_window("libreoffice")
    assert out == {"status": "closed", "title": "Document - LibreOffice"}
    assert ["xdotool", "windowclose", "900"] in calls


async def test_missing_xdotool_returns_error(monkeypatch):
    monkeypatch.setattr(w.shutil, "which", lambda _: None)
    assert "not installed" in (await w.list_windows())["error"]
    assert "not installed" in (await w.focus_window("x"))["error"]
    assert "not installed" in (await w.close_window("x"))["error"]
