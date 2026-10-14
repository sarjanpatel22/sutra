"""System information skill — read-only, no confirmation needed."""

from __future__ import annotations

import asyncio
import platform
import re
import shutil
import time

import psutil

from friday.skills.registry import skill

# Commands so destructive that a MISHEARD voice request must never run them. Enforced
# inside run_command itself (defense in depth) so NO caller — autonomous voice
# included — can trip them. A safety NET, not a full sandbox: it blocks the obvious
# system-killers (wiping root/home, formatting a disk, fork bombs, shutdown), and
# deliberately leaves ordinary pentest/CTF commands (nmap, openvpn, gobuster, rm of a
# scratch dir) alone.
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}")
_DISK = re.compile(
    r"\b(?:mkfs(?:\.\w+)?|wipefs)\b"                       # format / wipe a filesystem
    r"|\bdd\b.*\bof=/dev/(?:sd|nvme|mmcblk|vd|hd|disk)"    # dd onto a block device
    r"|>\s*/dev/(?:sd|nvme|mmcblk|vd|hd)",                 # redirect onto a disk node
    re.I,
)
_POWER = re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b|\binit\s+[06]\b", re.I)
_CHMOD_ROOT = re.compile(r"\bchmod\s+-R\s+0*777\s+/(?:\s|$)", re.I)
# A dangerous rm target. Two cases:
#  - root / a home dir / ~ as a WHOLE target — but a scratch subpath under it
#    (/home/me/ctf, /tmp/x) stays allowed, since that's normal CTF cleanup.
#  - a system dir OR anything inside it (/usr/lib, /etc/x) — all of it is system-owned.
_RM_TARGET = re.compile(
    r"(?:\s|=|'|\")(?:/home|/root|~|\$HOME|/)(?:\s|/?\*|'|\"|$)"
    r"|(?:\s|'|\")/(?:etc|usr|var|bin|sbin|boot|lib\w*|sys)(?:/\S*)?(?:\s|'|\"|$)",
    re.I,
)
# The user's OWN home dir, or a dotdir/glob inside it (~/.ssh, $HOME, /home/me, /home/me/*)
# — wiping it destroys SSH keys, configs, the repo. A NON-dot subpath (/home/me/ctf/loot)
# stays allowed for normal CTF cleanup; only the home root itself, its dotdirs, and a /* glob
# are blocked.
_RM_HOME = re.compile(
    r"(?:\s|=|'|\")(?:~|\$HOME|/home/[^/\s'\"]+)(?:/?(?:\s|'|\"|$)|/\.[^\s'\"]*|/\*)",
    re.I,
)
# A current-dir / parent-dir / bare-glob rm target (rm -rf .  ./  ..  *  ./*  .[!.]*).
# These wipe the WHOLE working directory — usually $HOME or the repo — yet name no path,
# so unlike `rm -rf ./build`, `/tmp/loot`, or a path-qualified `loot/*` they are never
# legitimate scratch cleanup. A regex floor can't resolve `$VAR` indirection
# (`X=$HOME; rm -rf $X`); that stays a documented gap — this is a net, not a sandbox.
_RM_CWD = re.compile(
    r"(?:^|\s|;|&|\|)(?:\.\.?/?|\./\*|\*|\.\*|\.\[[^\]]*\][^\s]*)(?=\s|$|;|&|\|)",
    re.I,
)


def is_catastrophic(command: str) -> bool:
    """True if `command` matches a system-destroying pattern that must never run.

    A safety NET against a misheard voice command, not a sandbox: it blocks the
    obvious machine-killers (wiping root/home, formatting a disk, a fork bomb,
    shutdown) and leaves ordinary CTF/pentest commands (nmap, openvpn, rm of a
    scratch dir) alone.
    """
    c = command or ""
    if _FORK_BOMB.search(c) or _DISK.search(c) or _POWER.search(c) or _CHMOD_ROOT.search(c):
        return True
    if re.search(r"\brm\b", c, re.I):  # rm needs BOTH recursive and force AND a whole-target
        recursive = re.search(r"(?:^|\s)-\S*r|--recursive\b", c, re.I)
        force = re.search(r"(?:^|\s)-\S*f|--force\b", c, re.I)
        if recursive and force and (
            _RM_TARGET.search(c) or _RM_HOME.search(c) or _RM_CWD.search(c)
        ):
            return True
    return False


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
    description=(
        "Run an explicit shell/terminal command the user gives (e.g. 'df -h', 'git log') and "
        "return stdout/stderr. Prefer a SPECIFIC tool when one fits: read_file to read a file, "
        "list_directory to list a folder, clipboard_get for the clipboard, system_info for "
        "CPU/RAM/disk. NOT for arithmetic (use calc) or launching apps (use open_app)."
    ),
    auto_confirm=False,  # requires confirmation — can mutate system state
    timeout_s=30,
)
async def run_command(command: str) -> dict[str, str | int]:
    """Execute a shell command.

    command: Shell command to run. Will be executed via /bin/sh -c.
    """
    if is_catastrophic(command):
        return {
            "stdout": "",
            "stderr": (
                f"refused: {command.strip()[:80]!r} looks system-destroying and is "
                "blocked for safety. Run it yourself if you truly mean it."
            ),
            "returncode": 126,
        }
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"stdout": "", "stderr": "command timed out after 30s", "returncode": 124}
    return {
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
        "returncode": proc.returncode or 0,
    }
