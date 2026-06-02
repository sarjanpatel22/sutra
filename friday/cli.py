"""Rich-based REPL for Sutra."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich import print as rprint

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
        f"[dim]tier: {tier.value}  |  /tier <name>  /skills  /budget  /clear  /quit[/dim]",
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
                    f"[dim]Daily: ${b.daily_spent:.4f} / ${b.daily_limit:.2f}  "
                    f"Monthly: ${b.monthly_spent:.4f} / ${b.monthly_limit:.2f}[/dim]"
                )
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


if __name__ == "__main__":
    app()
