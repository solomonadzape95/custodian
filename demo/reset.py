"""Undo everything Custodian wrote, so the demo can be re-run from clean.

Rehearsing the video means running `investigate` and `sweep` repeatedly, and
both write to the graph. Without this you are re-recording against an estate
that already has last take's tags and deprecations on it.

Removes: `incident-impacted` and `deprecation-candidate` tags, and any
deprecation Custodian applied. Leaves the datapack's own metadata alone.

Usage:
    python demo/reset.py
"""

from __future__ import annotations

import os
import sys

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import DeprecationClass, GlobalTagsClass

CUSTODIAN_TAGS = [
    "urn:li:tag:incident-impacted",
    "urn:li:tag:deprecation-candidate",
]
CUSTODIAN_ACTOR = "urn:li:corpuser:datahub"


def _graph() -> DataHubGraph:
    return DataHubGraph(
        DatahubClientConfig(
            server=os.getenv("DATAHUB_GMS_URL", "http://localhost:8080"),
            token=os.getenv("DATAHUB_GMS_TOKEN") or None,
        )
    )


def clear_tags(graph: DataHubGraph) -> int:
    cleared = 0
    for tag_urn in CUSTODIAN_TAGS:
        urns = list(
            graph.get_urns_by_filter(
                entity_types=["dataset", "dashboard", "chart"],
                extraFilters=[
                    {"field": "tags", "condition": "EQUAL", "values": [tag_urn]}
                ],
            )
        )
        for urn in urns:
            existing = graph.get_aspect(entity_urn=urn, aspect_type=GlobalTagsClass)
            if not existing or not existing.tags:
                continue
            kept = [t for t in existing.tags if t.tag != tag_urn]
            if len(kept) == len(existing.tags):
                continue
            graph.emit(
                MetadataChangeProposalWrapper(
                    entityUrn=urn, aspect=GlobalTagsClass(tags=kept)
                )
            )
            cleared += 1
    return cleared


def clear_deprecations(graph: DataHubGraph) -> int:
    """Un-deprecate anything Custodian retired.

    Scoped by actor and note so a deprecation set by a human, or shipped with
    the datapack, is never silently reverted.
    """
    cleared = 0
    for urn in graph.get_urns_by_filter(entity_types=["dataset"]):
        dep = graph.get_aspect(entity_urn=urn, aspect_type=DeprecationClass)
        if dep is None or not dep.deprecated:
            continue
        if dep.actor != CUSTODIAN_ACTOR or "Custodian" not in (dep.note or ""):
            continue
        graph.emit(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=DeprecationClass(
                    deprecated=False, note="", actor=CUSTODIAN_ACTOR
                ),
            )
        )
        cleared += 1
    return cleared


def main() -> int:
    graph = _graph()
    tags = clear_tags(graph)
    deps = clear_deprecations(graph)
    print(f"Cleared Custodian tags from {tags} entities.")
    print(f"Reverted {deps} Custodian deprecations.")
    print("\nEstate is clean. Re-plant the incident with:")
    print("  python demo/plant_incident.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
