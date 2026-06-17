"""Tests for the VPN skills — connect_vpn (gated) and vpn_status (read-only)."""

from friday.skills import connect_vpn as cv
from friday.skills import registry


# ── registration + gate posture ──────────────────────────────────────────────
def test_vpn_skills_registered_and_gated():
    registry.autodiscover()
    assert registry.get("connect_vpn").auto_confirm is False      # runs sudo → gated
    assert registry.get("disconnect_vpn").auto_confirm is False   # SIGTERMs root daemon → gated
    assert registry.get("vpn_status").auto_confirm is True        # read-only


def test_vpn_skill_names_are_audit_clean():
    # None of the names carry a mutation verb, so the confirmation audit won't flag them
    # (disconnect_vpn stays gated by its explicit auto_confirm=False above, not its name).
    from friday.skills.audit import mutation_hits

    assert mutation_hits("connect_vpn") == set()
    assert mutation_hits("disconnect_vpn") == set()
    assert mutation_hits("vpn_status") == set()


# ── _tun_info parsing ────────────────────────────────────────────────────────
async def test_tun_info_parses(monkeypatch):
    async def _run(cmd, timeout=15):
        return (
            0,
            "lo               UNKNOWN        127.0.0.1/8\n"
            "eth0             UP             192.168.1.5/24\n"
            "tun0             UNKNOWN        10.9.0.7/24\n",
            "",
        )

    monkeypatch.setattr(cv, "_run", _run)
    assert await cv._tun_info() == ("tun0", "10.9.0.7")


async def test_tun_info_none_when_no_tunnel(monkeypatch):
    async def _run(cmd, timeout=15):
        return (0, "lo  UNKNOWN  127.0.0.1/8\neth0  UP  192.168.1.5/24\n", "")

    monkeypatch.setattr(cv, "_run", _run)
    assert await cv._tun_info() == (None, None)


# ── config resolution ────────────────────────────────────────────────────────
def test_resolve_config_explicit_arg(tmp_path):
    f = tmp_path / "thm.ovpn"
    f.write_text("x")
    assert cv._resolve_config(str(f)) == str(f)
    assert cv._resolve_config(str(tmp_path / "nope.ovpn")) is None


def test_resolve_config_env(monkeypatch, tmp_path):
    f = tmp_path / "acct.ovpn"
    f.write_text("x")
    monkeypatch.setenv("SUTRA_VPN_CONFIG", str(f))
    assert cv._resolve_config("") == str(f)


# ── connect_vpn behaviour ────────────────────────────────────────────────────
async def test_connect_idempotent_when_already_up(monkeypatch):
    async def _tun(*a, **k):
        return ("tun0", "10.9.0.5")

    async def _boom(*a, **k):
        raise AssertionError("must not launch openvpn when a tunnel is already up")

    monkeypatch.setattr(cv, "_tun_info", _tun)
    monkeypatch.setattr(cv, "_run", _boom)
    assert await cv.connect_vpn() == {
        "status": "already_connected", "interface": "tun0", "ip": "10.9.0.5"
    }


async def test_connect_no_config(monkeypatch):
    async def _tun(*a, **k):
        return (None, None)

    monkeypatch.setattr(cv, "_tun_info", _tun)
    monkeypatch.setattr(cv, "_resolve_config", lambda c: None)
    out = await cv.connect_vpn()
    assert "error" in out and "No .ovpn" in out["error"]


async def test_connect_needs_passwordless_sudo(monkeypatch, tmp_path):
    monkeypatch.setattr(cv, "_STATE_DIR", tmp_path)

    async def _tun(*a, **k):
        return (None, None)

    async def _run(cmd, timeout=15):
        return (1, "", "sudo: a password is required")

    monkeypatch.setattr(cv, "_tun_info", _tun)
    monkeypatch.setattr(cv, "_resolve_config", lambda c: "/x.ovpn")
    monkeypatch.setattr(cv, "_run", _run)
    out = await cv.connect_vpn()
    assert "passwordless sudo" in out["error"]
    assert "sudoers.d/sutra-openvpn" in out["fix"]


