"""Gemini provider.

`google-genai` accepts an MCP `ClientSession` directly in `tools`, then drives
the tool-use loop itself via automatic function calling. That means Custodian
does not hand-roll a tool dispatch loop — the DataHub MCP tools are handed to
the model as-is, and the model decides which lineage hops to walk.
"""

from __future__ import annotations

from google import genai
from google.genai import types
from mcp import ClientSession

from ..config import CONFIG
from .base import AgentResult


class GeminiProvider:
    name = "gemini"

    def __init__(self) -> None:
        CONFIG.require_provider()
        self._client = genai.Client(api_key=CONFIG.gemini_api_key)

    async def run_agent(
        self,
        *,
        system: str,
        prompt: str,
        session: ClientSession,
        max_tool_calls: int,
    ) -> AgentResult:
        response = await self._client.aio.models.generate_content(
            model=CONFIG.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0,
                tools=[session],
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    # google-genai defaults this to 10. A multi-hop lineage walk
                    # blows past that, and the SDK then stops and returns a
                    # truncated answer with no error — so set it explicitly.
                    maximum_remote_calls=max_tool_calls,
                ),
            ),
        )

        tool_calls = _extract_tool_calls(response)
        return AgentResult(
            text=response.text or "",
            tool_calls=tool_calls,
            # The SDK stops silently at the ceiling; infer it from the count so
            # callers can flag the output as incomplete rather than trust it.
            hit_call_ceiling=len(tool_calls) >= max_tool_calls,
        )


def _extract_tool_calls(response: types.GenerateContentResponse) -> list[str]:
    """Names of the tools the model actually invoked, in order."""
    history = getattr(response, "automatic_function_calling_history", None) or []
    names: list[str] = []
    for entry in history:
        for part in getattr(entry, "parts", None) or []:
            call = getattr(part, "function_call", None)
            if call is not None and call.name:
                names.append(call.name)
    return names
