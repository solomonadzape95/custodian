"""Provider for any OpenAI-compatible chat-completions endpoint.

One implementation covers Groq, Cerebras and OpenRouter, because they all
speak the same wire format — only the base URL, key and model name differ.
That matters here for a practical reason: free-tier quota is the binding
constraint on how often Custodian can run, and the limits differ by an order
of magnitude between vendors. Being able to move by editing `.env` means a
spent daily quota is a config change, not a code change.

Tool-calling shape differs from Gemini's, so this is a separate loop rather
than a shared one, but the contract (`LLMProvider`) and the accounting
(`AgentResult`) are identical.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any

from mcp import ClientSession
from openai import AsyncOpenAI

from ..config import CONFIG, ConfigError
from .base import AgentResult
from .gemini import MAX_RESULT_CHARS, _result_text, looks_like_tool_error

# base_url, env var holding the key, and a sensible default model.
ENDPOINTS: dict[str, tuple[str, str, str]] = {
    "groq": (
        "https://api.groq.com/openai/v1",
        "GROQ_API_KEY",
        "llama-3.3-70b-versatile",
    ),
    "cerebras": (
        "https://api.cerebras.ai/v1",
        "CEREBRAS_API_KEY",
        "llama-3.3-70b",
    ),
    "openrouter": (
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        "meta-llama/llama-3.3-70b-instruct:free",
    ),
}


def _tool_schema(session_tools: Any, allow: set[str] | None) -> list[dict[str, Any]]:
    """Convert MCP tools to OpenAI function-tool definitions.

    These endpoints accept ordinary JSON Schema, so unlike the Gemini path
    there is no vocabulary to strip out.
    """
    out = []
    for tool in session_tools:
        if allow is not None and tool.name not in allow:
            continue
        raw = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", {})
        out.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": (tool.description or "")[:600],
                    "parameters": raw or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def _retry_delay(exc: Exception) -> float | None:
    """Seconds to wait on a rate-limit response, per the server's own hint."""
    message = str(exc)
    if "429" not in message and "rate" not in message.lower():
        return None
    match = re.search(r"try again in (\d+(?:\.\d+)?)s", message, re.I)
    if match:
        return min(float(match.group(1)) + 1.0, 65.0)
    return 20.0


class OpenAICompatProvider:
    """Groq / Cerebras / OpenRouter, selected by CUSTODIAN_PROVIDER."""

    def __init__(self, name: str) -> None:
        if name not in ENDPOINTS:
            raise ConfigError(f"Unknown OpenAI-compatible provider {name!r}")
        base_url, key_var, default_model = ENDPOINTS[name]

        import os

        api_key = os.getenv(key_var, "")
        if not api_key:
            raise ConfigError(
                f"{key_var} is not set. CUSTODIAN_PROVIDER={name} needs it in .env"
            )

        self.name = name
        self._model = CONFIG.compat_model or default_model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def _complete(self, **kwargs: Any) -> Any:
        for attempt in range(4):
            try:
                return await self._client.chat.completions.create(**kwargs)
            except Exception as exc:
                delay = _retry_delay(exc)
                if delay is None or attempt == 3:
                    raise
                print(
                    f"  [rate limited — waiting {delay:.0f}s]",
                    file=sys.stderr,
                    flush=True,
                )
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable")

    async def run_agent(
        self,
        *,
        system: str,
        prompt: str,
        session: ClientSession,
        max_tool_calls: int,
        allow_tools: set[str] | None = None,
    ) -> AgentResult:
        tools = _tool_schema((await session.list_tools()).tools, allow_tools)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

        called: list[str] = []
        failed: list[str] = []
        text = ""

        while True:
            response = await self._complete(
                model=self._model,
                messages=messages,
                tools=tools,
                temperature=0,
            )
            choice = response.choices[0]
            message = choice.message
            calls = message.tool_calls or []

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    **({"tool_calls": [c.model_dump() for c in calls]} if calls else {}),
                }
            )

            if not calls:
                text = message.content or ""
                break

            if len(called) + len(calls) > max_tool_calls:
                return AgentResult(
                    text=message.content or text,
                    tool_calls=called,
                    failed_calls=failed,
                    hit_call_ceiling=True,
                )

            for call in calls:
                called.append(call.function.name)
                try:
                    args = json.loads(call.function.arguments or "{}")
                    result = await session.call_tool(call.function.name, args)
                    payload = _result_text(result)
                except Exception as exc:
                    payload = f"ERROR calling {call.function.name}: {exc}"
                if looks_like_tool_error(payload):
                    failed.append(call.function.name)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": payload[:MAX_RESULT_CHARS],
                    }
                )

        return AgentResult(
            text=text, tool_calls=called, failed_calls=failed, hit_call_ceiling=False
        )
