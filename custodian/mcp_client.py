"""Connection to the DataHub MCP server (`mcp-server-datahub`).

The server is launched as a subprocess over stdio via `uvx`. Everything
Custodian reads from and writes to DataHub goes through here, with one
deliberate exception: `custodian approve` emits the `deprecation` aspect
through the DataHub Python SDK, because the MCP server does not expose it.
"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import CONFIG, ConfigError

# Tools the agent is expected to have. If the mutation half is missing, the
# server was started without TOOLS_IS_MUTATION_ENABLED and every write will
# silently no-op — which is far more confusing to debug than an early failure.
READ_TOOLS = {
    "search",
    "get_entities",
    "get_lineage",
    "get_lineage_paths_between",
    "get_dataset_queries",
    "list_schema_fields",
}
WRITE_TOOLS = {"add_tags", "add_structured_properties", "save_document"}


LOG_PATH = Path(tempfile.gettempdir()) / "custodian-mcp.log"


@contextlib.asynccontextmanager
async def datahub_session() -> AsyncIterator[ClientSession]:
    """Yield an initialized MCP session against the DataHub server."""
    CONFIG.require_datahub()

    params = StdioServerParameters(
        command="uvx",
        args=["mcp-server-datahub@latest"],
        env={
            "DATAHUB_GMS_URL": CONFIG.gms_url,
            "DATAHUB_GMS_TOKEN": CONFIG.gms_token,
            "TOOLS_IS_MUTATION_ENABLED": "true",
        },
    )

    # The server calls logging.basicConfig(level=INFO) at import and dumps full
    # GraphQL queries to stderr, which would bury Custodian's own output. Send
    # it to a file — it stays available for debugging without the noise.
    with LOG_PATH.open("w") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def assert_tools_available(session: ClientSession) -> set[str]:
    """Fail loudly if the server came up without its mutation tools.

    Returns the full set of registered tool names.
    """
    available = {tool.name for tool in (await session.list_tools()).tools}

    missing_writes = WRITE_TOOLS - available
    if missing_writes:
        raise ConfigError(
            f"MCP server is missing mutation tools: {sorted(missing_writes)}.\n"
            "Custodian cannot write findings back to the graph. Check that:\n"
            "  1. TOOLS_IS_MUTATION_ENABLED=true is set\n"
            "  2. DataHub is version 1.4.0 or newer (older servers do not "
            "register these tools at all)\n"
            f"Registered tools were: {sorted(available)}"
        )

    missing_reads = READ_TOOLS - available
    if missing_reads:
        raise ConfigError(
            f"MCP server is missing expected read tools: {sorted(missing_reads)}."
        )

    return available
