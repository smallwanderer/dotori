# LLM Model Catalog Developer & Operator Guide

The Model Catalog is the system's local source of truth for supported model artifacts and static model metadata.
Dynamic resource requirements such as RAM, VRAM, disk headroom, and fit status are computed by the Resource Estimator at runtime.

This guide explains the directory structure, the JSON schema requirements, how to add new models, and how the planner dynamically uses these fields instead of legacy static values.

---

## Directory Structure

The catalog subsystem is located in:

```text
app/document_ai/llm_installation_helper/catalog/
├── defaults.json
├── models.py
├── loader.py
├── evaluator.py
├── services.py
├── models/
│   └── qwen2.5-7b-instruct.json
└── artifacts/
    ├── qwen2.5-7b-instruct-q4_k_m.json
    └── qwen2.5-7b-instruct-awq.json
```

- **`defaults.json`**: Catalog manifest metadata (`schema_version`, name, and update date).
- **`models/*.json`**: Architecture, parameter, context, and capability metadata shared by artifacts.
- **`artifacts/*.json`**: Download source, format, quantization, disk size, and selection policy for one deployable artifact.

---

## Adding a New Model to the Catalog

Create one file in `models/` from the upstream model configuration. Then add
one or more files in `artifacts/` whose `model_id` references that model. The
loader rejects duplicate IDs and artifacts that reference an unknown model.

---

## Schema Fields and Resolution

The files are validated as `ModelCatalogEntry` and `ArtifactCatalogEntry`.
The loader resolves them into an internal `RAGModelCatalogEntry` candidate so
the planner does not depend on storage layout.

### 1. Model Entry (`models/*.json`)
- `id`: Stable model identity referenced by artifacts.
- `display_name`, `description`, `license`: Model-level presentation and governance metadata.
- `model_metadata`: Architecture and memory-estimation inputs.
- `capabilities`: Modalities, tasks, and languages shared by every artifact.

### 2. Artifact Entry (`artifacts/*.json`)
- `id`: Deployable artifact identity used by runtime configuration.
- `model_id`: Required reference to a model entry.
- `enabled`, `priority`: Artifact selection controls.
- `display_name`, `description`: Optional artifact-specific presentation.
- `hf`, `artifact`, `backend_profiles`, `policy`: Source, binary format, compatible backend profiles, and selection policy.

### 3. Hugging Face Spec (`hf`)
- `repo_id` *(string, Required)*: The repository ID on Hugging Face (e.g., `"Qwen/Qwen2.5-7B-Instruct-GGUF"`).
- `revision` *(string, Default: "main")*: Specific branch or commit hash.
- `gated` *(boolean, Default: false)*: Set to `true` if the model repository requires user agreement.
- `trust_remote_code` *(boolean, Default: false)*: Allows execution of custom code in the HF repo.

### 4. Artifact Spec (`artifact`)
- `format` *(string, Required)*: Must be `"gguf"`, `"safetensors"`, `"awq"`, or `"gptq"`.
  - Format determines the compatible backend candidates in Stage 2.
  - The Stage 2 backend resolver selects and freezes the backend. Later planners must not select it again.
- `quant` *(string, Required)*: The quantization format identifier (e.g., `"Q4_K_M"`, `"AWQ-INT4"`). This is normalized to lowercase and checked against `QUANT_BYTES_PER_PARAM` to compute model weight memory.
- `download_size_mb` *(integer, Required)*: File size to download. Used for installer download progress indicators.
- `disk_required_mb` *(integer, Optional)*: Catalog-provided lower bound for disk space.
  - Stage 3 resolves the final positive `disk_required_mb` as the greater of this value and the format multiplier (`1.1x` for GGUF; `1.5x` for vLLM artifacts).

### 5. Backend Compatibility (`backend_profiles`)

- `backend_profiles` *(non-empty list, Required)*: Backend profiles with which
  this exact artifact is compatible. Values are `vllm-cuda`, `llamacpp-cpu`,
  and `llamacpp-gpu-offload`.
- The catalog author validates architecture, format, quantization, dtype, and
  runtime-image support before adding a profile to this list.
