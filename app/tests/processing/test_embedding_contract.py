from dataclasses import replace
from unittest.mock import Mock

import pytest

from documents.processing.embedding_contract import (
    COMMAND_SCHEMA, EmbeddingContractError, ResolvedEmbeddingScope,
    TargetOutcome, bind_embedding_targets, build_embedding_result, parse_embedding_command,
    validate_embedding_execution,
)


def payload():
    return {
        "schema": COMMAND_SCHEMA,
        "source": {"workspace_id": "workspace", "uid": "source", "revision": "blob", "checksum": "sha"},
        "input": {"artifact_id": "document-extract:17", "schema": "dotori.document.extract.v1",
                  "revision": "parse-5", "processing_fingerprint": "parser-policy"},
        "selector": {"type": "chunk_ids", "ids": [42, 41, 42]},
        "mode": "refresh",
        "runtime": {"generation_id": "server-generation", "fingerprint": "runtime"},
    }


def scope(command):
    return ResolvedEmbeddingScope(command.source, command.input, command.runtime, (41, 42, 43), True, False)


def test_canonical_payload_is_detached_and_round_trips():
    raw = payload()
    command = parse_embedding_command(raw)
    raw["selector"]["ids"].append(99)
    assert command.selector.ids == (41, 42)
    assert parse_embedding_command(command.to_payload()) == command
    assert len(command.intent_fingerprint()) == 64


def test_intent_ignores_duplicate_order_but_not_mode_revision_or_runtime():
    command = parse_embedding_command(payload())
    raw = payload()
    raw["selector"]["ids"] = [41, 42]
    assert parse_embedding_command(raw).intent_fingerprint() == command.intent_fingerprint()
    for changed in (replace(command, mode="missing"),
                    replace(command, input=replace(command.input, revision="new")),
                    replace(command, runtime=replace(command.runtime, generation_id="new"))):
        assert changed.intent_fingerprint() != command.intent_fingerprint()


@pytest.mark.parametrize("field,value,code", [
    ("schema", "other", "UNSUPPORTED_COMMAND_SCHEMA"),
    ("mode", "replace", "INVALID_MODE"),
    ("selector", {"type": "page", "ids": [1]}, "UNSUPPORTED_SELECTOR"),
    ("selector", {"type": "chunk_ids", "ids": []}, "INVALID_SELECTOR"),
    ("selector", {"type": "chunk_ids", "ids": [True]}, "INVALID_SELECTOR"),
    ("selector", {"type": "chunk_ids", "ids": [0]}, "INVALID_SELECTOR"),
    ("selector", {"type": "all", "ids": [1]}, "INVALID_COMMAND"),
    ("runtime", {"generation_id": "x", "fingerprint": ""}, "INVALID_IDENTITY"),
])
def test_invalid_payload(field, value, code):
    raw = payload()
    raw[field] = value
    with pytest.raises(EmbeddingContractError) as error:
        parse_embedding_command(raw)
    assert error.value.code == code


def test_unknown_model_setting_and_wrong_artifact_schema_rejected():
    raw = payload()
    raw["runtime"]["model"] = "user-model"
    with pytest.raises(EmbeddingContractError):
        parse_embedding_command(raw)
    raw = payload()
    raw["input"]["schema"] = "dotori.text.embedding-input.v1"
    with pytest.raises(EmbeddingContractError) as error:
        parse_embedding_command(raw)
    assert error.value.code == "UNSUPPORTED_INPUT_SCHEMA"


def test_scope_uses_mandatory_fresh_authority_and_preserves_batch_ownership():
    command = parse_embedding_command(payload())
    resolver = Mock(return_value=scope(command))
    assert bind_embedding_targets(command, resolve_authorized_scope=resolver) == (41, 42)
    resolver.assert_called_once_with(command)
    with pytest.raises(TypeError):
        bind_embedding_targets(command)
    raw = payload()
    raw["selector"] = {"type": "all"}
    assert bind_embedding_targets(parse_embedding_command(raw), resolve_authorized_scope=resolver) == (41, 42, 43)


