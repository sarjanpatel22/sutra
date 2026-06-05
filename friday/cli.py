"""Rich-based REPL for Sutra."""

from __future__ import annotations

import asyncio
from typing import Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from friday.core.router import Tier, get_budget

app = typer.Typer(name="sutra", help="Sutra — voice-native ambient AI assistant")
console = Console()


@app.callback()
def _bootstrap() -> None:
    """Runs before any command — load keyring secrets into the environment."""
    try:
        from friday.core.secrets import load_into_env
        load_into_env()
    except Exception:  # noqa: BLE001 — secret-loading must never block the CLI
        pass


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

    console.print(Panel(
        "[bold cyan]Sutra[/bold cyan] — ready.\n"
        f"[dim]tier: {tier.value}  |  /tier <name>  /skills  /budget  /status  /clear  /quit[/dim]",
        border_style="cyan",
    ))

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
                    f"[dim]Daily: ${b.daily_spent:.4f} / ${b.daily_limit:.2f} ({b.daily_pct:.1f}%)  "
                    f"Monthly: ${b.monthly_spent:.4f} / ${b.monthly_limit:.2f} ({b.monthly_pct:.1f}%)  "
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
            ok = await asyncio.get_event_loop().run_in_executor(
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


@app.command()
def chat(
    tier: str = typer.Option("medium", "--tier", "-t", help="Routing tier: local/medium/heavy/max"),
) -> None:
    """Start an interactive chat session."""
    asyncio.run(_chat_loop(_tier_from_str(tier)))


@app.command()
def ask(
    query: str = typer.Argument(..., help="Single question to ask"),
    tier: str = typer.Option("medium", "--tier", "-t", help="local/reason/medium/heavy/max"),
) -> None:
    """Ask a single question and exit."""
    from friday.core.agent import flush_persist, run

    async def _once() -> None:
        reply, _ = await run(query, tier=_tier_from_str(tier))
        console.print(Markdown(reply))          # show the answer immediately
        await flush_persist()                    # then let the background save finish

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


@app.command()
def voice(
    mode: str = typer.Option("ptt", "--mode", "-m", help="ptt (push-to-talk) or vad (auto-detect)"),
) -> None:
    """Start voice mode. ptt=hold SPACE, vad=always listening."""
    from friday.voice.pipeline import push_to_talk_loop, vad_listen_loop
    if mode == "vad":
        asyncio.run(vad_listen_loop())
    else:
        asyncio.run(push_to_talk_loop())


@app.command()
def graph(
    port: int = typer.Option(8700, "--port", "-p", help="Port to serve the viewer on"),
    no_open: bool = typer.Option(False, "--no-open", help="Don't auto-open a browser"),
) -> None:
    """Open the brain-graph viewer for the memory layer (Graphiti/FalkorDB)."""
    from friday.memory.graphview import serve
    serve(port=port, open_browser=not no_open)


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
def status() -> None:
    """Run a one-shot health check and print results."""
    from friday.core.monitor import print_report, run_health_checks

    async def _run() -> None:
        report = await run_health_checks()
        print_report(report)

    asyncio.run(_run())


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


@app.command()
def secrets(
    import_env: bool = typer.Option(
        False, "--import", help="Move .env secrets into the OS keyring"
    ),
) -> None:
    """Show where each secret resolves from (keyring/env/missing), or --import to keyring."""
    from friday.core.secrets import import_from_env
    from friday.core.secrets import status as secret_status

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


if __name__ == "__main__":
    app()
