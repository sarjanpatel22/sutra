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

## Key finding
Almost every remaining type error is **loose typing / variable-name reuse, not a live
bug** — which is reassuring about runtime correctness. The genuine latent-bug candidates
found (dom_xss maybe-None `.group`, the dedup idioms, calc operator confusion) were all
either safe-at-runtime or fixed. A few categories below (`arg-type`, `attr-defined`,
`str`) still deserve a line-by-line look for the rare real one.

## Remaining (for the next session)
### mypy --strict: 71 errors in 34 files
By file (top): voice/pipeline.py 13 · skills/recon.py 9 · cli.py 6 · vision/perceive.py 3 ·
gui/server.py 3 · then ~29 files with 1–2 each.
By category: no-any-return 14 · type-arg 12 · no-untyped-call 10 · arg-type 9 ·
no-untyped-def 5 · assignment 5 · str 3 · misc 3.
- `voice/pipeline.py` (13) is the biggest single file left — needs a focused pass
  (mostly return-type annotations + a few no-untyped-call on the pipecat API).
- `arg-type` (9) + `assignment` (5) — review each for a real bug (most are the
  numpy-ndarray / optional-dep boundaries, loose but not live bugs).
- `recon.py` (9): the `cert`/`pk` Element-vs-dict typing needs the function annotated.
- `no-any-return` (14) + `type-arg` (12) + `no-untyped-call` (10) are mostly mechanical.

### Skills still without dedicated tests (6, down from 9)
audit, calc, clock, delegate, desktop, preferences. (calc/clock are nearly trivial;
desktop + delegate are the meaningful ones.)

### Not yet done
- Per-skill *behavioral* deep-review beyond types/tests (adversarial gap-fill like the
  earlier geo/fleet pass) across all 82 skills — only types + the security-sensitive
  skills were covered this pass.
- **Wire `mypy --strict` into CI** — the durable fix so this can't rot again. Do it
  **once errors hit 0** (a hard gate at 100 would just make CI red). Until then it's the
  tracking number above.
