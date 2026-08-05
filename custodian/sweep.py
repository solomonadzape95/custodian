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
Sweep the catalog for dead assets{scope_clause}.

Examine up to {limit} datasets. For each one, gather its death signals, then
score it:

  no downstream lineage   0.40
  no owner                0.25
  no description          0.15
  stale last-operation    0.10
  no recent queries       0.10

An asset is a candidate only if it has NO downstream lineage AND scores
{threshold} or higher. Anything with a live dependent is not a candidate,
whatever else is true of it.

For each candidate:
  1. `add_tags` — apply `{tag}`
  2. `add_structured_properties` — record the score and a one-line reason

Then `save_document` a single sweep report titled "Custodian Sweep", listing
each candidate with its score and reasoning, and stating clearly that nothing
has been deprecated and the candidates await human approval.

Finally, reply with that report.
"""


async def sweep(
    *,
    scope: str = "",
    limit: int = 25,
    threshold: float = DEFAULT_THRESHOLD,
) -> AgentResult:
    provider = get_provider(CONFIG.provider)

    prompt = _PROMPT.format(
        scope_clause=f" within {scope}" if scope else "",
        limit=limit,
        threshold=threshold,
        tag=DEPRECATION_TAG,
    )

    async with datahub_session() as session:
        await assert_tools_available(session)
        return await provider.run_agent(
            system=SWEEP_SYSTEM,
            prompt=prompt,
            session=session,
            max_tool_calls=CONFIG.max_tool_calls,
        )
