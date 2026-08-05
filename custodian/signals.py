"""Gather death signals for datasets, in code rather than through the agent.

Deciding whether an asset is dead needs two different things: enumerating
lineage/owners/descriptions across the catalog, and judging what the result
means. Only the second needs a model.

Asking the agent to do both means one tool call per asset per signal — a
sweep of a dozen datasets exhausted a 40-call budget before it reached the
point of writing anything down, and every one of those calls was a
deterministic lookup with no judgment in it.

So the scan runs here: fast, free, repeatable, and using the scoring in
`sweep.py` that is unit-testable. The agent gets a scored shortlist and does
the part that actually calls for reading — sanity-checking the candidates and
recording the reasoning back into the graph.
"""

from __future__ import annotations

from dataclasses import dataclass

from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import (
    DatasetPropertiesClass,
    EditableDatasetPropertiesClass,
    OwnershipClass,
)
from datahub.sdk.main_client import DataHubClient

from .config import CONFIG
from .sweep import Signals, explain, is_candidate, score


@dataclass
class Candidate:
    urn: str
    name: str
    signals: Signals
    score: float
    reason: str


def _clients() -> tuple[DataHubGraph, DataHubClient]:
    CONFIG.require_datahub()
    token = CONFIG.gms_token or None
    graph = DataHubGraph(DatahubClientConfig(server=CONFIG.gms_url, token=token))
    sdk = DataHubClient(server=CONFIG.gms_url, token=token)
    return graph, sdk


def _readable(urn: str) -> str:
    return urn.split(",")[-2] if "," in urn else urn


def _has_description(graph: DataHubGraph, urn: str) -> bool:
    editable = graph.get_aspect(
        entity_urn=urn, aspect_type=EditableDatasetPropertiesClass
    )
    if editable is not None and (editable.description or "").strip():
        return True
    props = graph.get_aspect(entity_urn=urn, aspect_type=DatasetPropertiesClass)
    return bool(props is not None and (props.description or "").strip())


def scan(limit: int = 25, platform: str | None = None) -> tuple[list[Candidate], int]:
    """Score datasets for deadness. Returns (candidates, number scanned)."""
    graph, sdk = _clients()

    urns = list(
        graph.get_urns_by_filter(entity_types=["dataset"], platform=platform)
    )[:limit]

    candidates: list[Candidate] = []
    for urn in urns:
        try:
            downstream = sdk.lineage.get_lineage(
                source_urn=urn, direction="downstream", max_hops=1
            )
        except Exception:
            continue  # an asset we cannot read lineage for is not a safe candidate

        ownership = graph.get_aspect(entity_urn=urn, aspect_type=OwnershipClass)

        signals = Signals(
            no_downstream=not downstream,
            no_owner=not (ownership and ownership.owners),
            no_description=not _has_description(graph, urn),
        )
        if not is_candidate(signals):
            continue

        candidates.append(
            Candidate(
                urn=urn,
                name=_readable(urn),
                signals=signals,
                score=score(signals),
                reason=explain(signals),
            )
        )

    candidates.sort(key=lambda c: -c.score)
    return candidates, len(urns)