- Stage 2 may choose only from this list and must not infer an undeclared
  backend merely because another artifact has the same base model.

### 6. Model Metadata Spec (`model_metadata`)
These fields must be extracted from the model's Hugging Face `config.json` to calculate KV cache size:
- `parameter_count_b` *(float, Required)*: Number of parameters in billions (e.g., `7.0`). Used to calculate weight memory (`parameter_count_b * bytes_per_param * 1024`).
- `max_context_length` *(integer, Default: 4096)*: Model's native context window limit.
- `num_hidden_layers` *(integer, Required)*: Total transformer layers (corresponds to `num_hidden_layers` in HF config).
- `num_key_value_heads` *(integer, Optional)*: Key-Value attention heads (corresponds to `num_key_value_heads` in HF config). If missing or `0`, the planner assumes an MHA model and defaults to `num_attention_heads`.
  > [!WARNING]
  > For GQA/MQA models (like Llama 3 or Qwen 2.5), this field must be specified. Omitting it triggers the MHA fallback, which will overestimate KV cache memory by 4x to 8x, potentially causing false `NOFIT` rejections.
- `hidden_size` *(integer, Required)*: Model's hidden dimension size (corresponds to `hidden_size` in HF config).
- `num_attention_heads` *(integer, Required)*: Number of query attention heads (corresponds to `num_attention_heads` in HF config).
- `head_dim` *(integer, Optional)*: Dimensions per attention head. If missing or `0`, it is automatically computed as `hidden_size / num_attention_heads`.

### 7. Policy Spec (`policy`)
- `visible` *(boolean, Default: true)*: Set to `false` to hide this model from the UI/CLI.
- `allow_auto_select` *(boolean, Default: true)*: Allows Stage 5 to automatically select this artifact.
- `allow_manual_select` *(boolean, Default: true)*: Allows an operator to choose this artifact from the Stage 5 catalog.
- `safety` *(string, Default: "safe")*: Can be `"safe"` or `"danger"`. `danger` produces `catalog_status = "BLOCKED"`; it is not a fit status.
- `tags` *(list of strings, Optional)*: Tags used for UI filtering (e.g., `["gguf", "q4", "rag"]`).

`enabled`, `visible`, `allow_auto_select`, `allow_manual_select`, and `safety` are evaluated as
`CatalogEligibility` in Stage 4. They must not create additional values in the
`FIT | RISKY | NOFIT` resource status.


## Quantization Bytes per Parameter

When calculating weights memory dynamically, the planner normalizes the `artifact.quant` string and maps it to parameter sizes (in bytes) using the following constants defined in `planner.py`:

| Quantization Format (`quant`) | Bytes per Parameter |
|---|---|
| `f32`, `fp32` | 4.0 |
| `f16`, `fp16`, `bf16` | 2.0 |
| `q8_0` | 1.05 |
| `q6_k` | 0.80 |
| `q5_k_m` | 0.68 |
| `q4_k_m`, `q4_0` | 0.58 |
| `q3_k_m` | 0.48 |
| `q2_k` | 0.37 |
| `awq`, `awq-4bit`, `awq-int4`, `gptq`, `gptq-int4` | 0.50 |

*Fallback*: If the quantization format is unrecognized, the planner falls back to the conservative `f32` default (`4.0` bytes/parameter).

---

## Hardware Profiler

Hardware profiler is used to estimate the performance of the model.
Estimation is not used for strict performance guarantees. It is used in model catalog to give user a rough idea of how fast the model will run.


## Calculation of performance metrics

Detailed docs in ./memory-estimation.md and ./per_token_estimation.md

---

## Verification

After adding or modifying model or artifact JSON, verify that references resolve and fit evaluation still passes:

1. **CLI Validation**:
   Run the CLI check command to see if the new model is parsed and correctly evaluated:
   ```bash
   python install.py --list-llm-models
   ```
2. **Local Tests**:
   Run unit tests to ensure that the catalog loader and planner functions parse the schema correctly:
   ```bash
   .venv\Scripts\python.exe -m pytest app/tests/test_llm_router.py
   ```
