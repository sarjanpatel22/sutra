"""Autonomous voice mode + the always-on catastrophic-command floor.

The floor must block the obvious machine-killers (so a MISHEARD voice command can't
wipe the box) while leaving ordinary CTF/pentest commands alone. Autonomous mode is
opt-in (SUTRA_AUTONOMOUS) and only changes the voice CONFIRM policy — the floor stays.
"""

from __future__ import annotations

import pytest

from friday.core.agent import _voice_confirm
from friday.skills.system_info import is_catastrophic, run_command

# --- the catastrophic floor: these must always be blocked --------------------
BLOCKED = [
    "rm -rf /",
    "rm -rf /*",
    "rm -rf / ",
    "sudo rm -rf /etc",
    "rm -rf /usr/lib",
    "rm -rf ~",
    "rm -rf $HOME",
    "rm -rf /home",
    "rm -rf /home/*",
    "rm -fr /",
    "rm -r -f /boot",
    "dd if=/dev/zero of=/dev/sda",
    "dd if=x of=/dev/nvme0n1 bs=1M",
    "mkfs.ext4 /dev/sdb1",
    "wipefs -a /dev/sda",
    "echo boom > /dev/sda",
    ":(){ :|:& };:",
    "chmod -R 777 /",
    "shutdown now",
    "reboot",
    "init 0",
    # the operator's OWN home dir / its dotdirs / a home glob — must be blocked
    "rm -rf /home/sarjan",
    "rm -rf /home/sarjan/",
    "rm -rf /home/sarjan/*",
    "rm -rf /home/sarjan/.ssh",
    "rm -rf ~/.ssh",
    "rm -rf $HOME/.config",
]

# --- ordinary CTF / dev commands: these must run -----------------------------
ALLOWED = [
    "nmap -sV -sC 10.10.10.10",
    "sudo openvpn /home/sarjan/thm.ovpn",
    "ls -la /home/sarjan",
    "rm -rf ./build",
    "rm -rf /tmp/scratch",
    "rm -rf /home/sarjan/ctf/loot",
    "gobuster dir -u http://10.10.10.10 -w words.txt",
    "cat /etc/passwd",
    "grep -rf patterns.txt .",  # -rf here are grep flags, not rm
    "df -h",
    "curl http://10.10.10.10/robots.txt",
]


@pytest.mark.parametrize("cmd", BLOCKED)
def test_catastrophic_blocked(cmd: str) -> None:
    assert is_catastrophic(cmd) is True, f"should block: {cmd!r}"


@pytest.mark.parametrize("cmd", ALLOWED)
def test_legit_allowed(cmd: str) -> None:
    assert is_catastrophic(cmd) is False, f"should allow: {cmd!r}"


@pytest.mark.asyncio
async def test_run_command_refuses_catastrophic_without_executing() -> None:
    res = await run_command("rm -rf /")
    assert res["returncode"] == 126
    assert "refused" in str(res["stderr"]).lower()


@pytest.mark.asyncio
async def test_run_command_runs_safe() -> None:
    res = await run_command("echo hello-ctf")
    assert res["returncode"] == 0
    assert "hello-ctf" in str(res["stdout"])


@pytest.mark.asyncio
async def test_voice_confirm_default_declines_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUTRA_AUTONOMOUS", raising=False)
    assert await _voice_confirm("run_command", {"command": "ls"}) is False
    assert await _voice_confirm("run_python", {"code": "print(1)"}) is False
    # the browsing family is always hands-free, autonomous or not
    assert await _voice_confirm("web_task", {}) is True


@pytest.mark.asyncio
async def test_voice_confirm_autonomous_approves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    assert await _voice_confirm("run_command", {"command": "nmap 10.10.10.10"}) is True
    assert await _voice_confirm("run_python", {"code": "print(1)"}) is True
    assert await _voice_confirm("web_task", {}) is True


@pytest.mark.asyncio
async def test_autonomous_off_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for off in ("0", "false", "no", ""):
        monkeypatch.setenv("SUTRA_AUTONOMOUS", off)
        assert await _voice_confirm("run_command", {"command": "ls"}) is False
