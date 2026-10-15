# Sutra hardening audit — 2026-10-14 (overnight pass)

Goal: strengthen the whole codebase to production quality — not cosmetic. This is an
**honest ledger**: what was audited, what was fixed, and exactly what remains. It is a
large, multi-session effort; this file is the checkpoint so it can continue cleanly.

Branch: `harden-sutra` (stacked on the NIM-router work). **Not pushed** — awaiting the go.

## Baseline (start of pass)
- 82 skill modules, 159 Python files, ~31.5K LOC, 148 test files.
- Full suite: **1862 passing**, ruff clean repo-wide.
- `mypy --strict` (configured in pyproject but **CI never ran it**, so it had rotted):
  **~160 errors across ~52 files.**
- 9 skills with no dedicated test file: audit, calc, clock, code_exec, delegate,
  desktop, file_ops, preferences, system_info.
- Convention check: all skills already have `from __future__ import annotations` ✓.

## Done this pass (6 hardening commits, all green)
1. **Bug-prone type fixes** (`51b7864`): agent.py per-turn `stats` dict[str,object] →
   typed `_TurnStats` dataclass (14 errors, the counters were all `object`); dom_xss
   stopped re-running a regex and calling `.group()` on a maybe-None; waf_detect &
   rerank rewrote the cryptic `set.add()`-in-a-comprehension dedup as plain loops;
   calc stopped reusing one name for binary+unary operators; fleet early-return `res`
   annotation; wakeword empty-env guard.
2. **file_ops** (`1c77964`): `list_directory` was declared `-> dict[str, list[str]]`
   but returned a `str` for `path` and a list-valued error (every other skill returns a
   string). Fixed the type + made the error a string. **Added tests/test_file_ops.py
   (11 tests)** incl. the home-dir containment guard.
3. **Core type-arg sweep** (`a540d0e`): filled missing generics (`dict`→`dict[str, Any]`
   etc.) in planner, subagents, study, skillsmith, skill_profile, geo, graphql.
4. **Tests for security-sensitive untested skills** (`696f5e5`):
   **tests/test_code_exec.py (8)** — run_python: empty-guard, stdout, nonzero-exit,
   timeout-kill, timeout-clamp, isolated-mode (`-I`) proof, truncation.
   **tests/test_system_info.py (5)** — stats keys, battery present/absent, run_command
   execution. (The safety-critical `is_catastrophic` + run_command refusal are already
   well-covered in test_autonomous.py.)
5. **cli** (`f487911`): renamed a reused subprocess var so the ranked-rows loop
   types cleanly (not a live bug — name reuse confused mypy).
6. **report dicts + stale ignores** (`a97628a`): mcp_audit.audit_report/rank_batch
   `dict[str, object]`→`dict[str, Any]` (cleared 4 downstream cli errors too);
   train._serialize_tool_calls `tcs: object`→`Any`; removed 7 stale `type: ignore`
   comments and fixed audio.py's wrong ignore code.
7. **collectors** (`f423c98`): typed `_RUN` as `dict[str, Any]` so `**_RUN` matches
   subprocess.run's overloads (4 call-overload errors; valid kwargs at runtime).
8. **remaining untyped helpers** (`889eec3`): annotated the `_NoRedirect.redirect_request`
   overrides (open_redirect/host_header/crlf) and web_scrape._soup.

Result: full suite **1886 passing** (+24 tests), ruff clean, **mypy ~160 → 71** (56% down).

## Update — second pass (same day): mypy --strict driven to ZERO + wired into CI
Continued in focused, per-area commits (each green, ruff + targeted tests before commit):
- voice package (pipeline.py 13 + vad/tts): lazy STT/TTS getters typed via TYPE_CHECKING,
  Iterator[str] generators, Kokoro voice resolved to guaranteed str.
- recon + recon_assets (11): stopped reusing `pk` for both an XML Element and the parsed
  dict; str()-ed the getaddrinfo sockaddr; widened the gather guard to BaseException.
- cli.py (6) + injection_guard.verdict: widened the too-tight `dict[str, object]` return.
- gui + vision (12): Popen[bytes]; a real def for the agent observe hook; **CardsWidget
  validator was silently dropping CardItem instances — fixed**; ImageMagick-binary narrowing.
- final 27 across skills/core/memory/rag/ambient/watch: two real smells fixed (forget()
  reused the deleted `except as e` name; agentic_target coerces server_address to str/int),
  root-caused the watch model errors to `constants.VISION_MODEL` (the
  `getenv(x) or getenv(y, default)` idiom types as str|None under this mypy — rewrote as
  `... or ... or "literal"`); shodan now guards `ip is None`; the rest mechanical.

