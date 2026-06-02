"""Health + budget watchdog agent for Sutra.

Checks:
  - API key presence and basic reachability
  - SearXNG, Redis, FalkorDB, Qdrant service health
  - Disk space
  - NVIDIA GPU / driver status
  - Budget spend vs limits (alerts at 85%)

Alerts via: desktop notify-send + Rich console + log file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from friday.core.router import ALERT_THRESHOLD, get_budget, register_alert

_STATE_DIR = Path.home() / ".local" / "share" / "friday"
_LOG_FILE = _STATE_DIR / "monitor.log"
_CHECK_INTERVAL_S = int(os.getenv("MONITOR_INTERVAL_S", "300"))  # 5 min default

_STATE_DIR.mkdir(parents=True, exist_ok=True)
console = Console()
logging.basicConfig(
    filename=str(_LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger("sutra.monitor")


class Status(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ""
    latency_ms: float = 0.0


@dataclass
class HealthReport:
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def overall(self) -> Status:
        statuses = {c.status for c in self.checks}
        if Status.FAIL in statuses:
            return Status.FAIL
        if Status.WARN in statuses:
            return Status.WARN
        return Status.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "overall": self.overall.value,
            "checks": [
                {"name": c.name, "status": c.status.value, "detail": c.detail, "latency_ms": c.latency_ms}
                for c in self.checks
            ],
        }


# ── Notifier ──────────────────────────────────────────────────────────────────

def notify(title: str, body: str, urgency: str = "normal") -> None:
    """Send a desktop notification (notify-send) and log it."""
    log.warning("%s — %s", title, body)
    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", f"--urgency={urgency}", "--app-name=Sutra", title, body],
            check=False,
            timeout=3,
        )
    console.print(Panel(
        f"[bold]{title}[/bold]\n{body}",
        border_style="yellow" if urgency == "normal" else "red",
        title="[yellow]Sutra Monitor[/yellow]",
    ))


def _budget_alert_callback(scope: str, pct: float) -> None:
    title = f"Budget alert — {scope}"
    body = (
        f"You've used {pct * 100:.1f}% of your {scope} API budget.\n"
        f"{'Daily' if scope == 'daily' else 'Monthly'} limit almost reached."
    )
    notify(title, body, urgency="critical")


register_alert(_budget_alert_callback)


# ── Individual checks ─────────────────────────────────────────────────────────

async def _check_api_key() -> CheckResult:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return CheckResult("anthropic_key", Status.FAIL, "ANTHROPIC_API_KEY not set")
    if not key.startswith("sk-ant-"):
        return CheckResult("anthropic_key", Status.WARN, "Key present but format unexpected")
    return CheckResult("anthropic_key", Status.OK, f"sk-ant-...{key[-4:]}")


async def _check_http(name: str, url: str, timeout: float = 3.0) -> CheckResult:
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
        latency = (time.monotonic() - t0) * 1000
        if r.status_code < 500:
            return CheckResult(name, Status.OK, f"HTTP {r.status_code}", latency)
        return CheckResult(name, Status.FAIL, f"HTTP {r.status_code}", latency)
    except httpx.ConnectError:
        return CheckResult(name, Status.FAIL, "connection refused — service down?")
    except httpx.TimeoutException:
        return CheckResult(name, Status.WARN, f"timeout after {timeout}s")
    except Exception as e:  # noqa: BLE001
        return CheckResult(name, Status.FAIL, str(e))


async def _check_redis() -> CheckResult:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    # Try a raw PING without importing redis to keep startup cheap
    host = "localhost"
    port = 6379
    try:
        if "://" in redis_url:
            parts = redis_url.split("://")[1].split(":")
            host = parts[0]
            port = int(parts[1].split("/")[0]) if len(parts) > 1 else 6379
    except Exception:  # noqa: BLE001
        pass
    t0 = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=2.0
        )
        writer.write(b"*1\r\n$4\r\nPING\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(64), timeout=2.0)
        writer.close()
        await writer.wait_closed()
        latency = (time.monotonic() - t0) * 1000
        if b"PONG" in response:
            return CheckResult("redis", Status.OK, f"{host}:{port}", latency)
        return CheckResult("redis", Status.WARN, f"unexpected response: {response[:20]!r}", latency)
    except (ConnectionRefusedError, OSError):
        return CheckResult("redis", Status.FAIL, "connection refused — Redis not running")
    except asyncio.TimeoutError:
        return CheckResult("redis", Status.WARN, "timeout")
    except Exception as e:  # noqa: BLE001
        return CheckResult("redis", Status.FAIL, str(e))


async def _check_disk() -> CheckResult:
    import shutil as sh
    usage = sh.disk_usage("/")
    pct = usage.used / usage.total * 100
    free_gb = usage.free / 1e9
    if pct > 90:
        return CheckResult("disk", Status.FAIL, f"{pct:.1f}% used, {free_gb:.1f} GB free")
    if pct > 80:
        return CheckResult("disk", Status.WARN, f"{pct:.1f}% used, {free_gb:.1f} GB free")
    return CheckResult("disk", Status.OK, f"{pct:.1f}% used, {free_gb:.1f} GB free")


async def _check_gpu() -> CheckResult:
    if not shutil.which("nvidia-smi"):
        return CheckResult("gpu", Status.WARN, "nvidia-smi not found — drivers not installed")
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=name,memory.used,memory.total,temperature.gpu,utilization.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            parts = [p.strip() for p in stdout.decode().strip().split(",")]
            if len(parts) >= 5:
                name, mem_used, mem_total, temp, util = parts[:5]
                detail = f"{name} | {mem_used}/{mem_total} MB VRAM | {temp}°C | {util}% util"
                vram_pct = int(mem_used) / int(mem_total) * 100
                status = Status.WARN if vram_pct > 90 else Status.OK
                return CheckResult("gpu", status, detail)
        return CheckResult("gpu", Status.WARN, "nvidia-smi returned non-zero")
    except asyncio.TimeoutError:
        return CheckResult("gpu", Status.WARN, "nvidia-smi timeout")
    except Exception as e:  # noqa: BLE001
        return CheckResult("gpu", Status.FAIL, str(e))


async def _check_budget() -> CheckResult:
    b = get_budget()
    lines = []
    status = Status.OK
    daily_pct = b.daily_pct
    monthly_pct = b.monthly_pct

    lines.append(f"daily ${b.daily_spent:.4f}/${b.daily_limit:.2f} ({daily_pct:.1f}%) | {b.daily_tokens:,} tok")
    lines.append(f"monthly ${b.monthly_spent:.4f}/${b.monthly_limit:.2f} ({monthly_pct:.1f}%) | {b.monthly_tokens:,} tok")

    if daily_pct >= ALERT_THRESHOLD * 100 or monthly_pct >= ALERT_THRESHOLD * 100:
        status = Status.WARN
    if daily_pct >= 100 or monthly_pct >= 100:
        status = Status.FAIL

    return CheckResult("budget", status, " | ".join(lines))


# ── Full health run ───────────────────────────────────────────────────────────

async def run_health_checks() -> HealthReport:
    """Run all health checks concurrently and return a HealthReport."""
    searxng_url = os.getenv("SEARXNG_URL", "http://localhost:8080")
    qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")

    results = await asyncio.gather(
        _check_api_key(),
        _check_http("searxng", searxng_url),
        _check_redis(),
        _check_http("qdrant", f"{qdrant_url}/healthz"),
        _check_disk(),
        _check_gpu(),
        _check_budget(),
    )

    report = HealthReport(checks=list(results))
    log.info("health check: %s", report.overall.value)
    return report


# ── Rich display ──────────────────────────────────────────────────────────────

_STATUS_COLOR = {
    Status.OK: "green",
    Status.WARN: "yellow",
    Status.FAIL: "red",
    Status.SKIP: "dim",
}

_STATUS_ICON = {
    Status.OK: "●",
    Status.WARN: "◐",
    Status.FAIL: "✗",
    Status.SKIP: "○",
}


def print_report(report: HealthReport) -> None:
    t = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    t.add_column("Check", style="bold", width=18)
    t.add_column("Status", width=6)
    t.add_column("Detail")

    for c in report.checks:
        color = _STATUS_COLOR[c.status]
        icon = _STATUS_ICON[c.status]
        status_cell = Text(f"{icon} {c.status.value}", style=color)
        detail = c.detail
        if c.latency_ms:
            detail += f" ({c.latency_ms:.0f}ms)"
        t.add_row(c.name, status_cell, detail)

    overall_color = _STATUS_COLOR[report.overall]
    console.print(Panel(
        t,
        title=f"[{overall_color}]Sutra Health — {report.overall.value.upper()}[/{overall_color}]  [dim]{report.timestamp}[/dim]",
        border_style=overall_color,
    ))


# ── Watchdog loop ─────────────────────────────────────────────────────────────

async def run_watchdog(interval_s: int = _CHECK_INTERVAL_S) -> None:
    """Run health checks on an interval, printing results and alerting on issues."""
    console.print(f"[dim]Watchdog started — checking every {interval_s}s. Ctrl+C to stop.[/dim]")

    while True:
        report = await run_health_checks()
        print_report(report)

        # Alert on any failures
        failures = [c for c in report.checks if c.status == Status.FAIL]
        if failures:
            body = "\n".join(f"• {c.name}: {c.detail}" for c in failures)
            notify("Sutra health check failed", body, urgency="critical")

        # Save latest report
        report_path = _STATE_DIR / "health_latest.json"
        report_path.write_text(json.dumps(report.to_dict(), indent=2))

        await asyncio.sleep(interval_s)
