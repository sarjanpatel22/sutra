"""VPN skills — bring up / tear down / check an OpenVPN tunnel (e.g. a TryHackMe config).

`connect_vpn` launches openvpn via `sudo -n` (non-interactive), so it NEVER hangs on
or handles your password — it relies on a passwordless-sudo rule scoped to openvpn
only, and prints the exact rule to add if it's missing. `disconnect_vpn` stops that
root daemon the same way (a `sudo -n pkill` scoped to processes named exactly openvpn).
Both are gated (`auto_confirm=False`): they run sudo and change routing, so they ask
first unless SUTRA_AUTONOMOUS is set. `vpn_status` is read-only.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from friday.skills.registry import skill

_STATE_DIR = Path.home() / ".local" / "share" / "friday"
_LOG = _STATE_DIR / "openvpn.log"
_PID = _STATE_DIR / "openvpn.pid"

# Poll cadence while the tunnel comes up (tests shrink these).
_POLL_TRIES = 15
_POLL_SLEEP = 1.0

_SUDOERS_HINT = (
    "Add passwordless sudo scoped to openvpn (run once):\n"
    "  echo 'sarjan ALL=(root) NOPASSWD: /usr/sbin/openvpn' | "
    "sudo tee /etc/sudoers.d/sutra-openvpn >/dev/null && "
    "sudo chmod 440 /etc/sudoers.d/sutra-openvpn"
)

# disconnect_vpn stops the root daemon with a pkill scoped to processes named exactly
# "openvpn"; this is the one-time rule that makes that teardown passwordless too.
_TEARDOWN_HINT = (
    "Add passwordless sudo to STOP openvpn (run once):\n"
    "  echo 'sarjan ALL=(root) NOPASSWD: /usr/bin/pkill -x openvpn' | "
    "sudo tee /etc/sudoers.d/sutra-openvpn-stop >/dev/null && "
    "sudo chmod 440 /etc/sudoers.d/sutra-openvpn-stop"
)

# Poll cadence while the tunnel drops on disconnect (tests shrink these).
_DOWN_TRIES = 10
_DOWN_SLEEP = 0.5


def _openvpn_bin() -> str:
    return shutil.which("openvpn") or "/usr/sbin/openvpn"


def _pkill_bin() -> str:
    return shutil.which("pkill") or "/usr/bin/pkill"


async def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return 124, "", "timed out"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def _tun_info() -> tuple[str | None, str | None]:
    """The first tun/tap interface and its IPv4 address, or (None, None)."""
    rc, out, _ = await _run(["ip", "-4", "-br", "addr"], timeout=5)
    if rc != 0:
        return None, None
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0].split("@")[0].startswith(("tun", "tap")):
            ip = parts[2].split("/")[0] if len(parts) >= 3 else None
            return parts[0], ip
    return None, None


async def _openvpn_running() -> bool:
    """True if an openvpn process exists — checked WITHOUT sudo (pgrep), so an idle
    disconnect_vpn call never escalates to sudo just to find there is nothing to stop."""
    rc, out, _ = await _run([shutil.which("pgrep") or "pgrep", "-x", "openvpn"], timeout=5)
    return rc == 0 and bool(out.strip())


def _resolve_config(config: str) -> str | None:
    """A trusted .ovpn path: an explicit `config=` arg, else SUTRA_VPN_CONFIG. Nothing is
    auto-discovered — `sudo openvpn` runs the config's up/down scripts as root (NOPASSWD),
    so a config is NEVER picked up from a globbed dir (e.g. ~/Downloads); the operator names
    the file. Only point it at .ovpn files you trust."""
    if config:
        p = Path(config).expanduser()
        return str(p) if p.is_file() else None
    env = os.getenv("SUTRA_VPN_CONFIG", "").strip()
    if env and Path(env).expanduser().is_file():
        return str(Path(env).expanduser())
    return None


def _log_tail(n: int = 12) -> str:
    try:
        return "\n".join(_LOG.read_text(errors="replace").splitlines()[-n:])
    except Exception:  # noqa: BLE001 — log unreadable (root-owned / absent); best-effort
        return ""


@skill(
    name="connect_vpn",
    description=(
        "Bring up the OpenVPN tunnel (e.g. your TryHackMe access config) and return the "
        "assigned VPN IP. Pass config=<path to .ovpn>, or set SUTRA_VPN_CONFIG; nothing is "
        "auto-discovered (openvpn runs the config's scripts as root, so only a trusted, named "
        "file is used). Needs scoped passwordless sudo for openvpn (it tells you the rule if "
        "missing) and never handles your password. No-op if already connected."
    ),
    auto_confirm=False,  # runs sudo + changes routing — gated
    timeout_s=40,
)
async def connect_vpn(config: str = "") -> dict[str, object]:
    """Connect the OpenVPN tunnel.

    config: path to a .ovpn file. Empty → SUTRA_VPN_CONFIG. Nothing is auto-discovered —
            openvpn runs the config's up/down scripts as root, so name a trusted file.
    """
    iface, ip = await _tun_info()
    if iface:
        return {"status": "already_connected", "interface": iface, "ip": ip}

    path = _resolve_config(config)
    if not path:
        return {"error": "No .ovpn config found. Set SUTRA_VPN_CONFIG=<path> or pass "
                         "config=<path> — a trusted config only (openvpn runs its scripts "
                         "as root)."}

    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = ["sudo", "-n", _openvpn_bin(), "--config", path, "--daemon",
           "--log", str(_LOG), "--writepid", str(_PID)]
    rc, out, err = await _run(cmd, timeout=20)
    if rc != 0:
        blob = (err + out).lower()
        if "password is required" in blob or "terminal is required" in blob or "sudo:" in blob:
            return {"error": "passwordless sudo for openvpn is not set up.",
                    "fix": _SUDOERS_HINT, "config": path}
        return {"error": f"openvpn failed to launch: {(err or out).strip()[:300]}", "config": path}

    # openvpn daemonized; wait for the tunnel to actually come up.
    for _ in range(_POLL_TRIES):
        await asyncio.sleep(_POLL_SLEEP)
        iface, ip = await _tun_info()
        if iface and ip:
            return {"status": "connected", "interface": iface, "ip": ip,
                    "config": os.path.basename(path)}
    return {"status": "starting", "config": os.path.basename(path),
            "note": "openvpn launched but the tunnel is not up yet — check vpn_status shortly.",
            "log_tail": _log_tail()}


@skill(
    name="disconnect_vpn",
    description=(
        "Tear down the OpenVPN tunnel that connect_vpn started — stops the root openvpn "
        "daemon via scoped passwordless sudo (pkill) and drops the tunnel. No-op if nothing "
        "is connected; prints the one-time sudo rule to add if it is missing. Never handles "
        "your password."
    ),
    auto_confirm=False,  # SIGTERMs a root daemon + drops routing — gated
    timeout_s=20,
)
async def disconnect_vpn() -> dict[str, object]:
    """Stop the OpenVPN tunnel and report what came down.

    No-op (and no sudo) unless a tunnel is up or an openvpn process is running, so an
    idle call never prompts you to set up sudo. Otherwise sends SIGTERM to the root
    daemon via a pkill scoped to processes named exactly "openvpn".
    """
    iface, ip = await _tun_info()
    if not iface and not await _openvpn_running():
        cleaned = _PID.exists()
        _PID.unlink(missing_ok=True)
        return {"status": "not_connected", "cleaned_stale_pid": cleaned}

    rc, out, err = await _run(["sudo", "-n", _pkill_bin(), "-x", "openvpn"], timeout=10)
    blob = (err + out).lower()
    # A sudo failure also exits 1, so screen for it BEFORE reading pkill's exit code.
    if "password is required" in blob or "terminal is required" in blob or "sudo:" in blob:
        return {"error": "passwordless sudo for pkill is not set up.", "fix": _TEARDOWN_HINT}
    if rc not in (0, 1):  # 0 = signalled, 1 = nothing matched; anything else is a real error
        return {"error": f"could not stop openvpn: {(err or out).strip()[:300]}"}

    # Wait for the interface to actually drop.
    for _ in range(_DOWN_TRIES):
        cur, _cur_ip = await _tun_info()
        if not cur:
            _PID.unlink(missing_ok=True)
            return {"status": "disconnected", "was": iface or "(daemon)", "ip": ip}
        await asyncio.sleep(_DOWN_SLEEP)

    return {"status": "still_up", "interface": iface,
            "note": "sent SIGTERM but the tunnel is still up — re-run or check vpn_status.",
            "log_tail": _log_tail()}


@skill(
    name="vpn_status",
    description="Check whether a VPN tunnel is up and report its interface and IP. Read-only.",
    auto_confirm=True,
    timeout_s=8,
)
async def vpn_status() -> dict[str, object]:
    """Whether a tun/tap tunnel is up, with its assigned IP."""
    iface, ip = await _tun_info()
    if iface:
        return {"connected": True, "interface": iface, "ip": ip}
    return {"connected": False}
