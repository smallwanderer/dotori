import asyncio
import pytest
from unittest.mock import AsyncMock

from dotori_mcp import server


def test_headers_require_token(monkeypatch):
    monkeypatch.delenv("DOTORI_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="DOTORI_TOKEN"):
        server._headers()


def test_headers_include_optional_workspace(monkeypatch):
    monkeypatch.setenv("DOTORI_TOKEN", "dtr_cli_test")
    monkeypatch.setenv("DOTORI_WORKSPACE", "workspace-uid")
    assert server._headers() == {
        "Authorization": "Bearer dtr_cli_test",
        "X-Dotori-Workspace": "workspace-uid",
    }


def test_result_path_segments_are_url_encoded(monkeypatch):
    request = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(server, "_request", request)

    asyncio.run(server.list_results(source_uid="source/with space"))
    request.assert_awaited_once_with(
        "GET", "/api/cli/v1/documents/source%2Fwith%20space/results/"
    )

    request.reset_mock()
    asyncio.run(server.read_result("custom/result id"))
    request.assert_awaited_once_with(
        "GET", "/api/cli/v1/results/custom%2Fresult%20id/"
    )
