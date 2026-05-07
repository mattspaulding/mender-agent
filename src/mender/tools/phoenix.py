"""Phoenix MCP toolset — the partner integration (component C2).

We launch `@arizeai/phoenix-mcp` over stdio via npx and expose its tools
(find_traces, get_eval_scores, list_prompts, etc.) to the ADK agent.

This module returns `None` when `PHOENIX_API_KEY` isn't configured so a
developer can still run `mender doctor` and `mender heartbeat --offline`
without Phoenix.
"""

from __future__ import annotations

import logging
import os
import shutil

_log = logging.getLogger(__name__)


def build_phoenix_toolset():
    """Build the ADK MCPToolset wrapping @arizeai/phoenix-mcp.

    Returns the toolset, or None if Phoenix isn't configured. Caller is
    expected to check and degrade gracefully.
    """
    api_key = os.environ.get("PHOENIX_API_KEY", "").strip()
    base_url = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "https://app.phoenix.arize.com").rstrip("/")

    if not api_key:
        _log.warning("PHOENIX_API_KEY not set; skipping Phoenix MCP toolset.")
        return None

    npx = shutil.which("npx")
    if not npx:
        _log.error("npx not found on PATH; install Node 18+ to use the Phoenix MCP server.")
        return None

    from google.adk.tools.mcp_tool.mcp_session_manager import (
        StdioConnectionParams,
        StdioServerParameters,
    )
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

    # Generous timeout: first invocation may need to npm-install the package.
    # We pre-warm the cache in deploy, but don't trust that locally.
    timeout = float(os.environ.get("PHOENIX_MCP_TIMEOUT", "60"))

    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=npx,
                args=[
                    "-y",
                    "@arizeai/phoenix-mcp@latest",
                    "--apiKey",
                    api_key,
                    "--baseUrl",
                    base_url,
                ],
            ),
            timeout=timeout,
        ),
    )
