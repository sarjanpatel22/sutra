"""`sutra doctor` — preflight: does Sutra have what it needs to run, and if not, how to fix it.

This is the component that knows Sutra's own requirements. It inspects four layers and,
for anything missing, prints the exact command to fix it:

  1. Python packages, per feature group — core (base) + the voice/memory/action/mcp/telegram
     extras. Probed by the import name the code actually uses.
  2. System binaries Sutra shells out to (xdotool, tesseract, imagemagick, openvpn, …).
  3. Skill-discovery health — how many @skill modules loaded, and which failed to import and
     why (from registry.failed_imports()).
  4. Live services (Ollama, SearXNG, Redis, FalkorDB, GPU, …) — reused from core.monitor.

Read-only; it only inspects. The point is that a missing piece becomes a clear, actionable
line ("install X") instead of a cryptic runtime failure later.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from dataclasses import dataclass

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from friday.core.monitor import (
    _STATUS_COLOR,
    _STATUS_ICON,
    Status,
    console,
    run_health_checks,
)

# Python imports the base install must provide (probe by the name the code imports).
_CORE_IMPORTS = [
    "litellm", "anthropic", "httpx", "pydantic", "dotenv",
    "keyring", "rich", "typer", "numpy", "psutil",
    "cv2", "mss",  # fast screen perception (template match + mss capture) — base deps
]
# Each optional extra -> the import names it provides.
_DEP_GROUPS: dict[str, list[str]] = {
    "voice": ["faster_whisper", "sounddevice", "kokoro_onnx", "onnxruntime",
              "webrtcvad", "openwakeword", "pynput"],
    "memory": ["graphiti_core", "falkordb", "redis"],
    "action": ["playwright"],
    "mcp": ["mcp"],
    "telegram": ["telegram"],
    "train": ["torch", "transformers", "trl", "peft", "bitsandbytes", "datasets"],
}

# binary -> (what it powers, apt package to install). "(...)" packages aren't apt-installable.
_BINARIES: dict[str, tuple[str, str]] = {
    "xdotool": ("desktop click/type", "xdotool"),
    "tesseract": ("screen OCR", "tesseract-ocr"),
    "import": ("screenshots", "imagemagick"),
    "gtk-launch": ("launch apps by name", "libgtk-3-bin"),
    "xdg-open": ("open URLs/files", "xdg-utils"),
    "xsel": ("clipboard", "xsel"),
    "spd-say": ("speech fallback", "speech-dispatcher"),
    "openvpn": ("pentest-lab VPN", "openvpn"),
    "notify-send": ("desktop notifications", "libnotify-bin"),
    "nvidia-smi": ("GPU detection", "(nvidia driver)"),
}


def _importable(mod: str) -> bool:
    """True if `mod` can be imported, without importing it (no heavy init / side effects)."""
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:  # noqa: BLE001 — a broken import machinery counts as unavailable
        return False


@dataclass
class _Row:
    name: str
    status: Status
    detail: str


def python_rows() -> tuple[list[_Row], list[str]]:
    """Dependency rows (core + each extra) plus the pip commands to fix what's missing."""
    rows: list[_Row] = []
    fixes: list[str] = []
    missing_core = [m for m in _CORE_IMPORTS if not _importable(m)]
    if missing_core:
        rows.append(_Row("core", Status.FAIL, "MISSING: " + ", ".join(missing_core)))
        fixes.append("pip install -e .")
    else:
        rows.append(_Row("core", Status.OK, "all base deps present"))
    for group, mods in _DEP_GROUPS.items():
        missing = [m for m in mods if not _importable(m)]
        if missing:
            rows.append(_Row(group, Status.WARN, "missing: " + ", ".join(missing)))
            fixes.append(f'pip install -e ".[{group}]"')
        else:
            rows.append(_Row(group, Status.OK, "available"))
    return rows, fixes


def binary_rows() -> tuple[list[_Row], list[str]]:
    """System-binary rows plus a single apt command for the apt-installable ones missing."""
    rows: list[_Row] = []
    apt: list[str] = []
    for binary, (feature, pkg) in _BINARIES.items():
        if shutil.which(binary):
            rows.append(_Row(binary, Status.OK, feature))
        else:
            rows.append(_Row(binary, Status.WARN, f"missing — {feature}"))
            if not pkg.startswith("("):
                apt.append(pkg)
    fixes = [f"sudo apt install {' '.join(sorted(set(apt)))}"] if apt else []
    return rows, fixes


def skill_rows() -> list[_Row]:
    """One row for the registered-skill count, plus a row per skill that failed to import."""
    from friday.skills import registry

    registry.autodiscover()
    n = len(registry.all_tools())
    rows = [_Row("skills loaded", Status.OK if n else Status.FAIL, f"{n} registered")]
    for mod, err in registry.failed_imports():
        rows.append(_Row(f"skill:{mod}", Status.WARN, err[:80]))
    return rows


# The worker's external tools for a real engagement. A missing one degrades gracefully (the skill
# returns a clear "not installed"), but the box gets harder — these live on the Kali host, not CI.
_ENGAGEMENT_BINARIES: dict[str, str] = {
    "nmap": "deep port/service scan",
    "gobuster": "web content brute (web_enum)",
    "ffuf": "web fuzz (web_enum fallback)",
    "john": "hash cracking (crack_hash)",
    "searchsploit": "Exploit-DB search",
    "dig": "DNS lookup",
    "whois": "WHOIS",
}


