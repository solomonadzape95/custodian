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
    StructuredPropertiesClass,
)
from datahub.sdk.main_client import DataHubClient

from .config import CONFIG
from .sweep import Signals, explain, is_candidate, protected_reason, score


@dataclass
class Candidate:
    urn: str
    name: str
    signals: Signals
    score: float
    reason: str


@dataclass
class Protected:
    """Scored as dead, but held back by an explicit retention/legal signal."""

    urn: str
    name: str
    score: float
    reason: str
    protection: str


def _clients() -> tuple[DataHubGraph, DataHubClient]:
    CONFIG.require_datahub()
    token = CONFIG.gms_token or None
    graph = DataHubGraph(DatahubClientConfig(server=CONFIG.gms_url, token=token))
    sdk = DataHubClient(server=CONFIG.gms_url, token=token)
    return graph, sdk


def _readable(urn: str) -> str:
    return urn.split(",")[-2] if "," in urn else urn


def _has_description(
    graph: DataHubGraph, urn: str, props: DatasetPropertiesClass | None
) -> bool:
    editable = graph.get_aspect(
        entity_urn=urn, aspect_type=EditableDatasetPropertiesClass
    )
    if editable is not None and (editable.description or "").strip():
        return True
    return bool(props is not None and (props.description or "").strip())


def _governance_properties(
    graph: DataHubGraph, urn: str, props: DatasetPropertiesClass | None
) -> dict[str, str]:
    """Every key/value that could carry a retention or legal signal.

    Retention lives in different places depending on how an estate was built.
    Ingestion connectors put it in `customProperties`; a governance team that
    defined it deliberately puts it in *structured* properties. The
    showcase estate uses the latter (`showcase.retentionPeriod`), and reading
    only customProperties silently missed every one of them — the veto looked
    implemented and protected nothing.

    So both are merged before matching. The property name is stripped of its
    namespace so `showcase.retentionPeriod` matches the same rule as a bare
    `retentionPeriod`.
    """
    merged: dict[str, str] = dict(props.customProperties) if props else {}

    try:
        structured = graph.get_aspect(
            entity_urn=urn, aspect_type=StructuredPropertiesClass
        )
    except Exception:
        structured = None

    for assignment in structured.properties if structured else []:
        name = assignment.propertyUrn.split(":")[-1].split(".")[-1]
        if assignment.values:
            merged[name] = ", ".join(str(v) for v in assignment.values)

    return merged


def scan(
    limit: int = 25, platform: str | None = None
) -> tuple[list[Candidate], list[Protected], int]:
    """Score datasets for deadness.

    Returns (candidates, protected, number scanned). `protected` holds assets
    that scored as dead but carry a retention or legal-hold property; they are
    reported rather than staged, so the human sees what was spared and why.
    """
    graph, sdk = _clients()

    urns = list(
        graph.get_urns_by_filter(entity_types=["dataset"], platform=platform)
    )[:limit]

    candidates: list[Candidate] = []
    protected: list[Protected] = []
    for urn in urns:
        try:
            downstream = sdk.lineage.get_lineage(
                source_urn=urn, direction="downstream", max_hops=1
            )
        except Exception:
            continue  # an asset we cannot read lineage for is not a safe candidate

        ownership = graph.get_aspect(entity_urn=urn, aspect_type=OwnershipClass)
        props = graph.get_aspect(entity_urn=urn, aspect_type=DatasetPropertiesClass)

        signals = Signals(
            no_downstream=not downstream,
            no_owner=not (ownership and ownership.owners),
            no_description=not _has_description(graph, urn, props),
        )
        if not is_candidate(signals):
            continue

        # Checked only after scoring, so the report can say "this scored 0.80
        # and was spared anyway" rather than quietly omitting the asset.
        protection = protected_reason(_governance_properties(graph, urn, props))
        if protection:
            protected.append(
                Protected(
                    urn=urn,
                    name=_readable(urn),
                    score=score(signals),
                    reason=explain(signals),
                    protection=protection,
                )
            )
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
    protected.sort(key=lambda p: -p.score)
    return candidates, protected, len(urns)
