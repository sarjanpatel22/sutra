"""Tests for the auto router — one brain decides act-vs-chat + effort, free by default."""

import pytest

import friday.core.autoroute as ar
from friday.core.autoroute import chat_tier, decide, pick_tier, route
from friday.core.router import Tier

_FREE = {Tier.MEDIUM, Tier.CODE, Tier.LOCAL, Tier.REASON}


@pytest.mark.parametrize("text", [
    "hi", "thanks!", "what do you think of that", "tell me a joke", "good morning",
])
def test_chitchat_does_not_act_and_stays_free(text):
    d = decide(text)
    assert d.act is False and d.effort == "low" and d.tier in _FREE


@pytest.mark.parametrize("text", [
    "what time is it?",
    "how much RAM do I have",
    "what's the weather",
    "do you remember my sister's name",
])
def test_light_tool_turns_act_at_medium(text):
    d = decide(text)
    assert d.act is True and d.effort == "medium" and d.tier is Tier.MEDIUM


@pytest.mark.parametrize("text", [
    "okay, can you open the try hack me?",   # natural speech + STT spacing
    "show me the tryhackme window",
    "open tryhackme",
    "can you launch hack the box",
    "pull up my email",
    "search for the latest news",
])
def test_action_intent_routes_agentic(text):
    # The real-world failure: spoken action requests must reach the tool agent.
    d = decide(text)
    assert d.act is True


@pytest.mark.parametrize("text", [
    "what do you see on my screen",
    "see the screen",
    "look at my screen",
    "read the screen",
    "describe my screen",
    "take a screenshot",
    "what am i looking at",
])
def test_screen_vision_intent_routes_agentic(text):
    # Live bug: "can't see the screen" — these were classified as chit-chat so the
    # screen-capture tool was never reached. Must act with a tool now.
    d = decide(text)
    assert d.act is True and d.tool_required is True


@pytest.mark.parametrize("text", [
    "I see what you mean",
    "look, can you explain recursion",
    "I see",
    "that looks great",
])
def test_screen_lookalikes_stay_chat(text):
    # 'see'/'look' are chat words — must NOT trip the screen-vision action path.
    assert decide(text).act is False


@pytest.mark.parametrize("text", [
    "read my notes file",
    "create a new file called notes",
    "delete that file",
    "edit the config file",
    "save this to a file",
    "make a new folder",
    "append to the log file",
    "rename that file",
    "read notes.txt",
    "write to output.json",
])
def test_file_operation_intent_routes_agentic(text):
    # File commands need read_file/write_file/edit_file but carry no open/go verb, so
    # they used to fall to chit-chat. Must act with a tool now.
    d = decide(text)
    assert d.act is True and d.tool_required is True


@pytest.mark.parametrize("text", [
    "write a poem",
    "make dinner",
    "delete my account",
    "save the world",
    "I'm reading a document about history",
    "I read that article",
    "create a beautiful melody",
    "remove your shoes",
])
def test_file_operation_lookalikes_stay_chat(text):
    # A file verb alone (no concrete file/folder target) must NOT trip the file-op path.
    assert decide(text).act is False


@pytest.mark.parametrize("text", [
    "go to tryhackme.com and start the first room",
    "open https://news.ycombinator.com and tell me the top story",
    "log in to my account and book the appointment",
    "analyze this error and figure out the fix",
    "compare these two approaches and recommend one",
    "first open the site, and then fill out the form",
])
def test_high_turns_act_and_stay_free_by_default(text, monkeypatch):
    monkeypatch.delenv("ROUTER_ALLOW_PAID", raising=False)
    d = decide(text)
    assert d.act is True and d.effort == "high" and d.tier in _FREE


def test_tryhackme_phrase_now_goes_agentic():
    # The exact phrase that silently fell to local chat before — must act now.
    d = decide("go to tryhackme.com and start a room")
    assert d.act is True and d.effort == "high"


@pytest.mark.parametrize("text", [
    # The live-test phrases that fell to chit-chat — finishing/continuing a task.
    "let's finish this room",
    "now tell me what should we do to finish this openjail room",
    "why are you not finishing this room",
    "let's solve it",
    "keep going",
    "enumerate the box and get the flag",
])
def test_finish_continue_task_acts_with_tool(text):
    d = decide(text)
    assert d.act is True and d.effort == "high" and d.tool_required is True


def test_finish_task_does_not_break_chitchat():
    # The new task pattern must not swallow ordinary talk.
    for chat in ("tell me a joke", "what do you think of that", "good morning"):
        assert decide(chat).act is False
    # word-boundary: "solve"/"complete" stems shouldn't fire inside other words
    assert decide("give me a short explanation of planets").effort != "high"


def test_coding_routes_to_free_code_tier(monkeypatch):
    monkeypatch.delenv("ROUTER_ALLOW_PAID", raising=False)
    d = decide("refactor this function and write a unit test")
    assert d.act is True and d.tier is Tier.CODE and d.effort == "high"


