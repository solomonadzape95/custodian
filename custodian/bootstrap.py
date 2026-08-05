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
from datahub.metadata.schema_classes import (
    StructuredPropertyDefinitionClass,
    TagPropertiesClass,
)

from .config import CONFIG
from .report import DEPRECATION_TAG, INCIDENT_TAG

CUSTODIAN_TAGS = {
    INCIDENT_TAG: "Impacted by an incident Custodian investigated.",
    DEPRECATION_TAG: (
        "Staged for retirement by Custodian; awaiting human approval "
        "via `custodian approve`."
    ),
}


# Structured properties have the same prerequisite as tags: the definition
# must exist before a value can be attached to an entity.
DEADNESS_SCORE = "custodian_deadness_score"
DEADNESS_REASON = "custodian_reason"


def _graph() -> DataHubGraph:
    return DataHubGraph(
        DatahubClientConfig(server=CONFIG.gms_url, token=CONFIG.gms_token or None)
    )


def ensure_custodian_tags() -> None:
    """Create Custodian's tag entities if they are not already present."""
    graph = _graph()
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


def ensure_custodian_properties() -> None:
    """Define the structured properties Custodian stamps onto candidates.

    Same prerequisite as tags — DataHub rejects a value for a property it has
    no definition for, and the MCP server reports that as result text rather
    than a tool error.
    """
    graph = _graph()
    definitions = [
        (
            DEADNESS_SCORE,
            "NUMBER",
            "Custodian deadness score",
            "0.0-1.0. Higher means more evidence the asset is unused. "
            "Lineage orphanhood is weighted heaviest and acts as a veto.",
        ),
        (
            DEADNESS_REASON,
            "STRING",
            "Custodian reason",
            "Why Custodian staged this asset for retirement.",
        ),
    ]
    for qualified_name, value_type, display, description in definitions:
        urn = f"urn:li:structuredProperty:{qualified_name}"
        if (
            graph.get_aspect(
                entity_urn=urn, aspect_type=StructuredPropertyDefinitionClass
            )
            is not None
        ):
            continue
        graph.emit(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=StructuredPropertyDefinitionClass(
                    qualifiedName=qualified_name,
                    valueType=f"urn:li:dataType:datahub.{value_type.lower()}",
                    entityTypes=["urn:li:entityType:datahub.dataset"],
                    displayName=display,
                    description=description,
                    cardinality="SINGLE",
                ),
            )
        )
