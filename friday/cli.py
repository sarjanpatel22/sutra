"""Rich-based REPL for Sutra."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from friday.core.router import Tier, allow_paid, get_budget

app = typer.Typer(name="sutra", help="Sutra — voice-native ambient AI assistant")
console = Console()


@app.callback(invoke_without_command=True)
def _bootstrap(ctx: typer.Context) -> None:
    """Runs before any command — load keyring secrets into the environment. With NO subcommand,
    bare `sutra` boots the presence (orb + chat) instead of just printing help."""
    try:
        from friday.core.secrets import load_into_env
        load_into_env()
    except Exception:  # noqa: BLE001 — secret-loading must never block the CLI
        pass
    if ctx.invoked_subcommand is None:
        _start_presence(Tier.MEDIUM)


def _tier_from_str(s: str) -> Tier:
    mapping = {
        "local": Tier.LOCAL,
        "reason": Tier.REASON,
        "medium": Tier.MEDIUM,
        "heavy": Tier.HEAVY,
        "max": Tier.MAX,
    }
    return mapping.get(s.lower(), Tier.MEDIUM)


async def _chat_loop(tier: Tier) -> None:
    from friday.core.agent import flush_persist, run  # late import to avoid circular
    from friday.core.history import History

    hist = History(session="chat")
    history: list[dict[str, Any]] = hist.load()[-24:]  # resume recent context across restarts

    # Connect any configured MCP servers so their tools join the skill registry for
    # this session. No-op (returns None) when no server is configured.
    from friday.mcp.bridge import close_bridge, get_bridge
    mcp_bridge = None
    try:
        mcp_bridge = await get_bridge()
    except Exception as e:  # noqa: BLE001 — MCP must never block chat
        console.print(f"[yellow]MCP connect failed: {e}[/yellow]")

    # Background-warm the embedding model so the first memory recall / rag_search skips
    # nomic's ~3s cold load (both reuse friday.rag.store.embed). Fire-and-forget.
    import threading

    from friday.rag.store import warm as _rag_warm
    threading.Thread(target=_rag_warm, daemon=True, name="rag-warm").start()

    console.print(Panel(
        "[bold cyan]Sutra[/bold cyan] — ready.\n"
        f"[dim]tier: {tier.value}  |  /tier <name>  /skills  /budget  /status  /clear  /quit[/dim]",
        border_style="cyan",
    ))
    if mcp_bridge and mcp_bridge.tool_names:
        console.print(
            f"[dim]MCP: {len(mcp_bridge.tool_names)} tool(s) from "
            f"{', '.join(mcp_bridge.servers)}[/dim]"
        )

    while True:
        try:
            raw = Prompt.ask("[bold cyan]>[/bold cyan]")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye.[/dim]")
            break

        user = raw.strip()
        if not user:
            continue

        # Slash commands
        if user.startswith("/"):
            parts = user[1:].split(None, 1)
            cmd = parts[0].lower()

            if cmd == "quit":
                console.print("[dim]Bye.[/dim]")
                break
            elif cmd == "clear":
                history = []
                hist.clear()
                console.print("[dim]History cleared.[/dim]")
            elif cmd == "tier":
                if len(parts) > 1:
                    tier = _tier_from_str(parts[1])
                    console.print(f"[dim]Tier → {tier.value}[/dim]")
                else:
                    console.print(f"[dim]Current tier: {tier.value}[/dim]")
            elif cmd == "skills":
                from friday.skills.registry import autodiscover, list_skills
                autodiscover()
                skills = list_skills()
                t = Table("Skill", show_header=True, header_style="bold cyan")
                for s in skills:
                    t.add_row(s)
                console.print(t)
            elif cmd == "budget":
                b = get_budget()
                console.print(
                    f"[dim]Daily: ${b.daily_spent:.4f} / ${b.daily_limit:.2f} "
                    f"({b.daily_pct:.1f}%)  "
                    f"Monthly: ${b.monthly_spent:.4f} / ${b.monthly_limit:.2f} "
                    f"({b.monthly_pct:.1f}%)  "
                    f"| Tokens: {b.daily_tokens:,} daily / {b.monthly_tokens:,} monthly[/dim]"
                )
            elif cmd == "status":
                from friday.core.monitor import print_report, run_health_checks
                with console.status("[cyan]running health checks…[/cyan]"):
                    report = await run_health_checks()
                print_report(report)
            else:
                console.print(f"[yellow]Unknown command: /{cmd}[/yellow]")
            continue

        # Agent call. Irreversible skills (write_file, run_command, forget) pause
        # the spinner and ask for a y/n before they run.
        status = console.status("[cyan]thinking…[/cyan]")
        status.start()

        async def _confirm(skill_name: str, args: dict[str, Any]) -> bool:
            status.stop()
            console.print(Panel(
                f"[bold]{skill_name}[/bold]\n[dim]{args}[/dim]",
                title="[yellow]Confirm action[/yellow]", border_style="yellow",
            ))
            ok = await asyncio.get_running_loop().run_in_executor(
                None, lambda: Confirm.ask("  Run this?", default=False)
            )
            status.start()
            return ok

        try:
            reply, history = await run(user, history=history, tier=tier, confirm=_confirm)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Error: {e}[/red]")
            continue
        finally:
            status.stop()

        console.print(Panel(Markdown(reply), border_style="dim", padding=(0, 1)))
        hist.append_turn(user, reply)

    # Let any background memory writes from the last turns finish before exit.
    with console.status("[dim]saving memory…[/dim]"):
        await flush_persist()
    await close_bridge()  # tear down MCP server subprocesses (no-op if none)


def _start_presence(tier: Tier, *, orb: bool = True) -> None:
    """Boot Sutra's presence: the orb on the desktop (background) + an interactive chat session.
    The orb is ambient — if it can't start (no display, no browser) the chat runs anyway."""
    httpd = browser = None
    if orb:
        try:
            from friday.gui.server import serve_background
            httpd, browser = serve_background()
            console.print("[dim]· presence orb up[/dim]")
        except Exception as e:  # noqa: BLE001 — the orb is ambient; never block the session
            console.print(f"[yellow]orb didn't start ({e}); continuing without it[/yellow]")
    try:
        asyncio.run(_chat_loop(tier))
    finally:
        if browser is not None:
            browser.terminate()
        if httpd is not None:
            httpd.shutdown()


@app.command()
def start(
    tier: str = typer.Option("medium", "--tier", "-t", help="Routing tier: local/medium/heavy/max"),
    no_orb: bool = typer.Option(False, "--no-orb", help="Skip the presence orb (chat only)"),
) -> None:
    """Boot Sutra — bring up the presence orb and drop into chat. This is what bare `sutra` runs."""
    _start_presence(_tier_from_str(tier), orb=not no_orb)


@app.command()
def chat(
    tier: str = typer.Option("medium", "--tier", "-t", help="Routing tier: local/medium/heavy/max"),
) -> None:
    """Start an interactive chat session (no orb)."""
    asyncio.run(_chat_loop(_tier_from_str(tier)))


@app.command()
def ask(
    query: str = typer.Argument(..., help="Single question to ask"),
    tier: str = typer.Option("medium", "--tier", "-t", help="local/reason/medium/heavy/max"),
) -> None:
    """Ask a single question and exit."""
    from friday.core.agent import flush_persist, run
    from friday.mcp.bridge import close_bridge, get_bridge

    async def _once() -> None:
        # Connect any configured MCP servers so their tools are available for this
        # one-shot too (parity with `sutra chat`). No-op when none configured.
        try:
            await get_bridge()
        except Exception as e:  # noqa: BLE001 — MCP must never block the answer
            console.print(f"[yellow]MCP connect failed: {e}[/yellow]")
        try:
            reply, _ = await run(query, tier=_tier_from_str(tier))
            console.print(Markdown(reply))      # show the answer immediately
            await flush_persist()                # then let the background save finish
        finally:
            await close_bridge()                 # tear down MCP subprocesses

    asyncio.run(_once())


