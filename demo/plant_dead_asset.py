"""Plant a genuinely dead asset for the sweep demo.

Once the retention veto works, the showcase estate has no unprotected dead
assets left: the two orphans it ships both carry a `retentionPeriod`, so
Custodian correctly refuses to stage either. That is the right answer, and it
leaves nothing to demonstrate the staging half of the loop.

So this creates one asset that is dead by every signal and protected by none —
an abandoned scratch table of the kind every real warehouse accumulates. No
owner, no description, no downstream consumers, no retention policy.

Run it alongside `plant_incident.py` to get the full story:

    python -m demo.plant_dead_asset     # something to retire
    python -m demo.plant_incident       # something to investigate

`python -m demo.plant_dead_asset --clean` removes it again.
"""

from __future__ import annotations

import argparse

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import DatasetPropertiesClass, StatusClass

from custodian.config import CONFIG

PLATFORM = "snowflake"
NAME = "b2fd91.order_entry_db.analytics.tmp_order_backfill_2019"
URN = f"urn:li:dataset:(urn:li:dataPlatform:{PLATFORM},{NAME},PROD)"


def _graph() -> DataHubGraph:
    CONFIG.require_datahub()
    return DataHubGraph(
        DatahubClientConfig(server=CONFIG.gms_url, token=CONFIG.gms_token or None)
    )


def plant(graph: DataHubGraph) -> None:
    # Deliberately minimal: no description, no owner, no retention property,
    # and nothing downstream. Every death signal fires and no veto applies.
    graph.emit(
        MetadataChangeProposalWrapper(
            entityUrn=URN,
            aspect=DatasetPropertiesClass(name="tmp_order_backfill_2019"),
        )
    )
    graph.emit(
        MetadataChangeProposalWrapper(entityUrn=URN, aspect=StatusClass(removed=False))
    )
    print(f"Planted dead asset: {NAME}")
    print("  no owner, no description, no consumers, no retention policy")
    print("\nNow run:")
    print("  custodian sweep")


def clean(graph: DataHubGraph) -> None:
    graph.emit(
        MetadataChangeProposalWrapper(entityUrn=URN, aspect=StatusClass(removed=True))
    )
    print(f"Removed {NAME}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="Remove the asset again.")
    args = parser.parse_args()

    graph = _graph()
    clean(graph) if args.clean else plant(graph)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
