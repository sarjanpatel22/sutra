# Authentication, Session & Business Logic — the manual craft scanners can't do

These need *understanding the app's intent*, so they're rarely duped and often well-paid. Pure
human edge. Authorized targets, your own accounts only.

## Authentication flaws
- **Password reset:** token leaks in response/Referer, reused/non-expiring/guessable tokens, host
  header poisoning (`Host: attacker` → reset link points to you), reset for *another* email by
  swapping the param, no rate limit on the code → brute the 6-digit OTP.
- **Registration/verification:** register an already-taken email (case/unicode variant) to take it
  over; skip email verification; pre-verify via mass-assignment.
- **2FA/MFA:** bypass by removing the `otp` param, brute (no rate limit), reuse a code, race it,
  or reach the post-2FA endpoint directly (force-browse). Backup-code or "remember device" flaws.
- **OAuth/SSO:** `redirect_uri` not strictly validated → steal the code/token; `state` missing →
  CSRF; account linking confusion → takeover; leaking `client_secret`.
- **JWT:** `alg:none`, weak HMAC secret (crack it), `kid` injection/path traversal, `jku`/`x5u`
  pointing to your key, not checking expiry/signature, swapping `sub` to another user.

## Session flaws
Session fixation (session id not rotated on login), tokens not invalidated on logout/password
change, long-lived tokens, predictable ids, session in URL, missing `HttpOnly`/`Secure`/`SameSite`
that enables a real attack (pair with XSS/CSRF for impact).

## Business logic — where the creative money is
A scanner has no concept of "should." You do. Hunt:
- **Numeric/price manipulation:** negative quantity/amount, decimal/overflow, currency swap, apply
  a coupon N times, change price client-side and see if the server trusts it.
- **Race conditions (TOCTOU):** fire the same request in parallel — redeem one coupon/gift card
  many times, double-spend balance, over-withdraw, bypass a "once only" limit. (The classic
  high-payout logic bug.)
- **Workflow bypass:** skip steps in a multi-stage flow (checkout→pay→ship: jump straight to ship),
  reach step 3 without 1–2, replay a one-time action.
- **Quota/limit bypass:** exceed plan limits, free-tier→paid features via a flag, invite/referral
  abuse, trial reset.
- **Parameter/state tampering:** flip `isPaid`, `status:approved`, `discount`, `accountId` you
  shouldn't control.

## How to CONFIRM + ESCALATE
Demonstrate the *unintended outcome* with your own accounts: "ordered $500 of goods for $0",
"redeemed a $50 gift card 10× = $500 (parallel requests attached)", "took over account X via reset
host-header". Tie it to **money or data**: financial loss, account takeover, or other users' data —
that's what sets the bounty. Chain where possible (logic flaw → ATO → admin).

## Why this is the long game
This is exactly what `[[bb_access_control]]` and `[[bb_server_side]]` can't be fully automated to
do — it rewards a sharp operator who understands the app. Sutra's job here is to **coach**: suggest
which of these to test for a given feature, and how to prove + escalate it. That's the human+AI loop
that actually earns — see [[project_sutra_bugbounty_money]].
