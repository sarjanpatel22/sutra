# Access Control — IDOR / BOLA / Privilege Escalation (the #1 payable, findable-by-hand class)

Broken access control is consistently the top-paid, most-common bug-bounty class, and a scanner
*cannot* find it (it needs understanding of who-should-access-what). This is where a human + coach
beats automation. Authorized targets only; never access/modify real users' data — prove with two
accounts you control or a benign marker.

## What it is
The app fails to check that the *current* user is allowed to act on the *requested* object or
function. Variants:
- **IDOR / BOLA** (object level): `GET /api/orders/1043` returns *someone else's* order by changing
  the id. The #1 API bug (OWASP API #1).
- **BFLA** (function level): a normal user calls an admin-only endpoint (`POST /api/admin/users`)
  and it works.
- **Mass assignment**: sending extra fields the UI never shows (`{"role":"admin"}`,
  `{"is_verified":true}`) and the server blindly binds them.
- **Privilege escalation**: low-priv → high-priv via any of the above.

## How to FIND it (two accounts = your superpower)
1. Make **two accounts** (A and B). Do an action as A, capture the request (Burp/proxy).
2. **Replay A's request with B's session** (swap cookie/token), keeping A's object id. If B gets
   A's data → IDOR.
3. **Swap the identifier**, keep your own session: numeric ids (1043→1042), UUIDs (try leaked
   ones), usernames, emails, hashes, base64'd ids (decode→change→re-encode).
4. **Hunt id sources**: URLs, JSON bodies, headers (`X-User-Id`), cookies, GraphQL `node(id:)`,
   nested objects, bulk endpoints, export/download links (`/invoice?id=`), websocket messages.
5. **Method & param tricks**: `GET`→`PUT`/`DELETE`, add `?id=` an admin endpoint ignores via UI,
   wrap id in an array `[1042]`, duplicate params `id=mine&id=victim`, path vs body id mismatch.
6. **BFLA**: enumerate admin/internal routes from JS files & API docs, call them as a low-priv user.
7. **Mass assignment**: GET an object, note ALL fields it returns, then PUT/POST those extra fields
   (role, owner, price, status, verified) and see if they stick.

## How to CONFIRM (the proof that gets it paid)
Show **cross-account impact** with accounts you own: "As user B (id 2002) I retrieved user A's full
order incl. name, address, last-4 — request/response attached." A clean two-account PoC, no real
victim, is what triages fast. State exactly which field proves it isn't your own data.

## How to ESCALATE (impact = payout)
- Single record → **enumerate the whole range** (1..N) to show bulk data exposure (PII = higher
  bounty). Show the *count* you could reach; don't actually exfiltrate.
- Read → **write**: if you can also PUT/DELETE another user's object, that's higher.
- IDOR on a role/permission field or mass-assignment `role:admin` → **account takeover / full admin
  = critical**.
- IDOR on password-reset token / email-change / 2FA endpoint → **ATO**.
- Chain: IDOR leaks an API key/token → use it for broader access.

## Dupe / scope notes
Common but *not* auto-found, so less duped than scanner bugs. Always test with your own accounts;
a "self-only" finding (changing your *own* id) is **not** a vuln. Check scope — some programs
exclude low-impact IDOR on non-sensitive objects.