async def test_connect_launch_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(cv, "_STATE_DIR", tmp_path)

    async def _tun(*a, **k):
        return (None, None)

    async def _run(cmd, timeout=15):
        return (1, "", "Options error: bad config")

    monkeypatch.setattr(cv, "_tun_info", _tun)
    monkeypatch.setattr(cv, "_resolve_config", lambda c: "/x.ovpn")
    monkeypatch.setattr(cv, "_run", _run)
    out = await cv.connect_vpn()
    assert "failed to launch" in out["error"]


async def test_connect_success(monkeypatch, tmp_path):
    monkeypatch.setattr(cv, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cv, "_POLL_SLEEP", 0)
    monkeypatch.setattr(cv, "_resolve_config", lambda c: "/x.ovpn")

    seen = {"n": 0}

    async def _tun(*a, **k):
        seen["n"] += 1
        return ("tun0", "10.9.0.5") if seen["n"] >= 2 else (None, None)

    async def _run(cmd, timeout=15):
        return (0, "", "")

    monkeypatch.setattr(cv, "_tun_info", _tun)
    monkeypatch.setattr(cv, "_run", _run)
    out = await cv.connect_vpn()
    assert out["status"] == "connected" and out["ip"] == "10.9.0.5" and out["interface"] == "tun0"


async def test_connect_starting_when_tunnel_slow(monkeypatch, tmp_path):
    monkeypatch.setattr(cv, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cv, "_POLL_SLEEP", 0)
    monkeypatch.setattr(cv, "_POLL_TRIES", 2)
    monkeypatch.setattr(cv, "_resolve_config", lambda c: "/x.ovpn")
    monkeypatch.setattr(cv, "_log_tail", lambda n=12: "")

    async def _tun(*a, **k):
        return (None, None)

    async def _run(cmd, timeout=15):
        return (0, "", "")

    monkeypatch.setattr(cv, "_tun_info", _tun)
    monkeypatch.setattr(cv, "_run", _run)
    out = await cv.connect_vpn()
    assert out["status"] == "starting"


# ── vpn_status ───────────────────────────────────────────────────────────────
async def test_vpn_status(monkeypatch):
    async def _up(*a, **k):
        return ("tun0", "10.9.0.5")

    monkeypatch.setattr(cv, "_tun_info", _up)
    assert await cv.vpn_status() == {"connected": True, "interface": "tun0", "ip": "10.9.0.5"}

    async def _down(*a, **k):
        return (None, None)

    monkeypatch.setattr(cv, "_tun_info", _down)
    assert await cv.vpn_status() == {"connected": False}


def test_resolve_config_never_globs_downloads(monkeypatch):
    # Security: no auto-discovery — a malicious .ovpn in ~/Downloads must never be picked
    # up (sudo openvpn runs its up/down scripts as root). Only explicit arg / env count.
    monkeypatch.delenv("SUTRA_VPN_CONFIG", raising=False)
    assert cv._resolve_config("") is None


# ── disconnect_vpn behaviour ─────────────────────────────────────────────────
async def test_disconnect_noop_when_nothing_connected(monkeypatch, tmp_path):
    # No tunnel and no openvpn process → idempotent no-op that NEVER touches sudo.
    monkeypatch.setattr(cv, "_PID", tmp_path / "openvpn.pid")

    async def _down(*a, **k):
        return (None, None)

    async def _not_running(*a, **k):
        return False

    async def _boom(*a, **k):
        raise AssertionError("a no-op disconnect must not invoke sudo")

    monkeypatch.setattr(cv, "_tun_info", _down)
    monkeypatch.setattr(cv, "_openvpn_running", _not_running)
    monkeypatch.setattr(cv, "_run", _boom)
    assert await cv.disconnect_vpn() == {"status": "not_connected", "cleaned_stale_pid": False}


