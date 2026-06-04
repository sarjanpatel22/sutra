"""System information skill — read-only, no confirmation needed."""

from __future__ import annotations

import asyncio
import platform
import shutil
import time

import psutil  # type: ignore[import-untyped]

from friday.skills.registry import skill


@skill(
    name="system_info",
    description="Get current system stats: CPU, RAM, disk, GPU, uptime.",
    auto_confirm=True,
    timeout_s=5,
)
async def system_info() -> dict[str, object]:
    """Return a snapshot of current system resource usage."""
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    cpu_pct = psutil.cpu_percent(interval=0.2)
    uptime_s = int(time.time() - psutil.boot_time())

    result: dict[str, object] = {
        "platform": platform.platform(),
        "cpu_percent": cpu_pct,
        "cpu_count": psutil.cpu_count(),
        "ram_total_gb": round(mem.total / 1e9, 1),
        "ram_used_gb": round(mem.used / 1e9, 1),
        "ram_percent": mem.percent,
        "disk_total_gb": round(disk.total / 1e9, 1),
        "disk_used_gb": round(disk.used / 1e9, 1),
        "disk_percent": disk.percent,
        "uptime_hours": round(uptime_s / 3600, 1),
    }

    # GPU info via nvidia-smi if available
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        proc = await asyncio.create_subprocess_exec(
            nvidia_smi,
            "--query-gpu=name,memory.used,memory.total,utilization.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            parts = stdout.decode().strip().split(", ")
            if len(parts) == 4:
                result["gpu_name"] = parts[0]
                result["gpu_vram_used_mb"] = int(parts[1])
                result["gpu_vram_total_mb"] = int(parts[2])
                result["gpu_utilization_pct"] = int(parts[3])

    return result


@skill(
    name="battery",
    description="Get battery charge percentage and whether AC power is connected.",
    auto_confirm=True,
    timeout_s=3,
)
async def battery() -> dict[str, object]:
    """Battery status: charge %, plugged-in state, and estimated time left."""
    bat = psutil.sensors_battery()
    if bat is None:
        return {"error": "no battery detected (desktop or unsupported platform)"}
    secs = bat.secsleft
    return {
        "percent": round(bat.percent, 1),
        "plugged_in": bool(bat.power_plugged),
        "minutes_left": (secs // 60) if isinstance(secs, int) and secs >= 0 else None,
    }


@skill(
    name="run_command",
    description="Run a shell command and return stdout/stderr. Sandboxed — use for read-only commands.",
    auto_confirm=False,  # requires confirmation — can mutate system state
    timeout_s=30,
)
async def run_command(command: str) -> dict[str, str | int]:
    """Execute a shell command.

    command: Shell command to run. Will be executed via /bin/sh -c.
    """
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"stdout": "", "stderr": "command timed out after 30s", "returncode": 124}
    return {
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
        "returncode": proc.returncode or 0,
    }
