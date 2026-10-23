from dataclasses import replace
from unittest.mock import Mock

import pytest

from orchestration.artifact_bindings import (
    ArtifactBinding, ResolvedArtifact, resolve_artifact_binding, validate_artifact_binding,
)
from orchestration.plans import SourceReference


SOURCE = SourceReference("file", "blob", "source.schema", "checksum")
ARTIFACT = ResolvedArtifact("artifact", "output.schema", "parse-1", "file", "blob",
                            "checksum", "policy", "ready", False, "creator-job")


def resolve(binding, artifact=ARTIFACT, **kwargs):
    return resolve_artifact_binding(binding, source=SOURCE,
                                    resolve_authorized_artifact=lambda _: artifact, **kwargs)


def test_reuse_preserves_creator_and_exact_revision():
    binding = ArtifactBinding("artifact", "artifact", "output.schema", "parse-1")
    assert resolve(binding) == ARTIFACT
    assert resolve(binding).producer_job_id == "creator-job"
    with pytest.raises(ValueError):
        resolve(binding, replace(ARTIFACT, revision="parse-2"))


def test_step_requires_exact_dependency_producer_and_resolves_concrete_revision():
    binding = ArtifactBinding("step", "extract", "output.schema")
    assert resolve(binding, producer_job_id="creator-job").revision == "parse-1"
    for kwargs in ({}, {"producer_job_id": "other-job"}):
        with pytest.raises(ValueError):
            resolve(binding, **kwargs)


@pytest.mark.parametrize("change", [
    {"stale": True}, {"status": "failed"}, {"source_id": "other"},
    {"source_revision": "new"}, {"source_checksum": "new"},
    {"schema": "other"}, {"processing_fingerprint": ""},
])
def test_bad_inputs_cannot_bind(change):
    with pytest.raises(ValueError):
        resolve(ArtifactBinding("artifact", "artifact", "output.schema", "parse-1"),
                replace(ARTIFACT, **change))


@pytest.mark.parametrize("binding", [
    ArtifactBinding("artifact", "artifact", "output.schema"),
    ArtifactBinding("step", "extract", "output.schema", "made-up"),
    ArtifactBinding("other", "artifact", "output.schema"),
])
def test_invalid_reference_contract(binding):
    with pytest.raises(ValueError):
        validate_artifact_binding(binding)


def test_permission_callback_cannot_be_omitted_or_suppressed():
    binding = ArtifactBinding("artifact", "artifact", "output.schema", "parse-1")
    with pytest.raises(TypeError):
        resolve_artifact_binding(binding, source=SOURCE)
    with pytest.raises(PermissionError):
        resolve_artifact_binding(binding, source=SOURCE,
            resolve_authorized_artifact=Mock(side_effect=PermissionError()))
