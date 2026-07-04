# Backend Profile Selection

## Purpose

Stage 2 resolves the serving engine and backend profile for each deployable
model artifact. It runs after hardware probing and before memory estimation.

Stage 2 owns backend selection. Stages 3 through 7 must preserve its result and
must not silently switch to another backend when estimation or fit evaluation
fails.

## Inputs

```text
HardwareProfile
InstallationOptions:
  cluster_mode: boolean
RAGModelCatalogEntry:
  id
  artifact.format
  artifact.quant
  artifact.dtype
  backend_profiles
```

`cluster_mode` is derived during installation. It is not an operator-facing
model setting and is not part of `HardwareProfile`.

## Output

Stage 2 emits exactly one `BackendResolution` for every artifact:

```text
BackendResolution:
  artifact_id: string
  backend_status: AVAILABLE | UNAVAILABLE
  runtime: llama.cpp | vllm | null
  backend_profile:
    llamacpp-cpu | llamacpp-gpu-offload | vllm-cuda | null
  reason_code: string
  reason: string
```

Invariants:

- `backend_status == AVAILABLE` requires non-null `runtime` and `backend_profile`.
- `backend_status == UNAVAILABLE` requires null `runtime` and `backend_profile`.
- `runtime == "vllm"` if and only if `backend_profile == "vllm-cuda"`.
- `runtime == "llama.cpp"` if and only if `backend_profile` is
  `llamacpp-cpu` or `llamacpp-gpu-offload`.
- The resolver returns one result and never a ranked list of fallback backends.

`reason_code` is one of:

- `SELECTED_VLLM_CUDA`
- `SELECTED_LLAMACPP_GPU_OFFLOAD`
- `SELECTED_LLAMACPP_CPU`
- `CLUSTER_REQUIRES_VLLM`
- `CUDA_UNAVAILABLE`
- `NO_DECLARED_BACKEND`
- `LLAMACPP_GPU_OFFLOAD_UNAVAILABLE`

The human-readable `reason` may change without changing program behavior;
downstream logic uses `reason_code`.

## Compatibility Matrix

| Artifact format | Backend candidate | Required compatibility |
|---|---|---|
| `awq` | `vllm-cuda` | Profile declared by artifact and CUDA available. |
| `gptq` | `vllm-cuda` | Profile declared by artifact and CUDA available. |
| `safetensors` | `vllm-cuda` | Profile declared by artifact and CUDA available. |
| `gguf` | `llamacpp-gpu-offload` | Profile declared by artifact, standalone mode, at least one detected accelerator, and `llamacpp_gpu_offload_supported == true`. |
| `gguf` | `llamacpp-cpu` | Profile declared by artifact and standalone mode. |

An artifact format without a row in this matrix is invalid catalog data, not a
runtime fallback opportunity.

## Resolution Rules

Apply these rules in order for each artifact:

1. If the artifact is malformed, has an unsupported format, or declares no
   compatible backend profile, reject it as invalid catalog data.
2. In cluster mode, only `vllm-cuda` is allowed. A non-vLLM-compatible artifact
   is `UNAVAILABLE`; do not route GGUF to standalone llama.cpp.
3. Resolve `awq`, `gptq`, and `safetensors` to `vllm-cuda` only when that
   profile is declared and its hardware requirements pass. Otherwise return
   `UNAVAILABLE`.
4. In standalone mode, resolve GGUF to declared `llamacpp-gpu-offload` when a
   usable accelerator and llama.cpp GPU-offload support are both present.
5. Otherwise resolve GGUF to declared `llamacpp-cpu`; if it is not declared,
   return `UNAVAILABLE`.

Visibility, selection permission, safety policy, RAM, VRAM, and disk capacity do
not participate in backend selection. They are evaluated after Stage 2.

Stage 2 checks backend and artifact compatibility only. It must not compare RAM,
VRAM, disk, context length, concurrency, or TPS. Those belong to Stages 3 and 4.

## Downstream Behavior

- Stage 3 estimates memory, placement, and TPS only for `AVAILABLE` results.
- Stage 4 maps every `UNAVAILABLE` result directly to `fit_status = "NOFIT"`
  using the Stage 2 reason.
- Runtime policy receives the already fixed backend from the Stage 5 selected
  assessment and cannot reselect it.
