"""Stdio and HTTP MCP proxies with fault injection."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import aiohttp
from aiohttp import web

from chaosmcp.faults import FaultAction, FaultEngine


def _serialize_fault_action(action: FaultAction) -> tuple[bytes, bool]:
    """Serialize a FaultAction into wire bytes and whether it was truncated."""
    if action.raw_bytes is not None:
        payload = action.raw_bytes
    else:
        payload = json.dumps(action.response_obj, separators=(",", ":")).encode("utf-8")

    truncated = False
    if action.truncate_bytes is not None:
        keep = max(1, min(len(payload), action.truncate_bytes))
        payload = payload[:keep]
        truncated = True
    elif action.truncate_ratio is not None:
        keep = max(1, int(len(payload) * action.truncate_ratio))
        payload = payload[:keep]
        truncated = True

    return payload, truncated


class StdioProxy:
    """Bidirectional stdio MCP proxy that intercepts JSON-RPC messages and injects faults."""

    def __init__(
        self,
        server_command: list[str],
        fault_engine: FaultEngine,
        env: dict[str, str] | None = None,
    ) -> None:
        self.server_command = server_command
        self.fault_engine = fault_engine
        self.env = env or {}
        self._process: asyncio.subprocess.Process | None = None

    async def start_server(self) -> asyncio.subprocess.Process:
        merged_env = os.environ.copy()
        merged_env.update(self.env)
        self._process = await asyncio.create_subprocess_exec(
            *self.server_command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr,
            env=merged_env,
        )
        return self._process

    async def stop(self) -> None:
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

    async def run_streams(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter | Any,
    ) -> None:
        """Proxy between arbitrary async client streams and the upstream MCP stdio server."""
        proc = await self.start_server()
        assert proc.stdin is not None and proc.stdout is not None

        async def forward_client_to_server() -> None:
            try:
                while True:
                    line = await client_reader.readline()
                    if not line:
                        break
                    stripped = line.strip()
                    if stripped:
                        try:
                            msg = json.loads(stripped.decode("utf-8"))
                            if isinstance(msg, dict):
                                self.fault_engine.record_request(msg)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass
                    proc.stdin.write(line)
                    await proc.stdin.drain()
            finally:
                if not proc.stdin.is_closing():
                    proc.stdin.close()

        async def forward_server_to_client() -> None:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    msg = json.loads(stripped.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    client_writer.write(line)
                    await client_writer.drain()
                    continue

                if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                    action = await self.fault_engine.apply_response_faults(msg)
                    wire_bytes, truncated = _serialize_fault_action(action)
                    client_writer.write(wire_bytes + (b"" if truncated else b"\n"))
                    await client_writer.drain()
                    if truncated:
                        # Simulate abrupt stream cutoff
                        if hasattr(client_writer, "close"):
                            client_writer.close()
                        break
                else:
                    client_writer.write(line)
                    await client_writer.drain()

        try:
            await asyncio.gather(
                forward_client_to_server(),
                forward_server_to_client(),
            )
        finally:
            await self.stop()

    async def run_stdio(self) -> None:
        """Run proxy attached to current process stdin/stdout (cross-platform)."""
        proc = await self.start_server()
        assert proc.stdin is not None and proc.stdout is not None

        async def forward_client_to_server() -> None:
            try:
                while True:
                    line = await asyncio.to_thread(sys.stdin.buffer.readline)
                    if not line:
                        break
                    stripped = line.strip()
                    if stripped:
                        try:
                            msg = json.loads(stripped.decode("utf-8"))
                            if isinstance(msg, dict):
                                self.fault_engine.record_request(msg)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass
                    proc.stdin.write(line)
                    await proc.stdin.drain()
            finally:
                if not proc.stdin.is_closing():
                    proc.stdin.close()

        async def forward_server_to_client() -> None:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    msg = json.loads(stripped.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()
                    continue

                if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                    action = await self.fault_engine.apply_response_faults(msg)
                    wire_bytes, truncated = _serialize_fault_action(action)
                    sys.stdout.buffer.write(wire_bytes + (b"" if truncated else b"\n"))
                    sys.stdout.buffer.flush()
                    if truncated:
                        break
                else:
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()

        try:
            await asyncio.gather(
                forward_client_to_server(),
                forward_server_to_client(),
            )
        finally:
            await self.stop()


class HttpProxy:
    """HTTP / SSE MCP proxy that forwards requests to an upstream URL and injects faults."""

    def __init__(
        self,
        target_url: str,
        fault_engine: FaultEngine,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self.target_url = target_url
        self.fault_engine = fault_engine
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._client_session: aiohttp.ClientSession | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> str:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle_request)
        self._client_session = aiohttp.ClientSession()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        # Update port in case port=0 (ephemeral) was requested
        if self._site._server and self._site._server.sockets:
            self.port = self._site._server.sockets[0].getsockname()[1]
        return self.url

    async def stop(self) -> None:
        if self._client_session:
            await self._client_session.close()
            self._client_session = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_request(self, request: web.Request) -> web.StreamResponse:
        assert self._client_session is not None
        raw_body = await request.read()
        try:
            msg = json.loads(raw_body.decode("utf-8")) if raw_body else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            msg = None

        if isinstance(msg, dict) and "method" in msg:
            self.fault_engine.record_request(msg)

        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "transfer-encoding")
        }
        target = self.target_url.rstrip("/")
        if request.path_qs and request.path_qs != "/" and not target.endswith(request.path):
            target = f"{target}{request.path_qs}"

        async with self._client_session.request(
            method=request.method,
            url=target,
            headers=headers,
            data=raw_body,
        ) as upstream_resp:
            content_type = upstream_resp.headers.get("Content-Type", "")

            if "text/event-stream" in content_type:
                return await self._proxy_sse(request, upstream_resp)

            resp_bytes = await upstream_resp.read()
            try:
                resp_json = json.loads(resp_bytes.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return web.Response(
                    body=resp_bytes,
                    status=upstream_resp.status,
                    content_type=upstream_resp.content_type,
                )

            if isinstance(resp_json, dict) and "id" in resp_json:
                action = await self.fault_engine.apply_response_faults(resp_json)
                wire_bytes, truncated = _serialize_fault_action(action)
                status = action.http_status or upstream_resp.status
                if truncated:
                    stream_resp = web.StreamResponse(
                        status=status,
                        headers={"Content-Type": "application/json"},
                    )
                    await stream_resp.prepare(request)
                    await stream_resp.write(wire_bytes)
                    if request.transport is not None:
                        request.transport.close()
                    return stream_resp
                return web.Response(
                    body=wire_bytes,
                    status=status,
                    headers={"Content-Type": "application/json"},
                )

            return web.Response(
                body=resp_bytes,
                status=upstream_resp.status,
                content_type=upstream_resp.content_type,
            )

    async def _proxy_sse(
        self,
        request: web.Request,
        upstream_resp: aiohttp.ClientResponse,
    ) -> web.StreamResponse:
        stream_resp = web.StreamResponse(
            status=upstream_resp.status,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await stream_resp.prepare(request)

        while True:
            raw_line = await upstream_resp.content.readline()
            if not raw_line:
                break
            line_str = raw_line.decode("utf-8", errors="replace")
            if line_str.startswith("data:"):
                data_part = line_str[5:].strip()
                try:
                    msg = json.loads(data_part)
                except json.JSONDecodeError:
                    await stream_resp.write(raw_line)
                    continue
                if isinstance(msg, dict) and "id" in msg:
                    action = await self.fault_engine.apply_response_faults(msg)
                    wire_bytes, truncated = _serialize_fault_action(action)
                    await stream_resp.write(b"data: " + wire_bytes + (b"" if truncated else b"\n"))
                    if truncated:
                        if request.transport is not None:
                            request.transport.close()
                        return stream_resp
                    continue
            await stream_resp.write(raw_line)

        await stream_resp.write_eof()
        return stream_resp
