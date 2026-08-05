"""Custodian CLI.

    custodian investigate <table>   data broke — explain impact and cause
    custodian sweep                 data died — stage dead assets for review
    custodian approve               human lands the staged retirements
    custodian doctor                check the environment before demoing
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from .config import CONFIG, ConfigError

app = typer.Typer(
    add_completion=False,
    help="An autonomous data steward for DataHub.",
)
console = Console()


def _run(coro):
    try:
        return asyncio.run(coro)
    except ConfigError as exc:
        console.print(f"[bold red]Configuration problem[/bold red]\n{exc}")
        raise typer.Exit(code=1)


def _emit(result) -> None:
    if result.hit_call_ceiling:
        console.print(
            "[bold yellow]Warning:[/bold yellow] the agent used its entire "
            f"tool-call budget ({CONFIG.max_tool_calls}). This output is "
            "likely incomplete — raise CUSTODIAN_MAX_TOOL_CALLS and re-run."
        )
    console.print(Markdown(result.text))
    if result.tool_calls:
        console.print(
            f"\n[dim]{len(result.tool_calls)} tool calls: "
            f"{', '.join(result.tool_calls)}[/dim]"
        )


@app.command()
def investigate(
    table: str = typer.Argument(..., help="Name or URN of the broken table."),
    context: str = typer.Option("", help="What the reporter observed."),
) -> None:
    """Data broke: trace the blast radius and likely cause, write a post-mortem."""
    from .investigate import investigate as run

    console.print(f"[bold]Investigating[/bold] {table} …")
    _emit(_run(run(table, context=context)))


@app.command()
def sweep(
    scope: str = typer.Option("", help="Limit to a domain or platform."),
    limit: int = typer.Option(25, help="Maximum assets to examine."),
) -> None:
    """Data died: find dead assets and stage them for human approval."""
    from .sweep import sweep as run

    console.print("[bold]Sweeping[/bold] for dead assets …")
    _emit(_run(run(scope=scope, limit=limit)))


@app.command()
def approve(
    yes: bool = typer.Option(False, "--yes", help="Approve all without prompting."),
) -> None:
    """Land the retirements Custodian staged. This is the human half of sweep."""
    from .approve import approve as do_approve
    from .approve import find_candidates, open_graph

    try:
        graph = open_graph()
        candidates = find_candidates(graph)
    except ConfigError as exc:
        console.print(f"[bold red]Configuration problem[/bold red]\n{exc}")
        raise typer.Exit(code=1)

    if not candidates:
        console.print("No staged candidates. Run [bold]custodian sweep[/bold] first.")
        return

    table = Table(title="Staged for deprecation")
    table.add_column("Asset")
    table.add_column("Reason", overflow="fold")
    for candidate in candidates:
        table.add_row(candidate.name, candidate.note)
    console.print(table)

    approved = 0
    for candidate in candidates:
        if yes or typer.confirm(f"Deprecate {candidate.name}?"):
            do_approve(graph, candidate)
            approved += 1
            console.print(f"  [green]deprecated[/green] {candidate.name}")

    console.print(f"\n{approved} of {len(candidates)} retired.")


@app.command()
def doctor() -> None:
    """Verify the environment before you demo. Checks every failure we know of."""
    from .mcp_client import LOG_PATH, assert_tools_available, datahub_session

    if CONFIG.gms_token:
        token_state = "set"
    elif CONFIG.is_local:
        token_state = "[dim]not needed (local quickstart runs without auth)[/dim]"
    else:
        token_state = "[red]MISSING[/red]"

    console.print(f"provider     : {CONFIG.provider} ({CONFIG.gemini_model})")
    console.print(f"gms url      : {CONFIG.gms_url}")
    console.print(f"gms token    : {token_state}")
    console.print(
        f"provider key : {'set' if CONFIG.gemini_api_key else '[red]MISSING[/red]'}"
    )
    console.print(f"tool budget  : {CONFIG.max_tool_calls}")
    console.print(f"mcp log      : [dim]{LOG_PATH}[/dim]")

    async def check():
        async with datahub_session() as session:
            return await assert_tools_available(session)

    try:
        tools = _run(check())
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 — surface whatever went wrong
        console.print(f"\n[bold red]MCP server check failed[/bold red]\n{exc}")
        raise typer.Exit(code=1)

    console.print(
        f"\n[bold green]OK[/bold green] — MCP server up with "
        f"{len(tools)} tools, mutations enabled."
    )


if __name__ == "__main__":
    app()
