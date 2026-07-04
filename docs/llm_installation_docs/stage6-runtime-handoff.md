# Stage 6 Runtime Policy Handoff

## Purpose

Stage 6 creates the immutable boundary between model selection and runtime
parameter resolution. It freezes the exact Stage 5 selection, Stage 4
assessment, and Stage 1 hardware profile into one RuntimePolicyInput.

Stage 6 performs validation and serialization only. It does not re-run probing,
catalog loading, backend selection, estimation, fit evaluation, ranking, or
runtime parameter resolution.

## Input

~~~text
SelectionResult where selection_status == SELECTED
HardwareProfile used for the selected CatalogAssessment
~~~

## Output

~~~text
RuntimePolicyInput:
  schema_version: 1
  artifact_id: string
  model_id: string
  runtime: llama.cpp | vllm
  backend_profile:
    llamacpp-cpu | llamacpp-gpu-offload | vllm-cuda
  fit_status: FIT | RISKY
  selection_mode: automatic | manual
  priority_preset: speed | balanced | quality
  selection_reason_code: string
  risky_confirmed: boolean
  catalog_assessment: immutable JSON snapshot
  hardware_profile: immutable JSON snapshot
  integrity_sha256: string
~~~

The implementation stores canonical JSON internally and returns decoded copies
to consumers. Mutating a returned dictionary cannot change the handoff.

## Validation

Stage 6 rejects the handoff unless all conditions hold:

- Stage 5 returned SELECTED and exactly one selected candidate.
- fit_status is FIT or confirmed RISKY.
- runtime and backend_profile are non-null and form a supported pair.
- backend_profile is declared by the selected artifact.
- baseline memory placement exists.
- artifact_id and model_id come from the selected catalog entry.
- assessment and hardware values are JSON serializable.

Supported runtime/backend pairs:

| runtime | backend_profile |
|---|---|
| vllm | vllm-cuda |
| llama.cpp | llamacpp-cpu |
| llama.cpp | llamacpp-gpu-offload |

## Integrity

integrity_sha256 is calculated from canonical, sorted, compact UTF-8 JSON over
every handoff field except the digest itself.

~~~text
integrity_sha256 = SHA256(canonical_json(unsigned_payload))
~~~

Stage 7 must call verify_integrity before reading the snapshot. A mismatch is a
RUNTIME_HANDOFF_INTEGRITY_ERROR and aborts installation; it must not trigger
reselection or fallback.

## Stage 7 Boundary

Stage 7 receives RuntimePolicyInput as its only planning argument. It must use
the embedded catalog assessment and hardware snapshot. Passing a live catalog
entry, a newly probed HardwareProfile, or a separately supplied backend,
fit_status, or priority preset is prohibited.

The final runtime config persists the full RuntimePolicyInput so operators can
audit which assessment and hardware snapshot produced the serving parameters.
