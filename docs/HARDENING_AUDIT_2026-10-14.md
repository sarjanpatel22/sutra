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
5. **cli** (`<this commit>`): renamed a reused subprocess var so the ranked-rows loop
   types cleanly (not a live bug — name reuse confused mypy).

Result: full suite **1886 passing** (+24 tests), ruff clean, **mypy ~160 → 100.**

## Key finding
Almost every remaining type error is **loose typing / variable-name reuse, not a live
bug** — which is reassuring about runtime correctness. The genuine latent-bug candidates
found (dom_xss maybe-None `.group`, the dedup idioms, calc operator confusion) were all
either safe-at-runtime or fixed. A few categories below (`arg-type`, `attr-defined`,
`str`) still deserve a line-by-line look for the rare real one.

## Remaining (for the next session)
### mypy --strict: 100 errors in 44 files
By file (top): voice/pipeline.py 13 · cli.py 10 · skills/recon.py 9 ·
ambient/collectors.py 5 · voice/audio.py 4 · vision/perceive.py 3 · gui/server.py 3 ·
core/mcp_audit.py 3 · then ~36 files with 1–2 each.
By category: str 19 · no-any-return 16 · type-arg 12 · arg-type 11 · no-untyped-call 10 ·
no-untyped-def 9 · unused-ignore 7 · call-overload 7 · attr-defined 6 · assignment 5.
- `unused-ignore` (7) and `no-untyped-def` (9) are trivial mechanical wins.
- `arg-type` (11) + `attr-defined` (6) + `str` (19) — review each for a real bug.
- `recon.py` (9): the `cert`/`pk` Element-vs-dict typing needs the function annotated.

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