@app.command()
def think(
    query: str = typer.Argument(..., help="A hard question to reason through, locally"),
    show: bool = typer.Option(False, "--show", help="Also print Gemma 4's reasoning trace"),
) -> None:
    """Reason through a hard question locally with Gemma 4 (thinking on). Free."""
    from friday.core.agent import reason

    async def _once() -> None:
        with console.status("[dim]Gemma 4 is thinking…[/dim]"):
            reply, thinking = await reason(query)
        if show and thinking:
            console.print(f"[dim]── reasoning ──\n{thinking}\n──────────[/dim]")
        console.print(Markdown(reply))

    asyncio.run(_once())


@app.command(name="skills")
def show_skills() -> None:
    """List all registered skills."""
    from friday.skills.registry import autodiscover, dump_registry
    autodiscover()
    console.print_json(dump_registry())


@app.command()
def devices() -> None:
    """List available audio input/output devices."""
    from friday.voice.audio import list_devices
    t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
    t.add_column("Index", justify="right")
    t.add_column("Name")
    t.add_column("In", justify="right")
    t.add_column("Out", justify="right")
    for d in list_devices():
        t.add_row(
            str(d["index"]),
            str(d["name"]),
            str(d["inputs"]) if d["inputs"] else "-",
            str(d["outputs"]) if d["outputs"] else "-",
        )
    console.print(t)


def _audio_check() -> None:
    """Quick speaker+mic doctor: list devices, play a test tone, measure mic level.

    Run with `sutra voice --check` to confirm you can hear Sutra and she can hear
    you BEFORE a real session — diagnoses the 'nothing happened' / silent-HDMI case.
    """
    from friday.voice import audio

    report = audio.audio_report()
    table = Table(title="Audio devices", show_header=True, header_style="bold")
    table.add_column("role")
    table.add_column("resolved")
    table.add_row("output (speaker)", str(report.get("resolved_output")))
    table.add_row("input (mic)", str(report.get("resolved_input")))
    console.print(table)
    console.print(f"[dim]all outputs: {report.get('all_outputs')}[/dim]")
    console.print(f"[dim]all inputs:  {report.get('all_inputs')}[/dim]")

    console.print("\n[bold]▶ playing a 1s test tone — you should HEAR it…[/bold]")
    try:
        audio.play_test_tone(seconds=1.0)
    except Exception as e:  # noqa: BLE001 — report, don't crash the doctor
        console.print(f"[red]tone playback failed: {e}[/red]")
    if not Confirm.ask("Did you hear the tone?", default=True):
        console.print(
            "[yellow]Set the right speaker with SUTRA_OUTPUT_DEVICE "
            "(an index or a name substring from 'all outputs' above), then re-run.[/yellow]"
        )

    console.print("\n[bold]🎙  say something for 3 seconds…[/bold]")
    try:
        rms = audio.mic_rms(seconds=3.0)
        verdict = (
            "good — she can hear you" if rms > 0.01
            else "TOO QUIET — check the mic / SUTRA_INPUT_DEVICE"
        )
        console.print(f"mic level (RMS): [bold]{rms:.4f}[/bold] — {verdict}")
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]mic capture failed: {e}[/red]")


@app.command()
def voice(
    mode: str = typer.Option(
        "ptt", "--mode", "-m",
        help="ptt (hold SPACE), vad (auto-detect), wake (wake word), or text (type, mic-free)",
    ),
    orb: bool = typer.Option(False, "--orb", help="Show the presence orb full-screen too"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Print live transcripts, routing decisions, and errors (INFO logging)",
    ),
    check: bool = typer.Option(
        False, "--check", help="Audio doctor: test speaker + mic, then exit (no session)",
    ),
    tutor: bool = typer.Option(
        False, "--tutor", help="Coaching session: Sutra TEACHES every turn (pentest tutor)",
    ),
) -> None:
    """Start voice mode. ptt=hold SPACE, vad=always listening, wake=say the wake word, text=type.

    --tutor turns the whole session into a hands-free pentest coach: it teaches the why and hints
    the next step instead of doing anything; say "look at my screen" and it reads your screen.
    """
    if verbose:
        import logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        )
        logging.getLogger("LiteLLM").setLevel(logging.WARNING)  # mute its INFO spam
    if check:
        _audio_check()
        return

    from friday.voice.pipeline import (
        push_to_talk_loop,
        text_presence_loop,
        vad_listen_loop,
        wake_listen_loop,
    )
    if tutor:
        from friday.voice.pipeline import set_tutor_mode
        set_tutor_mode(True)
        console.print(
            "[bold green]Tutor mode[/bold green] — I'll coach you, not do it. "
            "Say [cyan]'look at my screen'[/cyan] anytime."
        )
    httpd = browser = None
    if orb:
        from friday.gui.server import serve_background
        httpd, browser = serve_background()
    try:
        if mode == "vad":
            asyncio.run(vad_listen_loop())
        elif mode == "wake":
            asyncio.run(wake_listen_loop())
        elif mode == "text":
            asyncio.run(text_presence_loop())
        else:
            asyncio.run(push_to_talk_loop())
    finally:
        if browser is not None:
            browser.terminate()
        if httpd is not None:
            httpd.shutdown()


@app.command()
def graph(
    port: int = typer.Option(8700, "--port", "-p", help="Port to serve the viewer on"),
    no_open: bool = typer.Option(False, "--no-open", help="Don't auto-open a browser"),
) -> None:
    """Open the brain-graph viewer for the memory layer (Graphiti/FalkorDB)."""
    from friday.memory.graphview import serve
    serve(port=port, open_browser=not no_open)


@app.command()
def orb(
    port: int = typer.Option(8710, "--port", "-p", help="Port to serve the orb on"),
    no_open: bool = typer.Option(False, "--no-open", help="Just serve, don't open a window"),
    windowed: bool = typer.Option(False, "--windowed", help="Normal browser tab (not full-screen)"),
) -> None:
    """Open Sutra's HORIZON presence orb — full-screen ambient on your desktop."""
    from friday.gui.server import serve
    serve(port=port, open_browser=not no_open, fullscreen=not windowed)


@app.command()
def dashboard(
    port: int = typer.Option(8720, "--port", "-p", help="Port to serve the dashboard on"),
    no_open: bool = typer.Option(False, "--no-open", help="Just serve, don't open a window"),
    windowed: bool = typer.Option(False, "--windowed", help="Normal browser tab (not full-screen)"),
) -> None:
    """Open the Sutra dashboard — chat, live actions, and telemetry around the orb."""
    from friday.gui.dashboard import serve
    serve(port=port, open_browser=not no_open, fullscreen=not windowed)


@app.command()
def ambient(
    once: bool = typer.Option(False, "--once", help="Single poll+flush then exit (test)"),
) -> None:
    """Run the ambient OS-memory daemon — watches activity and feeds the graph."""
    from friday.ambient.daemon import AmbientDaemon
    d = AmbientDaemon()
    if once:
        n = asyncio.run(d.run_once())
        console.print(f"[dim]Flushed {n} episode(s).[/dim]")
    else:
        asyncio.run(d.run())


@app.command()
def guide(
    goal: str = typer.Argument(..., help="What you're trying to do on screen right now"),
) -> None:
    """Look at your screen and tell you which control to click — read-only, no clicking.

    Grabs one screenshot, asks the free vision model where the relevant button/menu/field
    is, and prints that. Never moves the mouse or types — use `sutra do` for that.
    """
    from friday.skills.guide import guide_me

    async def _run() -> dict[str, Any]:
        with console.status("[cyan]looking at your screen…[/cyan]"):
            return await guide_me(goal)

    result = asyncio.run(_run())
    if "error" in result:
        console.print(f"[red]{result['error']}[/red]")
        return
    console.print(Panel(
        result["guidance"],
        title=f"[cyan]{result['goal']}[/cyan]", border_style="cyan", padding=(0, 1),
    ))