**`mypy --strict` now reports `Success: no issues found in 159 source files` (0 errors).**
Verified against a *simulated CI install* (optional voice/memory/action/scrape deps forced
to Any via follow_imports=skip) — also 0, so the one graphiti-boundary cast is env-safe
(used `cast(Any, llm)`, not a bare `type: ignore`, so it can't become an "unused ignore"
in CI where graphiti is absent).

**Wired into CI** (`.github/workflows/ci.yml`): new `Mypy (strict types)` step between Ruff
and the gate-audit; job renamed `ruff · mypy · gate-audit · tests`. The rot can't recur.

**Lesson (caught by the first CI run):** CI installs the latest `mypy>=1.10`, whose newer
bundled typeshed types `socket.getaddrinfo`'s sockaddr as `str | int` — so `dns_brute.py`
had one extra set-comprehension error that an older local mypy (2.1.0) didn't flag. Fixed
the same way as recon.py (`str(i[4][0])`) and **upgraded the local venv to the CI mypy
version** so the gate is reproducible. Always run the same mypy version locally as CI.

## Key finding
Almost every type error cleared was **loose typing / variable-name reuse, not a live
bug** — reassuring about runtime correctness. The handful of genuine issues (dom_xss
maybe-None `.group`, dedup idioms, calc operator confusion, the CardsWidget validator
dropping model instances, the `except as e` reuse) were all fixed or were safe-at-runtime.

## Remaining (for the next session)
### Skills without dedicated tests — CLOSED
Added `test_calc.py` / `test_clock.py` / `test_preferences.py` / `test_desktop.py` (+51
tests). `audit` + `delegate` turned out to be already exercised by
`test_confirmation_audit.py` (the earlier "6 untested" list was by filename, not
coverage). So every skill now has coverage, directly or via a shared suite.

## Behavioral deep-review (started — the big item, in progress)
Going skill-by-skill, enumerating the failure CLASS adversarially up front (not patching
single examples), each fix committed-green.

**Cluster 1 — crash-safety / never-raise contract (SYSTEMIC, done).** Fuzzing check_sqli
and its siblings with malformed URLs (unclosed IPv6 `http://[::1`, control chars,
non-latin host, non-numeric port) showed ~29 active probes RAISED instead of returning an
error dict — urllib raises `ValueError` / `http.client.InvalidURL` (an HTTPException, NOT
a ValueError) during URL parse/fetch, escaping the narrow `except (URLError, OSError)`.
Fixed at the root: the `@skill` decorator now wraps every skill so a body exception
returns `{"error": …}` — the never-raise contract is ENFORCED for all 126 skills at one
point. Argument-binding errors (invented kwargs) are bound before the guard so they still
reach the agent's `expected_params` correction; `CancelledError` (BaseException) is not
swallowed. Also broadened the 28 probe fetch helpers to catch ValueError + HTTPException
so the common malformed-URL case gives the specific "could not reach X". Adversarial
harness: 126 hostile calls → 0 raises (was 29). +4 contract tests.

**Cluster 2 — resource bounds (in progress).** `web_recon._wildcard_len` buffered a whole
response via `r.read()` just to measure length (OOM on a hostile body, in the unattended
`sutra hunt` path) → capped at 2MB. `param_discovery` built its candidate set from an
unbounded caller `extra` CSV → capped at 1000.

**Detection-logic review.** reflected_xss (raw `<svg>` vs encoded `&lt;svg&gt;` — correct,
conservative), open_redirect (reserved `.example` canary + precise host check), csp_analyze
(token-based directive/source parsing), and cors (tests exact origin reflection, correctly
AVOIDS the `*`+credentials false positive) all reviewed for false-positive/negative traps:
sound. **One real FP fixed — email_security:** `"p=none" in dmarc` also matched `sp=none`
(subdomain policy), so a strong `p=reject; sp=none` was reported "monitor-only / spoofable".
Fixed with exact DMARC-tag parsing; also tokenized the SPF `all`-mechanism check (caught a
bare-`all` false negative too). +4 regression tests.

**Desktop/vision cluster.** desktop_control (OCR→click take-over): `_perception_ready` returns
a clean error when tools are absent, `best_match` uses a score threshold with honest no-match
replies, `_llm_pick` degrades to None on any failure, clicks use real OCR coords. Graceful +
crash-safe. No change needed.

**Command-injection audit (clean bill).** All 16 subprocess call sites across the skills use
`create_subprocess_exec` / list-form `subprocess.run`/`Popen` — zero `shell=True`, zero
`os.system`/`os.popen`, zero string-form commands. `create_subprocess_exec` can't invoke a
shell, so user/model input passed as argv can't be shell-injected. The command-execution
surface is injection-safe by construction. No change needed.

