"""Engagement scope guard — keep the autonomous loop on AUTHORIZED targets only.

`sutra pwn <ip>` / `sutra auto --scope …` define the in-scope targets (IPs, CIDRs,
hostnames). Before the loop auto-runs a `run_command`, the hosts it would touch are
extracted and checked: anything outside scope is escalated to the operator instead of
running hands-free — so a prompt-injection from target output ("now scan 8.8.8.8 / curl
evil.com") can't quietly point the engagement at a box you never authorized.

Targets are pulled from IPv4 + IPv6 literals, URL hosts (`http://host`), `user@host`
(ssh/scp), and bare domain names (`nmap target.com`). Loopback / `localhost` are always
in scope (local tooling). Limits, documented rather than hidden: no DNS resolution
(resolving attacker-influenced output is itself an injection vector — match hostnames
literally); and a bare domain whose final label collides with a code/data file extension
(`evil.sh`, `evil.zip`, `evil.md` — all real TLDs) is read as a filename, so an off-scope
domain on one of those TLDs isn't caught — IP-literal targets (the norm for lab boxes) are
unaffected. Both `run_command` and the browser family (web_task/fill_form start_url/url)
are scope-checked.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_BRACKET = re.compile(r"\[([0-9A-Fa-f:]+)\]")  # bracketed IPv6 in URLs: http://[::1]/
_URL_HOST = re.compile(r"https?://([^/\s:?#]+)", re.I)
_USER_AT = re.compile(r"\b[\w.\-]+@([A-Za-z0-9.\-]+)")
# A bare domain name target (nmap target.com / box.htb), so it can't dodge the guard by
# omitting the scheme. Conservative: a dotted name whose final label is ≥2 alphabetic chars
# and not a common file extension — anything ambiguous errs toward asking, never silent-allow.
_FQDN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$", re.I)
_NOT_HOST_EXT = frozenset({
    "txt", "xml", "json", "html", "htm", "log", "conf", "cfg", "csv", "md", "py", "sh", "js",
    "ts", "png", "jpg", "jpeg", "gif", "pdf", "bin", "out", "bak", "db", "zip", "tar", "gz",
    "tgz", "yaml", "yml", "gnmap", "nmap", "php", "asp", "aspx", "jsp", "key", "pem", "crt",
    "cap", "pcap", "lst", "dic",
})

_Net = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class Scope:
    """Authorized targets: a set of IP networks plus literal hostnames."""

    nets: tuple[_Net, ...]
    hosts: frozenset[str]


def parse_scope(spec: str | None) -> Scope | None:
    """Parse a comma/space-separated spec (IPs, CIDRs, hostnames) → Scope, or None if
    empty. A bare IP becomes a /32 (or /128); anything that isn't an IP/CIDR is treated
    as a literal hostname."""
    if not spec or not spec.strip():
        return None
    nets: list[_Net] = []
    hosts: set[str] = set()
    for tok in re.split(r"[,\s]+", spec.strip()):
        if not tok:
            continue
        try:
            nets.append(ipaddress.ip_network(tok, strict=False))
        except ValueError:
            hosts.add(tok.lower())
    if not nets and not hosts:
        return None
    return Scope(tuple(nets), frozenset(hosts))


def extract_hosts(text: str) -> set[str]:
    """Hosts a shell command would touch: IPv4/IPv6 literals, URL hosts, and user@host."""
    hosts: set[str] = set()
    # URL hosts, minus malformed bracketed-IPv6 captures ("[2001") — those are pulled
    # out properly by _IPV6_BRACKET below.
    hosts |= {h for h in _URL_HOST.findall(text) if not h.startswith("[")}
    hosts |= set(_USER_AT.findall(text))
    for m in _IPV4.findall(text):
        try:
            ipaddress.ip_address(m)  # reject 999.1.1.1 and version-string look-alikes
            hosts.add(m)
        except ValueError:
            continue
    # IPv6 literals — bracketed (http://[2001:db8::1]/) and bare (ping 2001:db8::1), each
    # validated by ipaddress. Without this a bare IPv6 target wasn't extracted at all, so it
    # was silently treated as in-scope — a scope bypass.
    v6: set[str] = set(_IPV6_BRACKET.findall(text))
    for tok in re.split(r"[\s;&|<>()'\"`,]+", text):
        cand = tok.strip("[]").split("/")[0]
        if cand.count(":") >= 2:  # cheap IPv6 prefilter (an IPv6 addr has ≥2 colons)
            v6.add(cand)
    for h in v6:
        try:
            if ipaddress.ip_address(h).version == 6:
                hosts.add(h)
        except ValueError:
            continue
    # Bare domain targets (nmap target.com) — scheme-less, so they can't dodge the guard.
    for tok in re.split(r"[\s;&|<>()'\"`,]+", text):
        if not tok or tok.startswith("-") or "://" in tok or "@" in tok:
            continue
        cand = tok.split("/")[0].split(":")[0]  # strip any path / port
        tld = cand.rsplit(".", 1)[-1].lower()
        if _FQDN.match(cand) and tld.isalpha() and len(tld) >= 2 and tld not in _NOT_HOST_EXT:
            hosts.add(cand)
    # Lower-case; strip :port for IPv4/hostnames but NOT IPv6 (it is colon-delimited).
    out: set[str] = set()
    for h in hosts:
        if not h:
            continue
        out.add(h.lower() if h.count(":") >= 2 else h.split(":")[0].lower())
    return out


def _host_in_scope(host: str, scope: Scope) -> bool:
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.lower() in scope.hosts  # a hostname — match literally, no DNS
    if ip.is_loopback:
        return True
    return any(ip in n for n in scope.nets)


def command_in_scope(command: str, scope: Scope) -> bool:
    """True if every host the command targets is in scope. A command with no network
    target (whoami, cat /etc/passwd) is in scope — there's nothing to leave it for."""
    hosts = extract_hosts(command or "")
    return all(_host_in_scope(h, scope) for h in hosts)


def args_in_scope(args: dict[str, object], scope: Scope) -> bool:
    """True if every host referenced by a skill's STRING arguments is in scope — scope-checks
    the browser family (web_task/web_task_vision start_url, fill_form url, and any URL in the
    goal text)."""
    text = " ".join(v for v in args.values() if isinstance(v, str))
    return command_in_scope(text, scope)
