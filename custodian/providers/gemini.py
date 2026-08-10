"""Gemini provider.

Custodian drives the tool-use loop itself rather than using google-genai's
automatic function calling with an MCP session. That is a deliberate choice,
not a workaround for its own sake:

  * google-genai deep-copies the request config before sending, which throws
    on a live MCP ClientSession (it holds asyncio Tasks).
  * Its MCP bridge reads `tool.inputSchema`, but mcp >= 2.0 renamed that field
    to `input_schema`, so the built-in conversion raises AttributeError.

Both are version-coupling bugs between two fast-moving SDKs. Owning ~80 lines
of loop removes the coupling entirely, means a judge cloning this repo does not
have to land on an exact pair of SDK versions, and gives Custodian an exact
count of the tool calls it made — which is what lets it tell you honestly when
an investigation was truncated.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from typing import Any

from google import genai
from google.genai import types
from mcp import ClientSession

from ..config import CONFIG
from .base import AgentResult

# Gemini accepts a subset of JSON Schema. DataHub's tool schemas carry
# vocabulary it rejects outright ($schema, $defs, additionalProperties, ...),
# so anything not on this list is dropped before the call.
_ALLOWED_SCHEMA_KEYS = {
    "type",
    "format",
    "description",
    "nullable",
    "enum",
    "items",
    "properties",
    "required",
    "minItems",
    "maxItems",
}


def _clean_schema(schema: Any) -> Any:
    """Recursively reduce a JSON Schema to what Gemini will accept."""
    if not isinstance(schema, dict):
        return schema

    # A union collapses to its first concrete branch; Gemini has no anyOf.
    for union in ("anyOf", "oneOf", "allOf"):
        if union in schema:
            branches = [b for b in schema[union] if isinstance(b, dict)]
            concrete = next(
                (b for b in branches if b.get("type") not in (None, "null")),
                branches[0] if branches else {"type": "string"},
            )
            merged = {k: v for k, v in schema.items() if k not in (union, "$ref")}
            merged.update(concrete)
            return _clean_schema(merged)

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _ALLOWED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: _clean_schema(v) for k, v in value.items()}
        elif key == "items":
            out[key] = _clean_schema(value)
        else:
            out[key] = value

    if "type" not in out and "properties" in out:
        out["type"] = "object"
    # Gemini rejects an OBJECT with an empty properties map.
    if out.get("type") == "object" and not out.get("properties"):
        out.pop("required", None)
        out["properties"] = {"_": {"type": "string", "description": "unused"}}
    return out


async def _declarations(
    session: ClientSession, allow: set[str] | None
) -> list[types.FunctionDeclaration]:
    """Build declarations, optionally narrowed to the tools a command needs.

    Every request resends the full tool set, and DataHub's schemas are large
    (the `search` filter DSL alone is substantial). Sending all 20 on every
    turn is a large fixed cost against a per-minute input-token quota, so each
    command declares only what it actually uses.
    """
    tools = (await session.list_tools()).tools
    declarations = []
    for tool in tools:
        if allow is not None and tool.name not in allow:
            continue
        # mcp < 2.0 exposed this as `inputSchema`; >= 2.0 uses `input_schema`.
        raw = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", {})
        declarations.append(
            types.FunctionDeclaration(
                name=tool.name,
                description=(tool.description or "")[:600],
                parameters=_clean_schema(raw or {"type": "object", "properties": {}}),
            )
        )
    return declarations


# A single lineage call on a well-connected asset returns far more text than
# the model needs to reason about it, and it is resent on every later turn.
MAX_RESULT_CHARS = 2500

# Gemini's free tier meters *input* tokens per minute. An agentic loop resends
# its whole history every turn, so spend grows quadratically with turn count:
# a 10-turn investigation can push a quarter-million input tokens through in
# well under a minute. Retrying into that limit makes it worse, because the
# retry re-sends the same oversized request and re-burns the budget.
#
# So Custodian paces itself against the real numbers the API reports back,
# rather than waiting to be told no.
FREE_TIER_INPUT_TPM = 250_000
PACING_HEADROOM = 0.75


class _TokenPacer:
    """Keep a rolling 60s input-token spend under the free-tier ceiling."""

    def __init__(self, limit: int = FREE_TIER_INPUT_TPM) -> None:
        self._budget = int(limit * PACING_HEADROOM)
        self._spent: list[tuple[float, int]] = []

    def record(self, tokens: int) -> None:
        self._spent.append((time.monotonic(), tokens))

    async def wait_for(self, projected: int) -> None:
        """Sleep just long enough that `projected` more tokens stays in budget."""
        while True:
            now = time.monotonic()
            self._spent = [(t, n) for t, n in self._spent if now - t < 60.0]
            used = sum(n for _, n in self._spent)
            if used + projected <= self._budget or not self._spent:
                return
            oldest = self._spent[0][0]
            sleep_for = max(1.0, 60.0 - (now - oldest)) + 0.5
            print(
                f"  [pacing — {used:,} input tokens in the last minute,"
                f" waiting {sleep_for:.0f}s]",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.sleep(sleep_for)


def looks_like_tool_error(text: str) -> bool:
    """True if a tool result is really a failure.

    The DataHub MCP server reports failures as ordinary result text with
    `isError` unset, so a model reads "Error calling tool ..." and can still
    decide the step succeeded. Custodian checks the text itself.
    """
    head = text.lstrip()[:80].lower()
    return head.startswith("error calling tool") or head.startswith("error:")


# Tools that only read. Their results are safe to serve twice within a single
# run, because nothing the agent does can change the answer mid-investigation.
# Write tools are deliberately absent: replaying a cached "ok" would hide a
# second write that never happened.
READ_ONLY_TOOLS = frozenset(
    {
        "search",
        "get_entities",
        "get_lineage",
        "get_lineage_paths_between",
        "get_dataset_queries",
        "list_schema_fields",
        "get_documents",
    }
)

# Warn the agent while it still has room to act on the warning. Being told at
# the ceiling is useless — it has already been cut off mid-thought.
BUDGET_WARNING_AT = 0.75


def cache_key(name: str, args: dict[str, Any]) -> str | None:
    """Stable key for a repeatable lookup, or None if it must not be cached."""
    if name not in READ_ONLY_TOOLS:
        return None
    try:
        return name + "|" + json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None


def budget_note(used: int, ceiling: int) -> str | None:
    """A nudge to start writing up, issued once, while there is still budget."""
    remaining = ceiling - used
    return (
        f"[Custodian: {used} of {ceiling} tool calls used, {remaining} left. "
        "Stop gathering and write your final answer now, including the "
        "required write-back calls, or the run will be cut off before you "
        "can record anything.]"
    )


def _result_text(result: Any) -> str:
    """Flatten an MCP tool result, capped so history cannot balloon."""
    if getattr(result, "isError", False):
        return f"ERROR: {result}"[:MAX_RESULT_CHARS]
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    joined = "\n".join(parts) if parts else json.dumps(str(result))
    if len(joined) > MAX_RESULT_CHARS:
        return (
            joined[:MAX_RESULT_CHARS]
            + f"\n... [truncated {len(joined) - MAX_RESULT_CHARS} chars —"
            " narrow the query if you need the rest]"
        )
    return joined


def _retry_delay(exc: Exception) -> float | None:
    """Seconds to wait per the API's own RetryInfo, if this is a 429."""
    message = str(exc)
    if "RESOURCE_EXHAUSTED" not in message and "429" not in message:
        return None
    match = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", message)
    if match:
        return min(float(match.group(1)) + 1.0, 65.0)
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", message)
    return min(float(match.group(1)) + 1.0, 65.0) if match else 30.0


