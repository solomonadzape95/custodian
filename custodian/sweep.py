"""`custodian sweep` — find dead assets and stage them for human approval.

Custodian never deprecates anything itself. It tags candidates, records why,
and writes a report; `custodian approve` is the human-triggered half that
actually lands the change.

Scoring uses only signals present in DataHub Core. Usage-based ranking
(`queryCountLast30DaysFeature`) is a DataHub Cloud feature and is deliberately
not relied on here.
"""

from __future__ import annotations

from dataclasses import dataclass

from datahub.emitter.mcp import MetadataChangeProposalWrapper

from .bootstrap import ensure_custodian_properties, ensure_custodian_tags
from .config import CONFIG
from .mcp_client import assert_tools_available, datahub_session
from .providers.base import AgentResult, get_provider
from .report import DEPRECATION_TAG, SWEEP_SYSTEM

# Downstream lineage dominates on purpose: an asset with live dependents is
# never safe to retire, however bad its other signals look.
WEIGHTS = {
    "no_downstream": 0.40,
    "no_owner": 0.25,
    "no_description": 0.15,
    "stale": 0.10,
    "no_queries": 0.10,
}

DEFAULT_THRESHOLD = 0.65

# Some assets are orphaned in lineage on purpose. A retention policy is the
# clearest case: nothing reads the table today, but somebody committed to
# keeping it for a fixed period, usually for a regulator. Deadness scoring
# cannot see that intent — lineage says "dead", the policy says "keep" — so
# these keys veto retirement outright rather than merely lowering a score.
PROTECTED_KEYS = ("retention", "legal_hold", "legalhold", "compliance")

# Only what a sweep needs — declared tools cost input tokens every turn.
SWEEP_TOOLS = {
    "search",
    "get_entities",
    "get_lineage",
    "get_dataset_queries",
    "add_tags",
    "save_document",
}


@dataclass
class Signals:
    """Death signals for one asset. Every field defaults to 'looks alive'."""

    no_downstream: bool = False
    no_owner: bool = False
    no_description: bool = False
    stale: bool = False
    no_queries: bool = False


def score(signals: Signals) -> float:
    """Weighted deadness score in [0.0, 1.0]. Higher means deader."""
    return round(
        sum(weight for key, weight in WEIGHTS.items() if getattr(signals, key)),
        2,
    )


def is_candidate(signals: Signals, threshold: float = DEFAULT_THRESHOLD) -> bool:
    """A live downstream dependency vetoes retirement regardless of score."""
    if not signals.no_downstream:
        return False
    return score(signals) >= threshold


def protected_reason(custom_properties: dict[str, str] | None) -> str | None:
    """Why this asset must not be retired, or None if nothing protects it.

    Matches on the property *key*, not its value: `retentionPeriod: '7 years'`
    and `legal_hold: 'false'` both surface here. A false-looking value is not
    treated as permission to delete — deciding that is the human's call, and
    the reason is carried into the report so they can make it.
    """
    for key, value in (custom_properties or {}).items():
        folded = key.lower().replace("-", "_")
        if any(marker in folded for marker in PROTECTED_KEYS):
            return f"{key} = {value}"
    return None


def explain(signals: Signals) -> str:
    """Human-readable justification, for the structured property and report."""
    reasons = {
        "no_downstream": "nothing downstream depends on it",
        "no_owner": "no owner assigned",
        "no_description": "no description",
        "stale": "not updated recently",
        "no_queries": "no recent query activity",
    }
    hits = [text for key, text in reasons.items() if getattr(signals, key)]
    return "; ".join(hits) if hits else "no death signals"


_PROMPT = """\
A deterministic scan of {coverage}{scope_clause} has already scored
every asset for deadness. These {count} cleared the bar — no downstream
dependents, and a weighted score at or above {threshold}:

{candidates}
{spared_clause}
Scoring: no downstream lineage 0.40, no owner 0.25, no description 0.15,
stale last-operation 0.10, no recent queries 0.10. Lineage orphanhood acts as
a veto, so nothing with a live dependent reached this list.

Your job is the judgment and the record, not the arithmetic.

1. Spot-check the riskiest one or two with `get_lineage` and `get_entities`.
   The scan is mechanical and can be wrong about intent — an asset can be
   orphaned in lineage yet obviously deliberate (a published extract, a
   regulatory archive, something whose description says to keep it). If you
   find a reason to spare a candidate, drop it and say why.
2. For every candidate you still endorse:
     - `add_tags` with `{tag}` (one call, pass all the URNs together)
   Custodian records the score and reason onto the asset itself, so do not
   try to write those — just tag what you endorse.
3. `save_document` one report titled "Custodian Sweep" listing each endorsed
   candidate with its score and reasoning, and any you dropped with why. If
   any assets were held back by a retention or legal signal, list them in a
   "Spared" section with the property that protected them — a reader needs to
   see that they were considered and deliberately left alone. State plainly
   that nothing has been deprecated and that these await human approval via
   `custodian approve`.

Then reply with that report.
"""


