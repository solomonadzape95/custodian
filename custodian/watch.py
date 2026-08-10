"""`custodian watch` — the sweep, running continuously.

A CLI someone remembers to run is not a steward. This is the same sweep on a
loop, with two properties that matter more than the loop itself.

**The graph is the dedup store.** What has already been staged is recorded in
DataHub as a tag, so each tick asks the catalog what it already knows rather
than keeping its own state file. That makes `watch` stateless: kill it, restart
it, run two of them, and nothing is double-staged or lost. It also means a
human removing the tag in the UI is a decision `watch` will respect on the next
tick, instead of a change it silently reverts.

**Findings are retracted, not just added.** An asset staged last week may have
gained a consumer since. A stale candidate is worse than no candidate, because
someone may approve it — and approval is the irreversible half. So every tick
re-checks what is already staged and unstages anything that has come back to
life, before it looks for anything new.

Model calls are spent only on genuinely new candidates. A quiet estate costs
nothing but the deterministic scan.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import GlobalTagsClass
from datahub.sdk.main_client import DataHubClient

from .config import CONFIG
from .report import DEPRECATION_TAG

TAG_URN = f"urn:li:tag:{DEPRECATION_TAG}"


@dataclass
class TickResult:
    scanned: int = 0
    new: list[str] = field(default_factory=list)
    retracted: list[str] = field(default_factory=list)
    already_staged: int = 0
    spared: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.new or self.retracted)


def _clients() -> tuple[DataHubGraph, DataHubClient]:
    CONFIG.require_datahub()
    token = CONFIG.gms_token or None
    return (
        DataHubGraph(DatahubClientConfig(server=CONFIG.gms_url, token=token)),
        DataHubClient(server=CONFIG.gms_url, token=token),
    )


def staged_urns(graph: DataHubGraph) -> set[str]:
    """What DataHub already believes is staged. This is our whole memory."""
    return set(
        graph.get_urns_by_filter(
            entity_types=["dataset"],
            extraFilters=[
                {"field": "tags", "condition": "EQUAL", "values": [TAG_URN]}
            ],
        )
    )


def unstage(graph: DataHubGraph, urn: str) -> None:
    """Remove the candidate tag, preserving every other tag on the asset.

    Read-merge-write, not whole-aspect-write: the datapack ships its own tags
    and clobbering them would be a silent data loss that looks like success.
    """
    tags = graph.get_aspect(entity_urn=urn, aspect_type=GlobalTagsClass)
    if tags is None or not tags.tags:
        return
    kept = [t for t in tags.tags if t.tag != TAG_URN]
    if len(kept) == len(tags.tags):
        return
    graph.emit(
        MetadataChangeProposalWrapper(
            entityUrn=urn, aspect=GlobalTagsClass(tags=kept)
        )
    )


def revived(sdk: DataHubClient, urn: str) -> bool:
    """True if a staged asset has gained a downstream consumer since staging.

    Errors count as revived, deliberately. If we cannot prove an asset is
    orphaned, it must not stay queued for retirement — the failure mode of
    being wrong here is someone approving the deletion of a live table.
    """
    try:
        downstream = sdk.lineage.get_lineage(
            source_urn=urn, direction="downstream", max_hops=1
        )
    except Exception:
        return True
    return bool(downstream)


async def tick(*, limit: int, scope: str = "") -> TickResult:
    """One pass: retract what came back to life, then stage what is new."""
    from .signals import scan
    from .sweep import sweep

    graph, sdk = _clients()
    result = TickResult()

    # Retraction first. If an asset is both revived and rescanned this tick,
    # the retraction must not race the restage.
    for urn in staged_urns(graph):
        if revived(sdk, urn):
            unstage(graph, urn)
            result.retracted.append(urn)

    candidates, protected, scanned = scan(limit=limit, platform=scope or None)
    result.scanned = scanned
    result.spared = len(protected)

    known = staged_urns(graph)
    fresh = [c for c in candidates if c.urn not in known]
    result.already_staged = len(candidates) - len(fresh)

    if not fresh:
        return result

    # Only now is a model call justified.
    await sweep(scope=scope, limit=limit)
    result.new = [c.urn for c in fresh]
    return result


async def watch(
    *,
    interval: int,
    limit: int,
    scope: str = "",
    once: bool = False,
    on_tick=None,
) -> None:
    """Run ticks forever. `on_tick` receives each TickResult for reporting."""
    while True:
        result = await tick(limit=limit, scope=scope)
        if on_tick is not None:
            on_tick(result)
        if once:
            return
        await asyncio.sleep(interval)