@app.command()
def do(
    goal: str = typer.Argument(..., help="What to click, e.g. 'press Save' or 'open Search menu'"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt (scripted/autonomous use)"
    ),
) -> None:
    """Actually CLICK a control on your real desktop — the take-over after `guide`.

    Finds the control by its on-screen text (OCR) and clicks its real location. This is
    IRREVERSIBLE — it moves the mouse and clicks the live desktop — so it confirms first
    unless you pass --yes. Text-labelled controls only.
    """
    from friday.skills.desktop_control import act_on_screen

    if not yes:
        console.print(Panel(
            f"[bold]Click on your live desktop:[/bold] {goal}\n"
            "[dim]This moves the mouse and clicks for real.[/dim]",
            title="[yellow]Confirm action[/yellow]", border_style="yellow",
        ))
        if not Confirm.ask("  Do it?", default=False):
            console.print("Cancelled.")
            return

    async def _run() -> dict[str, Any]:
        with console.status("[cyan]finding the control…[/cyan]"):
            return await act_on_screen(goal)

    result = asyncio.run(_run())
    if "error" in result:
        console.print(f"[red]{result['error']}[/red]")
    elif result.get("acted"):
        coords = result.get("coords", [])
        console.print(
            f"[green]Clicked[/green] [bold]{result.get('target')}[/bold] "
            f"[dim]at {tuple(coords)}[/dim]"
        )
    else:  # honest "nothing matched — here's what I can see"
        console.print(f"[yellow]{result.get('note', 'Nothing matched.')}[/yellow]")


@app.command()
def watch(
    once: bool = typer.Option(
        False, "--once", help="Run a single tick, print the suggestion (or quiet line), then exit"
    ),
    tutor: bool = typer.Option(
        False, "--tutor", help="Pentest tutor watch: when you stall on a box, offer a coaching hint"
    ),
) -> None:
    """Watch your desktop and proactively suggest — Sutra glancing over your shoulder.

    Sends a screenshot to the free vision model only when the screen changes (or you've
    dwelled on the same screen too long — "are you stuck?"), and stays silent unless it
    genuinely helps. --tutor makes those nudges pentest COACHING hints (the next step to
    try, adapted to your skill level) instead of generic tips. Off until you run this;
    tune via WATCH_* env (WATCH_DWELL_S, WATCH_COOLDOWN_S, …). Ctrl+C to stop.
    """
    from friday.watch.loop import run_watch, watch_once
    from friday.watch.suggest import suggest_from_screen, tutor_suggest_from_screen

    suggest_fn = tutor_suggest_from_screen if tutor else suggest_from_screen

    if once:
        suggestion = asyncio.run(watch_once(suggest_fn=suggest_fn))
        if suggestion:
            console.print(Panel(suggestion, title="[cyan]Sutra[/cyan]", border_style="cyan"))
        else:
            console.print("[dim]Nothing worth interrupting you for right now.[/dim]")
        return

    label = "Sutra is tutoring" if tutor else "Sutra is watching"
    hint = ("a coaching hint when you stall on a box" if tutor
            else "suggestions only when they help")
    console.print(f"\n  👁  [bold]{label}[/bold] — {hint}")
    console.print(
        "      [dim]silent unless useful · a screenshot goes to the free vision model "
        "only when the screen changes · Ctrl+C to stop[/dim]\n"
    )
    try:
        asyncio.run(run_watch(suggest_fn=suggest_fn))
    except KeyboardInterrupt:
        console.print("\n[dim]Watch stopped.[/dim]")


rag_app = typer.Typer(help="Local document RAG — ingest files, ask grounded questions.")
app.add_typer(rag_app, name="rag")


@rag_app.command("ingest")
def rag_ingest(
    paths: list[str] = typer.Argument(..., help="Files or folders to ingest"),
    collection: str = typer.Option("default", "--collection", "-c", help="Collection name"),
) -> None:
    """Chunk + embed files into a RAG collection (free, local nomic-embed)."""
    from friday.rag.ingest import ingest_paths
    with console.status("[cyan]chunking + embedding…[/cyan]"):
        res = ingest_paths(paths, collection)
    console.print(
        f"[green]Ingested {res['chunks']} chunks from {res['files']} file(s)[/green] "
        f"→ '{res['collection']}' (total {res['total']})."
    )


@rag_app.command("ask")
def rag_ask(
    query: str = typer.Argument(..., help="Question to answer from your documents"),
    k: int = typer.Option(5, "-k", help="Passages to retrieve"),
    collection: str = typer.Option("default", "--collection", "-c", help="Collection name"),
) -> None:
    """Retrieve relevant passages and answer grounded in them, citing sources."""
    from rich.live import Live

    from friday.core.router import Tier, stream_complete
    from friday.rag.store import RagStore

    async def _run() -> None:
        store = RagStore(collection)
        if len(store) == 0:
            console.print("[yellow]Nothing ingested — run `sutra rag ingest <path>`.[/yellow]")
            return
        hits = store.search(query, k)
        ctx = "\n\n".join(f"[{h['source']}]\n{h['text']}" for h in hits)
        msgs = [
            {"role": "system", "content": "Answer using ONLY the provided context from the "
             "user's documents. Cite the source files. If the answer isn't in the context, "
             "say so plainly — don't invent."},
            {"role": "user", "content": f"Context:\n{ctx}\n\nQuestion: {query}"},
        ]
        # Stream the answer so the first words land in ~0.7s rather than blocking for the
        # whole completion — same model and context, so the answer itself is unchanged. The
        # live region is transient (cropped to the viewport while generating); the full
        # Markdown prints once on completion so a long answer renders cleanly in scrollback.
        answer = ""
        try:
            with Live(console=console, refresh_per_second=12, transient=True,
                      vertical_overflow="crop") as live:
                async for tok in stream_complete(msgs, tier=Tier.MEDIUM, tools=None,
                                                 max_tokens=400):
                    answer += tok
                    live.update(Markdown(answer))
        except Exception as e:  # noqa: BLE001 — every model tier failed; show it, don't traceback
            console.print(f"[red]Could not get an answer:[/red] [dim]{e}[/dim]")
            return
        if not answer.strip():
            console.print("[yellow](no answer returned)[/yellow]")
            return
        console.print(Markdown(answer))
        console.print("[dim]sources: " + ", ".join(sorted({h["source"] for h in hits})) + "[/dim]")

    asyncio.run(_run())


@app.command()
def status() -> None:
    """Run a one-shot health check and print results."""
    from friday.core.monitor import print_report, run_health_checks

    async def _run() -> None:
        report = await run_health_checks()
        print_report(report)

    asyncio.run(_run())


@app.command()
def doctor(
    services: bool = typer.Option(
        True, "--services/--no-services",
        help="Also probe live services (Ollama, SearXNG, Redis, …)",
    ),
) -> None:
    """Preflight: check Python deps, system binaries, skills, and services — and print
    the exact command to fix anything missing. Run this first if something won't work."""
    from friday.core.doctor import run_doctor

    ok = asyncio.run(run_doctor(services=services))
    raise typer.Exit(0 if ok else 1)


@app.command()
def monitor(
    interval: int = typer.Option(300, "--interval", "-i", help="Check interval in seconds"),
) -> None:
    """Run the health + budget watchdog on a loop (Ctrl+C to stop)."""
    from friday.core.monitor import run_watchdog
    asyncio.run(run_watchdog(interval_s=interval))


@app.command()
def budget() -> None:
    """Print current API spend vs limits."""
    b = get_budget()
    t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
    t.add_column("Scope")
    t.add_column("Spent", justify="right")
    t.add_column("Limit", justify="right")
    t.add_column("Pct", justify="right")
    t.add_column("Tokens", justify="right")

    daily_color = "red" if b.daily_pct >= 85 else "yellow" if b.daily_pct >= 60 else "green"
    monthly_color = "red" if b.monthly_pct >= 85 else "yellow" if b.monthly_pct >= 60 else "green"

    t.add_row(
        "Daily",
        f"${b.daily_spent:.4f}",
        f"${b.daily_limit:.2f}",
        f"[{daily_color}]{b.daily_pct:.1f}%[/{daily_color}]",
        f"{b.daily_tokens:,}",
    )
    t.add_row(
        "Monthly",
        f"${b.monthly_spent:.4f}",
        f"${b.monthly_limit:.2f}",
        f"[{monthly_color}]{b.monthly_pct:.1f}%[/{monthly_color}]",
        f"{b.monthly_tokens:,}",
    )
    console.print(t)