def record_reasoning(candidates) -> int:
    """Stamp each staged candidate's score and reason onto it, in code.

    The agent was asked to do this with `add_structured_properties` and it is
    unreliable at it — a malformed payload fails with `property_values cannot
    be empty`, and the MCP server reports that as ordinary text the model reads
    past and calls done.

    There is no judgment in this step anyway. The score and the reason were
    both computed deterministically by the scan; having a model retype them
    into a tool call adds a failure mode and nothing else. The agent still
    decides *which* candidates it endorses — that is the judgment — and code
    records the numbers it already knows.

    Only assets the agent actually tagged are stamped, so an endorsement it
    withdrew leaves no trace.
    """
    from datahub.metadata.schema_classes import (
        StructuredPropertiesClass,
        StructuredPropertyValueAssignmentClass,
    )

    from .bootstrap import DEADNESS_REASON, DEADNESS_SCORE
    from .watch import _clients, staged_urns

    graph, _ = _clients()
    tagged = staged_urns(graph)
    stamped = 0

    for candidate in candidates:
        if candidate.urn not in tagged:
            continue

        # Read-merge-write. The estate's own governance properties live in this
        # same aspect — clobbering `showcase.retentionPeriod` here would delete
        # the very signal the retention veto depends on.
        existing = graph.get_aspect(
            entity_urn=candidate.urn, aspect_type=StructuredPropertiesClass
        )
        ours = {
            f"urn:li:structuredProperty:{DEADNESS_SCORE}": [candidate.score],
            f"urn:li:structuredProperty:{DEADNESS_REASON}": [candidate.reason],
        }
        kept = [
            assignment
            for assignment in (existing.properties if existing else [])
            if assignment.propertyUrn not in ours
        ]
        graph.emit(
            MetadataChangeProposalWrapper(
                entityUrn=candidate.urn,
                aspect=StructuredPropertiesClass(
                    properties=kept
                    + [
                        StructuredPropertyValueAssignmentClass(
                            propertyUrn=urn, values=values
                        )
                        for urn, values in ours.items()
                    ]
                ),
            )
        )
        stamped += 1

    return stamped


async def sweep(
    *,
    scope: str = "",
    limit: int = 500,
    threshold: float = DEFAULT_THRESHOLD,
) -> AgentResult:
    provider = get_provider(CONFIG.provider)
    ensure_custodian_tags()
    ensure_custodian_properties()

    # The scan is deterministic and free; only the judgment needs a model.
    from .signals import scan

    candidates, protected, scanned, total = scan(limit=limit, platform=scope or None)
    coverage = (
        f"{scanned} of {total} datasets"
        if scanned < total
        else f"all {total} datasets"
    )

    spared = ""
    if protected:
        spared = "\nHeld back — these scored as dead but carry an explicit " \
            "retention or legal signal. Do not stage them. Report them as " \
            "spared, with the property that protected them:\n\n" + "\n".join(
                f"- {p.name} (score {p.score:.2f}) — protected by {p.protection}"
                for p in protected
            ) + "\n"

    if not candidates:
        note = (
            f" {len(protected)} scored as dead but were spared by a retention "
            "or legal signal." if protected else ""
        )
        # Say what was *not* looked at. A partial scan reporting "no dead
        # assets" reads as an all-clear for the whole estate, and is not one.
        caveat = (
            f" Note: {total - scanned} dataset(s) were not examined — raise "
            "--limit to cover the estate."
            if scanned < total
            else ""
        )
        return AgentResult(
            text=(
                f"Scanned {coverage}{' within ' + scope if scope else ''}. "
                "No dead assets found — every asset either has a live downstream "
                f"dependency or scored below the threshold.{note}{caveat} "
                "Nothing staged."
            )
        )

    listing = "\n".join(
        f"- {c.name}\n"
        f"    urn: {c.urn}\n"
        f"    score: {c.score:.2f} — {c.reason}"
        for c in candidates
    )

    prompt = _PROMPT.format(
        scope_clause=f" within {scope}" if scope else "",
        coverage=coverage,
        count=len(candidates),
        threshold=threshold,
        candidates=listing,
        spared_clause=spared,
        tag=DEPRECATION_TAG,
    )

    async with datahub_session() as session:
        await assert_tools_available(session)
        result = await provider.run_agent(
            system=SWEEP_SYSTEM,
            prompt=prompt,
            session=session,
            max_tool_calls=CONFIG.max_tool_calls,
            allow_tools=SWEEP_TOOLS,
        )

    # Record the arithmetic in code, once the agent has said what it endorses.
    record_reasoning(candidates)
    return result
