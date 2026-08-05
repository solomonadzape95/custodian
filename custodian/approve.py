"""`custodian approve` — the human half of the sweep loop.

The MCP server exposes no deprecation tool, so this is the one place Custodian
talks to DataHub through the Python SDK instead. That split is deliberate and
worth keeping: the agent can only ever *stage* a retirement, and landing it
requires a human running this command.
"""

from __future__ import annotations

from dataclasses import dataclass

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import DeprecationClass

from .config import CONFIG
from .report import DEPRECATION_TAG

CUSTODIAN_ACTOR = "urn:li:corpuser:datahub"


@dataclass
class Approval:
    urn: str
    name: str
    note: str


def _graph() -> DataHubGraph:
    CONFIG.require_datahub()
    return DataHubGraph(
        DatahubClientConfig(server=CONFIG.gms_url, token=CONFIG.gms_token)
    )


def find_candidates(graph: DataHubGraph, limit: int = 50) -> list[Approval]:
    """Datasets Custodian tagged `deprecation-candidate` and not yet deprecated.

    The tag filter is pushed server-side. Listing every dataset and checking
    aspects client-side costs two HTTP round-trips per asset, which on a
    thousand-entity estate is thousands of calls to find a handful of matches.
    """
    tag_urn = f"urn:li:tag:{DEPRECATION_TAG}"
    candidates: list[Approval] = []

    tagged = graph.get_urns_by_filter(
        entity_types=["dataset"],
        extraFilters=[{"field": "tags", "condition": "EQUAL", "values": [tag_urn]}],
    )

    for urn in tagged:
        existing = graph.get_aspect(entity_urn=urn, aspect_type=DeprecationClass)
        if existing is not None and existing.deprecated:
            continue  # already retired; nothing to approve

        candidates.append(
            Approval(urn=urn, name=_readable(urn), note=_reason_for(graph, urn))
        )
        if len(candidates) >= limit:
            break

    return candidates


def _readable(urn: str) -> str:
    """`urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.orders,PROD)` -> table name."""
    if "," in urn:
        parts = urn.split(",")
        if len(parts) >= 2:
            return parts[-2]
    return urn


def approve(graph: DataHubGraph, candidate: Approval) -> None:
    """Emit the real `deprecation` aspect. This is the change a human authorized."""
    graph.emit(
        MetadataChangeProposalWrapper(
            entityUrn=candidate.urn,
            aspect=DeprecationClass(
                deprecated=True,
                note=candidate.note,
                actor=CUSTODIAN_ACTOR,
            ),
        )
    )


def _tags_class():
    from datahub.metadata.schema_classes import GlobalTagsClass

    return GlobalTagsClass


def _reason_for(graph: DataHubGraph, urn: str) -> str:
    """Reuse Custodian's own recorded reasoning as the deprecation note."""
    default = "Retired via Custodian sweep: no downstream dependencies found."
    try:
        from datahub.metadata.schema_classes import StructuredPropertiesClass

        props = graph.get_aspect(
            entity_urn=urn, aspect_type=StructuredPropertiesClass
        )
    except Exception:
        return default

    if props is None:
        return default

    for assignment in props.properties:
        if "custodian_reason" in assignment.propertyUrn and assignment.values:
            return f"Retired via Custodian sweep: {assignment.values[0]}"
    return default


def open_graph() -> DataHubGraph:
    return _graph()