@app.command(name="logs")
def show_logs(
    n: int = typer.Option(20, "--last", "-n", help="Number of recent turns to show"),
    surface: str = typer.Option(
        "", "--surface", "-s", help="Filter by surface (text/voice/telegram)"
    ),
) -> None:
    """Recent agent turns + per-tier / per-surface latency and error breakdown.

    Reads turns.jsonl (one record per turn, all surfaces). Build on top of
    ``sutra stats`` which reads the narrower text-only turn_latency.jsonl.
    """
    from friday.core.turnlog import aggregate_turns, load_turns

    turns = load_turns()
    if not turns:
        console.print(
            "[dim]No turns logged yet. Run [cyan]sutra ask[/cyan] / [cyan]sutra chat[/cyan] "
            "first.[/dim]"
        )
        return

    # Filter if --surface specified
    subset = [t for t in turns if t.get("surface") == surface] if surface else turns
    if surface and not subset:
        console.print(f"[yellow]No turns found for surface '{surface}'.[/yellow]")
        return

    # --- Recent turns table ---
    recent = subset[-n:]
    rt = Table(
        title=f"Recent turns (last {len(recent)})",
        show_header=True, header_style="bold cyan", box=None, padding=(0, 1),
    )
    rt.add_column("Time")
    rt.add_column("Surface")
    rt.add_column("Tier")
    rt.add_column("Tools", justify="right")
    rt.add_column("ms", justify="right")
    rt.add_column("OK")
    for t in recent:
        ok_mark = "[green]✓[/green]" if t.get("ok", True) else "[red]✗[/red]"
        tool_names = t.get("tool_calls") or []
        tools_cell = f"{len(tool_names)}" + (
            f" ({','.join(tool_names[:2])}{'…' if len(tool_names) > 2 else ''})"
            if tool_names else ""
        )
        rt.add_row(
            str(t.get("ts", ""))[-8:],
            str(t.get("surface", "?")),
            str(t.get("tier", "?")),
            tools_cell,
            str(t.get("latency_ms", "")),
            ok_mark,
        )
    console.print(rt)

    # --- Aggregated breakdown ---
    agg = aggregate_turns(subset)

    total = agg["total"]
    console.print(
        f"\n[bold]Total:[/bold] {total['n']} turns  "
        f"p50 [cyan]{total['p50_ms']:.0f}ms[/cyan]  "
        f"p95 [cyan]{total['p95_ms']:.0f}ms[/cyan]  "
        f"cost [green]${total['cost_usd']:.4f}[/green]  "
        f"errors [{'red' if total['errors'] else 'dim'}]{total['errors']} "
        f"({total['error_rate']*100:.1f}%)[/{'red' if total['errors'] else 'dim'}]"
    )

    if agg["by_surface"]:
        st = Table(
            title="By surface", show_header=True, header_style="bold cyan",
            box=None, padding=(0, 1),
        )
        st.add_column("Surface")
        st.add_column("n", justify="right")
        st.add_column("p50 ms", justify="right")
        st.add_column("p95 ms", justify="right")
        st.add_column("cost $", justify="right")
        st.add_column("err%", justify="right")
        for surf, m in agg["by_surface"].items():
            st.add_row(
                surf, str(m["n"]),
                f"{m['p50_ms']:.0f}", f"{m['p95_ms']:.0f}",
                f"{m['cost_usd']:.4f}",
                f"[red]{m['error_rate']*100:.1f}%[/red]" if m["errors"]
                else f"[dim]{m['error_rate']*100:.1f}%[/dim]",
            )
        console.print(st)

    if agg["by_tier"]:
        tt = Table(
            title="By tier", show_header=True, header_style="bold cyan",
            box=None, padding=(0, 1),
        )
        tt.add_column("Tier")
        tt.add_column("n", justify="right")
        tt.add_column("p50 ms", justify="right")
        tt.add_column("p95 ms", justify="right")
        tt.add_column("cost $", justify="right")
        tt.add_column("err%", justify="right")
        for tier_name, m in agg["by_tier"].items():
            tt.add_row(
                tier_name, str(m["n"]),
                f"{m['p50_ms']:.0f}", f"{m['p95_ms']:.0f}",
                f"{m['cost_usd']:.4f}",
                f"[red]{m['error_rate']*100:.1f}%[/red]" if m["errors"]
                else f"[dim]{m['error_rate']*100:.1f}%[/dim]",
            )
        console.print(tt)


@app.command()
def stats() -> None:
    """Latency p50/p95 (text per-tier, voice per-stage) + today's spend — shakedown dashboard."""
    from friday.core.stats import gather_stats

    s = gather_stats()
    if not s["text_turns"] and not s["voice_turns"]:
        console.print(
            "[dim]No turns logged yet. Use [cyan]sutra ask[/cyan]/[cyan]chat[/cyan] or "
            "[cyan]sutra voice[/cyan], then re-run.[/dim]"
        )
        return

    if s["text_by_tier"]:
        tt = Table(title="Text turns", show_header=True, header_style="bold cyan",
                   box=None, padding=(0, 2))
        tt.add_column("Tier")
        tt.add_column("n", justify="right")
        tt.add_column("p50 ms", justify="right")
        tt.add_column("p95 ms", justify="right")
        tt.add_column("avg rounds", justify="right")
        tt.add_column("tokens", justify="right")
        for tier, m in s["text_by_tier"].items():
            tt.add_row(tier, str(m["n"]), f"{m['p50']:.0f}", f"{m['p95']:.0f}",
                       f"{m['avg_rounds']:.1f}", f"{m['tokens']:,}")
        console.print(tt)

    if s["voice_turns"]:
        vt = Table(title=f"Voice turns ({s['voice_turns']})", show_header=True,
                   header_style="bold cyan", box=None, padding=(0, 2))
        vt.add_column("Stage")
        vt.add_column("p50 ms", justify="right")
        vt.add_column("p95 ms", justify="right")
        labels = {"stt_ms": "STT", "agent_ttft_ms": "agent TTFT",
                  "tts_first_ms": "TTS first", "total_ms": "total"}
        for stage, label in labels.items():
            m = s["voice_stages"][stage]
            vt.add_row(label, f"{m['p50']:.0f}", f"{m['p95']:.0f}")
        console.print(vt)

    b = s["budget"]
    console.print(
        f"[dim]Today: ${b['daily_spent']:.4f} / ${b['daily_limit']:.2f}  "
        f"({b['daily_tokens']:,} tokens)  |  Month: "
        f"${b['monthly_spent']:.4f} / ${b['monthly_limit']:.2f}[/dim]"
    )


@app.command()
def delegate(
    agent: str = typer.Argument(..., help="architect | researcher | creative | watchdog"),
    task: str = typer.Argument(..., help="The focused sub-task to hand off"),
) -> None:
    """Run a specialist sub-agent on a task in isolated context and print its report."""
    from friday.core.subagents import SUBAGENTS, run_subagent

    if agent.lower() not in SUBAGENTS:
        console.print(f"[red]Unknown sub-agent '{agent}'.[/red] Try: {', '.join(SUBAGENTS)}")
        raise typer.Exit(1)

    async def _run() -> None:
        with console.status(f"[cyan]{agent} is working…[/cyan]"):
            answer = await run_subagent(agent, task)
        console.print(Panel(Markdown(answer), title=f"[cyan]{agent}[/cyan]", border_style="cyan"))

    asyncio.run(_run())


@app.command(name="subagents")
def show_subagents() -> None:
    """List the available specialist sub-agents."""
    from friday.core.subagents import list_subagents
    t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
    t.add_column("Agent")
    t.add_column("Role")
    for name, role in list_subagents():
        t.add_row(name, role)
    console.print(t)


@app.command()
def council(
    goal: str = typer.Argument(..., help="The multi-faceted goal to convene the council on"),
    trace: bool = typer.Option(
        False, "--trace", help="Also show each specialist's contribution and the review"
    ),
) -> None:
    """Convene the specialist council on a goal and print the synthesized answer."""
    from friday.core.council import convene

    async def _run() -> None:
        with console.status("[cyan]the council is convening…[/cyan]"):
            r = await convene(goal)
        if trace:
            console.print(Panel(Markdown(r.plan or "(no plan)"),
                                title="[dim]architect · plan[/dim]", border_style="dim"))
            for role, out in r.contributions.items():
                console.print(Panel(Markdown(out or "(empty)"),
                                    title=f"[cyan]{role}[/cyan]", border_style="cyan"))
            console.print(Panel(Markdown(r.critique or "(no critique)"),
                                title="[yellow]review[/yellow]", border_style="yellow"))
        console.print(Panel(Markdown(r.answer or "(no answer)"),
                            title="[bold green]Sutra[/bold green]", border_style="green"))

    asyncio.run(_run())