async def test_disconnect_kills_daemon_and_drops_tunnel(monkeypatch, tmp_path):
    monkeypatch.setattr(cv, "_PID", tmp_path / "openvpn.pid")
    monkeypatch.setattr(cv, "_DOWN_SLEEP", 0)
    seen = {"n": 0}

    async def _tun(*a, **k):
        seen["n"] += 1
        return ("tun0", "10.9.0.7") if seen["n"] == 1 else (None, None)

    async def _run(cmd, timeout=15):
        assert cmd[:3] == ["sudo", "-n", cv._pkill_bin()]  # scoped pkill, non-interactive
        assert cmd[3:] == ["-x", "openvpn"]                # matches ONLY processes named openvpn
        return (0, "", "")

    monkeypatch.setattr(cv, "_tun_info", _tun)
    monkeypatch.setattr(cv, "_run", _run)
    out = await cv.disconnect_vpn()
    assert out == {"status": "disconnected", "was": "tun0", "ip": "10.9.0.7"}


async def test_disconnect_needs_passwordless_sudo(monkeypatch, tmp_path):
    # sudo refuses (no NOPASSWD pkill rule) → exits 1 with the sudo banner, even though
    # 1 is also pkill's "nothing matched" code; the banner must win and surface the fix.
    monkeypatch.setattr(cv, "_PID", tmp_path / "openvpn.pid")

    async def _up(*a, **k):
        return ("tun0", "10.9.0.7")

    async def _run(cmd, timeout=15):
        return (1, "", "sudo: a password is required")

    monkeypatch.setattr(cv, "_tun_info", _up)
    monkeypatch.setattr(cv, "_run", _run)
    out = await cv.disconnect_vpn()
    assert "passwordless sudo" in out["error"]
    assert "sutra-openvpn-stop" in out["fix"]


async def test_disconnect_kills_zombie_daemon_without_tunnel(monkeypatch, tmp_path):
    # openvpn is running but never brought a tun up — disconnect still tears it down.
    monkeypatch.setattr(cv, "_PID", tmp_path / "openvpn.pid")
    monkeypatch.setattr(cv, "_DOWN_SLEEP", 0)

    async def _down(*a, **k):
        return (None, None)

    async def _running(*a, **k):
        return True

    async def _run(cmd, timeout=15):
        return (0, "", "")

    monkeypatch.setattr(cv, "_tun_info", _down)
    monkeypatch.setattr(cv, "_openvpn_running", _running)
    monkeypatch.setattr(cv, "_run", _run)
    out = await cv.disconnect_vpn()
    assert out["status"] == "disconnected" and out["was"] == "(daemon)"


async def test_disconnect_still_up_if_daemon_survives(monkeypatch, tmp_path):
    monkeypatch.setattr(cv, "_PID", tmp_path / "openvpn.pid")
    monkeypatch.setattr(cv, "_DOWN_SLEEP", 0)
    monkeypatch.setattr(cv, "_DOWN_TRIES", 2)
    monkeypatch.setattr(cv, "_log_tail", lambda n=12: "")

    async def _up(*a, **k):
        return ("tun0", "10.9.0.7")

    async def _run(cmd, timeout=15):
        return (0, "", "")

    monkeypatch.setattr(cv, "_tun_info", _up)
    monkeypatch.setattr(cv, "_run", _run)
    out = await cv.disconnect_vpn()
    assert out["status"] == "still_up"


async def test_openvpn_running_detects_process(monkeypatch):
    async def _hit(cmd, timeout=15):
        return (0, "12345\n", "")

    monkeypatch.setattr(cv, "_run", _hit)
    assert await cv._openvpn_running() is True

    async def _miss(cmd, timeout=15):
        return (1, "", "")

    monkeypatch.setattr(cv, "_run", _miss)
    assert await cv._openvpn_running() is False
