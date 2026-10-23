import json

import pytest

from capabilities.deployment import (
    DeploymentSnapshotError,
    clear_deployment_snapshot_cache,
    load_deployment_snapshot,
)
from capabilities.modules import DOCUMENTS_MODULE
from capabilities.registry import DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH
from documents.services.capability_admission import CapabilityAdmissionError, require_capability
from installation.deployment import build_module_deployment_plan, write_deployment_plan

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clear_snapshot_cache():
    clear_deployment_snapshot_cache()
    yield
    clear_deployment_snapshot_cache()


def _write_plan(tmp_path, monkeypatch, *, disabled_roles=()):
    plan = build_module_deployment_plan(
        [DOCUMENTS_MODULE], disabled_provider_roles=disabled_roles
    )
    path = write_deployment_plan(plan, repo_root=tmp_path)
    monkeypatch.setenv("DOTORI_DEPLOYMENT_PLAN_PATH", str(path))
    clear_deployment_snapshot_cache()
    return plan, path


def test_missing_deployment_plan_fails_closed(tmp_path):
    with pytest.raises(DeploymentSnapshotError, match="does not exist"):
        load_deployment_snapshot(path=tmp_path / "missing.json")


def test_single_snapshot_exposes_installed_and_enabled_state(tmp_path, monkeypatch):
    _plan, path = _write_plan(
        tmp_path, monkeypatch, disabled_roles=("embedding", "llm")
    )

    snapshot = load_deployment_snapshot(path=path)

    assert snapshot.is_installed(DOCUMENT_SEMANTIC_SEARCH) is True
    assert snapshot.is_enabled(DOCUMENT_SEMANTIC_SEARCH) is False
    assert snapshot.is_enabled(DOCUMENT_EXTRACT) is True


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda plan: plan.update(schema_version=1), "schema_version"),
        (lambda plan: plan.update(enabled_capabilities="documents.extract"), "list of strings"),
        (lambda plan: plan.update(capability_revision="cap-wrong"), "revision"),
        (lambda plan: plan.update(installed_capabilities=[]), "subset"),
    ],
)
def test_invalid_deployment_snapshot_is_rejected(
    tmp_path, monkeypatch, mutate, message
):
    plan, path = _write_plan(tmp_path, monkeypatch)
    mutate(plan)
    path.write_text(json.dumps(plan), encoding="utf-8")
    clear_deployment_snapshot_cache()

    with pytest.raises(DeploymentSnapshotError, match=message):
        load_deployment_snapshot()


def test_admission_uses_only_the_active_deployment_snapshot(tmp_path, monkeypatch):
    _plan, _path = _write_plan(
        tmp_path, monkeypatch, disabled_roles=("embedding", "llm")
    )

    snapshot = require_capability(DOCUMENT_EXTRACT)
    assert snapshot.is_enabled(DOCUMENT_EXTRACT)

    with pytest.raises(CapabilityAdmissionError) as disabled:
        require_capability(DOCUMENT_SEMANTIC_SEARCH)
    assert disabled.value.code == "CAPABILITY_DISABLED"


def test_unknown_capability_is_not_installed(tmp_path, monkeypatch):
    _write_plan(tmp_path, monkeypatch)

    with pytest.raises(CapabilityAdmissionError) as missing:
        require_capability("audio.unknown")
    assert missing.value.code == "CAPABILITY_NOT_INSTALLED"
