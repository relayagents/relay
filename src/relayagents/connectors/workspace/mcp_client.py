"""``OfficeSuite`` via the ``workspace-mcp`` container (taylorwilsdon/google_workspace_mcp).

Relay is an MCP *client* here. Per-user Google OAuth is handled entirely inside workspace-mcp
(multi-user mode); Relay passes the user's identity and never sees Google tokens. Tool names
follow that server; adjust ``TOOLS`` if you pin a different version.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

TOOLS = {"search_docs": "search_docs", "get_doc": "get_doc_content", "events": "get_events"}


class WorkspaceMCP:
    def __init__(self, url: str) -> None:
        self.url = url

    @asynccontextmanager
    async def _session(self, user_id: str):  # type: ignore[no-untyped-def]
        headers = {"X-Relay-User": user_id}
        async with (
            httpx.AsyncClient(headers=headers, timeout=60) as http,
            streamable_http_client(self.url, http_client=http) as (read, write, *_),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session

    async def _call(self, user_id: str, tool: str, args: dict[str, Any]) -> Any:
        async with self._session(user_id) as s:
            result = await s.call_tool(tool, {**args, "user_google_email": user_id})
            content = getattr(result, "structured_content", None) or getattr(
                result, "content", None
            )
            return content

    async def search_documents(
        self, user_id: str, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        out = await self._call(user_id, TOOLS["search_docs"], {"query": query, "page_size": limit})
        return out if isinstance(out, list) else [{"result": out}]

    async def read_document(self, user_id: str, doc_ref: str) -> str:
        out = await self._call(user_id, TOOLS["get_doc"], {"document_id": doc_ref})
        return out if isinstance(out, str) else str(out)

    async def upcoming_meetings(self, user_id: str, *, hours: int = 24) -> list[dict[str, Any]]:
        out = await self._call(user_id, TOOLS["events"], {"hours": hours})
        return out if isinstance(out, list) else [{"result": out}]

    async def ping(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as http:
                r = await http.get(self.url.rsplit("/mcp", 1)[0] + "/health")
                return r.status_code < 500
        except Exception:
            return False