class GeminiProvider:
    name = "gemini"

    def __init__(self) -> None:
        CONFIG.require_provider()
        self._client = genai.Client(api_key=CONFIG.gemini_api_key)
        self._pacer = _TokenPacer()

    async def _generate(self, **kwargs: Any) -> types.GenerateContentResponse:
        """Call the model, waiting out free-tier rate limits rather than dying.

        The free tier meters input tokens per minute, and an agentic loop
        resends its history every turn, so a long investigation will brush the
        limit. The API tells us how long to wait; honour it.
        """
        for attempt in range(4):
            try:
                response = await self._client.aio.models.generate_content(**kwargs)
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
                continue

            usage = getattr(response, "usage_metadata", None)
            self._pacer.record(getattr(usage, "prompt_token_count", 0) or 0)
            return response
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
        declarations = await _declarations(session, allow_tools)
        tool = types.Tool(function_declarations=declarations)
        # Tool schemas are resent on every turn — a fixed cost worth counting.
        tool_tokens = sum(
            len(d.description or "") + len(str(d.parameters)) for d in declarations
        ) // 4

        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part(text=prompt)])
        ]

        called: list[str] = []
        failed: list[str] = []
        text = ""
        cache: dict[str, str] = {}
        warned = False

        # Rough size of what this turn will send, so the pacer can wait before
        # spending rather than after being refused. ~4 chars per token.
        def projected_tokens() -> int:
            body = sum(
                len(part.text or "") + len(str(getattr(part, "function_response", "")))
                for content in contents
                for part in (content.parts or [])
            )
            return (body + len(system)) // 4 + tool_tokens

        while True:
            await self._pacer.wait_for(projected_tokens())
            response = await self._generate(
                model=CONFIG.gemini_model,
                contents=contents,
                config={
                    "system_instruction": system,
                    "temperature": 0,
                    "tools": [tool],
                },
            )

            candidate = (response.candidates or [None])[0]
            if candidate is None or candidate.content is None:
                break

            contents.append(candidate.content)
            calls = [
                part.function_call
                for part in (candidate.content.parts or [])
                if getattr(part, "function_call", None)
            ]

            if not calls:
                text = response.text or ""
                break

            if len(called) + len(calls) > max_tool_calls:
                # Stop rather than silently returning a partial investigation.
                return AgentResult(
                    text=response.text or text,
                    tool_calls=called,
                    failed_calls=failed,
                    hit_call_ceiling=True,
                )

            responses = []
            for call in calls:
                called.append(call.name)
                args = dict(call.args or {})
                key = cache_key(call.name, args)
                if key is not None and key in cache:
                    # Repeat lookup: serve it back rather than paying a round
                    # trip. Still counted as a call — the model spent a turn on
                    # it, and hiding that would misreport the budget.
                    responses.append(
                        types.Part.from_function_response(
                            name=call.name, response={"result": cache[key]}
                        )
                    )
                    continue
                try:
                    result = await session.call_tool(call.name, args)
                    payload = _result_text(result)
                except Exception as exc:  # surface tool failure to the model
                    payload = f"ERROR calling {call.name}: {exc}"
                if looks_like_tool_error(payload):
                    failed.append(call.name)
                elif key is not None:
                    cache[key] = payload  # never cache a failure
                responses.append(
                    types.Part.from_function_response(
                        name=call.name, response={"result": payload}
                    )
                )

            if not warned and len(called) >= int(max_tool_calls * BUDGET_WARNING_AT):
                warned = True
                responses.append(
                    types.Part(text=budget_note(len(called), max_tool_calls))
                )

            contents.append(types.Content(role="user", parts=responses))

        return AgentResult(
            text=text, tool_calls=called, failed_calls=failed, hit_call_ceiling=False
        )
