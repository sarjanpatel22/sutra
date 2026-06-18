#!/usr/bin/env bash
# Sutra one-command setup: clone → running. Idempotent — safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf "\033[36m▸ %s\033[0m\n" "$*"; }
warn() { printf "\033[33m⚠ %s\033[0m\n" "$*"; }

# 1. Python virtualenv
command -v python3 >/dev/null || { echo "python3 not found — install Python 3.11+"; exit 1; }
[ -d .venv ] || { say "creating virtualenv (.venv)"; python3 -m venv .venv; }
# shellcheck disable=SC1091
source .venv/bin/activate

# 2. Dependencies (editable install with dev + search extras)
say "installing Sutra"
pip install -q --upgrade pip
pip install -q -e ".[dev,search]"

# 3. Config
if [ ! -f .env ]; then
  say "creating .env from template"; cp .env.example .env
  warn "optional: add free Groq/Gemini keys to .env for cloud fallback (local runs \$0 without them)"
fi

# 4. Services — best-effort (Sutra degrades gracefully if these are missing)
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  say "starting services (FalkorDB memory + SearXNG search)"
  docker compose up -d || warn "docker compose failed — graph memory / search will be limited"
else
  warn "docker not found — Sutra still runs; graph memory + web search will be limited"
fi

# 5. Local models — best-effort ($0 brains)
if command -v ollama >/dev/null; then
  say "pulling local models (gemma3:4b + nomic-embed-text) — may take a few minutes"
  ollama pull gemma3:4b        >/dev/null 2>&1 || warn "could not pull gemma3:4b"
  ollama pull nomic-embed-text >/dev/null 2>&1 || warn "could not pull nomic-embed-text"
else
  warn "ollama not found — install it for \$0 local models: https://ollama.com"
fi

# 6. Verify + next steps
say "verifying"
.venv/bin/sutra status || true
cat <<'EOF'

✓ Sutra is set up. Activate the venv and go:

    source .venv/bin/activate
    sutra chat                                  # talk to it
    sutra tutor "how do I approach an SMB box"  # it teaches you
    sutra watch                                 # over-the-shoulder help when you're stuck
    sutra skill draft "<an ability you want>"   # it writes its own skill; you approve

EOF
