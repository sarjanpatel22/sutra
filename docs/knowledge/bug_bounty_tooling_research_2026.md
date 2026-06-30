# Bug-Bounty Tooling Research & Gap Analysis (2026)

Web research into the current bug-bounty methodology/toolset, mapped against Sutra's existing
skills to decide what to build next. Sources at the bottom.

## 1. What the market pays for (2025–2026)

- **Access-control flaws are #1 and rising** — IDOR / Broken Object-Level Auth (BOLA) / Broken
  Access Control are consistently the **highest-paying** *and* the **hardest to automate** (they
  need context). IDOR typically pays **$1k–$5k**; a GraphQL introspection + batching + IDOR chain
  on a fintech paid **$12,500** (CVSS 9.1).
- **APIs are where the bugs are** — IDOR/BOLA now hide in REST and **GraphQL** APIs (GraphQL has no
  built-in access control; resolvers are written by hand → frequent authz bugs).
- **Exposed secrets via GitHub/source** remain a steady, very findable payout.
- **AI vulns exploding** — AI bug reports +200%, **prompt-injection +540%** (emerging, niche, needs
  a target AI app).
- Scale: HackerOne paid **$81M/yr**; Google **$17M**, Microsoft **$17M**, Meta **$4M** in 2025.

**Takeaway:** the money is in **access-control + APIs (IDOR/BOLA/GraphQL)** and **exposed secrets** —
exactly the classes a scanner can *assist* but not fully own. Sutra's "tools find / human verifies"
model fits: automate discovery + the deterministic checks, hand the operator the context calls.

## 2. Standard methodology phases + canonical tools

| Phase | Canonical tools | Sutra status |
|---|---|---|
| Passive subdomain enum | subfinder, amass, **crt.sh**, github-search | ✅ `find_subdomains`, `crtsh_subdomains`, `recon_assets` |
| Active subdomain enum | massdns, shuffledns, dnsx, puredns, dnsgen | ⚠️ partial (`dns_lookup`, `port_scan`); **no DNS brute/permute** |
| URL & archive discovery | gau, **waybackurls**, katana, hakrawler, paramspider | ✅ `wayback_urls`, `crawl`, `find_params` |
| HTTP probing | httpx, httprobe | ✅ (inside `sutra hunt` live-host probe) |
| JS analysis | **LinkFinder**, subjs, JSFinder, gf | ✅ `extract_js_endpoints`, `scan_secrets` |
| Content/param discovery | **arjun**, feroxbuster, dirsearch, **kiterunner (API)** | ✅ `find_params`, `web_enum`; ❌ **API-route discovery** |
| Cloud/infra | cloudenum, s3scanner, **gitdorker/trufflehog**, shodan, censys | ✅ `check_buckets`; ❌ **GitHub secret recon**, ❌ shodan/censys (key-gated) |
| Vuln scanning | **nuclei** (+ DAST) | ✅ `nuclei_scan` (+ `--active`) |
| Per-class testing | see §3 | mostly ✅ |
| Reporting | manual / custom | ✅ `draft_bounty_report` + `coach` |

## 3. Per-vulnerability-class coverage

| Class | Sutra | Notes |
|---|---|---|
| SQLi | ✅ `check_sqli` | error + boolean |
| SSRF | ✅ `check_ssrf` | metadata/file signatures (blind/OOB = manual) |
| LFI / path traversal | ✅ `check_path_traversal` | |
| XXE | ✅ `check_xxe` | |
| CRLF / response splitting | ✅ `check_crlf` | |
| Open redirect | ✅ `check_open_redirect` | |
| Host-header injection | ✅ `check_host_header` | |
| SSTI | ✅ `probe_ssti` | |
| CORS | ✅ `check_cors` | |
| Reflected XSS | ✅ `test_reflected_xss` | ❌ **stored/DOM XSS** |
| **IDOR / BAC** | ✅ `idor_check` | the top payable; mostly manual+context |
| GraphQL | ✅ `graphql_introspect` | ❌ **batching abuse**, ❌ **field-suggestion recovery (introspection-off)** |
| JWT | ✅ `analyze_jwt` | |
| Secrets | ✅ `scan_secrets` | page/JS only; ❌ **GitHub/source recon** |
| Subdomain takeover | ✅ `check_takeover` | |
| Email spoofing | ✅ `check_email_spoofing` | |
| **CSRF** | ❌ | gap |
| Open buckets | ✅ `check_buckets` | |

## 4. Prioritized gaps to build (by payable value × automatability × determinism)

1. **GitHub secret recon** (`github_dork` / `scan_github`) — search an org/user's public repos for
   leaked keys/creds/endpoints (regex on real code = deterministic; `gh` token already present).
   Steady high payout, scanner-adjacent, fits tools-find. **#1.**
2. **API route / schema discovery** (`api_discover`) — find Swagger/OpenAPI/`graphql`/`.well-known`
   specs → enumerate every API route. Feeds the IDOR/BOLA money. Mostly easy GETs. **#2.**
3. **GraphQL deeper abuse** (`graphql_attack`) — alias-based **batching** (rate-limit/IDOR brute)
   and **field-suggestion** schema recovery when introspection is disabled (Clairvoyance-style).
   Directly the $12.5k chain shape. **#3.**
4. **CSRF detection** (`check_csrf`) — state-changing forms/endpoints missing CSRF tokens and weak
   SameSite cookies. Deterministic, common. **#4.**
5. **DNS brute/permutation** (`dns_bruteforce`) — wordlist + permutation resolve to widen assets
   beyond passive sources. Medium. **#5.**
6. **DOM/stored XSS** — harder; DOM source→sink analysis in JS. Later.
7. **Shodan/Censys, prompt-injection testing** — key-gated / niche; park.

**Discipline holds:** each new skill stays pure-stdlib where possible, `$0` to run,
deterministic/confirmed-only, hermetic-tested, gated when active, wired into `sutra hunt`.

## Sources
- Bug Bounty Hunting Methodology 2025 (amrelsagaei) — github.com/amrelsagaei/Bug-Bounty-Hunting-Methodology-2025
- Intigriti — Recon for bug bounty: 8 essential tools
- PortSwigger Web Security Academy — GraphQL; YesWeHack — Hacking GraphQL endpoints
- InfoSec Writeups — GraphQL IDOR + batching ($12.5k); ProwlSec — Top bugs that paid in 2025
- BleepingComputer — HackerOne paid $81M; CSO Online — top programs 2025; Google/Microsoft/Meta 2025 payout reports
