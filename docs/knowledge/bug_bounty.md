# Bug Bounty & Web Application Security — Methodology Knowledge Base

Distilled methodology for finding, exploiting, and reporting web/app vulnerabilities on authorized
targets (bug bounty programs, your own systems, CTFs). Original distillation of standard field
practice — not reproduced book text. Recommended books/resources are listed at the end; get them
legitimately. Authorized targets only: read the program scope before you touch anything.

---

## 1. The hunter's mindset and workflow

Bug bounty is **recon → understand → find weak assumptions → prove impact → report**. Most bounties
come not from novel exploits but from *enumeration* (finding the forgotten asset) and *business-logic*
flaws (where the app trusts something it shouldn't). Depth on one target beats shallow spraying.

The loop on any target:
1. **Scope** — read the program policy. In-scope domains, excluded paths, allowed test types
   (no DoS, no social engineering, rate limits). Out-of-scope findings waste everyone's time.
2. **Recon / attack-surface mapping** — enumerate everything reachable.
3. **Fingerprint** — identify tech stack, frameworks, versions, third-party components.
4. **Probe each surface** for the vulnerability classes below.
5. **Prove impact** — a real, minimal proof-of-concept; escalate to show severity.
6. **Report** — clear repro, impact, and remediation.

---

## 2. Recon and attack-surface discovery

The single highest-leverage phase. You can't test what you haven't found.

- **Subdomain enumeration**: passive (crt.sh / certificate transparency, `subfinder`, `amass`,
  `assetfinder`, search engines, DNS aggregators) + active brute (`puredns`, wordlists). Resolve
  and probe live ones (`httpx`/`httprobe`).
- **Content / endpoint discovery**: directory and file brute (`ffuf`, `gobuster`, `feroxbuster`)
  with good wordlists (SecLists `raft-*`, `dirsearch`). Parse JS files for hidden endpoints, API
  routes, and keys (`LinkFinder`, manual). Check `robots.txt`, `sitemap.xml`, `.git/`, backups
  (`.bak`, `~`, `.old`, `.swp`), and source maps.
- **Historical data**: the Wayback Machine (`waybackurls`, `gau`) surfaces old endpoints and params.
- **Parameters**: discover hidden GET/POST params (`arjun`, `param-miner`). Unknown params are
  frequent IDOR/injection entry points.
- **Tech fingerprint**: Wappalyzer, response headers (`Server`, `X-Powered-By`), cookies, error
  pages, favicon hash. Map versions to known CVEs (`searchsploit`, vuln DBs).
- **Cloud / infra**: S3/GCS bucket names, exposed dashboards, dangling DNS (subdomain takeover),
  exposed `.env`/config, default-credential admin panels.

Keep notes per asset: tech, auth model, roles, interesting endpoints, and parameters.

---

## 3. The major vulnerability classes — what to look for, how to test

### Broken Access Control / IDOR / BOLA (the #1 bounty source)
The app exposes an object reference (`/api/orders/1043`) and doesn't check *you own it*. Test:
change IDs (sequential, UUIDs you collected, other users'), swap roles, replay a privileged
request as a low-priv user, remove/forge tenant IDs. Vertical (user→admin) and horizontal
(user→other user) escalation. For APIs this is **BOLA** — the top API risk.

### Authentication and session
Weak password reset (host-header poisoning of reset links, leaking tokens, no token expiry,
predictable tokens), OAuth flaws (open `redirect_uri`, missing `state` → CSRF, token leakage via
referrer), JWT issues (`alg:none`, weak HMAC secret, `kid` injection), MFA bypass, session fixation,
remembering tokens after logout/password change.

### Injection
- **SQLi**: error-based, boolean/blind, time-based, UNION. Test every param/header/cookie. `sqlmap`
  to confirm/exploit. Modern frameworks parameterize — look at legacy endpoints, search, sorting,
  and reporting features.
- **Command injection**: input that flows into a shell (ping/lookup/convert features). Blind via
  time delay or OOB (DNS/HTTP callback, e.g. Interactsh).
- **Template injection (SSTI)**: `{{7*7}}` → `49` reveals an engine; escalate to RCE per engine.
- **NoSQL / LDAP / XPath injection** in the relevant backends.

### Cross-Site Scripting (XSS)
- **Reflected**: input echoed into the response unsanitized.
- **Stored**: persisted then rendered (comments, profiles, filenames) — higher impact.
- **DOM**: client-side sink (`innerHTML`, `eval`, `document.write`) fed from a source
  (`location.hash`). Impact: session theft, account takeover, CSP-bypass. Always check context
  (HTML/attribute/JS/URL) and the CSP.

### SSRF (Server-Side Request Forgery)
Make the server request a URL you control or an internal one. Hunt in URL fetchers, webhooks, PDF/
image generators, import-from-URL, and link previews. Escalate to cloud metadata
(`169.254.169.254`), internal services, and port scanning. Try bypasses: alternate IP encodings,
DNS rebinding, redirects, `[::]`, `0.0.0.0`.

### XXE (XML External Entity)
XML parsers that resolve external entities → file read, SSRF, sometimes RCE. Test any XML/SOAP/
SVG/DOCX upload or `Content-Type: application/xml` endpoint with an external entity payload; use
OOB exfiltration when blind.

### Insecure deserialization
Untrusted serialized objects (Java, PHP, Python pickle, .NET, Ruby) → RCE. Spot magic bytes /
framework cookies; tools like `ysoserial`. High severity, often pre-auth.

### File upload / path traversal / LFI-RFI
Upload bypasses (extension, content-type, magic bytes, double extension) → webshell. Path traversal
(`../../etc/passwd`, encoded variants) for arbitrary read/write. LFI → RCE via log poisoning, PHP
wrappers, or session files.

### CSRF
State-changing requests without an anti-CSRF token / `SameSite` cookie. Highest impact on
password/email change and money movement. Check that tokens are actually validated and bound.

### Business-logic flaws (where the real money is)
Things scanners can't find: negative quantities, price/currency manipulation, coupon/discount
stacking, race conditions (parallel requests to double-spend, bypass limits — TOCTOU), skipping
workflow steps, replaying one-time actions, abusing trial/refund flows, IDOR in multi-tenant logic.
Think: "what does the app *assume* I'll do, and what if I don't?"

### API-specific
REST/GraphQL: BOLA, broken function-level auth, mass assignment (send extra fields like
`"isAdmin":true`), excessive data exposure (the API returns more than the UI shows), GraphQL
introspection + batching/DoS, missing rate limits.

### Misconfiguration and exposure
Default creds, debug endpoints, verbose errors/stack traces, directory listing, exposed `.git`/
`.env`/Swagger/actuator, secrets in JS or responses, CORS misconfig (`Access-Control-Allow-Origin`
reflecting the Origin with credentials), missing security headers, open redirects (useful in chains).

### Subdomain takeover
A dangling CNAME pointing to a deprovisioned service (S3, GitHub Pages, Heroku, etc.) you can claim.

---

## 4. From bug to impact

A finding's value = **demonstrated impact**, not the raw class. Always ask "so what?" and chain:
open redirect + OAuth → token theft; SSRF → metadata creds → cloud takeover; stored XSS in admin
view → admin ATO; IDOR on an internal user → mass data exposure. Build a minimal, reliable PoC.
Avoid destructive actions; never pivot beyond scope; never exfiltrate real user data — prove access
with a benign marker.

---

## 5. Reporting (this is what actually gets paid)

A great report: **title** (class + asset), **summary**, **steps to reproduce** (exact requests,
copy-pasteable), **impact** (concrete, business terms), **severity** (CVSS + realistic exploitability),
**PoC** (screenshots/video/curl), **remediation**. Clear, honest, reproducible reports get triaged
fast and rewarded well. Don't overstate severity; don't bury the repro.

---

## 6. Recommended reading and free resources (get these legitimately)

**Free / open (start here):**
- OWASP Web Security Testing Guide (WSTG) and the OWASP Top 10 / API Security Top 10
- PortSwigger Web Security Academy — free, hands-on labs for every class above
- PayloadsAllTheThings (GitHub) — payloads and bypasses per vuln class
- HackerOne Hacktivity & disclosed reports; Bugcrowd VRT; public writeups and `pentesterland`
- HackTricks — methodology reference

**Books (buy/borrow — do not pirate):**
- *The Web Application Hacker's Handbook* — Stuttard & Pinto (the classic)
- *Real-World Bug Hunting* — Peter Yaworski
- *Bug Bounty Bootcamp* — Vickie Li
- *The Hacker Playbook* series — Peter Kim
- *API Security in Action* — Neil Madden

**Practice:** PortSwigger Academy, TryHackMe, HackTheBox, PentesterLab, and live programs once you
have the fundamentals.

---

_Use only on targets you're authorized to test. Stay in scope, minimize impact, report honestly._
