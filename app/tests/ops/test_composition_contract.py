from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from capabilities.functions import (
    FUNCTION_REGISTRY, COMPOSITION_DEFINITIONS, get_function, validate_composition_provider,
)


def snapshot(**changes):
    provider = {**get_function("text.embed").metadata(), "role": "embedding",
                "adapter_contract": "dotori-internal-embed-v1", **changes}
    return SimpleNamespace(role_bindings={"embedding": {"provider_installation_id": "selected"}},
        resolve_function_provider=Mock(return_value=provider))


def test_composition_resolves_primitive_not_domain_provider():
    deployed = snapshot()
    provider = validate_composition_provider("document.embed", snapshot=deployed,
        adapter_contract="dotori-document-embedding-v1")
    deployed.resolve_function_provider.assert_called_once_with("text.embed",
        explicit_provider_installation_id="selected")
    assert provider["input_schema"] != COMPOSITION_DEFINITIONS["document.embed"].input_schema
    assert "document.embed" not in FUNCTION_REGISTRY


@pytest.mark.parametrize("changes", [{"role": "parser"}, {"input_schema": "dotori.document.extract.v1"},
    {"adapter_contract": "wrong"}, {"contract_version": True}])
def test_wrong_primitive_provider_rejected(changes):
    with pytest.raises(ValueError):
        validate_composition_provider("document.embed", snapshot=snapshot(**changes),
            adapter_contract="dotori-document-embedding-v1")


def test_wrong_domain_adapter_rejected_before_provider_lookup():
    deployed = snapshot()
    with pytest.raises(ValueError):
        validate_composition_provider("document.embed", snapshot=deployed, adapter_contract="wrong")
    deployed.resolve_function_provider.assert_not_called()
