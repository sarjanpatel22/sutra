"""Tests for the deterministic command router — all skills are faked, nothing launches."""

from __future__ import annotations

from typing import Any

import pytest

from friday.core import commands


class _Recorder:
    """Async fake skill that records its calls and returns a canned dict."""

    def __init__(self, name: str, ret: dict[str, Any] | None = None) -> None:
        self.__name__ = name
        self.ret = ret if ret is not None else {"status": "ok"}
        self.calls: list[tuple[Any, ...]] = []

    async def __call__(self, *args: Any) -> dict[str, Any]:
        self.calls.append(args)
        return self.ret


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Recorder]:
    recs = {
        "open_app": _Recorder("open_app", {"status": "launched", "app": "x"}),
        "open_url": _Recorder("open_url", {"status": "ok"}),
        "focus_window": _Recorder("focus_window", {"status": "focused"}),
        "close_window": _Recorder("close_window", {"status": "closed"}),
        "list_windows": _Recorder(
            "list_windows", {"windows": [{"id": "1", "title": "a"}, {"id": "2", "title": "b"}]}
        ),
    }
    for name, rec in recs.items():
        monkeypatch.setattr(commands, name, rec)
    return recs


async def test_open_app_basic(fakes: dict[str, _Recorder]) -> None:
    out = await commands.try_command("open firefox")
    assert out is not None
    assert out["skill"] == "open_app"
    assert out["spoken"] == "Opening firefox."
    assert fakes["open_app"].calls == [("firefox",)]


async def test_open_app_filler_stripped(fakes: dict[str, _Recorder]) -> None:
    out = await commands.try_command("can you open firefox for me?")
    assert out is not None
    assert out["skill"] == "open_app"
    assert out["spoken"] == "Opening firefox."
    assert fakes["open_app"].calls == [("firefox",)]


async def test_open_app_hey_sutra_stacked_filler(fakes: dict[str, _Recorder]) -> None:
    out = await commands.try_command("Hey Sutra, could you please open the terminal now?")
    assert out is not None
    assert out["skill"] == "open_app"
    assert fakes["open_app"].calls == [("the terminal",)]


async def test_open_url_for_domain(fakes: dict[str, _Recorder]) -> None:
    out = await commands.try_command("open google.com")
    assert out is not None
    assert out["skill"] == "open_url"
    assert out["spoken"] == "Opening google.com."
    assert fakes["open_url"].calls == [("https://google.com",)]
    assert fakes["open_app"].calls == []


async def test_known_site_name_opens_browser_not_app(fakes: dict[str, _Recorder]) -> None:
    # "open youtube" is a WEBSITE, not a desktop app — must hit open_url, never
    # gtk-launch a non-existent 'youtube' app (the live failure that opened nothing).
    out = await commands.try_command("open youtube")
    assert out is not None
    assert out["skill"] == "open_url"
    assert fakes["open_url"].calls == [("https://youtube.com",)]
    assert fakes["open_app"].calls == []


async def test_open_url_keeps_existing_scheme(fakes: dict[str, _Recorder]) -> None:
    out = await commands.try_command("open https://example.org/path")
    assert out is not None
    assert out["skill"] == "open_url"
    assert fakes["open_url"].calls == [("https://example.org/path",)]


async def test_open_app_for_dotted_non_tld(fakes: dict[str, _Recorder]) -> None:
    # "vlc" has no dot -> app; this checks a word that merely *contains* an app name.
    out = await commands.try_command("launch vlc")
    assert out is not None
    assert out["skill"] == "open_app"
    assert fakes["open_app"].calls == [("vlc",)]


