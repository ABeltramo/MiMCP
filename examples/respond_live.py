"""Watch the SSE event stream and answer tool calls live (human-in-the-loop).

For every tool call that blocks awaiting a response, this prints the call and
prompts you for a reply, then POSTs it back so the MCP client unblocks.

Usage:
    uv run python examples/respond_live.py [BASE_URL]
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=None) as client:
        print(f"watching {BASE_URL}/api/events ... (Ctrl-C to quit)")
        async with client.stream("GET", "/api/events") as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[len("data:") :].strip())
                if event["type"] != "tool_call_awaiting_response":
                    print(f"· {event['type']} {event.get('data', {})}")
                    continue

                data = event["data"]
                call_id = data["call_id"]
                print(
                    f"\n>>> tool '{data['tool']}' called "
                    f"(session {event['session_id']})"
                )
                print(f"    arguments: {data['arguments']}")
                text = await asyncio.to_thread(input, "    reply text> ")
                r = await client.post(
                    f"/api/calls/{call_id}/respond", json={"text": text}
                )
                print("    ->", r.status_code, r.json())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
