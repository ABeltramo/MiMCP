"""Runtime configuration for the MiMCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Process-wide settings, sourced from environment variables.

    All values have dev-friendly defaults so `uv run mimcp` just works.
    """

    host: str = "127.0.0.1"
    port: int = 8000

    # Per-call timeout for requests forwarded to a session's upstream server.
    upstream_timeout_s: float = 60.0

    # How long tools/list and relayed calls wait for the upstream connection
    # to become ready before failing.
    upstream_connect_timeout_s: float = 15.0

    # Recorded calls kept per session (most recent); older ones are dropped.
    max_recorded_calls: int = 1000

    # Optional bearer token guarding the /api/* control plane. Empty = no auth.
    api_token: str = ""

    # Default MCP `instructions` returned in the initialize handshake when a
    # session does not carry its own override (see POST /api/sessions).
    instructions: str = "MiMCP: a man-in-the-middle MCP proxy."

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("MIMCP_HOST", cls.host),
            port=int(os.getenv("MIMCP_PORT", str(cls.port))),
            upstream_timeout_s=float(
                os.getenv("MIMCP_UPSTREAM_TIMEOUT_S", str(cls.upstream_timeout_s))
            ),
            upstream_connect_timeout_s=float(
                os.getenv(
                    "MIMCP_UPSTREAM_CONNECT_TIMEOUT_S",
                    str(cls.upstream_connect_timeout_s),
                )
            ),
            max_recorded_calls=int(
                os.getenv("MIMCP_MAX_RECORDED_CALLS", str(cls.max_recorded_calls))
            ),
            api_token=os.getenv("MIMCP_API_TOKEN", cls.api_token),
            instructions=os.getenv("MIMCP_INSTRUCTIONS", cls.instructions),
        )
