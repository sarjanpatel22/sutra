"""Synthesize a tool-calling SFT dataset for the qwen2.5-7b offline-brain fine-tune.

Labels are authored deterministically (I KNOW 'what's the weather' → weather), and
phrasings are expanded template×slot — so labels are clean and utterances are diverse.
Crucially, phrasings here are DIFFERENT from scripts/eval_toolcalling.py's cases, so the
held-out eval stays honest (no train/test leakage). Emits JSONL of {"user","tool"|null,
"args"} — the train script renders these through Qwen2.5's chat template + tools schema.

    .venv/bin/python scripts/build_sft_dataset.py            # -> /tmp/sft_toolcalling.jsonl
"""

from __future__ import annotations

import json
import random

random.seed(7)
OUT = "/tmp/sft_toolcalling.jsonl"

_CITIES = ["Toronto", "Paris", "Tokyo", "London", "Berlin", "Mumbai", "Sydney", "Cairo",
           "Sudbury", "Oslo", "Lima", "Seoul"]
_APPS = ["firefox", "vlc", "vs code", "chrome", "the file manager", "gimp", "blender",
         "spotify", "the calculator", "libreoffice", "thunderbird", "discord"]
_FILES = ["/etc/hostname", "README.md", "~/notes.txt", "config.yaml", "/var/log/syslog",
          "pyproject.toml", "~/.bashrc", "data.csv", "main.py", "/etc/os-release"]
_DIRS = ["~/Downloads", "/tmp", "the docs folder", "~/projects", "/etc", "my home folder"]
_EXPR = [("what's 17 times 34", "17*34"), ("seven cubed", "7**3"),
         ("256 divided by 8", "256/8"), ("what is 19% of 240", "0.19*240"),
         ("the square root of 1024", "1024**0.5"), ("1234 plus 5678", "1234+5678"),
         ("2 to the power 16", "2**16"), ("nine times nine", "9*9")]
_TOPICS = ["my job", "my projects", "where I live", "my preferences", "what I'm building",
           "my hometown", "the Sutra project", "my goals"]
_QUERIES = ["the latest SpaceX launch", "who won the F1 race", "the current bitcoin price",
            "best ramen in Toronto", "news on the election", "the new iPhone reviews"]
_URLS = ["github.com", "news.ycombinator.com", "wikipedia.org", "arxiv.org", "stackoverflow.com"]
_CMDS = ["df -h", "uptime", "git log --oneline -5", "free -m", "whoami", "ps aux"]

_examples: list[dict] = []


def add(user: str, tool: str | None, args: dict | None = None) -> None:
    _examples.append({"user": user, "tool": tool, "args": args or {}})


def expand(templates: list[str], slots: list, tool: str, argfn) -> None:
    """For each (template, slot) add a labelled example. argfn(slot) -> args dict."""
    for s in slots:
        for t in templates:
            add(t.format(x=s), tool, argfn(s))


expand(["got the time?", "remind me what time it is", "how late is it", "what hour is it",
        "is it past noon", "clock check"], [None], "current_time", lambda _s: {})
expand(["which day are we on", "what's the date this morning", "today's date please",
        "what's the day today"], [None], "current_time", lambda _s: {})
expand(["how's my memory and CPU", "system health readout please", "is my disk filling up",
        "check my hardware stats", "how busy is the processor", "what's my free disk",
        "show resource usage"], [None], "system_info", lambda _s: {})
expand(["how much charge is left", "battery status", "is the laptop charging",
        "am I on battery", "how's my power"], [None], "battery", lambda _s: {})
expand(["which apps are showing", "show my open windows", "what's running on screen",
        "list everything open"], [None], "list_windows", lambda _s: {})
expand(["paste what I copied", "show me the clipboard", "what did I just copy",
        "read my clipboard"], [None], "clipboard_get", lambda _s: {})

expand(["is it cold in {x}", "weather for {x}", "will it rain in {x}", "temp in {x} now",
        "how's the sky in {x}"], _CITIES, "weather", lambda s: {"location": s})
