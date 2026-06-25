# Client-Side — XSS, CSRF, CORS, Clickjacking, postMessage, Prototype Pollution

High-volume, often-paid, and partly findable by hand where scanners produce only noise (a scanner
flags reflected input; it rarely proves *impact*). Authorized targets, your own accounts, benign
PoCs (an `alert(document.domain)` or a marker — never real session theft from real users).

## XSS (Cross-Site Scripting)
**Types:** reflected (input echoed in the response), stored (persisted then served — highest),
DOM (sink in JS: `innerHTML`, `eval`, `document.write`, `location`, jQuery `$()`).
**Find:** reflect a unique marker (`zqx123`), see where/how it lands (HTML body, attribute, JS
string, URL, JSON), then break out of that context: `"><svg onload=alert(1)>`, attribute
`" autofocus onfocus=alert(1) x="`, JS-string `'-alert(1)-'`, URL `javascript:`. Test stored fields
(name, bio, comments, filenames, support tickets, anything an *admin* later views).
**Confirm:** real execution — `alert(document.domain)` proving it runs on the *target's* origin.
**Escalate (= the bounty):**
- **Mitigated by CSP** → medium; **CSP-bypassing or no CSP** → high. Look for CSP gaps (`unsafe-inline`,
  wildcard, JSONP endpoints, trusted-but-controllable hosts).
- Stored XSS that fires for **other users/admins** → session/token theft → **account takeover**, or
  perform privileged actions as them. Chain to an admin → site compromise.
- Steal CSRF tokens, make API calls as the victim, exfiltrate page data. Show the *worst safe* PoC.

## CSRF (Cross-Site Request Forgery)
**Find:** state-changing requests (POST/PUT/DELETE) that rely only on cookies — no CSRF token, token
not validated, token swappable, or `SameSite` not enforced. **Confirm:** build an HTML page that
auto-submits the request; from a different origin it succeeds while logged in.
**Escalate:** CSRF on email/password change → **ATO**; on a money/transfer action → financial.
GET-based state change = trivially exploitable. Weak `SameSite=None` + no token is the sweet spot.

## CORS misconfiguration
**Find:** send `Origin: https://evil.com`; if the response reflects
`Access-Control-Allow-Origin: https://evil.com` **with** `Access-Control-Allow-Credentials: true`,
any site can read the victim's authenticated responses. Also test `Origin: null`, subdomain
reflection, suffix/prefix matching (`target.com.evil.com`). **Escalate:** read PII/tokens cross-origin.

## Clickjacking
Missing `X-Frame-Options`/`frame-ancestors` on a **sensitive state-changing** page → frame it and
trick a click. Only valuable with a real sensitive action (delete account, change settings); a
framed marketing page is informational.

## postMessage & Prototype Pollution
- **postMessage:** a listener that doesn't check `event.origin` and sinks `event.data` into the DOM →
  cross-origin XSS/data theft.
- **Prototype pollution:** `__proto__`/`constructor.prototype` in JSON/params → polluted properties
  → DOM XSS or gadget-chain → sometimes RCE in Node back-ends. High-skill, well-paid, rarely duped.

## Triage note
Self-XSS (only you can trigger on yourself) is **not** eligible — needs a delivery vector to another
user. Always demonstrate cross-user or privileged impact, not just "input reflects".
