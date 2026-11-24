"""Tests for StdioProxy and HttpProxy."""

import asyncio
import json
import sys

import aiohttp

from chaosmcp.config import FaultRule
from chaosmcp.demo import start_http_server
from chaosmcp.faults import FaultEngine
from chaosmcp.proxy import HttpProxy, StdioProxy


def test_http_proxy_forwards_and_injects_faults() -> None:
    async def _run() -> None:
        srv_runner, target_url = await start_http_server()
        engine = FaultEngine([
            FaultRule.from_dict({"type": "prompt_injection", "tool": "get_weather", "injection": "\n[INJECTED]"})
        ])
        proxy = HttpProxy(target_url=target_url, fault_engine=engine, port=0)
        proxy_url = await proxy.start()

        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_weather", "arguments": {"city": "Berlin"}},
                }
                async with session.post(f"{proxy_url}/mcp", json=payload) as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    text = data["result"]["content"][0]["text"]
                    assert "Berlin" in text
                    assert "[INJECTED]" in text
        finally:
            await proxy.stop()
            await srv_runner.cleanup()

    asyncio.run(_run())


def test_stdio_proxy_passthrough_and_truncate() -> None:
    async def _run() -> None:
        engine = FaultEngine([
            FaultRule.from_dict({"type": "truncate", "tool": "get_weather", "keep_ratio": 0.3})
        ])
        proxy = StdioProxy(
            server_command=[sys.executable, "-m", "chaosmcp.demo", "--server"],
            fault_engine=engine,
        )
        proc = await proxy.start_server()
        assert proc.stdin is not None and proc.stdout is not None

        try:
            # Send initialize (no fault)
            req1 = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
            engine.record_request(req1)
            proc.stdin.write((json.dumps(req1) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            line1 = await proc.stdout.readline()
            resp1 = json.loads(line1.decode("utf-8"))
            action1 = await engine.apply_response_faults(resp1)
            assert action1.truncate_ratio is None
            assert "serverInfo" in action1.response_obj["result"]

            # Send get_weather (truncated)
            req2 = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_weather", "arguments": {"city": "Rome"}}}
            engine.record_request(req2)
            proc.stdin.write((json.dumps(req2) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            line2 = await proc.stdout.readline()
            resp2 = json.loads(line2.decode("utf-8"))
            action2 = await engine.apply_response_faults(resp2)
            assert action2.truncate_ratio == 0.3
        finally:
            await proxy.stop()

    asyncio.run(_run())
