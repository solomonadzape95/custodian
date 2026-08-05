"""Provider seam.

Custodian's default path is Gemini's free tier so the project can be cloned
and run at zero cost. The seam exists so that swapping in another model is a
new file rather than a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from mcp import ClientSession


@dataclass
class AgentResult:
    """What an agent run produced."""

    text: str
    tool_calls: list[str] = field(default_factory=list)
    failed_calls: list[str] = field(default_factory=list)
    """Tools that returned an error.

    Worth surfacing separately: a model will often narrate a write as done
    even when the tool rejected it, so "the post-mortem says it tagged things"
    is not evidence that anything was tagged.
    """
    hit_call_ceiling: bool = False
    """True if the run stopped because it exhausted its tool-call budget.

    Callers must treat the output as incomplete when this is set — the model
    stops mid-task with no error of its own.
    """


class LLMProvider(Protocol):
    """Drives a tool-use loop with the DataHub MCP tools attached."""

    name: str

    async def run_agent(
        self,
        *,
        system: str,
        prompt: str,
        session: ClientSession,
        max_tool_calls: int,
        allow_tools: set[str] | None = None,
    ) -> AgentResult: ...


def get_provider(name: str) -> LLMProvider:
    if name == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider()
    raise ValueError(
        f"Unknown provider {name!r}. Supported: 'gemini'. "
        "Set CUSTODIAN_PROVIDER in .env"
    )
