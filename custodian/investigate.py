"""`custodian investigate <table>` — explain an incident and write it back.

The lineage walk is not scripted here. The agent is given the DataHub MCP
tools and decides which hops matter, which is the point: a fixed traversal
would break on the first graph shaped differently from the demo.
"""

from __future__ import annotations

from .bootstrap import ensure_custodian_tags
from .config import CONFIG
from .mcp_client import assert_tools_available, datahub_session
from .providers.base import AgentResult, get_provider
from .report import INCIDENT_TAG, POSTMORTEM_SYSTEM

_PROMPT = """\
The table `{table}` is producing bad data. {context}

Investigate and produce an incident post-mortem with these sections:

## Summary
One or two sentences: what broke and what it affected.

## Blast Radius (downstream)
Every impacted asset, one per line, as `- <name> (<type>) — owner: <owner>`.
Write "None found" if nothing depends on it.

## Likely Root Cause (upstream)
Trace back from `{table}` toward the origin. Name the specific upstream asset
you believe is responsible and the lineage path that connects them. If the
evidence does not support naming a cause, say the cause is undetermined and
state what you ruled out.

## Impacted Owners to Notify
The distinct owners across all impacted assets.

## Recommended Actions
Concrete next steps.

Then persist your findings:
  1. `save_document` — the full post-mortem, titled
     "Incident Post-Mortem: {table}"
  2. `add_tags` — apply `{tag}` to every impacted asset you identified

Finally, reply with the post-mortem text itself.
"""


# Only what an investigation needs. Every declared tool costs input tokens on
# every turn, and the free tier meters those per minute.
INVESTIGATE_TOOLS = {
    "search",
    "get_entities",
    "get_lineage",
    "get_lineage_paths_between",
    "get_dataset_queries",
    "add_tags",
    "save_document",
}


async def investigate(table: str, *, context: str = "") -> AgentResult:
    provider = get_provider(CONFIG.provider)
    # DataHub refuses to attach a tag whose entity does not exist yet, and the
    # MCP server reports that as ordinary text the agent can talk past.
    ensure_custodian_tags()

    prompt = _PROMPT.format(
        table=table,
        context=context or "A downstream consumer reported the issue this morning.",
        tag=INCIDENT_TAG,
    )

    async with datahub_session() as session:
        await assert_tools_available(session)
        return await provider.run_agent(
            system=POSTMORTEM_SYSTEM,
            prompt=prompt,
            session=session,
            max_tool_calls=CONFIG.max_tool_calls,
            allow_tools=INVESTIGATE_TOOLS,
        )
