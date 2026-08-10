"""Tell the people who need to know.

`investigate` already works out exactly who owns each impacted asset, and
`sweep` works out what is about to be retired. Writing that into the graph is
the durable half; this is the half that reaches someone who is not currently
looking at DataHub.

Two rules shape everything here.

**Console is the default transport.** Rendering a message and printing it is
the safe behaviour and the honest one — you can read exactly what would be
sent before anything leaves the machine. Actually sending requires setting
`CUSTODIAN_NOTIFY=smtp` *and* passing `--send`, because the owners in a demo
estate are synthetic: those addresses either bounce or belong to real strangers
who happen to occupy the same address space. Mailing them would be an
irreversible, outward-facing act taken on fabricated data.

**Link, do not reproduce.** A digest carries a summary and a DataHub link, not
the whole post-mortem. The artifact lives in the graph; the mail exists to
drive people back to it. Reproducing it in mail would route around the write-
back that is the entire point of Custodian.
"""

from __future__ import annotations

import os
import smtplib
from collections import defaultdict
from dataclasses import dataclass, field
from email.message import EmailMessage
from urllib.parse import quote

from datahub.ingestion.graph.client import DataHubGraph
from datahub.metadata.schema_classes import (
    CorpUserEditableInfoClass,
    CorpUserInfoClass,
    OwnershipClass,
)

from .config import CONFIG


@dataclass
class Digest:
    """One message to one person, covering every asset they own."""

    recipient: str
    subject: str
    body: str
    assets: list[str] = field(default_factory=list)


def datahub_url(urn: str) -> str:
    """Deep link to an asset in the DataHub UI."""
    base = CONFIG.frontend_url.rstrip("/")
    return f"{base}/dataset/{quote(urn, safe='')}"


def readable(urn: str) -> str:
    return urn.split(",")[-2] if "," in urn else urn


def _email_for(graph: DataHubGraph, user_urn: str) -> str | None:
    """Best available address for a corpuser, or None if we have none.

    Editable info wins: it is what a human last set through the UI, so it is
    likelier to be current than whatever ingestion first supplied.
    """
    for aspect_type in (CorpUserEditableInfoClass, CorpUserInfoClass):
        try:
            aspect = graph.get_aspect(entity_urn=user_urn, aspect_type=aspect_type)
        except Exception:
            continue
        email = getattr(aspect, "email", None) if aspect else None
        if email and "@" in email:
            return email
    return None


def resolve_recipients(
    graph: DataHubGraph, urns: list[str]
) -> tuple[dict[str, list[str]], list[str]]:
    """Map owner email -> assets they own.

    Returns (recipients, unreachable). `unreachable` lists assets whose owners
    have no address, or which have no owner at all — reported rather than
    dropped, because "nobody was told about this one" is the fact an operator
    most needs and least expects.
    """
    recipients: dict[str, list[str]] = defaultdict(list)
    unreachable: list[str] = []

    for urn in urns:
        try:
            ownership = graph.get_aspect(entity_urn=urn, aspect_type=OwnershipClass)
        except Exception:
            ownership = None

        owners = list(ownership.owners) if ownership and ownership.owners else []
        if not owners:
            unreachable.append(urn)
            continue

        reached = False
        for owner in owners:
            email = _email_for(graph, owner.owner)
            if email:
                # Dedupe: one person owning eight impacted tables gets one
                # digest listing eight, not eight separate mails.
                if urn not in recipients[email]:
                    recipients[email].append(urn)
                reached = True
        if not reached:
            unreachable.append(urn)

    return dict(recipients), unreachable


_INCIDENT_BODY = """\
Custodian investigated an incident affecting data you own.

Impacted assets you are listed as owning:
{assets}

{summary}

The full post-mortem is attached to the asset in DataHub as a document —
follow a link above to read it in context.

-- Custodian (automated). Nothing has been changed or deleted.
"""

_STAGED_BODY = """\
Custodian has staged assets you own as candidates for retirement.

{assets}

Nothing has been deprecated. These are proposals awaiting human approval, and
a person must run `custodian approve` for anything to change. If any of these
should be kept, remove the `deprecation-candidate` tag in DataHub or say so to
whoever runs the sweep.

-- Custodian (automated).
"""


def render_digest(
    recipient: str, urns: list[str], kind: str, summary: str = ""
) -> Digest:
    """Build one person's message. Pure — no I/O, so it is cheap to test."""
    listing = "\n".join(f"  - {readable(u)}\n    {datahub_url(u)}" for u in urns)
    count = len(urns)
    plural = "asset" if count == 1 else "assets"

    if kind == "incident":
        subject = f"[Custodian] Incident affects {count} {plural} you own"
        body = _INCIDENT_BODY.format(assets=listing, summary=summary.strip())
    elif kind == "staged":
        subject = f"[Custodian] {count} {plural} you own staged for retirement"
        body = _STAGED_BODY.format(assets=listing)
    else:
        raise ValueError(f"unknown digest kind: {kind!r}")

    return Digest(recipient=recipient, subject=subject, body=body, assets=list(urns))


def build_digests(
    graph: DataHubGraph, urns: list[str], kind: str, summary: str = ""
) -> tuple[list[Digest], list[str]]:
    recipients, unreachable = resolve_recipients(graph, urns)
    digests = [
        render_digest(email, owned, kind, summary)
        for email, owned in sorted(recipients.items())
    ]
    return digests, unreachable


def send_console(digests: list[Digest]) -> None:
    """Print what would be sent. The default, and what the demo shows."""
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    for digest in digests:
        console.print(
            Panel(
                digest.body.rstrip(),
                title=f"to: {digest.recipient}",
                subtitle=digest.subject,
                border_style="cyan",
            )
        )
    console.print(
        f"[dim]{len(digests)} digest(s) rendered. Nothing was sent — "
        "set CUSTODIAN_NOTIFY=smtp and pass --send to deliver.[/dim]"
    )


def send_smtp(digests: list[Digest]) -> int:
    """Actually deliver. Only reachable with explicit configuration and --send."""
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM") or user

    if not (host and sender):
        raise RuntimeError(
            "SMTP transport needs SMTP_HOST and SMTP_FROM (plus SMTP_USER / "
            "SMTP_PASSWORD if your server requires auth). Set them in .env, "
            "which is gitignored — do not commit credentials."
        )

    sent = 0
    with smtplib.SMTP(host, port) as smtp:
        smtp.starttls()
        if user and password:
            smtp.login(user, password)
        for digest in digests:
            message = EmailMessage()
            message["From"] = sender
            message["To"] = digest.recipient
            message["Subject"] = digest.subject
            message.set_content(digest.body)
            smtp.send_message(message)
            sent += 1
    return sent


def deliver(digests: list[Digest], *, send: bool) -> str:
    """Route digests to the configured transport. Console unless told otherwise."""
    if not digests:
        return "No owners to notify."

    transport = CONFIG.notify_transport
    if transport == "smtp" and send:
        count = send_smtp(digests)
        return f"Sent {count} digest(s) over SMTP."

    send_console(digests)
    if send and transport != "smtp":
        return (
            "Rendered only: --send was passed but CUSTODIAN_NOTIFY is "
            f"'{transport}', not 'smtp'. Nothing was delivered."
        )
    return "Rendered only (dry run)."
