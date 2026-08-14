"""Define the global default tool set via the REST control plane.

Usage:
    uv run python examples/set_tools.py [BASE_URL]
"""

from __future__ import annotations

import sys

import httpx

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

TOOLS = [
    {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
    {
        "name": "search",
        "description": "Search a knowledge base.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]


def main() -> None:
    r = httpx.put(f"{BASE_URL}/api/tools", json={"tools": TOOLS}, timeout=10)
    r.raise_for_status()
    print("default tools set:", r.json())
    print("connected sessions using them will receive tools/list_changed.")


if __name__ == "__main__":
    main()
