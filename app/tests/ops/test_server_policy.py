import json

import pytest

from capabilities.config import write_capability_config
from capabilities.registry import DOCUMENT_EXTRACT
from document_ai.services.server_policy import build_server_policy_payload


pytestmark = pytest.mark.unit


def test_server_policy_uses_deployed_extract_mode_and_hides_embedding_runtime(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "capabilities.json"
    config = write_capability_config([DOCUMENT_EXTRACT], path=config_path)
    deployment_path = tmp_path / "deployment_scopes" / "production" / "deployment_plan.json"
    deployment_path.parent.mkdir(parents=True)
    deployment_path.write_text(
        json.dumps(
            {
                "mode": "extract",
                "core_services": ["db", "redis", "app", "dotori-orchestrator"],
                "workers": [{"compose_service": "parser-executor", "enabled": True}],
                "enabled_capabilities": [DOCUMENT_EXTRACT],
                "capability_revision": config.revision,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOTORI_CAPABILITIES_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("DOTORI_OPERATION_MODE", "search")
    monkeypatch.setattr(
        "document_ai.services.server_policy.load_embedding_runtime",
        lambda: pytest.fail("disabled semantic search must not load embedding runtime"),
    )
    monkeypatch.setattr(
        "document_ai.services.server_policy.build_rag_summary",
        lambda **_kwargs: {"configured": False, "available": False},
    )

    payload = build_server_policy_payload()

    assert payload["operation_mode"] == "extract"
    assert payload["embedding"] == {
        "enabled": False,
        "configured": False,
        "available": False,
        "status": "disabled",
        "reason_code": "CAPABILITY_DISABLED",
        "model": "",
        "provider": "",
        "dimension": 0,
        "sparse_enabled": False,
        "distance_strategy": "",
    }
    assert payload["capabilities"][DOCUMENT_EXTRACT]["available"] is True
