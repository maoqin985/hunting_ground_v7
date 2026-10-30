"""Small Streamable HTTP client for the Tushare MCP service.

The client deliberately keeps the token out of configuration and log messages.
It implements only the JSON-RPC operations needed by the data layer, while
leaving the response rows as ordinary Python dictionaries for the fetcher.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

logger = logging.getLogger(__name__)


class TushareMCPError(RuntimeError):
    """An MCP transport or tool invocation error."""


def _sse_json(body: str) -> Dict[str, Any]:
    """Extract the first JSON-RPC message from an SSE or JSON response."""
    # Streamable HTTP servers may return a normal JSON body for errors and an
    # SSE body for successful calls.  Ignore comments/event names and accept
    # the first valid ``data:`` payload.
    candidates: List[str] = []
    for line in body.replace("\r\n", "\n").splitlines():
        if line.startswith("data:"):
            candidates.append(line[5:].lstrip())
    if not candidates:
        candidates = [body.strip()]
    for candidate in candidates:
        if not candidate or candidate == "[DONE]":
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise TushareMCPError("Tushare MCP returned an invalid JSON/SSE response")


def _content_value(result: Dict[str, Any]) -> Any:
    """Decode the MCP tool result into rows or raise a tool error."""
    if result.get("isError"):
        content = result.get("content") or []
        message = " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
        raise TushareMCPError(message or "Tushare MCP tool returned an error")

    # Some MCP servers use structuredContent, while Tushare currently returns
    # a JSON document in a text content block.
    if "structuredContent" in result:
        value = result["structuredContent"]
        if isinstance(value, dict) and "data" in value:
            return value["data"]
        return value

    content = result.get("content") or []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            continue
        text = item.get("text", "")
        try:
            return json.loads(text)
        except (TypeError, json.JSONDecodeError):
            # Tool errors occasionally arrive as plain text even when the
            # JSON-RPC result does not set isError.
            if text:
                raise TushareMCPError(str(text))
    return []


class TushareMCPClient:
    """Async JSON-RPC client for Tushare's Streamable HTTP MCP endpoint."""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        timeout: float = 30.0,
        retries: int = 2,
    ):
        self.base_url = (base_url or "").strip()
        self.token = (token or os.getenv("TUSHARE_TOKEN", "")).strip()
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_id: Optional[str] = None
        self._request_id = 0
        self._initialized = False
        self._init_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _url(self) -> str:
        """Append the token without ever logging the resulting URL."""
        parts = urlsplit(self.base_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["token"] = self.token
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
        return self._session

    async def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.configured:
            raise TushareMCPError("Tushare MCP is not configured (set TUSHARE_TOKEN and mcp_url)")
        self._request_id += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params or {}}
        headers: Dict[str, str] = {}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                session = await self._get_session()
                async with session.post(self._url(), json=payload, headers=headers) as response:
                    body = await response.text()
                    if response.status < 200 or response.status >= 300:
                        raise TushareMCPError(f"Tushare MCP HTTP {response.status}")
                    if response.headers.get("Mcp-Session-Id"):
                        self._session_id = response.headers["Mcp-Session-Id"]
                    message = _sse_json(body)
                    if "error" in message:
                        error = message["error"]
                        raise TushareMCPError(str(error.get("message", error)))
                    return message
            except (aiohttp.ClientError, asyncio.TimeoutError, TushareMCPError) as exc:
                last_error = exc
                if attempt < self.retries:
                    await asyncio.sleep(0.25 * (attempt + 1))
        raise TushareMCPError(str(last_error or "Tushare MCP request failed"))

    async def initialize(self) -> Dict[str, Any]:
        if self._initialized:
            return {}
        async with self._init_lock:
            if self._initialized:
                return {}
            result = await self._rpc(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "hunting-ground", "version": "7.0"},
                },
            )
            self._initialized = True
            return result.get("result", {})

    async def list_tools(self) -> List[Dict[str, Any]]:
        await self.initialize()
        result = await self._rpc("tools/list", {})
        return (result.get("result") or {}).get("tools", [])

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        await self.initialize()
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        return _content_value(result.get("result") or {})

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
