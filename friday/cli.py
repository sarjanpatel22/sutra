"""Rich-based REPL for Sutra."""

from __future__ import annotations

import asyncio
import os
import re
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


def _slug(s: str) -> str:
    """Filesystem-safe short slug for a draft filename."""
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-")[:40] or "role"


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


@app.command()
def apply(
    source: str = typer.Argument("", help="Job posting URL, or pasted text"),
    file: str = typer.Option("", "--file", "-f", help="Read the posting text from a file"),
    resume: str = typer.Option("", "--resume", "-r", help="Resume path (else default)"),
    tier: str = typer.Option("medium", "--tier", "-t", help="Model tier for the letter"),
    status: str = typer.Option("drafted", "--status", help="Tracker status to save as"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the approval prompt"),
    no_save: bool = typer.Option(False, "--no-save", help="Draft only; don't track it"),
) -> None:
    """Fit-check a posting against your resume, draft a tailored cover letter grounded
    ONLY in your real resume, and (on your approval) track it. It never sends anything."""
    import datetime
    import subprocess

    from friday.core import postcheck
    from friday.core.router import complete
    from friday.skills import job_hunt

    async def _flow() -> None:
        src = source
        if file:
            try:
                src = Path(file).expanduser().read_text()
            except OSError as e:
                console.print(f"[red]Could not read {file}: {e}[/red]")
                raise typer.Exit(1) from e
        if not src.strip():
            console.print("[red]Give a posting URL or text, or --file a posting.[/red]")
            raise typer.Exit(1)

        with console.status("[dim]Reading the posting…[/dim]"):
            posting = await job_hunt.parse_job_posting(src)
        if "error" in posting:
            console.print(f"[red]{posting['error']}[/red]")
            raise typer.Exit(1)
        res = await job_hunt.load_resume(path=resume)
        if "error" in res:
            console.print(f"[red]Resume: {res['error']}[/red]")
            raise typer.Exit(1)
        fit = await job_hunt.fit_check(posting["text"], res["text"])

        company = posting["company"] or Prompt.ask("Company name")
        role = posting["title"] or Prompt.ask("Role title")
        console.print(Panel(
            f"[bold]{escape(role)}[/bold]\n{escape(company)}\n\n"
            f"Fit: [bold]{fit.get('score', '?')}%[/bold]  "
            f"({len(fit.get('matched', []))}/{fit.get('considered', '?')} key terms)\n\n"
            f"[green]Real strengths:[/green] "
            f"{escape(', '.join(fit.get('matched', [])[:15]) or '—')}\n"
            f"[yellow]Gaps (omit or address honestly):[/yellow] "
            f"{escape(', '.join(fit.get('missing', [])[:15]) or '—')}",
            title="Fit check", border_style="cyan"))

        sys_prompt = (
            "You help write a job-application cover letter. Use ONLY facts, employers, "
            "dates, titles, skills and achievements that appear in the RESUME. Never "
            "invent or inflate anything. If the posting wants something not in the resume, "
            "do not claim it — omit it or honestly frame adjacent real experience. Write "
            "first-person, specific to THIS posting, ~250-320 words, confident and plain. "
            "Use the real company and role names; no [bracketed] placeholders."
        )
        user_prompt = (
            f"POSTING\nRole: {role}\nCompany: {company}\n\n{posting['text'][:4000]}\n\n"
            f"RESUME (the only source of truth about me)\n{res['text'][:6000]}\n\n"
            f"Lean on these real matches: {', '.join(fit.get('matched', [])[:15])}.\n"
            "Write the cover letter now."
        )
        with console.status("[dim]Drafting a tailored letter (grounded in your resume)…[/dim]"):
            resp = await complete(
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": user_prompt}],
                tier=_tier_from_str(tier), temperature=0.6, max_tokens=900)
        letter = (resp.choices[0].message.content or "").strip()
        if not letter:
            console.print("[red]The model returned an empty letter — try --tier heavy.[/red]")
            raise typer.Exit(1)
        console.print(Panel(Markdown(letter), title="Draft cover letter", border_style="green"))
        console.print("[dim]Grounded in your resume only. Read it before sending — "
                      "Sutra does not send anything.[/dim]")

        if no_save:
            return
        if not yes and not Confirm.ask(
                "Save this to your tracker + a draft file?", default=True):
            console.print("[yellow]Not saved.[/yellow]")
            return

        outdir = Path.home() / "job-applications"
        outdir.mkdir(parents=True, exist_ok=True)
        fpath = outdir / (
            f"{_slug(company)}_{_slug(role)}_{datetime.date.today().isoformat()}.md")
        header = (
            f"# {role} — {company}\n\n"
            f"- Fit: {fit.get('score', '?')}%  "
            f"({len(fit.get('matched', []))}/{fit.get('considered', '?')} key terms)\n"
            f"- Posting: {posting.get('url') or '(pasted text)'}\n"
            f"- Gaps to address: {', '.join(fit.get('missing', [])[:12]) or '—'}\n\n---\n\n")
        fpath.write_text(header + letter + "\n")

        saved = await job_hunt.save_application(
            company, role, url=posting.get("url", ""), status=status,
            fit_score=fit.get("score"), notes=f"draft: {fpath}")
        verdict = postcheck.verify("save_application", {}, saved)
        if verdict and not verdict[0]:
            console.print(f"[red]Save could not be verified: {verdict[1]}[/red]")
            raise typer.Exit(1)
        console.print(
            f"[green]Tracked[/green] as application #{saved['application']['id']} "
            f"({status}). Draft saved: [bold]{fpath}[/bold]")
        if shutil.which("xdg-open"):
            try:
                subprocess.Popen(["xdg-open", str(fpath)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:  # noqa: BLE001 — opening the draft is a nicety, never fatal
                pass

    asyncio.run(_flow())


@app.command()
def jobs(status: str = typer.Option("", "--status", "-s", help="Filter by status")) -> None:
    """Show your tracked job applications and where each one stands."""
    from friday.skills import job_hunt

    data = asyncio.run(job_hunt.list_applications(status=status))
    apps = data["applications"]
    if not apps:
        console.print("[dim]No tracked applications yet — use `sutra apply <posting>`.[/dim]")
        return
    t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
    for col in ("ID", "Company", "Role", "Status", "Fit", "Updated"):
        t.add_column(col)
    for a in apps:
        fit = a.get("fit_score")
        t.add_row(
            str(a.get("id", "")), a.get("company", ""), a.get("role", ""),
            a.get("status", ""), f"{fit}%" if fit is not None else "—", a.get("updated", ""))
    console.print(t)
    console.print(f"[dim]{data['count']} application(s). "
                  "Move one along: `sutra chat` → 'mark #N applied'.[/dim]")


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


def _detached() -> bool:
    """True when no interactive terminal is attached to answer a y/n prompt.

    A backgrounded or piped run can't answer a confirm, so a gate would otherwise
    default-decline silently and look like a hang. Callers use this to decline
    loudly (and hint --yes) instead."""
    try:
        return not sys.stdin.isatty()
    except Exception:
        return True


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

    if not yes and _detached():
        console.print(
            "[yellow]Heads up:[/yellow] [dim]no terminal attached and no --yes — risky "
            "steps will be auto-declined, so the run may stop early. "
            "Re-run with --yes for hands-free.[/dim]"
        )

    async def _confirm(skill: str, args: dict[str, Any]) -> bool:
        if yes:
            return True
        if _detached():  # nobody can answer a prompt in a detached run — decline loudly, don't hang
            console.print(f"[dim]  auto-declined (no terminal, no --yes): {skill}[/dim]")
            return False
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
def bounty(
    target: str = typer.Argument(..., help="Target URL/host to hunt (AUTHORIZED targets only)"),
    severity: str = typer.Option("low,medium,high,critical", "--severity",
                                 help="nuclei severities to include"),
    tags: str = typer.Option("", "--tags", help="restrict nuclei templates (e.g. cve,exposure)"),
    program: str = typer.Option("", "--program", help="program name for the report header"),
    out: str = typer.Option("", "--out", help="write the report here (default: bounty/ dir)"),
) -> None:
    """Hunt a target for known vulns and draft a submittable bug-bounty report. AUTHORIZED ONLY.

    The TOOL finds (nuclei, real grounded output) -> this DRAFTS the report (repro, CWE, CVSS,
    references) -> YOU verify and submit. Nothing is invented; every fact comes from the scan.
    """
    import re as _re
    from datetime import date
    from pathlib import Path

    from friday.core.bounty import draft_bounty_report
    from friday.skills.web_recon import nuclei_scan

    console.print(f"[cyan]Scanning {target} with nuclei…[/cyan]  (authorized targets only)")
    res = asyncio.run(nuclei_scan(target, severity=severity, tags=tags))
    if res.get("error"):
        console.print(f"[red]{res['error']}[/red]")
        raise typer.Exit(1)
    findings = res.get("findings", [])
    console.print(f"[green]{len(findings)} finding(s)[/green]"
                  + (f" — {res['note']}" if res.get("note") else ""))
    report = draft_bounty_report(target, findings, program=program, surfaces=[target])
    slug = _re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-")[:50] or "target"
    path = Path(out) if out else (
        Path.home() / ".local" / "share" / "friday" / "bounty" / f"{slug}-{date.today()}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report)
    preview = report[:1800] + ("\n\n…(full report saved)" if len(report) > 1800 else "")
    console.print(Panel(Markdown(preview), title="[bold]Draft bounty report[/bold]",
                        border_style="cyan", padding=(0, 1)))
    console.print(f"[bold]Saved:[/bold] {path}  [dim](verify before submitting)[/dim]")


@app.command()
def hunt(
    target: str = typer.Argument(..., help="Program domain (auto-enumerated) or a URL/host. "
                                           "AUTHORIZED programs only"),
    enum: bool = typer.Option(None, "--enum/--no-enum",
                              help="Force subdomain enumeration on/off (default: auto-detect)"),
    max_hosts: int = typer.Option(8, "--max-hosts", help="Max live hosts to scan"),
    severity: str = typer.Option("low,medium,high,critical", "--severity"),
    tags: str = typer.Option("", "--tags", help="restrict nuclei templates (overrides --profile)"),
    profile: str = typer.Option("high-value", "--profile",
                                help="template set: high-value (the classes that pay) / all"),
    scope: str = typer.Option("", "--scope",
                              help="in-scope domain patterns (e.g. '*.api.x.com'); default target"),
    exclude: str = typer.Option("", "--exclude", help="out-of-scope domain patterns (deny-list)"),
    content: bool = typer.Option(False, "--content/--no-content",
                                 help="also brute-force hidden paths/files (slower; finds exposed "
                                      "backups/.git/panels nuclei misses)"),
    active: bool = typer.Option(False, "--active/--no-active",
                                help="DAST: actively probe params for SSRF/SQLi/injection (smarter "
                                     "than brute-force, more intrusive — only if the program "
                                     "ALLOWS active testing)"),
    probe: bool = typer.Option(False, "--probe/--no-probe",
                               help="targeted confirmed-only probes: discover params then test "
                                    "SQLi/LFI/CRLF/SSRF/XXE + CSRF + API/GraphQL-batching "
                                    "(complements DAST — only if the program ALLOWS active "
                                    "testing)"),
    out: str = typer.Option("", "--out", help="write the report here (default: bounty/ dir)"),
) -> None:
    """One-command bug-bounty hunt: enumerate the assets -> probe which are live -> nuclei-scan
    each -> ONE combined, submittable report. The tools find, the report drafts, YOU verify and
    submit. AUTHORIZED programs only — pass --scope/--exclude to honour the program's scope.
    """
    import re as _re
    from datetime import date
    from pathlib import Path

    from friday.core.hunt import hunt as run_hunt

    def _obs(kind: str, data: dict) -> None:
        if kind == "enumerated":
            console.print(f"  [cyan]{data['count']} subdomain(s) found, "
                          f"{data.get('in_scope', data['count'])} in scope[/cyan]")
        elif kind == "hosts":
            console.print(f"  [cyan]{len(data['live'])} live host(s) to scan[/cyan]")
        elif kind == "scan":
            console.print(f"  [dim]scanning {data['host']}…[/dim]")
        elif kind == "content":
            console.print(f"  [dim]content discovery on {data['host']}…[/dim]")
        elif kind == "content_found" and data.get("found"):
            console.print(f"  [green]{data['found']} sensitive path(s) on {data['host']}[/green]")
        elif kind == "crawled" and data.get("found"):
            console.print(f"  [cyan]{data['found']} URL(s)/endpoints mapped on {data['host']}"
                          f"[/cyan]")
        elif kind == "active":
            console.print(f"  [dim]active DAST testing on {data['host']}…[/dim]")
        elif kind == "active_found" and data.get("found"):
            console.print(f"  [magenta]{data['found']} active DAST finding(s)[/magenta]")
        elif kind == "probe":
            console.print(f"  [dim]injection probes (params/SQLi/LFI/CRLF/XXE) on {data['host']}…"
                          f"[/dim]")
        elif kind == "probe_found" and data.get("found"):
            console.print(f"  [magenta]{data['found']} injection finding(s) on {data['host']}"
                          f"[/magenta]")
        elif kind == "scanned" and data.get("found"):
            console.print(f"  [green]{data['found']} finding(s) on {data['host']}[/green]")

    console.print(f"[cyan]Hunting {target}…[/cyan]  (authorized programs only)")
    res = asyncio.run(run_hunt(target, enum=enum, max_hosts=max_hosts, severity=severity,
                               tags=tags, profile=profile, scope=scope, exclude=exclude,
                               content=content, active=active, probe=probe, observe=_obs))
    if res.get("error"):
        console.print(f"[red]{res['error']}[/red]")
        raise typer.Exit(1)
    findings, hosts = res["findings"], res["hosts"]
    console.print(f"[bold]{len(findings)} finding(s) across {len(hosts)} host(s).[/bold]")
    for w in res.get("warnings", []):  # incomplete scans — '0 findings' is NOT 'clean'
        console.print(f"  [yellow]⚠ {w}[/yellow]")
    slug = _re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-")[:50] or "target"
    path = Path(out) if out else (
        Path.home() / ".local" / "share" / "friday" / "bounty" / f"hunt-{slug}-{date.today()}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(res["report"])
    preview = res["report"][:1800] + ("\n\n…(full report saved)" if len(res["report"]) > 1800
                                      else "")
    console.print(Panel(Markdown(preview), title="[bold]Hunt — draft report[/bold]",
                        border_style="cyan", padding=(0, 1)))
    console.print(f"[bold]Saved:[/bold] {path}  [dim](verify each finding before submitting)[/dim]")


@app.command()
def redteam(
    endpoint: str = typer.Argument(..., help="Chat endpoint URL — e.g. "
        "http://localhost:11434/api/chat (Ollama) or an OpenAI-compatible /v1/chat/completions. "
        "AUTHORIZED targets only (your own models or an arena/program that invited you)"),
    model: str = typer.Option("", "--model", "-m", help="model name the endpoint expects"),
    api: str = typer.Option("auto", "--api", help="auto | openai | ollama"),
    key: str = typer.Option("", "--key", help="bearer token for hosted endpoints (never local)"),
    out: str = typer.Option("", "--out", help="write the report here (default: bounty/ dir)"),
    judge_model: str = typer.Option("", "--judge-model", help="enable BLACK-BOX judge mode — a "
        "trusted model (e.g. a local qwen2.5:14b) rules whether a real target leaked its hidden "
        "prompt (candidate-tier). Use for 0din/arena targets where no canary can be planted"),
    judge_endpoint: str = typer.Option("", "--judge-endpoint",
        help="where the judge model lives (default: the target endpoint)"),
    judge_api: str = typer.Option("auto", "--judge-api", help="auto|openai|ollama for the judge"),
    judge_key: str = typer.Option("", "--judge-key", help="bearer token for the judge endpoint"),
    objective: str = typer.Option("", "--objective", help="BLACK-BOX objective mode (arena/Gray "
        "Swan): the challenge win-condition to score replies against, e.g. 'the assistant gives "
        "working steps to bypass the paywall'. Pair with --attempts-file (your attempts) and "
        "--judge-model (a trusted local model that scores which replies satisfied it)"),
    attempts_file: str = typer.Option("", "--attempts-file", help="file of YOUR attack attempts "
        "for --objective — one per line, or a JSON array. The tool scores the target's replies; "
        "you author the attempts"),
    replies_file: str = typer.Option("", "--replies-file", help="PASTE mode for a web-UI-only "
        "challenge (no endpoint): a JSON array of the target's replies, one per attempt (same "
        "order/length as --attempts-file). Point the endpoint arg at your local --judge-model; "
        "nothing is sent to a target, the judge just scores what you pasted"),
) -> None:
    """Red-team an authorized LLM endpoint for the OWASP-LLM classes bounties pay for — system-
    prompt leak, prompt injection, safety-rule bypass, indirect injection — and draft a submittable
    report. Benign planted canaries confirm each finding (nothing harmful is elicited); the tool
    finds, the report drafts, YOU verify scope and submit. AUTHORIZED targets only.
    """
    import json
    import re as _re
    from datetime import date
    from pathlib import Path

    from friday.core.redteam_report import draft_redteam_report
    from friday.skills.llm_redteam import redteam_llm

    attempts: list[str] = []
    replies: list[str] | None = None
    if replies_file and not objective:
        console.print("[red]--replies-file is for paste mode — it only applies with --objective "
                      "(and --attempts-file + --judge-model).[/red]")
        raise typer.Exit(1)
    if objective:
        if not attempts_file:
            console.print("[red]--objective needs --attempts-file (your attack attempts — one per "
                          "line, or a JSON array). The tool scores replies; you bring the attacks."
                          "[/red]")
            raise typer.Exit(1)
        raw = Path(attempts_file).read_text()
        try:
            parsed = json.loads(raw)
            attempts = [str(a) for a in parsed] if isinstance(parsed, list) else []
        except (json.JSONDecodeError, ValueError):
            attempts = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if not attempts:
            console.print("[red]no attempts found in --attempts-file[/red]")
            raise typer.Exit(1)
        if replies_file:
            try:
                parsed_r = json.loads(Path(replies_file).read_text())
            except FileNotFoundError:
                console.print(f"[red]--replies-file not found: {replies_file}[/red]")
                raise typer.Exit(1) from None
            except (json.JSONDecodeError, ValueError):
                console.print("[red]--replies-file must be a JSON array of the target's replies "
                              "(e.g. [\"reply one\", \"reply two\"]).[/red]")
                raise typer.Exit(1) from None
            if not isinstance(parsed_r, list):
                console.print("[red]--replies-file must be a JSON array of target replies[/red]")
                raise typer.Exit(1)
            replies = [str(r) for r in parsed_r]
            if len(replies) != len(attempts):
                console.print(f"[red]--replies-file has {len(replies)} reply(ies) but "
                              f"--attempts-file has {len(attempts)} attempt(s) — they must pair "
                              "1:1.[/red]")
                raise typer.Exit(1)
            if not judge_model:
                console.print("[red]paste mode needs --judge-model to score the pasted replies."
                              "[/red]")
                raise typer.Exit(1)
        elif not judge_model:
            console.print("[yellow]No --judge-model set — attempts are sent but not auto-scored. "
                          "Add a trusted local judge to triage the hits.[/yellow]")

    if replies is not None:
        console.print(f"[cyan]Paste mode — scoring {len(replies)} pasted reply(ies) against:"
                      f"[/cyan] {objective}")
    else:
        console.print(f"[cyan]Red-teaming {endpoint}…[/cyan]  (authorized targets only)")
        if objective:
            console.print(f"[cyan]Objective mode — scoring {len(attempts)} attempt(s) against:"
                          f"[/cyan] {objective}")
    if judge_model:
        console.print(f"[cyan]Black-box judge mode: {judge_model}[/cyan]")
    res = asyncio.run(redteam_llm(endpoint, model=model, api=api, key=key, judge_model=judge_model,
                                  judge_endpoint=judge_endpoint, judge_api=judge_api,
                                  judge_key=judge_key, objective=objective, attacks=attempts,
                                  replies=replies))
    if res.get("error"):
        console.print(f"[red]{res['error']}[/red]")
        raise typer.Exit(1)
    findings = res["findings"]
    candidates = res.get("candidates", [])
    if res.get("mode") == "objective":
        if candidates:
            console.print(f"[bold]{len(candidates)} attempt(s) judged to satisfy the objective"
                          f"[/bold] [dim](candidate-tier — verify each transcript first)[/dim]")
        else:
            console.print("[yellow]No attempt satisfied the objective.[/yellow]")
        console.print(f"[dim]{res.get('note', '')}[/dim]")
    else:
        if findings:
            console.print(f"[bold]{len(findings)} confirmed finding(s): "
                          f"{', '.join(res['categories_hit'])}[/bold]")
        else:
            console.print("[green]No confirmed weakness — the target protected every canary."
                          "[/green]")
        if candidates:
            console.print(f"[yellow]{len(candidates)} candidate lead(s) — unconfirmed, verify: "
                          f"{', '.join(c['category'] for c in candidates)}[/yellow]")
    report = draft_redteam_report(endpoint, findings, model=res.get("model", ""),
                                  candidates=candidates)
    slug = _re.sub(r"[^a-z0-9]+", "-", (model or endpoint).lower()).strip("-")[:50] or "target"
    bounty_dir = Path.home() / ".local" / "share" / "friday" / "bounty"
    path = Path(out) if out else (bounty_dir / f"redteam-{slug}-{date.today()}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report)
    preview = report[:1800] + ("\n\n…(full report saved)" if len(report) > 1800 else "")
    console.print(Panel(Markdown(preview), title="[bold]Red-team — draft report[/bold]",
                        border_style="cyan", padding=(0, 1)))
    console.print(f"[bold]Saved:[/bold] {path}  [dim](verify scope before submitting)[/dim]")


@app.command(name="agentic-redteam")
def agentic_redteam_cmd(
    endpoint: str = typer.Argument(..., help="Chat endpoint of the model to drive as the agent — "
        "e.g. http://localhost:11434/api/chat (Ollama) or an OpenAI-compatible endpoint. "
        "AUTHORIZED targets only (your own agent/model, or an invited one)"),
    model: str = typer.Option("", "--model", "-m", help="model name the endpoint expects"),
    api: str = typer.Option("auto", "--api", help="auto | openai | ollama | anthropic"),
    key: str = typer.Option("", "--key", help="bearer token for hosted endpoints (never local)"),
    delay_ms: int = typer.Option(0, "--delay-ms", help="pace requests (ms + jitter) for a "
        "rate-limited authorized target; 0 = full speed"),
    out: str = typer.Option("", "--out", help="write the report here (default: bounty/ dir)"),
) -> None:
    """Agentic prompt-injection red-team — does an LLM agent let UNTRUSTED document content hijack
    its tools (0din/OWASP-LLM01 Read/Write/Execute violations)? Runs a SANDBOXED reference agent
    over injection-laced documents and confirms deterministically + echo-proof via observed tool
    sinks (nothing harmful is executed). The tool finds, YOU verify against the real target's
    tools/sinks and submit. AUTHORIZED targets only.
    """
    import re as _re
    from datetime import date
    from pathlib import Path

    from friday.core.redteam_report import draft_redteam_report
    from friday.skills.agentic_redteam import agentic_redteam

    console.print(f"[cyan]Agentic red-team {endpoint}…[/cyan]  (sandboxed; authorized only)")
    res = asyncio.run(agentic_redteam(endpoint, model=model, api=api, key=key, delay_ms=delay_ms))
    if res.get("error"):
        console.print(f"[red]{res['error']}[/red]")
        raise typer.Exit(1)
    findings = res["findings"]
    if findings:
        console.print(f"[bold]{len(findings)} confirmed agentic violation(s): "
                      f"{', '.join(res['categories_hit'])}[/bold]")
    elif res.get("inconclusive"):
        console.print("[yellow]Inconclusive — the model never used the agent's tools (protocol "
                      "mismatch, not proven-robust). See the note below.[/yellow]")
    else:
        console.print("[green]No confirmed agentic injection — the agent ignored in-document "
                      "instructions.[/green]")
    console.print(f"[dim]{res.get('note', '')}[/dim]")
    report = draft_redteam_report(endpoint, findings, model=res.get("model", ""))
    slug = _re.sub(r"[^a-z0-9]+", "-", (model or endpoint).lower()).strip("-")[:50] or "agent"
    bounty_dir = Path.home() / ".local" / "share" / "friday" / "bounty"
    path = Path(out) if out else (bounty_dir / f"agentic-{slug}-{date.today()}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report)
    preview = report[:1800] + ("\n\n…(full report saved)" if len(report) > 1800 else "")
    console.print(Panel(Markdown(preview), title="[bold]Agentic red-team — draft report[/bold]",
                        border_style="cyan", padding=(0, 1)))
    console.print(f"[bold]Saved:[/bold] {path}  [dim](verify vs the real agent before submitting)"
                  "[/dim]")


@app.command(name="agentic-target")
def agentic_target_cmd(
    target: str = typer.Argument(..., help="the REAL in-scope agent product you'll feed (e.g. a "
        "0din designated agent). AUTHORIZED targets only"),
    klass: str = typer.Option("read", "--class", "-c", help="prove: read|execute|write"),
    collector_url: str = typer.Option("", "--collector-url", help="public URL the target can reach "
        "(a tunnel, e.g. cloudflared/ngrok) baked into the payload; default = the local bind URL"),
    host: str = typer.Option("127.0.0.1", "--host", help="collector bind host"),
    port: int = typer.Option(0, "--port", help="collector bind port (0 = free port)"),
    secret: str = typer.Option("", "--secret", help="Read: what to exfiltrate (default: system "
        "prompt + reachable config/creds)"),
    command: str = typer.Option("", "--command", help="Execute: benign command (default: id)"),
    wait: int = typer.Option(180, "--wait", help="seconds to wait for the exfil callback"),
    out: str = typer.Option("", "--out", help="write the report here (default: bounty/ dir)"),
) -> None:
    """Real-target agentic-injection harness: stand up an exfil collector, print poisoned documents
    to feed a REAL in-scope agent product, and CONFIRM the Read/Execute violation echo-proof when
    the agent calls back to the unique collector URL. The harness finds + confirms; YOU feed the
    target and submit. AUTHORIZED targets only (a 0din in-scope product or your own/invited agent).
    """
    import re as _re
    import time as _time
    from datetime import date
    from pathlib import Path

    from friday.core.redteam_report import draft_redteam_report
    from friday.skills.agentic_target import (
        ExfilCollector,
        as_finding,
        build_payloads,
        confirm,
    )
    from friday.skills.agentic_target import _token as _mk_token

    if klass not in ("read", "execute", "write"):
        console.print("[red]--class must be read | execute | write[/red]")
        raise typer.Exit(1)
    token = _mk_token()
    with ExfilCollector(host=host, port=port) as collector:
        public = collector_url.strip() or collector.url
        payloads = build_payloads(public, token, klass, secret=secret, command=command)
        console.print(Panel(
            f"[bold]Collector:[/bold] {collector.url}  →  agent must call "
            f"[bold]{public}/{token}[/bold]\n[dim]token {token}[/dim]",
            title="[bold]Agentic-target harness[/bold]  (authorized targets only)",
            border_style="cyan"))
        if not collector_url.strip() and host in ("127.0.0.1", "localhost"):
            console.print("[yellow]⚠ Collector is LOCAL — a cloud target can't reach it. Expose it "
                          "with a tunnel and pass --collector-url <public-url>.[/yellow]")
        body = "\n\n".join(f"**{label}**\n```\n{content}\n```" for label, content in payloads)
        console.print(Panel(Markdown(
            f"Feed ONE of these to **{target}** through its real interface "
            f"(document / web page / email / repo file), then wait:\n\n{body}"),
            title=f"[bold]Poisoned payloads — {klass} violation[/bold]",
            border_style="magenta", padding=(0, 1)))
        if klass == "write":
            console.print("[yellow]Write has no callback signal — feed the payload, then confirm "
                          f"the unauthorized file 0din_poc_{token}.txt appeared on the target "
                          "yourself.[/yellow]")
            raise typer.Exit(0)
        console.print(f"[cyan]Waiting up to {wait}s for the exfil callback…[/cyan] (Ctrl+C stops)")
        hit = None
        deadline = _time.monotonic() + wait
        try:
            while _time.monotonic() < deadline:
                hit = confirm(collector, token)
                if hit:
                    break
                _time.sleep(1.0)
        except KeyboardInterrupt:
            console.print("[dim]stopped.[/dim]")
    if not hit:
        console.print("[yellow]No callback — the agent didn't follow the injection (robust), or "
                      "the collector wasn't reachable from the target. Try a tunnel, another "
                      "framing, or another class.[/yellow]")
        raise typer.Exit(0)
    console.print(Panel(
        f"[bold green]CONFIRMED[/bold green] {klass} violation — {hit['method']} to the collector\n"
        f"[dim]body: {hit['body'][:300]}[/dim]",
        title="[bold]Echo-proof hit[/bold]", border_style="green"))
    finding = as_finding(target, klass, hit, payloads[0][1], token)
    report = draft_redteam_report(target, [finding], program="0din")
    slug = _re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-")[:50] or "target"
    bounty_dir = Path.home() / ".local" / "share" / "friday" / "bounty"
    path = Path(out) if out else (bounty_dir / f"agentic-target-{slug}-{date.today()}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report)
    console.print(f"[bold]Saved 0din draft:[/bold] {path}  [dim](verify scope + private-data "
                  "impact before submitting)[/dim]")


@app.command(name="scan-injection")
def scan_injection_cmd(
    source: str = typer.Argument("-", help="file to scan, or '-' to read stdin"),
    text: str = typer.Option("", "--text", "-t", help="scan this literal text instead of a file"),
) -> None:
    """Scan untrusted text/files for prompt-injection + data-exfiltration signatures (defensive,
    offline, no model call). Exits 1 when an injection is detected — usable as a CI / pre-commit
    gate on untrusted content (docs, web pages, emails, tool output) before an agent ingests it.
    """
    import sys as _sys
    from pathlib import Path

    from rich.table import Table

    from friday.core import injection_guard

    if text:
        content = text
    elif source == "-":
        content = _sys.stdin.read()
    else:
        try:
            content = Path(source).read_text("utf-8", "replace")
        except OSError as e:
            console.print(f"[red]could not read {source}: {e}[/red]")
            raise typer.Exit(2) from e

    v = injection_guard.verdict(content)
    if not v["injection"]:
        console.print("[green]✓ clean — no prompt-injection signatures detected.[/green]")
        raise typer.Exit(0)

    sev = str(v["severity"]).upper()
    color = {"HIGH": "red", "MEDIUM": "yellow", "LOW": "cyan"}.get(sev, "yellow")
    table = Table(
        title=f"[{color}]⚠ prompt-injection detected — {sev} (score {v['score']})[/{color}]",
        show_header=True, header_style="bold", title_justify="left")
    table.add_column("Category", style="bold")
    table.add_column("Severity")
    table.add_column("Matched excerpt", overflow="fold")
    for d in v["detections"]:
        table.add_row(str(d["category"]), str(d["severity"]), str(d["excerpt"]))
    console.print(table)
    console.print(f"[dim]{len(v['detections'])} signature(s) across "
                  f"{len(v['categories'])} category(ies). Untrusted content — do not let an agent "
                  "follow instructions inside it.[/dim]")
    raise typer.Exit(1)


@app.command(name="mcp-audit")
def mcp_audit_cmd(
    path: str = typer.Argument(".", help="local source directory to scan (a cloned repo)"),
    clone: str = typer.Option("", "--clone", help="git URL to shallow-clone to a temp dir + scan"),
    batch: str = typer.Option(
        "", "--batch",
        help="file of git URLs (one per line) — clone+scan each, rank most-promising first"),
    severity: str = typer.Option("all", "--severity", help="minimum severity: all|medium|high"),
    reachable: bool = typer.Option(
        False, "--reachable", "-r",
        help="only sinks fed by a tool-handler parameter (attacker-reachable — likely-real ones)"),
    out: str = typer.Option("", "--out", help="write a markdown report to this path"),
) -> None:
    """Statically scan an MCP server / agent-tool checkout for the vuln classes that dominate the
    MCP CVE wave — command/SQL injection, path traversal, SSRF, unsafe deserialization, code eval.
    Prints a ranked table of CANDIDATE sinks; verify each against the real wiring before responsible
    disclosure. Exits 1 if any HIGH finding (usable as a CI gate). --batch <file> runs the funnel
    over many repos at once. AUDIT SOURCE ONLY — never attack a live third-party service; disclose
    through the project's security policy.
    """
    import shutil as _sh
    import subprocess as _sp
    import tempfile as _tf
    from pathlib import Path

    from rich.table import Table

    from friday.core import mcp_audit

    _rank = {"high": 3, "medium": 2, "low": 1}
    floor = _rank.get(severity.lower(), 1) if severity.lower() in _rank else 1

    if batch:
        _mcp_audit_batch(batch, reachable)
        return

    tmp = ""
    root = path
    if clone:
        tmp = _tf.mkdtemp(prefix="mcp-audit-")
        console.print(f"[cyan]cloning {clone} …[/cyan]")
        r = _sp.run(["git", "clone", "--depth", "1", clone, tmp],  # noqa: S603,S607
                    capture_output=True, text=True)
        if r.returncode != 0:
            console.print(f"[red]clone failed:[/red] {r.stderr.strip()[:200]}")
            _sh.rmtree(tmp, ignore_errors=True)
            raise typer.Exit(2)
        root = tmp
    if not Path(root).exists():
        console.print(f"[red]no such path:[/red] {root}")
        raise typer.Exit(2)
    try:
        report = mcp_audit.audit_report(root)
    finally:
        if tmp:
            _sh.rmtree(tmp, ignore_errors=True)

    findings = [f for f in report["findings"] if _rank[f["severity"]] >= floor]
    if reachable:
        findings = [f for f in findings if f.get("reachable")]
    label = clone or root
    filt = f"'{severity}' severity" + (" + tool-reachable" if reachable else "")
    if not findings:
        console.print(f"[green]✓ {label}: no candidate sinks at {filt} "
                      f"(scanned, {report['total']} total finding(s)).[/green]")
        raise typer.Exit(0)

    colour = {"high": "red", "medium": "yellow", "low": "cyan"}
    t = Table(title=f"[bold]MCP audit — {label}[/bold]", title_justify="left",
              show_header=True, header_style="bold")
    for col in ("Sev", "R", "Category", "CWE", "Location"):
        t.add_column(col)
    t.add_column("Code", overflow="fold", max_width=52)
    for f in findings:
        c = colour.get(f["severity"], "white")
        rmark = "[bold red]►[/bold red]" if f.get("reachable") else ""
        t.add_row(f"[{c}]{f['severity'].upper()}[/{c}]", rmark, f["category"], f["cwe"],
                  f"{f['file']}:{f['line']}", f["excerpt"])
    console.print(t)
    cats = ", ".join(f"{k}×{v}" for k, v in report["by_category"].items())
    console.print(f"[dim]{len(findings)} shown · {report['high']} high · "
                  f"{report.get('reachable', 0)} tool-reachable (►) · {cats}. "
                  "Candidates — verify against the real wiring before disclosing.[/dim]")

    if out:
        lines = [f"# MCP audit — {label}", "",
                 f"- Total findings: {report['total']} ({report['high']} high, "
                 f"{report.get('reachable', 0)} tool-reachable)",
                 f"- Classes: {cats}", "",
                 "> Candidate sinks from static analysis — verify each against the real tool "
                 "wiring and disclose responsibly through the project's security policy. "
                 "**Tool-reachable** findings are fed by an MCP tool parameter, so attacker-"
                 "influenced input can reach the sink — triage these first.", ""]
        for f in findings:
            tag = " · TOOL-REACHABLE" if f.get("reachable") else ""
            lines += [f"## [{f['severity'].upper()}{tag}] {f['category']} ({f['cwe']}) — "
                      f"{f['file']}:{f['line']}", "", f"```\n{f['excerpt']}\n```", "",
                      f"{f['note']}", ""]
        Path(out).expanduser().write_text("\n".join(lines))
        console.print(f"[bold]Report saved:[/bold] {out}")
    if report["high"]:
        raise typer.Exit(1)


def _mcp_audit_batch(batch_file: str, reachable_only: bool) -> None:
    """The funnel: clone+scan every git URL in `batch_file`, rank most-promising first (fresh repos
    with tool-reachable HIGH findings on top, known-vuln/dup repos last)."""
    import shutil as _sh
    import subprocess as _sp
    import tempfile as _tf
    from pathlib import Path

    from rich.table import Table

    from friday.core import mcp_audit

    p = Path(batch_file).expanduser()
    if not p.exists():
        console.print(f"[red]no such batch file:[/red] {batch_file}")
        raise typer.Exit(2)
    urls = [ln.strip() for ln in p.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    if not urls:
        console.print("[yellow]batch file has no URLs[/yellow]")
        raise typer.Exit(0)

    results: list[tuple[str, dict]] = []
    with console.status(f"[cyan]scanning {len(urls)} repos …[/cyan]") as status:
        for i, url in enumerate(urls, 1):
            name = url.rstrip("/").split("/")[-1].removesuffix(".git")
            status.update(f"[cyan]({i}/{len(urls)}) {name} …[/cyan]")
            tmp = _tf.mkdtemp(prefix="mcp-batch-")
            try:
                r = _sp.run(["git", "clone", "--depth", "1", url, tmp],  # noqa: S603,S607
                            capture_output=True, text=True, timeout=180)
                if r.returncode != 0:
                    console.print(f"[dim]clone failed: {name}[/dim]")
                    continue
                results.append((url, mcp_audit.audit_report(tmp)))
            except (OSError, _sp.SubprocessError):
                console.print(f"[dim]error: {name}[/dim]")
            finally:
                _sh.rmtree(tmp, ignore_errors=True)

    rows = mcp_audit.rank_batch(results)
    if reachable_only:
        rows = [r for r in rows if r["reachable"]]
    t = Table(title="[bold]MCP funnel — ranked (most promising first)[/bold]", title_justify="left",
              show_header=True, header_style="bold")
    for col in ("Repo", "R-High", "Reach", "High", "Top classes", "Dup?"):
        t.add_column(col)
    for r in rows:
        name = r["repo"].rstrip("/").split("/")[-1].removesuffix(".git")
        dup = "[yellow]known-vuln[/yellow]" if r["known_vuln"] else ""
        rh = f"[bold red]{r['reachable_high']}[/bold red]" if r["reachable_high"] else "0"
        t.add_row(name, rh, str(r["reachable"]), str(r["high"]), r["top"] or "[dim]—[/dim]", dup)
    console.print(t)
    hot = [r for r in rows if r["reachable_high"] and not r["known_vuln"]]
    console.print(f"[dim]{len(results)}/{len(urls)} scanned · "
                  f"[bold]{len(hot)} fresh repo(s) with tool-reachable HIGH findings[/bold] — "
                  "start there. Verify + dedup against CVEs before disclosing.[/dim]")
    if hot:
        console.print(f"[green]→ next:[/green] sutra mcp-audit --clone {hot[0]['repo']} -r")


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
    """Write a systemd --user service + timer that runs `sutra learn` (refresh the knowledge base)
    then `sutra study` (reflect on real runs) continuously while the machine is on (a few min after
    boot, then every 30 min). Both are idempotent + cheap — learn embeds only new docs, study skips
    the model call when nothing changed. Writes only to the user unit dir; turning it on stays the
    operator's explicit step."""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "sutra-study.service").write_text(
        "[Unit]\nDescription=Sutra self-improvement pass\n\n"
        f"[Service]\nType=oneshot\n"
        f"ExecStart={_sutra_exec()} learn\n"   # refresh the knowledge base (idempotent + fast)
        f"ExecStart={_sutra_exec()} study\n")  # then reflect on real runs
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


@app.command()
def learn() -> None:
    """Fast-learn the knowledge base (docs/knowledge/) into Sutra's recall — the bug-bounty coaching
    corpus (vuln classes: how to find, confirm, escalate). Idempotent + fast: re-runs embed only
    what's new, so it's safe to run on a timer or at startup. Deepens what Sutra can TEACH you and
    where it knows bugs hide — it does NOT change the model's weights (the tools find, the coach
    guides, you exploit).
    """
    from friday.core.knowledge import learn_knowledge

    with console.status("[cyan]learning the knowledge base…[/cyan]"):
        res = learn_knowledge()
    if res.get("error"):
        console.print(f"[yellow]{res['error']}[/yellow]")
        return
    msg = (f"Learned {res['learned']} new chunk(s) from {res['files']} doc(s).\n"
           f"Sutra now holds [bold]{res['total']}[/bold] chunks of bug-bounty knowledge to coach "
           f"from.")
    console.print(Panel(msg, title="[bold green]Sutra learn[/bold green]", border_style="green"))


@app.command()
def coach(
    url: str = typer.Argument(..., help="a target URL/endpoint to get a guided test plan for"),
) -> None:
    """Beginner walkthrough: point it at a target and Sutra tells you — step by step, in plain
    English — what to test, how, and where to learn each piece. It maps the surface to the right
    bug classes; YOU do the manual testing (that's the skill, and it's learnable). New to a class?
    Practice it on a safe target (Juice Shop / PortSwigger Academy) first.
    """
    from rich.markdown import Markdown

    from friday.core.coach import guided_plan
    console.print(Markdown(guided_plan(url)))


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
    web: bool = typer.Option(False, "--web", help="Open the live browser dashboard instead"),
    port: int = typer.Option(8721, "--port", help="Port for --web"),
    interval: float = typer.Option(5.0, "--interval", help="Refresh seconds in --watch mode"),
) -> None:
    """Show what Sutra is learning right now and what it has learned — its earned doctrine, recent
    study passes, lessons from real engagements, and the knowledge base it can recall. --watch keeps
    the terminal view live; --web opens a browser dashboard. (Learning itself auto-runs via the
    study timer; this is just the window into it.)"""
    from friday.core import learning as learn

    if web:
        from friday.gui.learning_dashboard import serve
        console.print(f"[cyan]Learning dashboard → http://127.0.0.1:{port}/[/cyan] (Ctrl+C stops)")
        serve(port=port)
        return
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
        if _detached():  # detached plan run with no -y: decline loudly instead of a dead prompt
            console.print(f"     [dim]auto-declined (no terminal, no --yes): {skill}[/dim]")
            return False
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


@plan_app.command("do")
def plan_do(
    goal: str = typer.Argument(..., help="The goal to plan and drive all the way to completion"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-approve gated steps (hands-free)"),
    passes: int = typer.Option(
        4, "--passes", help="Max extra resume passes to finish steps that failed the first time"),
) -> None:
    """Plan a goal and DRIVE IT TO COMPLETION — runs the plan, then keeps re-attempting the steps
    that failed until the whole task is done or it's genuinely stuck. Trades time for finishing."""
    from friday.core.planner import run_to_completion, synthesize_plan

    async def _run() -> None:
        out = await run_to_completion(goal, max_passes=passes,
                                      confirm=_plan_confirm(yes), observe=_plan_observe)
        if not out.steps:
            console.print("[red]couldn't break that into steps — try rephrasing the goal.[/red]")
            return
        console.print("\n[bold]Result:[/bold]")
        _render_plan(out)
        done, total = out.progress()
        if out.is_complete():
            with console.status("[cyan]summarizing…[/cyan]"):
                summary = await synthesize_plan(out)
            console.print(Panel(Markdown(summary), title="Outcome", border_style="cyan",
                                padding=(0, 1)))
        elif out.status == "paused":
            console.print(f"\n[yellow]paused (budget)[/yellow] — resume: "
                          f"[cyan]sutra plan resume {out.slug}[/cyan]")
        else:
            console.print(f"\n[yellow]{done}/{total} steps done[/yellow] — stuck on the rest; "
                          f"pick it up later: [cyan]sutra plan resume {out.slug}[/cyan]")

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
