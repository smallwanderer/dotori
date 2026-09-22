"""Expose Dotori's existing read APIs as an optional MCP server.

The adapter is deliberately outside the Django image. It carries the same
Bearer token and optional workspace header to the public CLI API, so it does
not create a second permission or result-storage implementation.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server import MCPServer


def _server_url() -> str:
    return os.environ.get("DOTORI_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")


def _headers() -> dict[str, str]:
    token = os.environ.get("DOTORI_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DOTORI_TOKEN is required")
    headers = {"Authorization": f"Bearer {token}"}
    workspace = os.environ.get("DOTORI_WORKSPACE", "").strip()
    if workspace:
        headers["X-Dotori-Workspace"] = workspace
    return headers


async def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    async with httpx.AsyncClient(
        base_url=_server_url(), headers=_headers(), timeout=30.0
    ) as client:
        response = await client.request(method, path, **kwargs)
    try:
        payload = response.json()
    except ValueError:
        payload = {"ok": False, "error": {"message": response.text}}
    if response.is_error:
        raise RuntimeError(payload.get("error", {}).get("message", response.text))
    return payload


mcp = MCPServer("Dotori read-only")


@mcp.tool()
async def server_status() -> dict[str, Any]:
    """Return server runtime and capability status."""
    return await _request("GET", "/api/cli/v1/status/")


@mcp.tool()
async def capabilities() -> dict[str, Any]:
    """Return registered, enabled, and available capabilities."""
    return await _request("GET", "/api/cli/v1/capabilities/")


@mcp.tool()
async def list_documents(
    query: str = "",
    parent_id: str = "",
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    """List files visible in the selected Dotori workspace."""
    params = {"q": query, "page": page, "limit": limit}
    if parent_id:
        params["parent_id"] = parent_id
    return await _request("GET", "/api/cli/v1/documents/", params=params)


@mcp.tool()
async def list_results(source_uid: str = "", limit: int = 50) -> dict[str, Any]:
    """List persisted processing results, optionally limited to one source file."""
    if source_uid:
        encoded_source_uid = quote(source_uid, safe="")
        return await _request("GET", f"/api/cli/v1/documents/{encoded_source_uid}/results/")
    return await _request("GET", "/api/cli/v1/results/", params={"limit": limit})


@mcp.tool()
async def read_result(result_id: str) -> dict[str, Any]:
    """Read one persisted result envelope, including its payload."""
    encoded_result_id = quote(result_id, safe="")
    return await _request("GET", f"/api/cli/v1/results/{encoded_result_id}/")


@mcp.tool()
async def search(
    query: str, top_k: int = 5, node_ids: list[str] | None = None
) -> dict[str, Any]:
    """Run semantic search when the server's embedding capability is available."""
    return await _request(
        "POST",
        "/api/cli/v1/search/",
        json={
            "mode": "basic",
            "query": query,
            "top_k": top_k,
            "node_ids": node_ids or [],
        },
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
