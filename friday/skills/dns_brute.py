"""DNS brute-force + permutation — resolve a wordlist (and mutations of known subs) against a
domain to surface subdomains the PASSIVE sources (subfinder/crt.sh) miss. Many bounty assets only
exist in DNS, never in a cert log.

Confirmed-only: a candidate counts ONLY if it actually resolves to an IP. Wildcard DNS (a catch-all
`*.domain` answer) is detected up front and its IPs are filtered out, so a wildcard domain can't
manufacture hundreds of phantom subdomains. Read-only resolution (same category as dns_lookup), so
auto-confirmed; candidate count is hard-bounded so a run can't blow up. Pure-stdlib, CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from friday.skills.registry import skill

# Common subdomain labels worth a first resolve — the high-hit tier of any DNS wordlist.
_COMMON = (
    "www", "mail", "remote", "blog", "webmail", "server", "ns1", "ns2", "smtp", "secure",
    "vpn", "admin", "portal", "dev", "staging", "stage", "test", "testing", "qa", "uat",
    "api", "api-dev", "api-staging", "apidev", "app", "apps", "mobile", "m", "beta", "alpha",
    "demo", "sandbox", "internal", "intranet", "corp", "git", "gitlab", "github", "jenkins",
    "ci", "cd", "build", "deploy", "jira", "confluence", "wiki", "docs", "support", "help",
    "status", "monitor", "monitoring", "grafana", "kibana", "prometheus", "metrics", "logs",
    "db", "database", "mysql", "postgres", "redis", "mongo", "sql", "phpmyadmin", "pma",
    "cpanel", "whm", "ftp", "sftp", "ssh", "proxy", "gateway", "gw", "router", "firewall",
    "dashboard", "console", "manage", "management", "cloud", "aws", "azure", "gcp", "s3",
    "cdn", "static", "assets", "media", "img", "images", "files", "download", "downloads",
    "upload", "uploads", "store", "shop", "cart", "checkout", "pay", "payment", "billing",
    "account", "accounts", "auth", "login", "sso", "oauth", "id", "identity", "ldap", "ad",
    "old", "new", "legacy", "v1", "v2", "v3", "backup", "bak", "archive", "temp", "tmp",
    "preprod", "prod", "production", "live", "edge", "origin", "lb", "node1", "node2", "web",
)

# Mutation templates applied to each KNOWN subdomain label — surfaces sibling/env variants.
_PREFIXES = ("dev", "staging", "stage", "test", "qa", "uat", "prod", "preprod", "internal", "new",
             "old", "admin", "api")
_SUFFIXES = _PREFIXES + ("1", "2", "01", "02", "v1", "v2", "bak", "backup")

_MAX_CANDIDATES = 1500  # hard ceiling so a run can't fan out unbounded
_CONCURRENCY = 64


def _bare(domain: str) -> str:
    """Reduce a URL/host[:port] to its bare apex domain."""
    return (domain or "").strip().lower().split("://")[-1].split("/")[0].split(":")[0]


def _label(host: str, domain: str) -> str:
    """Leftmost sub-label of `host` under `domain` (api.x.example.com / example.com → api)."""
    if host == domain or not host.endswith("." + domain):
        return ""
    return host[: -(len(domain) + 1)].split(".")[0]


def _candidates(domain: str, words: list[str], known: list[str], permute: bool) -> list[str]:
    """Build the de-duplicated candidate hostname list (wordlist ∪ permutations of known subs)."""
    labels: set[str] = {w.strip().lower() for w in words if w.strip()}
    labels.update(_COMMON)
    if permute:
        for host in known:
            lab = _label(_bare(host), domain)
            if not lab:
                continue
            labels.add(lab)
            for p in _PREFIXES:
                labels.add(f"{p}-{lab}")
                labels.add(f"{p}.{lab}")  # becomes p.lab.domain — deeper env nesting
            for s in _SUFFIXES:
                labels.add(f"{lab}-{s}")
                labels.add(f"{lab}{s}")
    hosts = sorted(f"{lab}.{domain}" for lab in labels if lab and " " not in lab)
    return hosts[:_MAX_CANDIDATES]


async def _resolve(host: str, sem: asyncio.Semaphore) -> tuple[str, list[str]]:
    """Resolve `host` to its A/AAAA addresses (empty list if it doesn't resolve)."""
    async with sem:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, None, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, OSError, UnicodeError):
            return host, []
    return host, sorted({i[4][0] for i in infos})


async def _wildcard_ips(domain: str, sem: asyncio.Semaphore) -> set[str]:
    """Detect catch-all DNS: resolve random labels that should NOT exist; any answer is wildcard."""
    probes = [f"sutra-wild-{n}-zzqx.{domain}" for n in range(3)]
    results = await asyncio.gather(*(_resolve(p, sem) for p in probes))
    ips: set[str] = set()
    for _, addrs in results:
        ips.update(addrs)
    return ips


@skill(
    name="dns_brute",
    description=(
        "Brute-force subdomains by resolving a wordlist (and mutations of any KNOWN subdomains) "
        "against a domain — finds live DNS assets the passive sources (subfinder/crt.sh) miss. "
        "Confirmed-only: a hit must actually resolve, and wildcard/catch-all DNS is detected and "
        "filtered so it can't invent phantom subdomains. Pass known='a.x.com,b.x.com' to also "
        "permute env variants (dev-, staging-, -bak…). Read-only DNS recon. Authorized only."
    ),
    auto_confirm=True,  # read-only DNS resolution, same category as dns_lookup
    timeout_s=120,
)
async def dns_brute(domain: str, words: str = "", known: str = "",
                    permute: bool = True) -> dict[str, Any]:
    """Resolve candidate subdomains against `domain`; return the ones that exist.

    domain: apex domain (example.com); a URL/host is reduced to its bare domain.
    words:  extra labels to try, comma/space-separated (merged with the built-in wordlist).
    known:  known subdomains (comma/space-separated) to permute env variants from.
    permute: generate env-variant mutations of the known subs (default True).
    """
    domain = _bare(domain)
    if not domain or "." not in domain:
        return {"error": "give an apex domain like example.com"}
    word_list = [w for w in words.replace(",", " ").split() if w]
    known_list = [k for k in known.replace(",", " ").split() if k]
    candidates = _candidates(domain, word_list, known_list, permute)

    sem = asyncio.Semaphore(_CONCURRENCY)
    wildcard = await _wildcard_ips(domain, sem)
    results = await asyncio.gather(*(_resolve(h, sem) for h in candidates))

    found: list[dict[str, Any]] = []
    for host, addrs in results:
        real = [a for a in addrs if a not in wildcard]
        if real:
            found.append({"host": host, "ips": real})
    found.sort(key=lambda r: r["host"])
    out: dict[str, Any] = {
        "domain": domain,
        "tried": len(candidates),
        "count": len(found),
        "subdomains": found,
    }
    if wildcard:
        out["wildcard"] = sorted(wildcard)
        out["note"] = "wildcard DNS detected — catch-all IPs filtered out of the results"
    return out
