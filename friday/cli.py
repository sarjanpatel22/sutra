"""Rich-based REPL for Sutra."""

from __future__ import annotations

import asyncio
from typing import Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from friday.core.router import Tier, get_budget

app = typer.Typer(name="sutra", help="Sutra — voice-native ambient AI assistant")
console = Console()


def _tier_from_str(s: str) -> Tier:
    mapping = {"local": Tier.LOCAL, "medium": Tier.MEDIUM, "heavy": Tier.HEAVY, "max": Tier.MAX}
    return mapping.get(s.lower(), Tier.HEAVY)


async def _chat_loop(tier: Tier) -> None:
    from friday.core.agent import run  # late import to avoid circular at startup

    history: list[dict[str, Any]] = []

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

        # Agent call
        with console.status("[cyan]thinking…[/cyan]"):
            try:
                reply, history = await run(user, history=history, tier=tier)
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]Error: {e}[/red]")
                continue

        console.print(Panel(Markdown(reply), border_style="dim", padding=(0, 1)))


@app.command()
def chat(
    tier: str = typer.Option("heavy", "--tier", "-t", help="Routing tier: local/medium/heavy/max"),
) -> None:
    """Start an interactive chat session."""
    asyncio.run(_chat_loop(_tier_from_str(tier)))


@app.command()
def ask(
    query: str = typer.Argument(..., help="Single question to ask"),
    tier: str = typer.Option("heavy", "--tier", "-t"),
) -> None:
    """Ask a single question and exit."""
    from friday.core.agent import run

    async def _once() -> None:
        reply, _ = await run(query, tier=_tier_from_str(tier))
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


if __name__ == "__main__":
    app()
