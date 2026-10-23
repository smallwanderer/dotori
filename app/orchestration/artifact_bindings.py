"""Versioned input references; pure contracts, not journal or authorization.

The owner resolves fresh authorized artifact metadata. Step bindings are resolved
only after a producing job succeeds; the resolved revision must be pinned before
publication. Existing plan v1 does not yet consume this contract.
"""
from dataclasses import dataclass


BINDING_SCHEMA = "dotori.artifact-binding.v1"


@dataclass(frozen=True)
class ArtifactBinding:
    kind: str
    reference: str
    schema: str
    revision: str = ""


@dataclass(frozen=True)
class ResolvedArtifact:
    artifact_id: str
    schema: str
    revision: str
    source_id: str
    source_revision: str
    source_checksum: str
    processing_fingerprint: str
    status: str
    stale: bool
    producer_job_id: str = ""


def validate_artifact_binding(binding):
    if not isinstance(binding, ArtifactBinding) or binding.kind not in ("artifact", "step"):
        raise ValueError("Unsupported artifact binding")
    for value in (binding.reference, binding.schema):
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError("Binding reference and schema are required")
    if not isinstance(binding.revision, str) or binding.revision != binding.revision.strip():
        raise ValueError("Invalid binding revision")
    if binding.kind == "artifact" and not binding.revision:
        raise ValueError("Existing artifact must pin its revision")
    if binding.kind == "step" and binding.revision:
        raise ValueError("Unresolved step cannot invent an artifact revision")
    return binding


def resolve_artifact_binding(binding, *, source, resolve_authorized_artifact, producer_job_id=None):
    validate_artifact_binding(binding)
    if binding.kind == "step" and (not isinstance(producer_job_id, str) or not producer_job_id):
        raise ValueError("Producing step job identity is required")
    artifact = resolve_authorized_artifact(binding)
    if not isinstance(artifact, ResolvedArtifact):
        raise ValueError("Owning adapter must resolve artifact metadata")
    if (artifact.schema != binding.schema or artifact.status != "ready" or artifact.stale is not False
            or artifact.source_id != source.source_id or artifact.source_revision != source.revision
            or artifact.source_checksum != source.checksum):
        raise ValueError("Artifact is not a ready input for the authorized source")
    for value in (artifact.artifact_id, artifact.revision, artifact.processing_fingerprint):
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError("Artifact identity, revision and processing evidence are required")
    if binding.kind == "artifact":
        if artifact.artifact_id != binding.reference or artifact.revision != binding.revision:
            raise ValueError("Pinned artifact changed")
    elif artifact.producer_job_id != producer_job_id:
        raise ValueError("Artifact was not produced by the approved dependency job")
    return artifact
