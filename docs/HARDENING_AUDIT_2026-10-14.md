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

**Detection-logic review (spot-checks, sound).** reflected_xss (raw `<svg>` vs encoded
`&lt;svg&gt;` — correct, conservative) and open_redirect (reserved `.example` canary +
precise host check) reviewed for false-positive/negative traps: both correct. Consistent
with the type pass — runtime correctness is in good shape; this review is more confirmation
than bug-hunting.

### Remaining
- Continue the behavioral review across the rest of the 82 skills (recon/passive cluster,
  desktop/vision cluster, agent/meta cluster) at the same bar.