### State at this checkpoint
Full suite **1944**, mypy 0, ruff clean, CI green. The high-risk surface — every active/passive
security probe, the command-execution paths, and the desktop take-over — has been reviewed for
the material failure classes (crash-safety, resource bounds, detection false-positives/negatives,
command injection). Every real bug found was fixed; the rest confirmed sound. Consistent with the
type pass: **the codebase is fundamentally solid.**

### Confirmation-level sweep — DONE (lower-risk agent/meta + util skills)
Reviewed the remaining skills by the same failure classes. Two more real issues found and fixed;
one earlier wording corrected; the rest confirmed sound.

**New class found — argv OPTION-injection (a real bug).** Command-injection was clean (no shell),
but argv-form is still unsafe when a model-supplied value lands in an *option-parseable* positional
slot. Confirmed exploitable: `git_diff`/`code_review` with `ref="--output=FILE"` made `git diff`
write to an **arbitrary path** — an unintended file write from an `auto_confirm=True` read (a
prompt-injected model could trigger it silently). Fixed:
- git_diff + code_review: `--end-of-options` before the ref (legit HEAD~1 / main...HEAD unaffected).
- run_tests: `path` passed after `--` (can't be a pytest option).
- recon dns_lookup/whois/searchsploit: reject '-'-leading targets/terms (matches tls_scan's
  pre-existing guard; blocks dig -f / whois -h / searchsploit --nmap). sslscan was already guarded.
- Reviewed-sound (no fix): reversing/disassemble (objdump/file have no write primitive + path is
  is_file()-guarded), open_app (binary from an allowlist, fixed flags), crawl (url via stdin),
  connect_vpn/subfinder (value-of-option). +4 regression tests.

**Correction (truth-discipline).** The earlier command-injection note said "zero string-form
commands / zero shell". That was imprecise by one: `run_command` (system_info.py) DOES use
`create_subprocess_shell` — but that is *by design* (its contract is "run the explicit shell command
the user gives"), and it is gated (auto_confirm=False) + floored by `is_catastrophic`. There is no
*unintended* shell-injection surface, so the substance of the clean bill holds; the blanket wording
did not. Logged here so the record is exact.

**Resource bounds (Cluster 2 completed).** http_request + web_scrape read the whole response into
memory (resp.text/.json) before the max_chars slice → a hostile/unbounded body could OOM
(web_scrape is auto_confirm=True / unattended). Now stream with a 10 MB byte cap; oversize bodies
truncate honestly. check_buckets capped its candidate set at 1000 (same bound as find_params) so a
hostile `extra` can't fan out into thousands of cloud probes. +1 cap test.

**Confirmed sound (no bug).**
- job_hunt: the grounding contract holds — tools load the REAL resume and compute *literal* overlap
  (fit_check guards empty-keywords div-by-zero); the model writes the letter grounded in that text;
  nothing fabricates or sends; save/update have verify-before-claim postchecks.
- The red-team battery (llm_redteam / agentic_redteam / agentic_target) is the most
  truth-disciplined code in the repo: white-box uses planted canary tokens; black-box leak needs
  CROSS-FRAMING AGREEMENT (independent framings converge on the same >=40-char span, refusal
  fragments + attack echoes excluded) and is marked candidate-tier; agentic_target confirms exfil
  by a token arriving at a local collector. No FP/FN inversion.
- auth.analyze_jwt: alg=="none" is exact-match; the weak-secret finding only fires when the HMAC is
  actually cracked (proof, not heuristic); exp is type-checked; wordlist capped at 200k.
- check_buckets detection: a high-sev finding requires status 200 AND the `<ListBucketResult` XML
  root (token-anchored, not a loose substring); the looser `signature` match only feeds leads.
- mcp_audit (bounds limit, guards path), code_review (caps diff + handles model-down),
  prompt_injection (echo-proof reversed-token confirmation), council/delegate (thin core wrappers).
- The genuinely-thin util/vision wrappers (audit, memory, rag, web_search, weather, windows, guide,
  apps, recon_assets, encoding, clock, preferences, calc, file_ops, desktop*, see_screen,
  vision_agent, browse*, firefox_login, web_actions) emit no security verdict (nothing to FP), do no
  unbounded HTTP read, and contain no eval/shell; crash-safety is guaranteed by the registry net.

### Final state
Suite **1948 green**, mypy 0, ruff clean, CI-green. Every skill with verdict / grounding / resource
/ subprocess logic has been reviewed for the material classes; every real bug found was fixed. The
high-risk surface and the lower-risk remainder are both covered. **Sutra's codebase is
fundamentally solid.** Commits this sweep: `6268b9d` (option-injection), `8ecccaa` (resource bounds).
