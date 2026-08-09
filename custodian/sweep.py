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
    "add_structured_properties",
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
A deterministic scan of {scanned} datasets{scope_clause} has already scored
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
     - `add_structured_properties` recording the score and reason. The keys
       must be full URNs, not bare names, or the call is rejected:
         "urn:li:structuredProperty:custodian_deadness_score": [<score>]
         "urn:li:structuredProperty:custodian_reason": ["<reason>"]
3. `save_document` one report titled "Custodian Sweep" listing each endorsed
   candidate with its score and reasoning, and any you dropped with why. If
   any assets were held back by a retention or legal signal, list them in a
   "Spared" section with the property that protected them — a reader needs to
   see that they were considered and deliberately left alone. State plainly
   that nothing has been deprecated and that these await human approval via
   `custodian approve`.

Then reply with that report.
"""


async def sweep(
    *,
    scope: str = "",
    limit: int = 25,
    threshold: float = DEFAULT_THRESHOLD,
) -> AgentResult:
    provider = get_provider(CONFIG.provider)
    ensure_custodian_tags()
    ensure_custodian_properties()

    # The scan is deterministic and free; only the judgment needs a model.
    from .signals import scan

    candidates, protected, scanned = scan(limit=limit, platform=scope or None)

    spared = ""
    if protected:
        spared = "\nHeld back — these scored as dead but carry an explicit " \
            "retention or legal signal. Do not stage them. Report them as " \
            "spared, with the property that protected them:\n\n" + "\n".join(
                f"- {p.name} (score {p.score:.2f}) — protected by {p.protection}"
                for p in protected
            ) + "\n"

    if not candidates:
        note = f" {len(protected)} were spared by a retention signal." if protected else ""
        return AgentResult(
            text=(
                f"Scanned {scanned} datasets{' within ' + scope if scope else ''}. "
                "No dead assets found — every asset either has a live downstream "
                f"dependency or scored below the threshold.{note} Nothing staged."
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
        scanned=scanned,
        count=len(candidates),
        threshold=threshold,
        candidates=listing,
        spared_clause=spared,
        tag=DEPRECATION_TAG,
    )

    async with datahub_session() as session:
        await assert_tools_available(session)
        return await provider.run_agent(
            system=SWEEP_SYSTEM,
            prompt=prompt,
            session=session,
            max_tool_calls=CONFIG.max_tool_calls,
            allow_tools=SWEEP_TOOLS,
        )