def _engagement_observe(kind: str, data: dict[str, Any]) -> None:
    """Stream the engagement live: orchestrator objectives (tiered), each tool call, worker digests.

    Single-tier runs only ever emit tool_start, so they render exactly as before.
    """
    if kind == "objective":  # tiered: the strong orchestrator's next directive
        txt = str(data.get("text", ""))[:200]
        console.print(f"\n[bold cyan]🧠 objective[/bold cyan] [white]{txt}[/white]")
    elif kind == "worker_report":  # tiered: the local worker's digest back to the orchestrator
        console.print(f"[dim]  ↳ worker:[/dim] {str(data.get('text', ''))[:160]}")
    elif kind == "tool_start":
        gated = " [yellow][gated][/yellow]" if data.get("gated") else ""
        args = str(data.get("args", ""))[:90]
        console.print(f"[dim]   →[/dim] {data.get('skill')}{gated} [dim]{args}[/dim]")


def _default_auto_tier() -> str:
    """Default model tier for the autonomous loop (env-overridable)."""
    return os.getenv("SUTRA_AUTO_TIER", "heavy")


def _run_engagement(goal: str, rounds: int, tier_str: str, title: str, yes: bool = False,
                    scope: str | None = None, tiered: bool = False,
                    objectives: int = 8, worker_rounds: int = 6) -> None:
    """Drive one autonomous engagement: pick the tier, stream tools, confirm risky steps.

    tiered=True runs the orchestrator-worker loop (a `tier_str` orchestrator directs a free
    local-first worker); otherwise the single-tier loop runs every step at `tier_str`.
    """
    from friday.core.agent import (
        engagement_highlights,
        run_autonomous,
        run_engagement_tiered,
    )

    tier = _tier_from_str(tier_str)
    if tier in (Tier.HEAVY, Tier.MAX) and not allow_paid():
        model = "Opus" if tier is Tier.MAX else "Sonnet"
        role = "orchestrator" if tiered else "loop"
        console.print(
            f"[yellow]Note:[/yellow] [dim]ROUTER_ALLOW_PAID is off — the {role} '{tier_str}' "
            f"falls back to the free tier. Set ROUTER_ALLOW_PAID=1 to use {model}.[/dim]"
        )
    mode = "full auto" if yes else "asks before risky actions"
    scope_note = f" · scope {scope}" if scope else ""
    if tiered:
        console.print(
            f"[cyan]Sutra is working autonomously[/cyan] [dim](tiered · {tier_str} orchestrator "
            f"directs a local-first worker · {mode}{scope_note})[/dim]"
        )
    else:
        console.print(
            f"[cyan]Sutra is working autonomously[/cyan] "
            f"[dim](≤{rounds} rounds · {tier_str} · {mode}{scope_note})[/dim]"
        )

    async def _confirm(skill: str, args: dict[str, Any]) -> bool:
        if yes:
            return True
        console.print(Panel(
            f"[bold]{skill}[/bold]\n[dim]{str(args)[:300]}[/dim]",
            title="[yellow]Confirm risky action[/yellow]", border_style="yellow",
        ))
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: Confirm.ask("  Run this?", default=False)
        )

    async def _run() -> None:
        if tiered:
            report, messages = await run_engagement_tiered(
                goal, orchestrator_tier=tier, max_objectives=objectives,
                worker_rounds=worker_rounds, confirm=_confirm,
                observe=_engagement_observe, scope=scope,
            )
        else:
            report, messages = await run_autonomous(
                goal, max_rounds=rounds, tier=tier, confirm=_confirm,
                observe=_engagement_observe, scope=scope,
            )
        flags, findings = engagement_highlights(messages, report or "")
        if flags:  # the headline — surfaced even if the prose report forgot to restate it
            console.print(Panel(
                "\n".join(f"[bold green]🚩 {f}[/bold green]" for f in flags),
                title="[bold green]Flags captured[/bold green]", border_style="green",
            ))
        console.print(Panel(Markdown(report or "(no report)"), title=title, border_style="green"))
        if findings:
            console.print(Panel(
                Markdown("\n".join(f"- {f}" for f in findings)),
                title="Findings", border_style="cyan",
            ))
        console.print("[dim]Transcript saved under ~/.local/share/friday/engagements/[/dim]")

    asyncio.run(_run())


@app.command()
def auto(
    goal: str = typer.Argument(..., help="The objective to pursue autonomously"),
    rounds: int = typer.Option(25, "--rounds", help="Max tool rounds"),
    tier: str = typer.Option(
        _default_auto_tier(), "--tier",
        help="Model tier: medium (free 70B) / heavy (Sonnet 4.6) / max (Opus 4.8)",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Full auto: run everything without confirming risky actions (you accept the risk)",
    ),
    scope: str = typer.Option(
        "", "--scope",
        help="Authorized targets (IPs/CIDRs/hosts, comma-separated); off-scope commands ask first",
    ),
    single: bool = typer.Option(
        False, "--single", "-S",
        help="Single-tier loop (every step at --tier) instead of the DEFAULT tiered "
             "orchestrator-worker (a strong orchestrator directs a free local-first worker)",
    ),
    objectives: int = typer.Option(
        8, "--objectives",
        help="Tiered: max orchestrator objectives (each is one paid call — lower it for a "
             "cheap bounded first probe, e.g. 2)",
    ),
    worker_rounds: int = typer.Option(
        6, "--worker-rounds", help="Tiered: max worker tool rounds per objective"),
) -> None:
    """Pursue a goal hands-free to completion (authorized CTF). Needs SUTRA_AUTONOMOUS=1.

    Tiered by default — a strong orchestrator plans, a free local-first worker acts (paid budget
    spent only on strategy). Pass --single for the flat single-tier loop.
    """
    _run_engagement(
        goal, rounds, tier, "[bold green]Sutra — engagement report[/bold green]", yes=yes,
        scope=scope or os.getenv("SUTRA_SCOPE") or None, tiered=not single,
        objectives=objectives, worker_rounds=worker_rounds,
    )


@app.command()
def pwn(
    target: str = typer.Argument(..., help="Target IP/host to enumerate and compromise"),
    rounds: int = typer.Option(30, "--rounds", help="Max tool rounds"),
    tier: str = typer.Option(
        _default_auto_tier(), "--tier",
        help="Model tier: medium (free 70B) / heavy (Sonnet 4.6) / max (Opus 4.8)",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Full auto: run everything without confirming risky actions (you accept the risk)",
    ),
    single: bool = typer.Option(
        False, "--single", "-S",
        help="Single-tier loop (every step at --tier) instead of the DEFAULT tiered "
             "orchestrator-worker (a strong orchestrator directs a free local-first worker)",
    ),
    objectives: int = typer.Option(
        8, "--objectives",
        help="Tiered: max orchestrator objectives (each is one paid call — lower it for a "
             "cheap bounded first probe, e.g. 2)",
    ),
    worker_rounds: int = typer.Option(
        6, "--worker-rounds", help="Tiered: max worker tool rounds per objective"),
) -> None:
    """Autonomously enumerate, exploit, and report on a target box. Needs SUTRA_AUTONOMOUS=1.

    Tiered by default — a strong orchestrator plans, a free local-first worker acts. Pass
    --single for the flat single-tier loop.
    """
    goal = (
        f"Enumerate and compromise the box at {target}. Establish VPN reachability if needed, "
        f"scan it, find a foothold, escalate to root, and capture the user and root flags. "
        f"Report the findings, the flags, and how you got them."
    )
    _run_engagement(goal, rounds, tier, f"[bold green]Sutra — {target}[/bold green]", yes=yes,
                    scope=target, tiered=not single, objectives=objectives,
                    worker_rounds=worker_rounds)


