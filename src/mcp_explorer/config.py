"""Runtime configuration for the mcp-explorer server."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Process-wide settings, sourced from environment variables.

    All values have dev-friendly defaults so `uv run mcp-explorer` just works.
    """

    host: str = "127.0.0.1"
    port: int = 8000

    # How long a live (human-in-the-loop) tool call waits for a REST response
    # before it gives up and returns an error result to the MCP client.
    default_timeout_s: float = 300.0

    # Default interception mode applied to newly seen sessions.
    #   "manual" -> calls block until a preset or a live /respond resolves them
    #   "auto"   -> calls with no preset return a canned "no preset" result fast
    default_mode: str = "manual"

    # Optional bearer token guarding the /api/* control plane. Empty = no auth.
    api_token: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("MCP_EXPLORER_HOST", cls.host),
            port=int(os.getenv("MCP_EXPLORER_PORT", str(cls.port))),
            default_timeout_s=float(
                os.getenv("MCP_EXPLORER_TIMEOUT_S", str(cls.default_timeout_s))
            ),
            default_mode=os.getenv("MCP_EXPLORER_MODE", cls.default_mode),
            api_token=os.getenv("MCP_EXPLORER_API_TOKEN", cls.api_token),
        )
