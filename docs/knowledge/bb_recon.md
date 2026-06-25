# Deep Recon — Finding the Forgotten Assets & Endpoints (where un-duped bugs live)

The bug isn't on the front page everyone scans — it's on the asset nobody remembered. Recon depth
*is* the edge in automated/semi-automated hunting. Stay in scope; this maps the attack surface, the
other docs tell you what to test on it.

## 1. Asset discovery (broad-scope programs only)
- **Subdomains:** passive first (`subfinder`/`find_subdomains`, crt.sh, Amass) → resolve → probe
  live (httpx). The forgotten/legacy/staging/dev hosts are gold (`dev.`, `staging.`, `old.`, `api.`,
  `internal.`, `vpn.`, `admin.`, `test.`). Re-run periodically — new subdomains = fresh, un-hunted.
- **Acquisitions & ASNs:** a program's scope often includes acquired companies + IP ranges others
  ignore. Map ASNs → IP ranges → reverse-DNS → more hosts.
- **Takeover check:** dangling CNAMEs → unclaimed S3/GitHub-Pages/Heroku/Azure → register & claim
  (benign proof). High-value, automatable (`sutra hunt` profile `takeover`).

## 2. Content & endpoint discovery (per live host)
- **Dir/file brute:** `web_enum`/gobuster/ffuf with good wordlists (raft, seclists). On SPAs use
  `--exclude-length` for the app-shell (`sutra hunt --content` handles this). Hunt: `.git/`, `.env`,
  `/backup*`, `*.sql`, `.DS_Store`, `/admin`, `/actuator`, `/swagger`, `/.well-known`, `/ftp`.
- **JS analysis (the highest-ROI move):** download every `*.js`, grep for endpoints/paths, API
  routes, `fetch(`/`axios`, hardcoded **secrets/keys/tokens**, S3 buckets, internal hostnames,
  feature flags. JS reveals the *real* API surface the UI never shows. Tools: LinkFinder,
  `getJS`, manual.
- **Parameter discovery:** Arjun/`x8`/param-miner find hidden params (`debug=`, `admin=`, `test=`,
  `id=`) that unlock IDOR/SSRF/LFI on an endpoint that looked inert.
- **Wayback/archives:** `gau`/`waybackurls`/OTX → old URLs, dead params, removed endpoints that
  still work on the back-end.

## 3. APIs & GraphQL (modern bug goldmine)
- **REST:** find the OpenAPI/Swagger doc (`/swagger.json`, `/api-docs`, `/openapi.json`) → every
  endpoint + params handed to you. Test each for IDOR/BFLA (see [[bb_access_control]]).
- **GraphQL:** try **introspection** (`__schema` query) — if on, you get the entire schema; map
  queries/mutations, then hunt IDOR/BOLA on `node(id:)`, auth gaps on mutations, and batching/alias
  abuse for rate-limit/brute bypass. Look for `/graphql`, `/graphiql`, `/v1/graphql`.

## 4. Tech & version fingerprinting
`whatweb`/Wappalyzer → framework, server, libs, versions → map to known CVEs (`searchsploit`,
`sutra hunt --profile cve`) on the *neglected* hosts (the maintained ones are patched).

## 5. The workflow (tools find → coach guides → you exploit)
1. `sutra hunt <broad-scope-domain> --content --scope ...` → assets + exposures + discovered paths.
2. Triage the report; pick the interesting hosts/endpoints (APIs, admin, forgotten subdomains).
3. For each, ask the coach what to test ([[bb_access_control]] / [[bb_server_side]] /
   [[bb_client_side]] / [[bb_auth_logic]]) — then do the manual craft a scanner can't.
4. Confirm with your own accounts, escalate for impact, write it up ([[bb_high_value]]), submit.

This is lever 2 (targeting) + lever 3 (the coach) working together — the part that finds what the
dupe-crowd misses. See [[project_sutra_bugbounty_money]].