def engagement_rows() -> list[_Row]:
    """Readiness for an autonomous engagement (`sutra pwn`/`auto`, especially `--tiered`): the
    opt-in flag, whether the paid Sonnet orchestrator will ACTUALLY engage (the gotcha that
    otherwise degrades silently), and the recon tools the worker shells out to. All advisory
    (WARN, never FAIL) — a missing recon binary shouldn't fail the core health check."""
    from friday.core.router import allow_paid
    from friday.core.secrets import get_secret

    rows: list[_Row] = []
    auto = os.getenv("SUTRA_AUTONOMOUS", "").strip().lower() in ("1", "true", "yes")
    rows.append(_Row(
        "SUTRA_AUTONOMOUS", Status.OK if auto else Status.WARN,
        "hands-free runs enabled" if auto else "unset — export SUTRA_AUTONOMOUS=1 for hands-free",
    ))

    paid = allow_paid()
    key = bool(get_secret("ANTHROPIC_API_KEY"))
    if paid and key:
        rows.append(_Row("paid orchestrator", Status.OK,
                         "ROUTER_ALLOW_PAID + Anthropic key present → Sonnet orchestrator"))
    elif paid and not key:
        rows.append(_Row("paid orchestrator", Status.WARN,
                         "ROUTER_ALLOW_PAID set but NO Anthropic key — the orchestrator silently "
                         "falls back to free 70B. Fix: sutra secrets --set ANTHROPIC_API_KEY"))
    else:
        rows.append(_Row("paid orchestrator", Status.WARN,
                         "free-only — set ROUTER_ALLOW_PAID=1 (+ key) for the Sonnet orchestrator"))

    for binary, feature in _ENGAGEMENT_BINARIES.items():
        ok = shutil.which(binary) is not None
        rows.append(_Row(binary, Status.OK if ok else Status.WARN,
                         feature if ok else f"missing — {feature} degrades"))
    return rows


_NIM_CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"


def _nim_alive(model: str, key: str) -> tuple[Status, str]:
    """Ping one NIM model id with a 1-token request. OK if it answers; WARN (flagged EOL) on the
    404/410 NIM returns for a retired id. Isolated so it's monkeypatchable in tests (no network)."""
    import json
    import urllib.error
    import urllib.request

    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}],
                       "max_tokens": 1}).encode()
    req = urllib.request.Request(
        _NIM_CHAT_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 — fixed NIM host
            r.read(1)
        return Status.OK, f"{model} live"
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return Status.WARN, f"{model} → HTTP {e.code} EOL — update the NIM_*_MODEL ids"
        return Status.WARN, f"{model} → HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 — any transport error is 'unknown', never fatal
        return Status.WARN, f"{model} unreachable: {type(e).__name__}"


def nim_model_rows() -> list[_Row]:
    """Liveness of the configured free NVIDIA NIM brains (fast 120B / heavy 550B / fallback). NIM
    EOLs model ids without warning (glm-5.1 AND glm-5.2 both went 410 Gone, silently wasting a
    round-trip until caught) — this pings each wired id so a dead pick surfaces here instead of
    rotting. Advisory (never FAIL); only meaningful when the key is set."""
    key = os.getenv("NVIDIA_NIM_API_KEY", "").strip()
    if not key:
        return [_Row("NIM brains", Status.WARN,
                     "NVIDIA_NIM_API_KEY unset — free NIM reasoning/tool tier off")]
    from friday.core.router import _NIM_FALLBACK, _NIM_FAST, _NIM_HEAVY

    rows: list[_Row] = []
    for label, wired in (
        ("NIM fast", _NIM_FAST), ("NIM heavy", _NIM_HEAVY), ("NIM fallback", _NIM_FALLBACK)
    ):
        model = wired.split("nvidia_nim/", 1)[-1]  # strip the litellm prefix → raw NIM id
        status, detail = _nim_alive(model, key)
        rows.append(_Row(label, status, detail))
    return rows


def _render(title: str, rows: list[_Row]) -> None:
    t = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    t.add_column("Check", style="bold", width=20)
    t.add_column("Status", width=6)
    t.add_column("Detail")
    for r in rows:
        cell = Text(f"{_STATUS_ICON[r.status]} {r.status.value}", style=_STATUS_COLOR[r.status])
        t.add_row(r.name, cell, r.detail)
    console.print(Panel(t, title=f"[bold]{title}[/bold]", border_style="cyan"))


async def run_doctor(*, services: bool = True) -> bool:
    """Print the full preflight report. Returns False iff core deps are missing (exit 1)."""
    py_rows, py_fixes = python_rows()
    bin_rows, bin_fixes = binary_rows()

    _render("Python packages", py_rows)
    _render("System binaries", bin_rows)
    _render("Skills", skill_rows())
    _render("Engagement readiness", engagement_rows())

    if services:
        _render("NIM brains (free tier)", nim_model_rows())
        from friday.core.monitor import print_report

        print_report(await run_health_checks())

    fixes = py_fixes + bin_fixes
    if fixes:
        console.print(Panel(
            "\n".join(f"  {c}" for c in fixes),
            title="[yellow]To enable what's missing[/yellow]",
            border_style="yellow",
        ))

    core_ok = not any(r.name == "core" and r.status == Status.FAIL for r in py_rows)
    if core_ok:
        console.print("[green]✓ Core is healthy — Sutra can run.[/green]")
    else:
        console.print('[red]✗ Core deps missing — run:  pip install -e ".[all]"[/red]')
    return core_ok
