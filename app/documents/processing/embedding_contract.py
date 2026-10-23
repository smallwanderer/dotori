"""Internal scoped embedding payload contract; not an enabled work route.

Validation does not authorize artifacts or load ORM/models. Owning adapters must
resolve a fresh authorized scope before binding targets. Batch sizing stays in
the embedding executor. Existing task envelopes and plan v1 are unchanged.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import re
from uuid import UUID

from documents.services.result_envelope import DOCUMENT_EXTRACT_RESULT_SCHEMA


COMMAND_SCHEMA = "dotori.document.embedding-command.v1"
RESULT_SCHEMA = "dotori.document.embedding-result.v1"
INTENT_SCHEMA = "dotori.document.embedding-intent.v1"
DELIVERY_SCHEMA = "dotori.document.embedding-delivery.v1"
DOCUMENT_EMBED_WORK_KIND = "embed_document"  # Reserved, not an enabled route.
DOCUMENT_EMBED_FUNCTION_ID = "document.embed"  # Composition, not a model alias.


class EmbeddingContractError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _reject(code, message):
    raise EmbeddingContractError(code, message)


def _object(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        _reject("INVALID_COMMAND", f"{label} requires exactly {sorted(fields)}")
    return value


def _identity(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 256:
        _reject("INVALID_IDENTITY", f"{label} must be a non-empty bounded string")
    return value


@dataclass(frozen=True)
class SourceBinding:
    workspace_id: str
    uid: str
    revision: str
    checksum: str


@dataclass(frozen=True)
class ParseBinding:
    artifact_id: str
    schema: str
    revision: str
    processing_fingerprint: str


@dataclass(frozen=True)
class RuntimeBinding:
    generation_id: str
    fingerprint: str


@dataclass(frozen=True)
class ChunkSelector:
    type: str
    ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class EmbeddingCommand:
    schema: str
    source: SourceBinding
    input: ParseBinding
    selector: ChunkSelector
    mode: str
    runtime: RuntimeBinding

    def to_payload(self):
        value = asdict(self)
        value["selector"] = {"type": self.selector.type}
        if self.selector.type == "chunk_ids":
            value["selector"]["ids"] = list(self.selector.ids)
        return value

    def intent_fingerprint(self):
        """Canonical request intent, not an authorization token or job ID."""
        canonical = json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class EmbeddingIntent:
    source: SourceBinding
    selector: ChunkSelector
    mode: str
    runtime: RuntimeBinding

    def to_payload(self):
        value = asdict(self)
        value["schema"] = INTENT_SCHEMA
        value["selector"] = {"type": self.selector.type}
        if self.selector.type == "chunk_ids":
            value["selector"]["ids"] = list(self.selector.ids)
        return value

    def bind(self, artifact):
        return parse_embedding_command(EmbeddingCommand(COMMAND_SCHEMA, self.source,
            artifact, self.selector, self.mode, self.runtime).to_payload())


def parse_embedding_intent(payload):
    value = _object(payload, ("schema", "source", "selector", "mode", "runtime"), "intent")
    if value["schema"] != INTENT_SCHEMA:
        _reject("UNSUPPORTED_INTENT_SCHEMA", "Unsupported embedding intent schema")
    # Shape validation only; this local input is never an executable command.
    parsed = parse_embedding_command({**value, "schema": COMMAND_SCHEMA,
        "input": {"artifact_id": "shape-only", "schema": DOCUMENT_EXTRACT_RESULT_SCHEMA,
                  "revision": "shape-only", "processing_fingerprint": "shape-only"}})
    return EmbeddingIntent(parsed.source, parsed.selector, parsed.mode, parsed.runtime)


@dataclass(frozen=True)
class EmbeddingDelivery:
    command: EmbeddingCommand
    manifest_id: str
    manifest_fingerprint: str

    def to_payload(self):
        return {"schema": DELIVERY_SCHEMA, "command": self.command.to_payload(),
                "manifest": {"id": self.manifest_id, "fingerprint": self.manifest_fingerprint}}


def parse_embedding_delivery(payload):
    value = _object(payload, ("schema", "command", "manifest"), "delivery")
    if value["schema"] != DELIVERY_SCHEMA:
        _reject("UNSUPPORTED_DELIVERY_SCHEMA", "Unsupported embedding delivery schema")
    manifest = _object(value["manifest"], ("id", "fingerprint"), "manifest")
    identity = _identity(manifest["id"], "manifest.id")
    try:
        canonical = str(UUID(identity))
    except ValueError:
        _reject("INVALID_MANIFEST", "Manifest ID must be a UUID")
    if canonical != identity or not isinstance(manifest["fingerprint"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", manifest["fingerprint"]):
        _reject("INVALID_MANIFEST", "Manifest reference must be canonical")
    return EmbeddingDelivery(parse_embedding_command(value["command"]), identity,
                             manifest["fingerprint"])


def parse_embedding_command(payload):
    value = _object(payload, ("schema", "source", "input", "selector", "mode", "runtime"), "command")
    if value["schema"] != COMMAND_SCHEMA:
        _reject("UNSUPPORTED_COMMAND_SCHEMA", "Unsupported embedding command schema")
    source = _object(value["source"], SourceBinding.__dataclass_fields__, "source")
    artifact = _object(value["input"], ParseBinding.__dataclass_fields__, "input")
    runtime = _object(value["runtime"], RuntimeBinding.__dataclass_fields__, "runtime")
    for label, binding in (("source", source), ("input", artifact), ("runtime", runtime)):
        for key, item in binding.items():
            _identity(item, f"{label}.{key}")
    if artifact["schema"] != DOCUMENT_EXTRACT_RESULT_SCHEMA:
        _reject("UNSUPPORTED_INPUT_SCHEMA", "Embedding requires a document extraction artifact")
    if value["mode"] not in ("missing", "refresh"):
        _reject("INVALID_MODE", "mode must be missing or refresh")
    selector = value["selector"]
    if not isinstance(selector, dict):
        _reject("INVALID_SELECTOR", "selector must be an object")
    kind = selector.get("type")
    if kind == "all":
        _object(selector, ("type",), "selector")
        ids = ()
    elif kind == "chunk_ids":
        _object(selector, ("type", "ids"), "selector")
        raw_ids = selector["ids"]
        if not isinstance(raw_ids, list) or not raw_ids or any(type(i) is not int or i < 1 for i in raw_ids):
            _reject("INVALID_SELECTOR", "chunk_ids must be a non-empty list of positive integers")
        ids = tuple(sorted(set(raw_ids)))
    else:
        _reject("UNSUPPORTED_SELECTOR", "Only all/chunk_ids are supported")
    return EmbeddingCommand(COMMAND_SCHEMA, SourceBinding(**source), ParseBinding(**artifact),
                            ChunkSelector(kind, ids), value["mode"], RuntimeBinding(**runtime))


@dataclass(frozen=True)
class ResolvedEmbeddingScope:
    """Fresh domain-authorized values; callers, not this type, own access checks."""
    source: SourceBinding
    input: ParseBinding
    runtime: RuntimeBinding
    chunk_ids: tuple[int, ...]
    ready: bool
    stale: bool


def bind_embedding_targets(command, *, resolve_authorized_scope):
    """Mandatory adapter callback; compare identity before freezing targets.

    Returns the approved scope, not only missing vectors. Runtime comes from the
    server callback; a client cannot select a model through this contract.
    """
    command = parse_embedding_command(command.to_payload())
    current = resolve_authorized_scope(command)
    if not isinstance(current, ResolvedEmbeddingScope):
        _reject("INVALID_SCOPE", "Adapter must return a resolved scope")
    if current.source != command.source:
        _reject("SOURCE_CHANGED", "Workspace/source revision or checksum changed")
    if current.input != command.input or current.ready is not True or current.stale is not False:
        _reject("PARSE_NOT_READY", "Pinned extraction artifact is unavailable or changed")
    if current.runtime != command.runtime:
        _reject("RUNTIME_CHANGED", "Server runtime changed")
    if (not isinstance(current.chunk_ids, tuple) or not current.chunk_ids
            or any(type(i) is not int or i < 1 for i in current.chunk_ids)
            or len(set(current.chunk_ids)) != len(current.chunk_ids)):
        _reject("INVALID_SCOPE", "Resolved artifact must contain distinct valid chunks")
    available = set(current.chunk_ids)
    if command.selector.type == "all":
        return tuple(sorted(available))
    if not set(command.selector.ids).issubset(available):
        _reject("CHUNK_OUTSIDE_ARTIFACT", "Selected chunks do not belong to the pinned artifact")
    return command.selector.ids


def validate_embedding_execution(command, *, approved_target_ids, resolve_execution_input):
    """Check a server-approved manifest against live input without selecting work.

    Caller must authenticate the stored approval/manifest before invoking this
    guard. A target tuple supplied by a client is not authorization. Resource,
    deadline, attempt and final-write guards remain with the owning executor.
    """
    command = parse_embedding_command(command.to_payload())
    if (not isinstance(approved_target_ids, tuple) or not approved_target_ids
            or any(type(i) is not int or i < 1 for i in approved_target_ids)
            or len(set(approved_target_ids)) != len(approved_target_ids)):
        _reject("INVALID_SCOPE", "Execution requires distinct approved targets")
    if command.selector.type == "chunk_ids" and set(approved_target_ids) != set(command.selector.ids):
        _reject("INVALID_SCOPE", "Approved manifest does not match the command")
    current = resolve_execution_input(command)
    if not isinstance(current, ResolvedEmbeddingScope):
        _reject("INVALID_SCOPE", "Executor must return pinned input evidence")
    if current.source != command.source:
        _reject("SOURCE_CHANGED", "Pinned source changed")
    if current.input != command.input or current.ready is not True or current.stale is not False:
        _reject("PARSE_NOT_READY", "Pinned extraction is unavailable or changed")
    if current.runtime != command.runtime:
        _reject("RUNTIME_CHANGED", "Executor runtime does not match the command")
    if (not isinstance(current.chunk_ids, tuple)
            or any(type(i) is not int or i < 1 for i in current.chunk_ids)
            or len(set(current.chunk_ids)) != len(current.chunk_ids)):
        _reject("INVALID_SCOPE", "Invalid live input targets")
    if not set(approved_target_ids).issubset(current.chunk_ids):
        _reject("CHUNK_OUTSIDE_ARTIFACT", "Approved input targets are unavailable")
    return approved_target_ids


@dataclass(frozen=True)
class TargetOutcome:
    chunk_id: int
    status: str
    artifact_ref: str = ""
    completion_job_id: str = ""


def build_embedding_result(command, *, job_id, manifest_id, manifest_fingerprint,
                           target_ids, outcomes):
    """Aggregate owning-domain evidence; not an HTTP receipt/report adapter.

    Caller must revalidate manifest, permissions and serving/input stamps. A
    terminal delivery receipt alone is not target completion evidence.
    """
    command = parse_embedding_command(command.to_payload())
    for label, value in (("job_id", job_id), ("manifest_id", manifest_id),
                         ("manifest_fingerprint", manifest_fingerprint)):
        _identity(value, label)
    if (not isinstance(target_ids, tuple) or not target_ids
            or any(type(i) is not int or i < 1 for i in target_ids)
            or len(set(target_ids)) != len(target_ids)):
        _reject("INVALID_RESULT", "Manifest must contain distinct positive target IDs")
    if command.selector.type == "chunk_ids" and set(target_ids) != set(command.selector.ids):
        _reject("INVALID_RESULT", "Manifest must match the complete approved selection")
    rows = tuple(outcomes)
    if (any(not isinstance(row, TargetOutcome) or type(row.chunk_id) is not int for row in rows)
            or len(rows) != len(target_ids) or {row.chunk_id for row in rows} != set(target_ids)):
        _reject("INVALID_RESULT", "Exactly one outcome is required for every manifest target")
    counts = dict.fromkeys(("succeeded", "reused", "failed", "pending", "running"), 0)
    for row in rows:
        if not isinstance(row.status, str) or row.status not in counts:
            _reject("INVALID_RESULT", "Unknown target outcome")
        counts[row.status] += 1
        if row.status in ("succeeded", "reused"):
            _identity(row.artifact_ref, "target.artifact_ref")
        if row.status == "succeeded" and row.completion_job_id != job_id:
            _reject("INVALID_RESULT", "Fresh target completion must belong to this command")
        if row.status == "reused" and command.mode == "refresh":
            _reject("INVALID_RESULT", "Refresh cannot complete using an old result")
    ready = counts["succeeded"] + counts["reused"] == len(target_ids)
    status = "ready" if ready else (
        "running" if counts["pending"] or counts["running"] else (
            "partial" if counts["succeeded"] or counts["reused"] else "failed"))
    return {"schema": RESULT_SCHEMA, "job_id": job_id,
            "manifest_id": manifest_id, "manifest_fingerprint": manifest_fingerprint,
            "intent_fingerprint": command.intent_fingerprint(),
            "source": asdict(command.source), "input": asdict(command.input),
            "runtime": asdict(command.runtime), "mode": command.mode,
            "status": status, "ready": ready,
            "counts": {"requested": len(target_ids), **counts},
            "targets": [asdict(row) for row in sorted(rows, key=lambda row: row.chunk_id)]}
