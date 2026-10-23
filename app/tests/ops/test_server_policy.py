import pytest

from capabilities.deployment import clear_deployment_snapshot_cache
from capabilities.modules import DOCUMENTS_MODULE
from capabilities.registry import DOCUMENT_EXTRACT
from documents.services.server_policy import build_server_policy_payload
from installation.deployment import build_module_deployment_plan, write_deployment_plan


pytestmark = pytest.mark.unit


def test_server_policy_uses_deployed_extract_mode_and_hides_embedding_runtime(
    monkeypatch, tmp_path
):
    plan = build_module_deployment_plan(
        [DOCUMENTS_MODULE], disabled_provider_roles=("embedding", "llm")
    )
    deployment_path = write_deployment_plan(plan, repo_root=tmp_path)
    monkeypatch.setenv("DOTORI_DEPLOYMENT_PLAN_PATH", str(deployment_path))
    clear_deployment_snapshot_cache()
    monkeypatch.setenv("DOTORI_OPERATION_MODE", "search")
    monkeypatch.setattr(
        "documents.services.server_policy.load_embedding_runtime",
        lambda: pytest.fail("disabled semantic search must not load embedding runtime"),
    )
    monkeypatch.setattr(
        "documents.services.server_policy.build_rag_summary",
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