@app.command()
def train(
    dataset_only: bool = typer.Option(
        False, "--dataset-only",
        help="Only build the SFT dataset from engagement traces (no GPU, $0); skip training"),
    base_model: str = typer.Option(
        "", "--base-model", help="HF base checkpoint to fine-tune (default: the local worker)"),
    epochs: int = typer.Option(2, "--epochs", help="Training epochs"),
    include_failed: bool = typer.Option(
        False, "--all",
        help="Include engagements that captured no flag/finding (default: only successful ones — "
             "train on what worked)"),
    max_samples: int = typer.Option(
        0, "--max-samples", help="Cap the dataset to the N most recent samples"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the QLoRA plan without training, even if deps are present"),
) -> None:
    """Fine-tune the local worker on what actually worked in past engagements (LoRA).

    Pipeline: captured engagement traces -> SFT dataset -> 4-bit QLoRA over qwen 7B. The training
    deps are an optional extra installed on the GPU box; without them this still builds the
    dataset and prints the exact plan + install command. Run live engagements first (sutra pwn
    <ip> -y) so there is something to learn from.
    """
    from friday.core import train as trainer

    stats = trainer.build_dataset(include_failed=include_failed, max_samples=max_samples)
    console.print(Panel(
        f"traces: {stats['traces']}  ->  samples: {stats['samples']}\n"
        f"skipped — no flag/finding: {stats['skipped_unsuccessful']}   "
        f"no tool action: {stats['skipped_no_action']}\n"
        f"dataset: {stats['path']}",
        title="[bold cyan]Training dataset[/bold cyan]", border_style="cyan"))
    if stats["samples"] == 0:
        console.print(
            "[yellow]No training samples yet.[/yellow] Run live engagements first "
            "(sutra pwn <ip> -y), then re-run sutra train.")
        if not dataset_only:
            return
    if dataset_only:
        return
    status = trainer.run_finetune(base_model=base_model or trainer.DEFAULT_BASE_MODEL,
                                  epochs=epochs, dry_run=dry_run)
    # escape: the status carries a literal pip command with [train], which is Rich markup
    console.print(Panel(escape(status), title="[bold green]Fine-tune[/bold green]",
                        border_style="green"))


def _sutra_exec() -> str:
    """The command a scheduler should run for the study pass — the installed binary if present,
    else the module entrypoint with this interpreter."""
    return shutil.which("sutra") or f"{sys.executable} -m friday.cli"


def _install_study_timer() -> None:
    """Write a systemd --user service + timer that runs `sutra study` continuously while the
    machine is on (a few min after boot, then every 30 min). Each pass skips the model call when
    nothing has changed, so this is cheap. Writes only to the user unit dir; turning it on stays
    the operator's explicit step."""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "sutra-study.service").write_text(
        "[Unit]\nDescription=Sutra self-improvement pass\n\n"
        f"[Service]\nType=oneshot\nExecStart={_sutra_exec()} study\n")
    (unit_dir / "sutra-study.timer").write_text(
        "[Unit]\nDescription=Sutra study — learn continuously while the machine is on\n\n"
        "[Timer]\nOnBootSec=5min\nOnUnitActiveSec=30min\nPersistent=true\n\n"
        "[Install]\nWantedBy=timers.target\n")
    console.print(Panel(
        f"Wrote {unit_dir}/sutra-study.{{service,timer}}\n\n"
        "Runs ~5 min after boot, then every 30 min while the laptop is on (a user timer, so only\n"
        "while on; each pass is a no-op unless there is new run data, so it stays cheap).\n\n"
        "Enable it:\n"
        "  systemctl --user daemon-reload\n"
        "  systemctl --user enable --now sutra-study.timer\n\n"
        "Keep it running while logged out:  loginctl enable-linger $USER",
        title="[bold cyan]Study timer installed[/bold cyan]", border_style="cyan"))


@app.command()
def study(
    apply: bool = typer.Option(
        False, "--apply",
        help="Also grow the playbook with the principles (default: propose only, you review)"),
    force: bool = typer.Option(
        False, "--force", help="Reflect even if nothing has changed since the last pass"),
    tier: str = typer.Option("medium", "--tier", help="medium (free 70B) / heavy / max"),
    install_timer: bool = typer.Option(
        False, "--install-timer",
        help="Write a systemd --user timer to learn continuously while the machine is on"),
) -> None:
    """Sutra's self-improvement pass: learn from real runs, propose new doctrine, leave a digest.

    Grounded + $0 + takes no actions — it reflects on real engagement traces/lessons with the free
    model and writes a dated digest to ~/.local/share/friday/study/. By DEFAULT it only PROPOSES
    new principles (you are the gate); pass --apply to grow the playbook. Safe to run continuously:
    a pass with nothing new since the last one SKIPS the model call (use --force to override). With
    no run data yet it says so honestly instead of inventing lessons. Schedule it 24/7 while the
    laptop is on with --install-timer.
    """
    if install_timer:
        _install_study_timer()
        return
    from friday.core import study as study_mod

    path, refl, status = asyncio.run(
        study_mod.run_study(write=apply, tier=_tier_from_str(tier), force=force))
    if status == "unchanged":
        body = (f"Nothing new since the last pass — skipped (normal for the 24/7 timer; it only "
                f"reflects when there is new run data).\nlatest digest: {path}")
    elif status == "no-data":
        body = f"No run data yet — wrote an honest digest, invented nothing.\ndigest: {path}"
    else:
        learned = "\n".join(f"- {p}" for p in (refl.principles if refl else [])) or "- (none)"
        head = "Added to the playbook" if apply else "Proposed (review, then re-run with --apply)"
        body = f"digest: {path}\n\n{head}:\n{learned}"
    console.print(Panel(body, title="[bold green]Sutra study[/bold green]", border_style="green"))


def _render_learning(snap: Any) -> Any:
    """Build the Rich view of what Sutra is learning / has learned from a LearningSnapshot."""
    from rich.console import Group

    status = "[green]🟢 active[/green]" if snap.timer_active else "[dim]⚪ idle[/dim]"
    total_chunks = sum(n for _, n in snap.collections)
    parts: list[Any] = [Panel(
        f"study timer: {status}    last pass: {escape(snap.last_study)}    "
        f"knowledge: {total_chunks} chunks / {len(snap.collections)} collection(s)",
        title="[bold]🧠 Sutra — what it's learning[/bold]", border_style="cyan")]

    doctrine = ("\n".join(f"• {escape(p)}" for p in snap.doctrine)
                or "[dim]no earned principles yet — runs + study passes grow these[/dim]")
    parts.append(Panel(doctrine, title="[bold]Doctrine it has earned[/bold]", border_style="green"))

    if snap.digests:
        t = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        t.add_column("pass", style="cyan", no_wrap=True)
        t.add_column("what it concluded")
        for date, headline in snap.digests:
            t.add_row(escape(date), escape(headline))
        parts.append(Panel(t, title="[bold]Recent study passes[/bold]", border_style="blue"))

    if snap.lessons:
        body = "\n".join(f"[cyan]{escape(topic)}[/cyan] — {escape(lesson)}"
                         for topic, lesson in snap.lessons)
        parts.append(Panel(body, title="[bold]Lessons from real engagements[/bold]",
                           border_style="magenta"))

    if snap.collections:
        body = "\n".join(f"📚 {escape(name)}: {n} chunks" for name, n in snap.collections)
        parts.append(Panel(body, title="[bold]Knowledge base (recallable)[/bold]",
                           border_style="yellow"))
    return Group(*parts)


@app.command()
def learning(
    watch: bool = typer.Option(False, "--watch", "-w", help="Live view; refreshes until Ctrl+C"),
    interval: float = typer.Option(5.0, "--interval", help="Refresh seconds in --watch mode"),
) -> None:
    """Show what Sutra is learning right now and what it has learned — its earned doctrine, recent
    study passes, lessons from real engagements, and the knowledge base it can recall. --watch keeps
    it live. (Learning itself auto-runs via the study timer; this is just the window into it.)"""
    from friday.core import learning as learn

    if not watch:
        console.print(_render_learning(learn.snapshot()))
        return
    import time

    from rich.live import Live
    with Live(_render_learning(learn.snapshot()), console=console, refresh_per_second=2,
              screen=False) as live:
        try:
            while True:
                time.sleep(max(1.0, interval))
                live.update(_render_learning(learn.snapshot()))
        except KeyboardInterrupt:
            pass


