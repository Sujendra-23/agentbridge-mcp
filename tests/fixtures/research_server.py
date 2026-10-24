"""Local search double over real MCP stdio, plus startup fault modes.

Never represents a live Tavily API call. stdout belongs exclusively to JSON-RPC.
"""

import asyncio
import os
import sys

from mcp.server.fastmcp import FastMCP


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "crash":
        sys.exit(17)
    if len(sys.argv) > 1 and sys.argv[1] == "hang":
        asyncio.run(asyncio.Event().wait())
    server = FastMCP("deterministic-incident-research")

    @server.tool()
    def search_incidents(query: str) -> str:
        """Find the incident runbook in a controlled local search corpus."""
        if "queue" not in query.lower():
            raise ValueError("Fixture corpus only contains queue incidents")
        return os.environ["FIXTURE_RUNBOOK_URL"]

    server.run(transport="stdio")
