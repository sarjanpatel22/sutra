# Server-Side High-Impact — SSRF, Injection (SQLi / Command / SSTI), XXE

The classes that go **critical** ($1k–$10k+) because they reach the server/infra. Test safely on
authorized targets — these can cause real damage; prove with benign markers, never destructive
payloads or real data exfiltration.

## SSRF (Server-Side Request Forgery) — the cloud-takeover bug
**What:** you make the *server* fetch a URL you control/choose. Low impact if it only hits external
URLs; **critical** if it reaches internal/cloud.
**Find:** any feature where the server fetches a URL — webhooks, "import from URL", PDF/HTML
renderers, image/avatar-by-URL, link previews/unfurlers, SSO/OIDC `redirect_uri`/metadata, XML
parsers, `url=`/`callback=`/`dest=`/`feed=` params, file-upload that follows references.
**Confirm (out-of-band):** point it at a collaborator/`webhook.site` URL — a DNS/HTTP hit from the
*target's* IP proves it. Then try internal: `http://127.0.0.1`, `http://localhost:<ports>`,
`http://169.254.169.254` (cloud metadata).
**Escalate (= the money):**
- Cloud metadata → IAM creds: AWS `http://169.254.169.254/latest/meta-data/iam/security-credentials/`,
  GCP/Azure equivalents (need `Metadata-Flavor: Google` / `Metadata: true`). Creds → account compromise.
- Reach internal admin panels / databases / k8s API not exposed externally.
- Bypass filters: redirects, `http://2130706433/` (decimal IP), `[::1]`, DNS rebinding,
  `http://burp@internal`, alternate schemes (`file://`, `gopher://`, `dict://`).

## SQL Injection
**Find:** any input reaching a query — search, filters, sort, ids, login, JSON fields, headers.
**Confirm safely:** boolean (`' AND 1=1`‑vs‑`1=2` → different responses), time-based
(`'||pg_sleep(5)--` → response delays), error-based (DB error leaks). **Don't** dump data; prove
the injection, then note what's reachable. (sqlmap only with explicit program permission + your go.)
**Escalate:** read other tables (creds, PII) → auth bypass → in some DBs file read/write or RCE
(`xp_cmdshell`, `LOAD_FILE`, stacked queries).

## Command Injection
**Find:** inputs feeding shell calls — ping/DNS tools, file converters, export, filename handling,
git/url ops. **Confirm:** OOB DNS/HTTP via `; curl http://collab`, or time `; sleep 5`. **Escalate:**
RCE → read secrets/`.env` → pivot. (Benign proof only — `id`, a collaborator ping; never destructive.)

## SSTI (Server-Side Template Injection)
**Find:** user input rendered in a template (emails, names, custom pages) — probe `${7*7}`,
`{{7*7}}`, `<%= 7*7 %>`; a `49` means evaluation. **Escalate:** engine-specific → file read → RCE.

## XXE
**Find:** anything parsing XML/SVG/DOCX/SAML. **Confirm:** external entity → OOB callback. **Escalate:**
file read (`/etc/passwd`, app config/secrets), SSRF via entity, sometimes RCE.

## Escalation principle
Always answer "what does this reach?" — an SSRF that only fetches example.com is low; the *same bug*
reaching metadata creds is critical. The payout tracks the **demonstrated reach**, proven benignly.
