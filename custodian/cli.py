"""Custodian CLI.

    custodian investigate <table>   data broke — explain impact and cause
    custodian sweep                 data died — stage dead assets for review
    custodian watch                 sweep continuously; retract stale findings
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
    if result.failed_calls:
        console.print(
            f"[bold red]Warning:[/bold red] {len(result.failed_calls)} tool "
            f"call(s) failed ({', '.join(sorted(set(result.failed_calls)))}). "
            "The agent may still describe them as done — verify in DataHub "
            "before trusting the write-back."
        )
    console.print(Markdown(result.text))
    if result.tool_calls:
        console.print(
            f"\n[dim]{len(result.tool_calls)} tool calls: "
            f"{', '.join(result.tool_calls)}[/dim]"
        )


def _notify(urns: list[str], kind: str, *, send: bool, summary: str = "") -> None:
    """Render (and only optionally send) owner digests for a set of assets."""
    from .approve import open_graph
    from .notify import build_digests, deliver

    if not urns:
        return
    digests, unreachable = build_digests(open_graph(), urns, kind, summary)
    if unreachable:
        console.print(
            f"[yellow]{len(unreachable)} asset(s) have no reachable owner[/yellow] — "
            "nobody will be told about them: "
            f"{', '.join(u.split(',')[-2] if ',' in u else u for u in unreachable[:5])}"
            f"{' …' if len(unreachable) > 5 else ''}"
        )
    console.print(deliver(digests, send=send))


@app.command()
def investigate(
    table: str = typer.Argument(..., help="Name or URN of the broken table."),
    context: str = typer.Option("", help="What the reporter observed."),
    notify: bool = typer.Option(
        False, "--notify", help="Render a digest for each impacted asset's owners."
    ),
    send: bool = typer.Option(
        False,
        "--send",
        help="Actually deliver. Requires CUSTODIAN_NOTIFY=smtp. Off by default.",
    ),
) -> None:
    """Data broke: trace the blast radius and likely cause, write a post-mortem."""
    from .investigate import investigate as run

    console.print(f"[bold]Investigating[/bold] {table} …")
    result = _run(run(table, context=context))
    _emit(result)

    if notify:
        from .report import INCIDENT_TAG
        from .watch import _clients

        graph, _ = _clients()
        impacted = list(
            graph.get_urns_by_filter(
                entity_types=["dataset"],
                extraFilters=[
                    {
                        "field": "tags",
                        "condition": "EQUAL",
                        "values": [f"urn:li:tag:{INCIDENT_TAG}"],
                    }
                ],
            )
        )
        console.print(f"\n[bold]Notifying owners[/bold] of {len(impacted)} asset(s) …")
        _notify(
            impacted,
            "incident",
            send=send,
            summary=result.text.split("## Blast Radius")[0].strip()[:600],
        )


@app.command()
def sweep(
    scope: str = typer.Option("", help="Limit to a domain or platform."),
    limit: int = typer.Option(500, help="Maximum assets to examine."),
) -> None:
    """Data died: find dead assets and stage them for human approval."""
    from .sweep import sweep as run

    console.print("[bold]Sweeping[/bold] for dead assets …")
    _emit(_run(run(scope=scope, limit=limit)))


@app.command()
def watch(
    interval: int = typer.Option(300, help="Seconds between ticks."),
    limit: int = typer.Option(500, help="Maximum assets to examine per tick."),
    scope: str = typer.Option("", help="Limit to a domain or platform."),
    once: bool = typer.Option(False, "--once", help="Run a single tick and exit."),
    notify: bool = typer.Option(
        False, "--notify", help="Render a digest for owners of new candidates."
    ),
    send: bool = typer.Option(
        False, "--send", help="Actually deliver. Requires CUSTODIAN_NOTIFY=smtp."
    ),
) -> None:
    """Sweep continuously: stage what is newly dead, retract what came back."""
    from .watch import watch as run

    def report(result) -> None:
        for urn in result.retracted:
            name = urn.split(",")[-2] if "," in urn else urn
            console.print(f"[yellow]retracted[/yellow] {name} — it has consumers again")
        if result.new:
            console.print(f"[green]staged {len(result.new)} new candidate(s)[/green]")
            if notify:
                _notify(result.new, "staged", send=send)
        if not result.changed:
            console.print(
                f"[dim]scanned {result.scanned}; nothing new "
                f"({result.already_staged} already staged, {result.spared} spared). "
                "No model calls spent.[/dim]"
            )

    console.print(
        f"[bold]Watching[/bold] every {interval}s. Ctrl-C to stop."
        if not once
        else "[bold]Running one tick[/bold] …"
    )
    try:
        _run(run(interval=interval, limit=limit, scope=scope, once=once, on_tick=report))
    except KeyboardInterrupt:
        console.print("\nStopped.")


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

    import os

    from .providers.openai_compat import ENDPOINTS

    if CONFIG.provider == "gemini":
        model, key_var = CONFIG.gemini_model, "GEMINI_API_KEY"
    elif CONFIG.provider in ENDPOINTS:
        _, key_var, default_model = ENDPOINTS[CONFIG.provider]
        model = CONFIG.compat_model or default_model
    else:
        model, key_var = "?", "?"

    key_state = "set" if os.getenv(key_var) else f"[red]MISSING ({key_var})[/red]"

    console.print(f"provider     : {CONFIG.provider} ({model})")
    console.print(f"gms url      : {CONFIG.gms_url}")
    console.print(f"gms token    : {token_state}")
    console.print(f"provider key : {key_state}")
    console.print(f"tool budget  : {CONFIG.max_tool_calls}")

    # A misconfigured notifier fails by staying quiet, which looks exactly like
    # "there was nothing to report" — so say plainly what it would do.
    if CONFIG.notify_transport == "smtp":
        missing = [v for v in ("SMTP_HOST", "SMTP_FROM") if not os.getenv(v)]
        if missing:
            notify_state = f"smtp [red]incomplete — missing {', '.join(missing)}[/red]"
        else:
            notify_state = (
                f"smtp via {os.getenv('SMTP_HOST')} "
                "[yellow](will send real mail with --send)[/yellow]"
            )
    else:
        notify_state = f"{CONFIG.notify_transport} [dim](renders only, never sends)[/dim]"
    console.print(f"notify       : {notify_state}")
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
