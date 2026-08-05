"""Make sure the tags Custodian applies actually exist first.

DataHub's `batchAddTags` validates that a tag entity exists before it will
attach it, and fails with "Urn does not exist" otherwise. The MCP server
surfaces that as ordinary result *text* rather than a tool error, so an agent
will happily read the failure and still report success — the tags simply never
appear in the catalog.

Creating them up front is idempotent and costs one call per tag.
"""

from __future__ import annotations

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import TagPropertiesClass

from .config import CONFIG
from .report import DEPRECATION_TAG, INCIDENT_TAG

CUSTODIAN_TAGS = {
    INCIDENT_TAG: "Impacted by an incident Custodian investigated.",
    DEPRECATION_TAG: (
        "Staged for retirement by Custodian; awaiting human approval "
        "via `custodian approve`."
    ),
}


def ensure_custodian_tags() -> None:
    """Create Custodian's tag entities if they are not already present."""
    graph = DataHubGraph(
        DatahubClientConfig(server=CONFIG.gms_url, token=CONFIG.gms_token or None)
    )
    for name, description in CUSTODIAN_TAGS.items():
        urn = f"urn:li:tag:{name}"
        if graph.get_aspect(entity_urn=urn, aspect_type=TagPropertiesClass) is not None:
            continue
        graph.emit(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=TagPropertiesClass(name=name, description=description),
            )
        )
