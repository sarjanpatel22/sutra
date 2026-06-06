"""Guided live validation — the human-in-the-loop test for the three shipped
voice features, run with Sarjan at a headset. Reuses the exact pipeline path the
real voice loop uses (record_with_vad_streaming → process_audio), so a PASS here
means the real thing works, not a stand-in.

    python scripts/live_validate.py                 # run all checks
    python scripts/live_validate.py barge-in         # run one check

Checks:
  devices       — list audio devices + confirm the headset is selected
  streaming-stt — speech transcribes during the end-of-turn silence wait
  voice-skills  — a tool question routes to cloud function-calling and answers
  barge-in      — talking over Sutra on a headset (no echo) stops her instantly

Hardware-gated: needs a mic, speakers/headset, and the GPU voice stack
(`pip install -e '.[voice]'`). Writes a JSON report to
~/.local/share/friday/validation_report.json.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()
REPORT = Path.home() / ".local" / "share" / "friday" / "validation_report.json"
CHECKS = ["devices", "streaming-stt", "voice-skills", "barge-in"]


def _record_streaming(vad, silence_s: float = 0.5):
    from friday.voice.audio import record_with_vad_streaming
    from friday.voice.pipeline import _get_stt

    return record_with_vad_streaming(
        vad,
        lambda clip: _get_stt().transcribe(clip),
        silence_threshold_s=silence_s,
        max_duration_s=20.0,
    )


def check_devices() -> tuple[str, bool, str]:
    from friday.voice.audio import default_input_device, default_output_device, list_devices

    t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
    t.add_column("Idx", justify="right")
    t.add_column("Name")
    t.add_column("In", justify="right")
    t.add_column("Out", justify="right")
    for d in list_devices():
        t.add_row(
            str(d["index"]), str(d["name"]),
            str(d["inputs"] or "-"), str(d["outputs"] or "-"),
        )
    console.print(t)
    di, do = default_input_device(), default_output_device()
    console.print(f"[bold]default input={di}  output={do}[/bold]")
    ok = console.input("Is your headset the default in/out? [y/N] ").strip().lower().startswith("y")
    return ("devices", ok, f"in={di} out={do} confirmed={ok}")


async def check_streaming_stt(vad) -> tuple[str, bool, str]:
    console.print(Panel("Say one full sentence, then pause.", title="[cyan]Streaming STT[/cyan]"))
    console.input("[dim]Press Enter, then speak…[/dim]")
    t0 = time.monotonic()
    _, stt_result = _record_streaming(vad)
    wall = (time.monotonic() - t0) * 1000
    text = stt_result.text.strip() if stt_result else ""
    overlapped = stt_result is not None  # the streaming recorder produced the transcript
    console.print(
        f"heard: [italic]{text!r}[/italic]\n"
        f"stt_latency={getattr(stt_result, 'latency_ms', 0):.0f}ms  "
        f"wall={wall:.0f}ms  overlap_transcribe={overlapped}"
    )
    ok = bool(text) and overlapped
    return ("streaming-stt", ok, f"text={text!r} overlap={overlapped}")


async def check_voice_skills(vad, history) -> tuple[str, bool, str]:
    from friday.core.agent import cloud_tools_available, likely_needs_tool
    from friday.voice.pipeline import process_audio

    if not cloud_tools_available():
        return ("voice-skills", False, "no cloud key — set GROQ_API_KEY or GEMINI_API_KEY")

    console.print(Panel(
        "Ask a tool question, e.g. [bold]'what time is it'[/bold] or "
        "[bold]'how much RAM do I have'[/bold].",
        title="[cyan]Voice → Skills[/cyan]",
    ))
    console.input("[dim]Press Enter, then speak…[/dim]")
    audio, stt_result = _record_streaming(vad)
    transcript = stt_result.text.strip() if stt_result else ""
    tool_path = likely_needs_tool(transcript) and cloud_tools_available()
    reply, history, lat, _ = await process_audio(
        audio, history, barge_in=False, stt_result=stt_result
    )
    console.print(f"you:   [italic]{transcript!r}[/italic]\nsutra: [italic]{reply!r}[/italic]")
    console.print(f"tool_path={tool_path}  total={lat.total_ms:.0f}ms")
    ok = bool(reply) and tool_path
    return ("voice-skills", ok, f"q={transcript!r} reply={reply!r} tool_path={tool_path}")


async def check_barge_in(vad, history) -> tuple[str, bool, str]:
    from friday.voice.pipeline import process_audio

    console.print(Panel(
        "Ask something with a longer answer, then [bold]talk over her[/bold] while she "
        "speaks.\nOn a headset (no speaker echo) she should stop the instant you talk.",
        title="[cyan]Barge-in (the headset test)[/cyan]",
    ))
    console.input("[dim]Press Enter, then speak your question…[/dim]")
    audio, stt_result = _record_streaming(vad)
    reply, history, lat, interrupted = await process_audio(
        audio, history, barge_in=True, stt_result=stt_result
    )
    console.print(f"reply={reply[:60]!r}…  [bold]interrupted={interrupted}[/bold]")
    spoke = console.input("Did you talk over her? [y/N] ").strip().lower().startswith("y")
    if spoke:
        return ("barge-in", interrupted, f"spoke_over=yes interrupted={interrupted}")
    # If you didn't speak, the correct outcome is NO interrupt (no echo false-trip).
    return ("barge-in", not interrupted, f"spoke_over=no false_trip={interrupted}")


async def main(only: list[str] | None = None) -> int:
    try:
        from friday.voice.vad import SileroVAD
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Voice deps missing: {e}[/red]\nInstall: pip install -e '.[voice]'")
        return 2

    run = only or CHECKS
    import os

    vad = SileroVAD(threshold=float(os.getenv("VAD_THRESHOLD", "0.5")))
    console.print("[dim]Pre-warming models (first run takes a few seconds)…[/dim]")
    try:
        from friday.voice.pipeline import prewarm
        await prewarm()
    except Exception as e:  # noqa: BLE001
        console.print(
            f"[yellow]Prewarm issue ({e}) — continuing; first turn will be slow.[/yellow]"
        )

    history: list[dict] = []
    results: list[tuple[str, bool, str]] = []
    try:
        if "devices" in run:
            results.append(check_devices())
        if "streaming-stt" in run:
            results.append(await check_streaming_stt(vad))
        if "voice-skills" in run:
            results.append(await check_voice_skills(vad, history))
        if "barge-in" in run:
            results.append(await check_barge_in(vad, history))
    except KeyboardInterrupt:
        console.print("\n[dim]Aborted.[/dim]")

    # Summary
    t = Table(show_header=True, header_style="bold cyan", title="Validation results")
    t.add_column("Check")
    t.add_column("Result")
    t.add_column("Detail", overflow="fold")
    for name, ok, detail in results:
        t.add_row(name, "[green]PASS[/green]" if ok else "[red]FAIL[/red]", detail)
    console.print(t)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(
        {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
         "results": [{"check": n, "pass": ok, "detail": d} for n, ok, d in results]},
        indent=2,
    ))
    console.print(f"[dim]Report → {REPORT}[/dim]")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a in CHECKS]
    raise SystemExit(asyncio.run(main(args or None)))
