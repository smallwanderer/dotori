from unittest.mock import Mock

import pytest

from orchestration.contracts import WorkContractError
from orchestration.preparation import InputReadiness, prepare_request


def adapter(evidence):
    value = Mock()
    value.validate_request.return_value = "authorized-context"
    value.inspect_input.return_value = evidence
    return value


def test_ready_input_only_compiles_reuse():
    owner = adapter(InputReadiness(True, "CURRENT", "artifact"))
    result = prepare_request(owner)
    assert result.action == "reuse"
    owner.compile_reuse.assert_called_once_with("authorized-context", "artifact")
    owner.compose_prerequisite.assert_not_called()
    owner.require_prerequisite_allowed.assert_not_called()


def test_unready_input_requires_permission_before_prerequisite_composition():
    evidence = InputReadiness(False, "MISSING")
    owner = adapter(evidence)
    result = prepare_request(owner)
    assert result.action == "prerequisite" and result.reason == "MISSING"
    owner.require_prerequisite_allowed.assert_called_once_with("authorized-context", evidence)
    owner.compile_reuse.assert_not_called()


@pytest.mark.parametrize("failure", ["validate_request", "inspect_input", "require_prerequisite_allowed"])
def test_rejection_never_composes_or_compiles(failure):
    owner = adapter(InputReadiness(False, "MISSING"))
    getattr(owner, failure).side_effect = PermissionError("rejected")
    with pytest.raises(PermissionError):
        prepare_request(owner)
    owner.compose_prerequisite.assert_not_called()
    owner.compile_reuse.assert_not_called()
    if failure == "validate_request":
        owner.inspect_input.assert_not_called()


@pytest.mark.parametrize("evidence", [None, InputReadiness(1, "READY"), InputReadiness(False, "")])
def test_invalid_domain_evidence_cannot_authorize_work(evidence):
    owner = adapter(evidence)
    with pytest.raises(WorkContractError):
        prepare_request(owner)
    owner.compose_prerequisite.assert_not_called()
    owner.compile_reuse.assert_not_called()