async def test_open_app_error_spoken(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder("open_app", {"error": "couldn't find an app called 'wibble'"})
    monkeypatch.setattr(commands, "open_app", rec)
    out = await commands.try_command("open wibble")
    assert out is not None
    assert out["skill"] == "open_app"
    assert out["spoken"] == "I couldn't find wibble on your system."


async def test_close_window(fakes: dict[str, _Recorder]) -> None:
    out = await commands.try_command("close firefox")
    assert out is not None
    assert out["skill"] == "close_window"
    assert out["spoken"] == "Closing firefox."
    assert fakes["close_window"].calls == [("firefox",)]


async def test_close_strips_trailing_window(fakes: dict[str, _Recorder]) -> None:
    out = await commands.try_command("close the firefox window")
    assert out is not None
    assert fakes["close_window"].calls == [("the firefox",)]


async def test_close_generic_returns_none(fakes: dict[str, _Recorder]) -> None:
    assert await commands.try_command("close the window") is None
    assert await commands.try_command("close the tab") is None
    assert fakes["close_window"].calls == []


async def test_close_respects_confirm_decline(fakes: dict[str, _Recorder]) -> None:
    # close_window is GATED — a confirm hook that declines must stop the close.
    async def deny(_name: str, _args: dict[str, Any]) -> bool:
        return False

    out = await commands.try_command("close firefox", confirm=deny)
    assert out is not None and out["skill"] == "close_window"
    assert out["result"] == {"status": "declined"}
    assert fakes["close_window"].calls == []  # never closed


async def test_close_runs_when_confirm_approves(fakes: dict[str, _Recorder]) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def allow(name: str, args: dict[str, Any]) -> bool:
        seen.append((name, args))
        return True

    out = await commands.try_command("close firefox", confirm=allow)
    assert out is not None and out["spoken"] == "Closing firefox."
    assert fakes["close_window"].calls == [("firefox",)]
    assert seen == [("close_window", {"title": "firefox"})]  # gate was consulted


async def test_open_is_not_gated_by_confirm(fakes: dict[str, _Recorder]) -> None:
    # open_app is auto-confirm; a declining hook must NOT block it (only close is gated).
    async def deny(_name: str, _args: dict[str, Any]) -> bool:
        return False

    out = await commands.try_command("open firefox", confirm=deny)
    assert out is not None and fakes["open_app"].calls == [("firefox",)]


async def test_focus_window(fakes: dict[str, _Recorder]) -> None:
    out = await commands.try_command("switch to my editor")
    assert out is not None
    assert out["skill"] == "focus_window"
    assert out["spoken"] == "Switching to my editor."
    assert fakes["focus_window"].calls == [("my editor",)]


async def test_focus_variants(fakes: dict[str, _Recorder]) -> None:
    for phrase in ("focus on firefox", "bring up the editor", "go to slack"):
        await commands.try_command(phrase)
    assert len(fakes["focus_window"].calls) == 3


async def test_focus_url_becomes_open_url(fakes: dict[str, _Recorder]) -> None:
    out = await commands.try_command("go to github.com")
    assert out is not None
    assert out["skill"] == "open_url"
    assert fakes["open_url"].calls == [("https://github.com",)]


async def test_list_windows(fakes: dict[str, _Recorder]) -> None:
    out = await commands.try_command("what windows are open")
    assert out is not None
    assert out["skill"] == "list_windows"
    assert out["spoken"] == "You have 2 windows open."
    assert out["result"]["windows"] == [{"id": "1", "title": "a"}, {"id": "2", "title": "b"}]


async def test_list_windows_variants(fakes: dict[str, _Recorder]) -> None:
    for phrase in ("list windows", "list my open windows", "what's open"):
        out = await commands.try_command(phrase)
        assert out is not None
        assert out["skill"] == "list_windows"


async def test_compound_open_falls_through_to_llm(fakes: dict[str, _Recorder]) -> None:
    # The real misfire: a whole sentence got shoved into gtk-launch as an "app".
    out = await commands.try_command(
        "open the dry at me and start any room and let's finish it"
    )
    assert out is None  # must reach the LLM (web_task_vision), not open_app
    assert fakes["open_app"].calls == []


async def test_compound_open_site_then_action_falls_through(fakes: dict[str, _Recorder]) -> None:
    for phrase in (
        "open tryhackme and start a room",
        "open google.com and search for nmap",
        "launch firefox then go to my dashboard",
    ):
        assert await commands.try_command(phrase) is None
    assert fakes["open_app"].calls == []
    assert fakes["open_url"].calls == []


async def test_compound_focus_and_close_fall_through(fakes: dict[str, _Recorder]) -> None:
    assert await commands.try_command("go to my room and submit the flag") is None
    assert await commands.try_command("close firefox and open chrome") is None
    assert fakes["focus_window"].calls == []
    assert fakes["close_window"].calls == []


async def test_open_web_target_is_not_an_app(fakes: dict[str, _Recorder]) -> None:
    # Live bug: "open any hard room" (3 words) slipped the sentence-guard and got
    # gtk-launch'd as a fake app. Web/CTF targets must go to the LLM, never open_app.
    for phrase in ("open any hard room", "open the website", "open my box", "open a lab"):
        assert await commands.try_command(phrase) is None
    assert fakes["open_app"].calls == []


async def test_short_multiword_app_still_launches(fakes: dict[str, _Recorder]) -> None:
    # Guard must not over-fire: a genuine 3-word app name still launches directly.
    out = await commands.try_command("open visual studio code")
    assert out is not None
    assert out["skill"] == "open_app"
    assert fakes["open_app"].calls == [("visual studio code",)]


async def test_list_windows_singular(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder("list_windows", {"windows": [{"id": "1", "title": "a"}]})
    monkeypatch.setattr(commands, "list_windows", rec)
    out = await commands.try_command("list windows")
    assert out is not None
    assert out["spoken"] == "You have 1 window open."


@pytest.mark.parametrize(
    "phrase",
    [
        "open up to me",
        "open to new ideas",
        "open about your feelings",
        "what do you think",
        "tell me a joke",
        "",
        "   ",
        "open",
        "close",
        "switch to",
    ],
)
async def test_conversational_returns_none(
    fakes: dict[str, _Recorder], phrase: str
) -> None:
    assert await commands.try_command(phrase) is None
    assert fakes["open_app"].calls == []
    assert fakes["open_url"].calls == []
    assert fakes["focus_window"].calls == []
    assert fakes["close_window"].calls == []


async def test_skill_exception_becomes_clean_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_args: Any) -> dict[str, Any]:
        raise RuntimeError("xdotool exploded")

    boom.__name__ = "open_app"  # type: ignore[attr-defined]
    monkeypatch.setattr(commands, "open_app", boom)
    out = await commands.try_command("open firefox")
    assert out is not None
    # An exception surfaces as an error dict -> the "couldn't find" spoken phrase.
    assert "error" in out["result"]
    assert out["spoken"] == "I couldn't find firefox on your system."