def test_long_request_is_high_and_free(monkeypatch):
    monkeypatch.delenv("ROUTER_ALLOW_PAID", raising=False)
    d = decide("please " + "detail " * 45)
    assert d.act is True and d.effort == "high" and d.tier in _FREE


def test_paid_flag_enables_claude_for_high(monkeypatch):
    monkeypatch.setenv("ROUTER_ALLOW_PAID", "1")
    assert pick_tier("analyze https://example.com step by step") is Tier.HEAVY
    assert pick_tier("hello there") is Tier.MEDIUM  # chit-chat free even when paid allowed


def test_route_returns_reason(monkeypatch):
    monkeypatch.delenv("ROUTER_ALLOW_PAID", raising=False)
    tier, reason = route("open https://example.com")
    assert tier is Tier.MEDIUM and "free" in reason and "web" in reason
    tier, reason = route("hello there")
    assert tier is Tier.MEDIUM and reason


def test_word_boundary_avoids_false_high():
    # "plan" inside "explanation" / "planet" must NOT trip the high router.
    d = decide("give me a short explanation of planets")
    assert d.effort != "high"


def test_paid_over_budget_falls_back_to_free(monkeypatch):
    monkeypatch.setenv("ROUTER_ALLOW_PAID", "1")

    class B:
        daily_pct = 100.0
        monthly_pct = 40.0
    monkeypatch.setattr(ar, "get_budget", lambda: B())
    assert pick_tier("analyze https://example.com step by step") in _FREE


def test_budget_error_does_not_break_routing(monkeypatch):
    monkeypatch.setenv("ROUTER_ALLOW_PAID", "1")

    def boom():
        raise RuntimeError("no budget file")
    monkeypatch.setattr(ar, "get_budget", boom)
    assert pick_tier("analyze this deeply") is Tier.HEAVY  # falls through cleanly


def test_tool_required_only_for_concrete_actions():
    assert decide("open the tryhackme website").tool_required is True
    assert decide("go to https://example.com").tool_required is True
    assert decide("search for the news").tool_required is True
    # analysis/coding/complex are high-effort but don't force a tool
    assert decide("analyze the tradeoffs between threads and async").tool_required is False
    assert decide("tell me a joke").tool_required is False


# ── auto-council routing (Stage 3) — opt-in, conservative ────────────────────
_MULTI = "research the top vector databases, design a schema, and draft a migration plan"


def test_council_off_by_default(monkeypatch):
    monkeypatch.delenv("SUTRA_COUNCIL_AUTO", raising=False)
    assert decide(_MULTI).council is False  # available but never auto-fires by default


def test_council_flags_multifaceted_when_enabled(monkeypatch):
    monkeypatch.setenv("SUTRA_COUNCIL_AUTO", "1")
    d = decide(_MULTI)
    assert d.council is True and d.act is True and d.effort == "high"


@pytest.mark.parametrize("text", ["hi", "what time is it?", "open tryhackme", "tell me a joke"])
def test_council_not_triggered_for_simple_turns(monkeypatch, text):
    monkeypatch.setenv("SUTRA_COUNCIL_AUTO", "1")
    assert decide(text).council is False


def test_council_needs_two_distinct_verbs(monkeypatch):
    monkeypatch.setenv("SUTRA_COUNCIL_AUTO", "1")
    # one work-verb, however verbose, is not council-worthy
    assert decide(
        "please research the entire history of the roman empire in great detail"
    ).council is False


@pytest.mark.parametrize("text", [
    "hi", "hey sutra", "hello", "good morning", "good night",
    "how are you", "what's up", "thanks", "thank you", "thanks so much",
    "ok", "okay", "cool", "nice", "lol", "haha", "bye", "yes", "no", "got it",
])
def test_chat_tier_trivial_stays_local(text):
    # Greetings / acknowledgements / social filler → instant, free, dim local model.
    assert chat_tier(text) is Tier.LOCAL


@pytest.mark.parametrize("text", [
    "explain how transformers work",
    "should I take the job offer in Toronto?",
    "what's the difference between TCP and UDP",
    "why is my python script leaking memory",
    "give me your opinion on rust versus go for a new backend",
    "how does HTTPS actually keep a connection secure",
    "I've been thinking about quitting my job. It pays well but I'm bored.",
])
def test_chat_tier_substantive_uses_medium(text):
    # Real questions / explanations / advice / multi-sentence → the free 70B.
    assert chat_tier(text) is Tier.MEDIUM


def test_chat_tier_empty_is_local():
    assert chat_tier("") is Tier.LOCAL
    assert chat_tier("   ") is Tier.LOCAL


def test_chat_tier_short_question_still_medium():
    # Few words but clearly a question → don't demote it to the 4B model.
    assert chat_tier("why though?") is Tier.MEDIUM
    assert chat_tier("how come?") is Tier.MEDIUM
