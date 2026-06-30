"""Open cloud-storage bucket discovery — finding world-readable S3 / GCS buckets for a target.

check_buckets generates likely bucket names from a domain (base + common affixes like -backup,
-assets, -uploads) and probes AWS S3 and Google Cloud Storage for each. A bucket that returns a
public listing (<ListBucketResult>) is a confirmed exposure — often dumping source, backups,
credentials or user data (a classic, high-severity payable find). Buckets that exist but deny access
are reported separately as leads to check manually. Active (enumerates guessed bucket names against
the cloud providers) so it is gated. Pure-stdlib (urllib), CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from friday.skills.registry import skill

# Common bucket-name affixes — appended/prepended to the base name.
_AFFIXES = (
    "", "-backup", "-backups", "-assets", "-static", "-media", "-images", "-uploads", "-files",
    "-data", "-dev", "-staging", "-prod", "-public", "-private", "-logs", "-db", "-cdn", "-storage",
    "-web", "-app", "-test", "-archive",
)
_PREFIXES = ("backup-", "assets-", "static-", "dev-", "prod-")
# (provider, URL template). {n} = bucket name.
_PROVIDERS = (
    ("S3", "http://{n}.s3.amazonaws.com/"),
    ("GCS", "https://storage.googleapis.com/{n}/"),
)
_LISTING = re.compile(r"<ListBucketResult|<\?xml[^>]*>\s*<ListBucketResult", re.I)
_DENIED = re.compile(r"AccessDenied|<Code>AccessDenied|PermissionDenied|signature", re.I)


def _candidates(domain: str, extra: str) -> list[str]:
    bare = (domain or "").strip().lower()
    if "://" in bare:
        bare = urlparse(bare).hostname or bare
    bare = bare.split("/")[0].split(":")[0].lstrip("*.")
    labels = bare.split(".")
    bases = {bare.replace(".", "-"), bare.replace(".", ""), labels[0] if labels else bare}
    bases |= {e.strip().lower() for e in extra.split(",") if e.strip()}
    names: list[str] = []
    for base in bases:
        if not base:
            continue
        for aff in _AFFIXES:
            names.append(base + aff)
        for pre in _PREFIXES:
            names.append(pre + base)
    # S3 names: lowercase, 3-63 chars, no underscores — keep the valid ones, deduped
    valid = [n for n in dict.fromkeys(names) if 3 <= len(n) <= 63 and "_" not in n]
    return valid


def _probe(url: str) -> tuple[int, str, str | None]:
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-buckets/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 — cloud provider, not target
            return r.status, r.read(20_000).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read(20_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body, None
    except (urllib.error.URLError, OSError) as e:
        return 0, "", str(e)[:120]


@skill(
    name="check_buckets",
    description=(
        "Find open cloud-storage buckets for a target. Generates likely bucket names from a domain "
        "(base + affixes like -backup/-assets/-uploads) and probes AWS S3 and Google Cloud "
        "Storage. A public listing (<ListBucketResult>) is a confirmed exposure — often source, "
        "backups, credentials or user data (high severity). Buckets that exist but deny access are "
        "returned as manual leads. Pass extra= for custom names. Active (enumerates guessed names "
        "against the cloud providers) — authorized engagements only."
    ),
    auto_confirm=False,  # enumerates guessed bucket names — gated
    timeout_s=120,
)
async def check_buckets(domain: str, extra: str = "") -> dict[str, Any]:
    """Probe for world-readable S3 / GCS buckets belonging to a target.

    domain: the target domain — bucket-name candidates are derived from it.
    extra: optional comma-separated extra bucket base names to try.
    """
    if not (domain or "").strip() and not extra.strip():
        return {"error": "no domain to derive bucket names from"}
    names = _candidates(domain, extra)
    if not names:
        return {"error": "could not derive any valid bucket names"}

    def _run() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        public: list[dict[str, Any]] = []
        private: list[dict[str, Any]] = []
        for name in names:
            for provider, tmpl in _PROVIDERS:
                url = tmpl.format(n=name)
                status, body, err = _probe(url)
                if err is not None:
                    continue
                if status == 200 and _LISTING.search(body):
                    public.append({"provider": provider, "bucket": name, "url": url})
                elif status in (401, 403) or _DENIED.search(body):
                    private.append({"provider": provider, "bucket": name, "url": url})
        return public, private

    public, private = await asyncio.to_thread(_run)

    findings = [
        {
            "id": "open-bucket",
            "severity": "high",
            "matched_at": b["url"],
            "name": f"World-readable {b['provider']} bucket '{b['bucket']}'",
            "description": (
                f"The {b['provider']} bucket '{b['bucket']}' returns a public listing — its "
                "contents are world-readable. Review it for source, backups, credentials or user "
                "data, and report the specific sensitive objects (don't exfiltrate beyond proof)."
            ),
            "curl": f"curl -s '{b['url']}'",
        }
        for b in public
    ]

    return {
        "domain": (domain or "").strip().lower(),
        "checked": len(names),
        "public": public,
        "exists_private": private,  # leads: bucket exists but denied — try other perms manually
        "findings": findings,
        "vulnerable": bool(findings),
        "note": (
            f"{len(public)} public, {len(private)} exist-but-private of {len(names)} names probed. "
            "Public buckets are confirmed exposures — verify the sensitive contents. Private ones "
            "are leads (try ListObjects/GetObject with other auth, check for write access)."
        ),
    }