expand(["fire up {x}", "start {x} for me", "get {x} going", "launch {x}", "bring up {x}"],
       _APPS, "open_app", lambda s: {"name": s})
expand(["show what's in {x}", "pull up the contents of {x}", "open the file {x}",
        "let me see {x}"], _FILES, "read_file", lambda s: {"path": s})
expand(["what's inside {x}", "show the files in {x}", "what files are in {x}",
        "contents of {x}"], _DIRS, "list_directory", lambda s: {"path": s})
expand(["look up {x}", "search for {x}", "find me {x} online", "what's the latest on {x}"],
       _QUERIES, "web_search", lambda s: {"query": s})
expand(["what do you recall about {x}", "dig up what you know about {x}",
        "remind me about {x}", "what do you remember on {x}"], _TOPICS, "recall",
       lambda s: {"query": s})
expand(["pull up {x}", "take me to {x}", "open the site {x}", "go to {x}"], _URLS,
       "open_url", lambda s: {"url": f"https://{s}"})
expand(["run {x}", "execute {x} in the shell", "give me the output of {x}"], _CMDS,
       "run_command", lambda s: {"command": s})
for u, e in _EXPR:
    add(u, "calc", {"expression": e})
    add(u.replace("what's", "compute").replace("the ", ""), "calc", {"expression": e})
for c in _CITIES[:8]:
    add(f"copy {c} to my clipboard", "clipboard_set", {"text": c})
    add(f"put {c} on the clipboard", "clipboard_set", {"text": c})
for w in ["my editor", "the browser", "slack", "the terminal", "spotify"]:
    add(f"bring {w} to the front", "focus_window", {"title": w})
    add(f"switch over to {w}", "focus_window", {"title": w})
for w in ["the browser", "this window", "the editor", "slack"]:
    add(f"close {w} please", "close_window", {"title": w})
for k, v in [("units", "celsius"), ("default city", "Toronto"), ("editor", "vscode"),
             ("theme", "dark")]:
    add(f"remember my {k} is {v}", "set_preference", {"key": k, "value": v})
    add(f"set my {k} to {v}", "set_preference", {"key": k, "value": v})

# --- chat negatives: must fire NO tool (general knowledge / social / opinion) ---
_NEG = [
    ("hey, how's it going", "Doing well — what are we on?"),
    ("tell me something interesting", "Octopuses have three hearts."),
    ("what's your favorite color", "A deep gold, honestly."),
    ("explain how a rainbow forms", "Sunlight refracts inside raindrops, splitting into colours."),
    ("what's the capital of France", "Paris."),
    ("who wrote Hamlet", "Shakespeare."),
    ("thanks for the help", "Anytime."),
    ("you're awesome", "Appreciated — onward."),
    ("what do you think about AI", "Powerful tool; what matters is how you wield it."),
    ("tell me a joke", "I'd tell a UDP joke, but you might not get it."),
    ("good morning", "Morning. What's first?"),
    ("what is photosynthesis", "How plants turn light, water, and CO2 into energy."),
    ("define recursion", "A function calling itself on a smaller subproblem."),
    ("how are you feeling", "Sharp and ready."),
    ("what's 2 plus 2", "4."),
    ("what's the meaning of life", "Build things that matter; don't waste the days."),
    ("are you a robot", "An AI — Sutra."),
    ("what's the speed of light", "About 299,792 km/s."),
    ("who painted the Mona Lisa", "Leonardo da Vinci."),
    ("how many continents are there", "Seven."),
]
for u, a in _NEG:
    add(u, None, {"_answer": a})
for greet in ["yo", "hello there", "hiya", "sup", "evening", "hey sutra", "what's up"]:
    add(greet, None, {"_answer": "Hey — what do you need?"})


def main() -> None:
    random.shuffle(_examples)
    with open(OUT, "w") as f:
        for ex in _examples:
            f.write(json.dumps(ex) + "\n")
    tools = sum(1 for e in _examples if e["tool"])
    print(f"wrote {len(_examples)} examples to {OUT} ({tools} tool / {len(_examples)-tools} chat)")


if __name__ == "__main__":
    main()