@app.command()
def tutor(
    topic: str = typer.Argument("", help="What to learn / where you're stuck. Omit for a session."),
    tier: str = typer.Option("medium", "--tier", help="medium (free 70B) / heavy / max"),
) -> None:
    """Pentest mentor — Sutra teaches the methodology and the WHY, not just the answer.

    With a TOPIC it answers once; with no topic it opens an interactive coaching session
    (/screen to read your screen, /answer to just be told the step, /quit).
    """
    from friday.core.agent import (
        _latest_engagement_findings,
        note_hint_outcome_from_history,
        persist_turn_async,
        update_skill_profile_async,
    )
    from friday.core.agent import tutor as _tutor

    t = _tier_from_str(tier)

    async def _once() -> None:
        eng = _latest_engagement_findings()
        if eng:
            console.print("[dim]· coaching from your last engagement's findings[/dim]")
        with console.status("[cyan]thinking like a mentor…[/cyan]"):
            answer = await _tutor(topic, engagement=eng, tier=t)
        console.print(Panel(
            Markdown(answer or "(no answer)"),
            title="[bold green]Sutra — pentest tutor[/bold green]", border_style="green",
        ))

    async def _session() -> None:
        from friday.skills import registry
        registry.autodiscover()  # so /screen can call see_screen
        history: list[dict[str, Any]] = []
        engagement = _latest_engagement_findings()  # coach from the box his loop just worked
        loaded = "  [green]· last engagement loaded[/green]" if engagement else ""
        console.print(Panel(
            "[bold green]Sutra · pentest tutor[/bold green] — I coach, you hack.\n"
            "[dim]Tell me the room and where you're stuck.  "
            "/screen = read your screen   /box = reload last engagement   "
            f"/answer = just tell me   /quit[/dim]{loaded}",
            border_style="green",
        ))
        while True:
            try:
                said = console.input("[bold green]you› [/bold green]").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Good hunting.[/dim]")
                break
            if not said:
                continue
            if said.lower() in ("/quit", "/exit", "quit", "exit"):
                console.print("[dim]Good hunting.[/dim]")
                break
            note_hint_outcome_from_history(history, said)  # did the last hint land? → learn from it
            if said.lower() == "/box":
                engagement = _latest_engagement_findings()
                msg = "reloaded your last engagement" if engagement else "no engagement found yet"
                console.print(f"[dim]· {msg}[/dim]")
                continue
            screen = None
            if said.lower() == "/screen":
                with console.status("[cyan]looking at your screen…[/cyan]"):
                    res = await registry.call("see_screen")
                screen = res.get("description") if isinstance(res, dict) else None
                if not screen:
                    err = res.get("error", "couldn't read the screen") if isinstance(res, dict) \
                        else "couldn't read the screen"
                    console.print(f"[yellow]{err}[/yellow]")
                    continue
                said = "Here is my screen — what should I look at, and what's a good next step?"
            elif said.lower() == "/answer":
                said = "Just tell me the next step and the exact command."
            with console.status("[cyan]thinking…[/cyan]"):
                reply = await _tutor(said, history=history, screen=screen,
                                     engagement=engagement, tier=t)
            console.print(Panel(Markdown(reply), border_style="green", padding=(0, 1)))
            history.append({"role": "user", "content": said})
            history.append({"role": "assistant", "content": reply})
            history[:] = history[-24:]
            persist_turn_async(said, reply, source="tutor")
            update_skill_profile_async(said, reply, tier=t)  # learn his level → adapt next turns

    asyncio.run(_once() if topic.strip() else _session())


skill_app = typer.Typer(help="Self-extending skills — Sutra drafts a skill, you review + approve.")
app.add_typer(skill_app, name="skill")


def _code_panel(name: str, code: str) -> Panel:
    return Panel(Markdown(f"```python\n{code}\n```"), title=f"[bold]{name}[/bold]",
                 border_style="cyan")


def _print_flags(flags: list[str]) -> None:
    if flags:
        console.print("[yellow]⚠ review these before approving:[/yellow]")
        for fl in flags:
            console.print(f"  [yellow]•[/yellow] {fl}")
    else:
        console.print("[green]✓ no risky patterns flagged[/green]")


@skill_app.command("draft")
def skill_draft(
    description: str = typer.Argument(..., help="What the new skill should do"),
    tier: str = typer.Option("medium", "--tier", help="medium (free 70B) / heavy / max"),
) -> None:
    """Draft a new skill from a description. Writes an INERT file — it never runs the code."""
    from friday.core.skillsmith import draft_skill

    async def _run() -> None:
        with console.status("[cyan]drafting a skill…[/cyan]"):
            out = await draft_skill(description, tier=_tier_from_str(tier))
        if "error" in out:
            console.print(f"[red]{out['error']}[/red]")
            return
        console.print(_code_panel(f"draft: {out['name']}", out["code"]))
        _print_flags(out["flags"])
        console.print(
            f"[dim]review: sutra skill show {out['name']}  ·  approve: sutra skill approve "
            f"{out['name']}  ·  discard: sutra skill reject {out['name']}[/dim]"
        )

    asyncio.run(_run())


@skill_app.command("list")
def skill_list() -> None:
    """List pending drafts and approved (loaded) self-extended skills."""
    from friday.core.skillsmith import list_pending
    from friday.skills.registry import USER_SKILLS_DIR

    approved = (
        sorted(p.stem for p in USER_SKILLS_DIR.glob("*.py")) if USER_SKILLS_DIR.is_dir() else []
    )
    pending = ", ".join(list_pending()) or "(none)"
    console.print(f"[bold]pending (awaiting approval):[/bold] {pending}")
    console.print(f"[bold]approved (loaded):[/bold] {', '.join(approved) or '(none)'}")


@skill_app.command("show")
def skill_show(name: str = typer.Argument(..., help="Pending skill name")) -> None:
    """Show a pending draft's code and its safety flags."""
    from friday.core.skillsmith import read_pending, scan_code

    code = read_pending(name)
    if code is None:
        console.print(f"[red]no pending skill named '{name}'[/red]")
        return
    console.print(_code_panel(name, code))
    _print_flags(scan_code(code))


@skill_app.command("approve")
def skill_approve(name: str = typer.Argument(..., help="Pending skill to approve")) -> None:
    """Approve a drafted skill — promotes it so it loads on the next run. Your explicit go."""
    from friday.core.skillsmith import approve_skill

    dst = approve_skill(name)
    if dst is None:
        console.print(f"[red]no pending skill named '{name}'[/red]")
        return
    console.print(f"[green]✓ approved[/green] [dim]{dst}[/dim] — loads on the next `sutra` run.")


@skill_app.command("reject")
def skill_reject(name: str = typer.Argument(..., help="Pending skill to discard")) -> None:
    """Discard a pending draft."""
    from friday.core.skillsmith import reject_skill

    console.print("[green]✓ discarded[/green]" if reject_skill(name)
                  else f"[red]no pending skill named '{name}'[/red]")


plan_app = typer.Typer(
    help="Task planner — break a big goal into steps, run them one at a time, resume after a pause."
)
app.add_typer(plan_app, name="plan")

_PLAN_MARK = {"done": ("✓", "green"), "failed": ("✗", "red"),
              "skipped": ("–", "yellow"), "pending": ("○", "dim")}


def _render_plan(p: Any) -> None:
    done, total = p.progress()
    console.print(f"[bold]{p.goal}[/bold] [dim]({p.slug})[/dim] — {done}/{total} [{p.status}]")
    for s in p.steps:
        mark, color = _PLAN_MARK.get(s.status, ("?", "white"))
        console.print(f"  [{color}]{mark}[/{color}] {s.n}. {s.description}")
        if s.result and s.status in ("done", "failed"):
            console.print(f"     [dim]{s.result[:140]}[/dim]")