@pytest.mark.parametrize("change,code", [
    (lambda s: replace(s, source=replace(s.source, workspace_id="other")), "SOURCE_CHANGED"),
    (lambda s: replace(s, source=replace(s.source, revision="new")), "SOURCE_CHANGED"),
    (lambda s: replace(s, input=replace(s.input, revision="new")), "PARSE_NOT_READY"),
    (lambda s: replace(s, ready=False), "PARSE_NOT_READY"),
    (lambda s: replace(s, stale=True), "PARSE_NOT_READY"),
    (lambda s: replace(s, runtime=replace(s.runtime, fingerprint="new")), "RUNTIME_CHANGED"),
    (lambda s: replace(s, chunk_ids=(41,)), "CHUNK_OUTSIDE_ARTIFACT"),
    (lambda s: replace(s, chunk_ids=()), "INVALID_SCOPE"),
])
def test_changed_domain_scope_rejected(change, code):
    command = parse_embedding_command(payload())
    with pytest.raises(EmbeddingContractError) as error:
        bind_embedding_targets(command, resolve_authorized_scope=lambda _: change(scope(command)))
    assert error.value.code == code


def test_authorization_failure_not_converted_to_ready_scope():
    command = parse_embedding_command(payload())
    with pytest.raises(PermissionError):
        bind_embedding_targets(command, resolve_authorized_scope=Mock(side_effect=PermissionError()))


def test_execution_preserves_approved_all_targets_without_reselecting():
    raw = payload()
    raw["selector"] = {"type": "all"}
    command = parse_embedding_command(raw)
    assert validate_embedding_execution(command, approved_target_ids=(42, 41),
        resolve_execution_input=lambda _: scope(command)) == (42, 41)


@pytest.mark.parametrize("change,code", [
    (lambda s: replace(s, source=replace(s.source, revision="new")), "SOURCE_CHANGED"),
    (lambda s: replace(s, input=replace(s.input, revision="new")), "PARSE_NOT_READY"),
    (lambda s: replace(s, runtime=replace(s.runtime, fingerprint="new")), "RUNTIME_CHANGED"),
    (lambda s: replace(s, chunk_ids=(41,)), "CHUNK_OUTSIDE_ARTIFACT"),
])
def test_execution_rejects_changed_fixed_input(change, code):
    command = parse_embedding_command(payload())
    with pytest.raises(EmbeddingContractError) as error:
        validate_embedding_execution(command, approved_target_ids=(41, 42),
            resolve_execution_input=lambda _: change(scope(command)))
    assert error.value.code == code


@pytest.mark.parametrize("targets", [(), (True,), (41, 41), (41,), (41, 42, 43)])
def test_execution_rejects_invalid_or_expanded_manifest_before_reading(targets):
    resolver = Mock()
    with pytest.raises(EmbeddingContractError):
        validate_embedding_execution(parse_embedding_command(payload()),
            approved_target_ids=targets, resolve_execution_input=resolver)
    resolver.assert_not_called()


def result(command, outcomes):
    return build_embedding_result(command, job_id="command-job", manifest_id="manifest",
        manifest_fingerprint="manifest-hash", target_ids=(41, 42), outcomes=outcomes)


def test_refresh_requires_new_completion_for_every_target():
    command = parse_embedding_command(payload())
    success = TargetOutcome(41, "succeeded", "vector:41", "command-job")
    assert result(command, (success, TargetOutcome(42, "failed")))["status"] == "partial"
    assert result(command, (success, TargetOutcome(42, "pending")))["ready"] is False
    complete = result(command, (success, TargetOutcome(42, "succeeded", "vector:42", "command-job")))
    assert complete["ready"] is True
    assert complete["counts"]["requested"] == 2
    with pytest.raises(EmbeddingContractError):
        result(command, (success, TargetOutcome(42, "reused", "old-vector")))


def test_missing_can_reuse_valid_results_without_claiming_new_creator():
    command = replace(parse_embedding_command(payload()), mode="missing")
    body = result(command, (TargetOutcome(41, "reused", "old-vector", "old-job"),
                            TargetOutcome(42, "succeeded", "new-vector", "command-job")))
    assert body["status"] == "ready" and body["counts"]["reused"] == 1
    assert body["targets"][0]["completion_job_id"] == "old-job"


@pytest.mark.parametrize("outcomes", [
    (TargetOutcome(41, "failed"),),
    (TargetOutcome(41, "failed"), TargetOutcome(41, "failed")),
    (TargetOutcome(41, "failed"), TargetOutcome(99, "failed")),
    (TargetOutcome(41, "succeeded", "vector", "old-job"), TargetOutcome(42, "failed")),
    (TargetOutcome(41, "succeeded", "", "command-job"), TargetOutcome(42, "failed")),
])
def test_invalid_target_completion_is_not_ready(outcomes):
    with pytest.raises(EmbeddingContractError):
        result(parse_embedding_command(payload()), outcomes)
