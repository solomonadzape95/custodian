"""Stage a believable incident in the showcase-ecommerce graph.

The scenario: an upstream ETL change starts writing NULL into
`order_items.unit_price`. Everything downstream that computes revenue from it
silently reports zero, and the exec revenue dashboards flatline.

Nothing here fakes the *answer* — no tag says "this is the root cause". It
plants the same evidence a real incident would leave in the catalog, and the
agent has to walk lineage to find it. Run `demo/plant_incident.py --clean` to
remove it again.

Usage:
    python demo/plant_incident.py          # plant
    python demo/plant_incident.py --clean  # revert
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import (
    AuditStampClass,
    EditableDatasetPropertiesClass,
    GlobalTagsClass,
    TagAssociationClass,
)

PLATFORM = "snowflake"
DB = "b2fd91.order_entry_db"

CULPRIT = f"urn:li:dataset:(urn:li:dataPlatform:{PLATFORM},{DB}.order_entry.order_items,PROD)"
BROKEN = f"urn:li:dataset:(urn:li:dataPlatform:{PLATFORM},{DB}.analytics.order_details,PROD)"

ANOMALY_TAG = "urn:li:tag:freshness-anomaly"
ACTOR = "urn:li:corpuser:datahub"

CULPRIT_NOTE = (
    "UPSTREAM CHANGE 2026-08-04: the nightly ETL was migrated to a new "
    "extractor. `unit_price` is now written as NULL for all rows loaded after "
    "02:00 UTC; `quantity` is unaffected. Source system still has the values. "
    "Not yet backfilled."
)

BROKEN_NOTE = (
    "Revenue columns computed from order_items.unit_price have been reporting "
    "0.00 since 2026-08-04. Row counts look normal, which is why nothing "
    "alerted."
)


def _graph() -> DataHubGraph:
    return DataHubGraph(
        DatahubClientConfig(
            server=os.getenv("DATAHUB_GMS_URL", "http://localhost:8080"),
            token=os.getenv("DATAHUB_GMS_TOKEN") or None,
        )
    )


def _describe(graph: DataHubGraph, urn: str, text: str | None) -> None:
    """Write an editable description.

    Deliberately the *editable* aspect: it is where human/tool annotations
    belong, so this never clobbers the datapack's ingested properties.
    """
    now = AuditStampClass(time=int(time.time() * 1000), actor=ACTOR)
    graph.emit(
        MetadataChangeProposalWrapper(
            entityUrn=urn,
            aspect=EditableDatasetPropertiesClass(description=text, lastModified=now),
        )
    )


def _add_tag(graph: DataHubGraph, urn: str, tag_urn: str) -> None:
    """Add one tag, preserving the rest.

    GlobalTags is a whole-aspect write — emitting a bare list silently drops
    every tag the datapack already applied. Read, merge, write back.
    """
    existing = graph.get_aspect(entity_urn=urn, aspect_type=GlobalTagsClass)
    tags = list(existing.tags) if existing and existing.tags else []
    if any(t.tag == tag_urn for t in tags):
        return
    tags.append(TagAssociationClass(tag=tag_urn))
    graph.emit(MetadataChangeProposalWrapper(entityUrn=urn, aspect=GlobalTagsClass(tags=tags)))


def _remove_tag(graph: DataHubGraph, urn: str, tag_urn: str) -> None:
    """Remove only our tag, leaving everything else intact."""
    existing = graph.get_aspect(entity_urn=urn, aspect_type=GlobalTagsClass)
    if not existing or not existing.tags:
        return
    kept = [t for t in existing.tags if t.tag != tag_urn]
    if len(kept) == len(existing.tags):
        return
    graph.emit(MetadataChangeProposalWrapper(entityUrn=urn, aspect=GlobalTagsClass(tags=kept)))


def plant(graph: DataHubGraph) -> None:
    _describe(graph, CULPRIT, CULPRIT_NOTE)
    _add_tag(graph, CULPRIT, ANOMALY_TAG)
    _describe(graph, BROKEN, BROKEN_NOTE)
    print("Planted incident:")
    print(f"  culprit (upstream) : {CULPRIT.split(',')[-2]}")
    print(f"  symptom (reported) : {BROKEN.split(',')[-2]}")
    print("\nNow run:")
    print("  custodian investigate order_details \\")
    print('    --context "Exec revenue dashboards flatlined this morning."')


def clean(graph: DataHubGraph) -> None:
    _describe(graph, CULPRIT, None)
    _remove_tag(graph, CULPRIT, ANOMALY_TAG)
    _describe(graph, BROKEN, None)
    print("Incident metadata removed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="revert the incident")
    args = parser.parse_args()

    graph = _graph()
    if args.clean:
        clean(graph)
    else:
        plant(graph)
    return 0


if __name__ == "__main__":
    sys.exit(main())
