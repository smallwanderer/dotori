import json
import pytest

from capabilities.config import CapabilityConfigError, LEGACY_DOCUMENT_CAPABILITIES, load_capability_config, write_capability_config
from capabilities.registry import DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH
from document_ai.services.capability_admission import CapabilityAdmissionError, require_capability

pytestmark = pytest.mark.unit


def test_missing_config_preserves_legacy_documents_selection(tmp_path):
    config = load_capability_config(path=tmp_path / "missing.json")
    assert config.enabled_capabilities == LEGACY_DOCUMENT_CAPABILITIES
    assert config.source == "legacy_missing_config"


def test_explicit_empty_selection_is_core_only(tmp_path):
    path = tmp_path / "capabilities.json"
    written = write_capability_config([], path=path)
    assert load_capability_config(path=path) == written
    assert written.enabled_capabilities == ()


def test_semantic_search_requires_document_extract(tmp_path):
    with pytest.raises(CapabilityConfigError, match="requires documents.extract"):
        write_capability_config([DOCUMENT_SEMANTIC_SEARCH], path=tmp_path / "config.json")


def test_unknown_capability_is_rejected(tmp_path):
    with pytest.raises(CapabilityConfigError, match="Unknown capabilities"):
        write_capability_config(["audio.unknown"], path=tmp_path / "config.json")


@pytest.mark.parametrize("payload,message", [
    ({"schema_version": 2, "enabled_capabilities": []}, "schema_version"),
    ({"schema_version": 1, "enabled_capabilities": "documents.extract"}, "list of strings"),
    ({"schema_version": 1, "enabled_capabilities": [DOCUMENT_EXTRACT], "revision": "cap-wrong"}, "revision"),
])
def test_invalid_config_is_rejected(tmp_path, payload, message):
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CapabilityConfigError, match=message):
        load_capability_config(path=path)


def test_enabled_capability_requires_matching_deployment_revision(tmp_path, monkeypatch):
    config_path = tmp_path / "capabilities.json"
    config = write_capability_config(LEGACY_DOCUMENT_CAPABILITIES, path=config_path)
    monkeypatch.setenv("DOTORI_CAPABILITIES_CONFIG_PATH", str(config_path))

    with pytest.raises(CapabilityAdmissionError) as missing:
        require_capability(DOCUMENT_EXTRACT)
    assert missing.value.code == "CAPABILITY_NOT_INSTALLED"

    deployment_path = tmp_path / "deployment_scopes" / "production" / "deployment_plan.json"
    deployment_path.parent.mkdir(parents=True)
    deployment_path.write_text(
        json.dumps(
            {
                "enabled_capabilities": list(config.enabled_capabilities),
                "capability_revision": "cap-stale",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CapabilityAdmissionError) as stale:
        require_capability(DOCUMENT_EXTRACT)
    assert stale.value.code == "CAPABILITY_UNAVAILABLE"
    assert stale.value.retryable is True

    deployment_path.write_text(
        json.dumps(
            {
                "enabled_capabilities": list(config.enabled_capabilities),
                "capability_revision": config.revision,
            }
        ),
        encoding="utf-8",
    )
    assert require_capability(DOCUMENT_EXTRACT) == config
