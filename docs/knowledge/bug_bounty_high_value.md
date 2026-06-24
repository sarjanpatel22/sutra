# High-Value Bug Bounty — What Actually Pays, and How to Escalate

Companion to the methodology base. Honest economics + the finding classes worth chasing with an
automated recon→scan machine, and how to turn a finding into a bigger payout. Authorized programs
only; read each program's scope and rules first.

---

## 1. The honest economics

Most bounties are small ($50–$500). The huge ones ($10k+) go to expert humans doing **manual,
creative exploitation** — chained bugs, business logic, custom exploits — on the most-hunted
programs, where automated findings get **duplicated** instantly. Chasing the single mega-bounty
with a scanner is aiming at your weakest spot against the most competition.

**The realistic strategy: volume of valid, low-competition findings, then escalate impact.**
- Automate recon + scanning so you cover *many* assets cheaply.
- Hunt the assets others ignore — deep subdomains, forgotten/legacy hosts, newly-acquired
  companies in scope. That's where the un-duped findings live.
- Prefer programs with **broad scope** and **less saturation** (newer or private/invite programs)
  over the famous ones everyone scans.
- Submit clean, verified reports fast; protect your reputation by never submitting unverified or
  out-of-scope noise.

You earn your way up to the big bounties by banking small valid ones first — generality and skill
are earned, not configured.

---

## 2. The high-value classes that are also AUTOMATABLE

These pay real money (often hundreds to low-thousands) *and* a recon→nuclei machine can surface
them — the sweet spot:

- **Subdomain takeover** — a dangling CNAME pointing to a deprovisioned service (S3, GitHub Pages,
  Azure, Heroku, etc.) you can register and claim. High impact (full control of a company
  subdomain), often well paid, and detectable. *Escalate:* actually claim it (a benign proof
  page), show you control content served from their domain.
- **Exposed secrets / config / VCS** — `.env`, `.git/`, `config.php.bak`, exposed API keys, cloud
  credentials, `.aws/credentials`, CI tokens, `docker-compose` with secrets. *Escalate:* show the
  secret is live and what it unlocks (an API call that authenticates, a bucket you can list) —
  without abusing it.
- **Default / weak credentials** — admin panels, dashboards, databases, devices with
  default/guessable logins. *Escalate:* show the access level (admin → data, config, RCE path).
- **Known CVEs (n-day) on neglected assets** — an outdated, vulnerable component (especially on a
  forgotten subdomain). *Escalate:* confirm the version is exploitable here, demonstrate the
  concrete effect safely.
- **SSRF** — escalate to **cloud metadata** (`169.254.169.254`) → credentials → larger impact, or
  to reach internal services. SSRF that just hits an external URL is low; SSRF to internal/cloud is
  high.
- **Exposed admin/internal panels & info disclosure with real impact** — an exposed dashboard,
  internal API, or data leak. *Escalate:* show the sensitive data or function it exposes.

---

## 3. Impact is the payout — how to escalate

A finding's bounty tracks its **demonstrated impact**, not its class. For every finding, ask "so
what?" and prove the worst *safe* consequence:

- exposed `/metrics` or debug page → *what sensitive internal data does it leak?*
- subdomain takeover → *serve a benign proof page from their domain*
- default creds → *what does that account actually let you do?*
- SSRF → *can it reach cloud metadata / internal services?*
- info disclosure → *whose data, how much, how sensitive?*

Chain where you can (open redirect + OAuth → token theft; SSRF → metadata creds → account
takeover). Never exfiltrate real user data or run destructive actions — prove access with a benign
marker. A report that clearly demonstrates real impact gets triaged fast and paid well; a report
that just says "this endpoint is exposed" without impact gets closed as informational.

---

## 4. Targeting (what the machine should hunt)

Point the scanner at the **payable** template classes, not fingerprint noise:
`takeover`, `exposure`, `default-login`, `cve`, `config`, `backup`, `panel`. Then a human triages
the medium+ findings, confirms impact, and submits. (This is the `high-value` profile of
`sutra hunt`.)

---

_Authorized targets only. Stay in scope, minimise impact, verify before you submit, and report the
impact — that's what gets paid._
