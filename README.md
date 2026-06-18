<h1 align="center">Sutra</h1>

<p align="center">
  <b>A local-first, $0-by-default personal AI that learns from experience, teaches you, and writes its own skills.</b><br>
  <i>Voice-native. Your data stays on your machine. It grows with you.</i>
</p>

<p align="center">
  <code>776 tests</code> · <code>$0 by default</code> · <code>runs on your GPU</code> · <code>49 skills</code> · solo-built
</p>

---

Most "AI assistants" are a chat box wrapped around someone else's API, sending your life to someone else's servers. **Sutra is the opposite.** It runs on your own hardware, costs nothing by default (local models + free tiers), remembers you across sessions in a graph it keeps on *your* disk, acts on your real desktop, reasons as a team of sub-agents — and, uniquely, **it gets better the more you use it and can write new abilities for itself, with your approval.**

It's the assistant you'd build for yourself if you had the time. So one engineer did, in a few weeks.

## What makes it different

| | Sutra |
|---|---|
| 🔒 **Sovereign** | Local-first; your memory graph and data never leave your machine |
| 💸 **$0 by default** | Local Ollama → free Groq/Gemini tiers; paid Claude is strictly opt-in |
| 🗣️ **Voice-native** | Whisper + Kokoro + neural VAD, wake word, barge-in (~1.2s round-trip) |
| 🧠 **Real memory** | Graph memory (Graphiti/FalkorDB) + a reranked recall index + ambient capture |
| 🖥️ **Acts on your desktop** | OCR-locates real controls and clicks/types them — not pixel guessing |
| 👥 **Thinks as a team** | A council of specialist sub-agents that fan out in parallel and synthesize |
| 🌱 **Learns from experience** | *Dreaming* — records what worked/failed and recalls it on similar tasks |
| 🎓 **Teaches you** | A mentor mode that explains the *why* and gives you the next step to try |
| 🛠️ **Writes its own skills** | Drafts a new skill from a description → **you review & approve** → it loads |

## See it

> _(demo GIF goes here — `sutra chat`, a voice turn, and `sutra skill draft` → `approve`)_

## Quickstart

```bash
git clone <your-repo-url> sutra && cd sutra
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,search]"
cp .env.example .env          # optional: add free Groq/Gemini keys for cloud fallback
docker compose up -d          # FalkorDB (memory), SearXNG (search)
sutra chat                    # talk to it
```

Then try:
```bash
sutra ask "summarize my notes on X"     # one-shot
sutra tutor "how do I approach an SMB box"   # it teaches you the methodology
sutra skill draft "get a crypto price by symbol"  # it writes a new skill; you approve it
sutra watch                              # ambient over-the-shoulder suggestions
```

## How it's built

```
friday/
  core/      router ($0 tiered) · agent loop · autoroute brain · council · dreaming · skillsmith
  skills/    49 skills: web · files · system · desktop control · git · memory · VPN · http
  voice/     Whisper STT · Kokoro TTS · Silero VAD · wake word
  memory/    Graphiti + FalkorDB graph memory · reranked recall · 3D graph viewer
  vision/    screen capture + OCR → real click coordinates
```

- **Tiered routing** keeps it free: every call prefers local/free models; the paid tier is gated behind a flag and a budget.
- **A confirmation gate** means nothing irreversible runs without approval — enforced *and* audited in CI.
- **Self-extension is safe by design**: drafted skills are inert source until *you* approve them.

## Responsible use

Sutra includes an autonomous mode for **authorized security practice only** — your own machines, your own accounts, sanctioned labs like TryHackMe/HackTheBox. It is gated (opt-in via `SUTRA_AUTONOMOUS`), scope-restricted to targets you authorize, and floored against catastrophic commands. **Don't point it at anything you don't own or have explicit permission to test.**

## Status

A personal project, feature-complete in code and validated in pieces — not yet a polished, multi-user product. Built by one person as a real daily driver. Issues and ideas welcome.
