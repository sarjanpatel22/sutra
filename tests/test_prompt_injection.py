"""Tests for `friday.skills.prompt_injection` — network fully mocked, no real requests."""

from __future__ import annotations

import pytest

from friday.skills import prompt_injection as pi
from friday.skills.prompt_injection import _payloads, check_prompt_injection


def test_payloads_carry_reversed_token() -> None:
    pl = _payloads("xyz321")
    assert len(pl) >= 3
    assert all("xyz321" in p for p in pl)


async def test_confirms_when_forward_token_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    # A model that truly executed the injection returns the FORWARD token (reversed our payload).
    sent: list[tuple[str, str, str]] = []

    def fake_send(url: str, param: str, payload: str, method: str) -> str:
        sent.append((param, method, payload))
        # payload contains reversed_token; the "model" reverses it → forward token in reply.
        # Simulate that by reversing every alpha-num run >=12 chars found in the payload.
        import re
        out = payload
        for run in re.findall(r"[a-z0-9]{12}", payload):
            out += " " + run[::-1]
        return out.lower()

    monkeypatch.setattr(pi, "_send", fake_send)
    out = await check_prompt_injection("http://target/chat", param="message", method="json")
    assert out["vulnerable"] is True
    assert out["severity"] == "high"
    assert out["param"] == "message"
    assert out["method"] == "json"


async def test_plain_echo_does_not_false_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    # A dumb echo returns the payload verbatim — the FORWARD token is never present → no confirm.
    monkeypatch.setattr(pi, "_send", lambda url, param, payload, method: payload.lower())
    out = await check_prompt_injection("http://target/echo", param="q", method="json")
    assert out["vulnerable"] is False
    assert out["attempts"] >= 1


async def test_no_response_is_not_vulnerable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi, "_send", lambda *a, **k: "")
    out = await check_prompt_injection("http://target/", method="get")
    assert out["vulnerable"] is False


async def test_tries_all_default_params_until_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_params: set[str] = set()

    def fake_send(url: str, param: str, payload: str, method: str) -> str:
        seen_params.add(param)
        if param == "input":  # only this param reaches the model
            import re
            out = payload
            for run in re.findall(r"[a-z0-9]{12}", payload):
                out += " " + run[::-1]
            return out.lower()
        return "sorry, i didn't understand"

    monkeypatch.setattr(pi, "_send", fake_send)
    out = await check_prompt_injection("http://target/", method="json")
    assert out["vulnerable"] is True
    assert out["param"] == "input"
    assert {"message", "prompt"} & seen_params  # walked the default list before hitting input


async def test_empty_url_errors() -> None:
    assert "error" in await check_prompt_injection("")


def test_skill_is_confirmation_gated() -> None:
    from friday.skills import registry
    registry.autodiscover()
    assert registry.get("check_prompt_injection").auto_confirm is False