def _plan_confirm(yes: bool):
    """Hands-free auto-approve with -y, else an interactive y/n per gated step."""
    async def _auto(skill: str, args: dict[str, Any]) -> bool:
        console.print(f"     [dim]↳ auto-approved {skill}[/dim]")
        return True

    async def _ask(skill: str, args: dict[str, Any]) -> bool:
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: Confirm.ask(f"     run [bold]{skill}[/bold]?", default=False)
        )

    return _auto if yes else _ask


def _plan_observe(kind: str, data: dict[str, Any]) -> None:
    if kind == "step_start":
        console.print(f"\n[cyan]▶ {data['n']}.[/cyan] {data['description']}")
    elif kind == "step_end":
        mark, color = _PLAN_MARK.get(data.get("status", ""), ("?", "white"))
        console.print(f"  [{color}]{mark} {data.get('status')}[/{color}]")


@plan_app.command("run")
def plan_run(
    goal: str = typer.Argument(..., help="The goal to plan and execute"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-approve gated steps (hands-free)"),
) -> None:
    """Break a goal into steps and run them, saving after each so it's resumable."""
    from friday.core.planner import make_plan, run_plan, save, synthesize_plan

    async def _run() -> None:
        with console.status("[cyan]planning…[/cyan]"):
            plan = await make_plan(goal)
        if not plan.steps:
            console.print("[red]couldn't break that into steps — try rephrasing the goal.[/red]")
            return
        save(plan)
        console.print("[bold]Plan:[/bold]")
        _render_plan(plan)
        out = await run_plan(plan, confirm=_plan_confirm(yes), observe=_plan_observe)
        console.print("\n[bold]Result:[/bold]")
        _render_plan(out)
        if out.status == "paused":
            console.print(
                f"\n[yellow]paused[/yellow] — resume: [cyan]sutra plan resume {out.slug}[/cyan]"
            )
        else:
            with console.status("[cyan]summarizing…[/cyan]"):
                summary = await synthesize_plan(out)
            console.print(Panel(Markdown(summary), title="Outcome",
                                border_style="cyan", padding=(0, 1)))

    asyncio.run(_run())


@plan_app.command("resume")
def plan_resume(
    slug: str = typer.Argument(..., help="Plan slug to resume (see: sutra plan list)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-approve gated steps"),
) -> None:
    """Resume a paused plan from exactly where it stopped."""
    from friday.core.planner import resume_plan, synthesize_plan

    async def _run() -> None:
        out = await resume_plan(slug, confirm=_plan_confirm(yes), observe=_plan_observe)
        if out is None:
            console.print(f"[red]no plan '{slug}'[/red] — see: [cyan]sutra plan list[/cyan]")
            return
        console.print("\n[bold]Result:[/bold]")
        _render_plan(out)
        if out.status != "paused":
            with console.status("[cyan]summarizing…[/cyan]"):
                summary = await synthesize_plan(out)
            console.print(Panel(Markdown(summary), title="Outcome",
                                border_style="cyan", padding=(0, 1)))

    asyncio.run(_run())


@plan_app.command("list")
def plan_list() -> None:
    """List saved plans, newest first."""
    from friday.core.planner import list_plans

    plans = list_plans()
    if not plans:
        console.print('[dim]no plans yet — make one: sutra plan run "<goal>"[/dim]')
        return
    for p in plans:
        done, total = p.progress()
        color = _PLAN_MARK.get(p.status, ("?", "white"))[1]
        console.print(f"  [{color}]•[/{color}] [bold]{p.slug}[/bold] "
                      f"[dim]{done}/{total} {p.status}[/dim] — {p.goal[:60]}")


@plan_app.command("status")
def plan_status(slug: str = typer.Argument(..., help="Plan slug (see: sutra plan list)")) -> None:
    """Show a plan's steps and progress."""
    from friday.core.planner import load

    p = load(slug)
    if p is None:
        console.print(f"[red]no plan '{slug}'[/red]")
        return
    _render_plan(p)


@app.command()
def mcp() -> None:
    """Connect to configured MCP servers and list the tools they expose to Sutra."""
    from friday.mcp import get_bridge, mcp_configured
    from friday.mcp.bridge import close_bridge
    from friday.skills.registry import get as get_skill

    if not mcp_configured():
        console.print(
            "[yellow]No MCP servers configured.[/yellow] "
            "Copy [cyan]mcp_servers.example.json[/cyan] → [cyan]mcp_servers.json[/cyan] "
            "(or ~/.config/sutra/mcp_servers.json), set [cyan]disabled: false[/cyan], and re-run."
        )
        return

    async def _run() -> None:
        with console.status("[cyan]connecting to MCP servers…[/cyan]"):
            bridge = await get_bridge()
        if not bridge or not bridge.servers:
            console.print("[red]No servers came up.[/red] Check commands in your config.")
            await close_bridge()
            return
        t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
        t.add_column("Tool (skill name)")
        t.add_column("Gate")
        for name in bridge.tool_names:
            auto = get_skill(name).auto_confirm
            gated = "[green]auto[/green]" if auto else "[yellow]confirm[/yellow]"
            t.add_row(name, gated)
        console.print(
            f"[green]Connected:[/green] {', '.join(bridge.servers)}  "
            f"[dim]({len(bridge.tool_names)} tool(s))[/dim]"
        )
        console.print(t)
        await close_bridge()

    asyncio.run(_run())


@app.command()
def secrets(
    import_env: bool = typer.Option(
        False, "--import", help="Move .env secrets into the OS keyring"
    ),
    set_name: str = typer.Option(
        "", "--set", metavar="NAME",
        help="Securely store a secret in the keyring via a HIDDEN prompt "
             "(e.g. --set ANTHROPIC_API_KEY). Paste at the prompt — never on the command line.",
    ),
) -> None:
    """Show where each secret resolves from (keyring/env/missing); --import from .env; --set NAME
    to enter one securely (hidden prompt, so a key never lands in shell history or a transcript)."""
    from friday.core.secrets import import_from_env, set_secret
    from friday.core.secrets import status as secret_status

    if set_name:
        import getpass

        value = getpass.getpass(f"Paste value for {set_name} (hidden, not echoed): ").strip()
        if not value:
            console.print("[yellow]nothing entered — aborted.[/yellow]")
            return
        ok = set_secret(set_name, value)
        console.print(
            f"[green]✓ stored {set_name} in the keyring[/green]" if ok
            else "[red]keyring unavailable — not stored.[/red]"
        )
        return

    if import_env:
        moved = import_from_env()
        console.print(f"[green]Imported into keyring:[/green] {', '.join(moved) or '(none)'}")
    t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
    t.add_column("Secret")
    t.add_column("Source")
    colors = {"keyring": "green", "env": "yellow", "missing": "red"}
    for name, src in secret_status().items():
        t.add_row(name, f"[{colors[src]}]{src}[/{colors[src]}]")
    console.print(t)


@app.command()
def telegram() -> None:
    """Run the Sutra Telegram bot (long-polling). Requires TELEGRAM_BOT_TOKEN.

    Token resolution order: OS keyring (preferred) → TELEGRAM_BOT_TOKEN env var.
    Get a token by messaging @BotFather on Telegram (use /newbot).
    Restrict access to your own chat by setting TELEGRAM_ALLOWED_CHAT_IDS.
    """
    from friday.core.secrets import get_secret

    token = get_secret("TELEGRAM_BOT_TOKEN")
    if not token:
        console.print(
            "[red]No TELEGRAM_BOT_TOKEN found.[/red]\n"
            "  Set it in the keyring:  sutra secrets --import  (after adding to .env)\n"
            "  Or export TELEGRAM_BOT_TOKEN=<your_token> before running.\n"
            "  Get a token from @BotFather on Telegram."
        )
        raise typer.Exit(1)

    try:
        from friday.telegram.bot import run_bot
    except ImportError:
        console.print(
            "[red]python-telegram-bot is not installed.[/red]\n"
            "  pip install 'friday-os[telegram]'"
        )
        raise typer.Exit(1)

    console.print("[cyan]Sutra Telegram bridge starting…[/cyan] (Ctrl+C to stop)")
    run_bot(token)


if __name__ == "__main__":
    app()
