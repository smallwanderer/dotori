## Runtime Presets (llama.cpp)

Runtime presets are configured during installation or an explicit operator
reconfiguration. They are not changed during normal request processing.

This process corresponds to Stage 7. It accepts only the integrity-verified
RuntimePolicyInput produced by Stage 6; it must not read a live catalog entry or
probe hardware again.

The installer exposes three operator-facing presets:

```text
speed
balanced
quality
```

These presets are not only candidate sorting preferences. They are runtime planning policies.

For llama.cpp, a preset controls:

```text
ctx_size_per_slot
parallel
cache_type_k
cache_type_v
gpu_layers
threads
batch_size
ubatch_size
```

The installer must resolve these values during installation or operator-triggered re-detection and store the final values in `llm_runtime.json`.

Request-time code must not recompute these values.

## Stage 7 Input Contract

The planner accepts exactly one RuntimePolicyInput from Stage 6.

~~~text
build_serving_plan(runtime_policy_input)
~~~

Before resolving parameters it must verify integrity_sha256, use the fixed
runtime/backend/fit/preset values, and read only the embedded assessment and
hardware snapshot.

An integrity failure produces RUNTIME_HANDOFF_INTEGRITY_ERROR. A preset for
which no safe parameter combination exists produces
RUNTIME_CONFIG_UNRESOLVABLE. Neither error permits model/backend reselection.

---

## llama.cpp Runtime Profiles

Supported llama.cpp runtime profiles:

| Profile                | Description                                                                         |
| ---------------------- | ----------------------------------------------------------------------------------- |
| `llamacpp-cpu`         | CPU-only execution. GGUF model weights and KV cache are placed in RAM.              |
| `llamacpp-gpu-offload` | Partial or full GPU offload. Some weights and possibly KV cache are placed in VRAM. |

The resolver must branch by backend profile before calculating GPU-specific parameters.

## vLLM CUDA Resolution

For vllm-cuda, Stage 7 preserves the Stage 3 tensor-parallel device group and
derives:

~~~text
max_model_len = min(preset target context, model maximum, configured context cap)
max_num_seqs = preset concurrency target
max_num_batched_tokens = max_model_len * max_num_seqs
tensor_parallel_size = Stage 3 placement tensor_parallel_size
gpu_memory_utilization = 0.9
~~~

The final RAM and per-GPU VRAM requirements are recalculated for these values.
vLLM does not spill model weights to RAM. If any required GPU pool is NOFIT,
Stage 7 returns RUNTIME_CONFIG_UNRESOLVABLE.

---

## Resolution Order

Do not calculate llama.cpp parameters as a simple one-pass list.

The correct order is:

```text
1. Verify RuntimePolicyInput integrity and accept its fixed backend_profile
2. Select preset target policy
3. Select candidate cache_type_k/v
4. Select candidate ctx_size_per_slot
5. Select candidate KV cache placement
   - llamacpp-cpu: ["ram"]
   - llamacpp-gpu-offload: ["vram", "ram"]
6. Select candidate batch_size / ubatch_size
7. Resolve threads based on available CPU
8. Resolve gpu_layers (if backend_profile is gpu_offload) using baseline (parallel = 1)
9. Evaluate baseline single-request fit (parallel = 1)
10. If baseline fits, calculate maximum safe parallel slots from leftover memory
11. Run final memory fit evaluation with resolved parallel count
```

The resolver iterates candidates in preset-priority order and returns the first `FIT` configuration.

This avoids circular references between:

```text
gpu_layers
ctx_size
cache_type_k/v
parallel
kv_cache_budget_mb
```

---

## Preset Target Policy

### `speed`

`speed` prioritizes minimizing response latency for a single request (optimizing for fastest time-to-first-token and token generation speed). It does not target high concurrent multi-request throughput at the cost of latency.

Typical behavior:

```text
ctx_size_per_slot: lower or moderate (reduces prompt processing time)
parallel: capped conservatively to prevent scheduling contention
cache_type_k/v: memory-efficient (q8_0 or q4_0), reducing KV cache memory pressure and potentially improving memory bandwidth efficiency
gpu_layers: maximize to offload as much model execution to GPU as possible
batch_size: larger if memory permits, mainly to optimize prompt prefill throughput
ubatch_size: larger if memory permits, especially for GPU parallelism during prefill
```

### `balanced`

`balanced` prioritizes stable default operation and balanced multi-request handling.

Typical behavior:

```text
ctx_size_per_slot: moderate
parallel: moderate concurrency
cache_type_k/v: q8_0/q8_0 or f16/f16 depending on memory
gpu_layers: stable FIT value, not necessarily maximum
batch_size: moderate (512)
ubatch_size: moderate (256)
```

### `quality`

`quality` prioritizes context capacity and output quality.

Typical behavior:

```text
ctx_size_per_slot: higher to support long documents
parallel: forced to 1 to reserve all resources for context
cache_type_k/v: prefer f16/f16 to preserve KV cache precision, fallback to q8_0/q8_0 if needed
gpu_layers: adjusted after reserving memory for larger context
batch_size: conservative (256 or 128) to save memory overhead
ubatch_size: conservative (128 or 64)
```

`quality` does not mean using fewer CPU threads for better output quality. Thread count affects performance and resource contention, not model answer quality.

---

## `cache_type_k/v`

KV cache type must be resolved before final `ctx_size`, `parallel`, and `gpu_layers` decisions because it directly changes KV cache memory per token.

Allowed KV cache types should be limited to llama.cpp-supported values:

```text
f32
f16
bf16
q8_0
q4_0
q4_1
iq4_nl
q5_0
q5_1
```

Do not use model weight quantization names such as:

```text
Q4_K_M
Q5_K_M
Q6_K
```

as KV cache types.

Preset candidate order:

```text
quality:
  1. f16 / f16
  2. q8_0 / q8_0

balanced:
  1. q8_0 / q8_0
  2. f16 / f16
  3. q4_0 / q8_0

speed:
  1. q8_0 / q8_0
  2. q4_0 / q8_0
  3. q4_0 / q4_0
```

The resolver should choose the first candidate that satisfies the memory fit policy.

---

## `ctx_size`

Use `ctx_size_per_slot` as the project-level planning value.

```text
ctx_size_per_slot =
the desired context length available to each concurrent request slot
```

Preset behavior:

| Preset     | Context behavior                                                                  |
| ---------- | --------------------------------------------------------------------------------- |
| `speed`    | Prefer lower or moderate context to reduce KV cache pressure and improve latency. |
| `balanced` | Prefer a moderate context suitable for normal RAG usage.                          |
| `quality`  | Prefer the largest context that still fits safely.                                |

Recommended default target caps:

```text
speed:    min(model_max_context_length, 4096)
balanced: min(model_max_context_length, 8192)
quality:  min(model_max_context_length, 16384 or configured_quality_context_cap)
```

The actual cap should be configurable.

KV cache memory is proportional to context size:

```text
kv_cache_mb =
ctx_size_per_slot
* parallel
* kv_cache_per_token_mb
```

kv_cache_mb will then be updated.

Therefore, after `ctx_size_per_slot` is resolved and FIT status is determined, `parallel` is derived after the remaining memory.

For llama.cpp server execution:

server_ctx_size = ctx_size_per_slot * parallel

The runtime must pass:

--ctx-size server_ctx_size
--parallel parallel

Do not pass ctx_size_per_slot directly as --ctx-size when parallel > 1.

---

## `parallel`

`parallel` is the number of concurrent llama.cpp server slots.

The planner should not treat `parallel` as a free performance multiplier. More slots increase concurrent serving capacity, but they also increase KV cache reservation and memory pressure.

In this architecture, `parallel` is treated as a **post-fit optimization**. The resolver first verifies that a baseline configuration with a single slot (`parallel = 1`) and a baseline context length of `context_length = 4096` (or `min(model_max_context_length, 4096)`) fits successfully. Once a baseline configuration fits, any leftover memory in the target memory pool (RAM or VRAM) is allocated to support additional concurrent slots, up to a preset cap.

Preset concurrency caps:

```text
speed:    cap = 2 (keep low to prioritize single-request speed and prevent resource contention)
balanced: cap = 4 (supports multi-user concurrency)
quality:  cap = 1 (forces 1 to reserve all resources for maximum context length)
```

### Concurrency Resolution Formula

For `llamacpp-gpu-offload`, resolve the already selected Stage 3 device before
using any VRAM value:

```text
primary_gpu_index = memory_placement.device_indices[0]
available_vram_primary_gpu_mb =
    device_by_index[primary_gpu_index].free_vram_mb
```

1. First, evaluate a single-slot configuration (`parallel = 1`) to compute the baseline memory requirements.

   `required_ram_baseline_mb` is defined as:

   ```text
   required_ram_baseline_mb =
     cpu_resident_weight_memory_mb
     + cpu_resident_kv_cache_mb (ctx_size_per_slot × 1 × kv_cache_per_token_mb, if KV cache in RAM)
     + runtime_overhead_mb
   ```

   `required_vram_baseline_mb` (the VRAM required on the primary GPU, GPU 0) is defined as:

   ```text
   required_vram_baseline_mb =
     gpu_resident_weight_memory_mb
     + gpu_resident_kv_cache_mb (ctx_size_per_slot × 1 × kv_cache_per_token_mb, if KV offload enabled)
     + runtime_overhead_mb
     + cuda_context_overhead_mb
   ```

   For multi-GPU hardware, the final `required_vram_per_gpu_mb` aligns with
   `gpu_probe_result.devices`; only the entry identified by
   `primary_gpu_index` is populated.

   `runtime_overhead_mb` is a fixed constant set to `512` (in MB) for both RAM and VRAM.

   These are the raw component sums before the 1.25× safety headroom multiplier is applied. To guarantee that the final resolved concurrency fits safely within this headroom, the available hardware memory is scaled down by the headroom_multiplier in the leftover computation below.
2. Identify the active memory pool for KV cache:
   - For `llamacpp-cpu`: KV cache is stored in RAM.
   - For `llamacpp-gpu-offload` (with KV offload enabled): KV cache is stored in VRAM.
   - For `llamacpp-gpu-offload` (with KV offload disabled): KV cache is stored in RAM.
3. Compute the leftover memory in the active KV cache pool:
   - If KV cache is stored in RAM:
     ```text
     leftover_memory_mb =
       (effective_ram_available_mb / headroom_multiplier)
       - required_ram_baseline_mb
     ```
   - If KV cache is stored in VRAM:
     ```text
     leftover_memory_mb =
       (available_vram_primary_gpu_mb / headroom_multiplier)
       - required_vram_baseline_mb
     ```
4. Calculate additional slots supported by this leftover budget:
     ```text
     per_slot_kv_mb = ctx_size_per_slot * kv_cache_per_token_mb
     additional_slots = floor(leftover_memory_mb / per_slot_kv_mb)
     ```
5. Resolve final parallel value:
     ```text
     parallel = min(preset_parallel_cap, 1 + max(0, additional_slots))
     ```

---

## `gpu_layers`

`gpu_layers` applies only to `llamacpp-gpu-offload`.

### CPU-only branch

If backend profile is `llamacpp-cpu`:

```text
gpu_layers = 0
required_vram_mb = 0
```

Do not calculate VRAM-based `gpu_layers`.

---

### GPU offload branch

If backend profile is `llamacpp-gpu-offload`, calculate `gpu_layers` only after resolving:

```text
cache_type_k/v
ctx_size_per_slot
```

This avoids circular dependency. The VRAM calculation uses a baseline of 1 slot (`parallel = 1`) for KV cache allocation:

Approximate calculation:

```text
available_vram_for_weights_mb =
(available_vram_primary_gpu_mb / headroom_multiplier)
- runtime_overhead_mb
- gpu_resident_kv_cache_mb (for parallel = 1, if KV offload enabled)
```

`per_layer_weight_mb` is an approximation, dividing model weight memory by number of hidden layers:


```text
per_layer_weight_mb =
weight_memory_mb / num_hidden_layers
```

```text
gpu_layers =
floor(available_vram_for_weights_mb / per_layer_weight_mb)
```

Clamp the result:

```text
gpu_layers =
max(1, min(num_hidden_layers, gpu_layers))
```

If one layer does not fit, reject that runtime-parameter candidate. Do not
switch to `llamacpp-cpu`; Stage 2 has already fixed the backend profile.

This per-layer calculation is an approximation. Real GGUF tensor sizes are not perfectly uniform across layers. If tensor-level metadata is available, tensor-level placement estimation is preferred.

---

## KV Cache Placement

The planner must explicitly decide KV cache placement.

For `llamacpp-cpu`:

```text
kv_cache -> RAM
```

For `llamacpp-gpu-offload`:

```text
kv_cache -> VRAM (default, if kv_offload is supported)
kv_cache -> RAM (fallback, if VRAM is insufficient or kv_offload is disabled)
```

The selected placement affects the fit decision:

```text
required_ram_mb
required_vram_per_gpu_mb
```

Do not hide KV cache placement inside `gpu_layers`.

---

## `threads`

`threads` controls CPU worker threads for generation.

It is a performance and resource contention parameter. It does not improve or degrade model output quality.

Therefore, do not define:

```text
quality = lowest threads
```

Instead, resolve threads from available CPU resources.

Recommended calculation:

```text
usable_cpu_threads =
max(1, physical_cpu_cores - reserved_system_cores)
```

Default:

```text
reserved_system_cores = 1
```

Preset behavior:

```text
speed:
  threads = usable_cpu_threads

balanced:
  threads = max(1, floor(usable_cpu_threads * 0.75))

quality:
  threads = max(1, floor(usable_cpu_threads * 0.75))
```

For CPU-only execution, `threads` has stronger performance impact.

For GPU-offload execution, `threads` still matters for CPU-resident layers, sampling, prompt processing, and runtime overhead, but it is not the only performance parameter.

---

## `batch_size` and `ubatch_size`

`batch_size` is the logical maximum batch size.

`ubatch_size` is the physical maximum micro-batch size.

Constraints:

```text
ubatch_size <= batch_size
batch_size >= 1
ubatch_size >= 1
```

Both `batch_size` and `ubatch_size` use a shared candidate list. Each preset applies a cap to limit the upper bound before searching. The resolver iterates the filtered list in descending order and selects the first candidate that satisfies the fit policy.

### Shared candidate lists

```text
batch_size_candidates (shared):
  [2048, 1024, 512, 256, 128, 64]

ubatch_size_candidates (shared):
  [512, 256, 128, 64]
```

### Preset caps

```text
              batch_size_cap    ubatch_size_cap
speed:              2048               512
balanced:           1024               256
quality:             512               128
```

### Resolution procedure

```text
The resolver evaluates `(batch_size, ubatch_size)` pairs.

For each batch_size candidate:
  For each ubatch_size candidate where ubatch_size <= batch_size:
    evaluate_with(batch_size, ubatch_size)

The first FIT pair in preset-priority order is selected.
```

A candidate passes `evaluate_with(batch_size, ubatch_size)` if:

```text
1. ubatch_size <= batch_size
2. required_ram_mb satisfies fit policy
3. required_vram_per_gpu_mb satisfies fit policy
4. no backend compatibility rule is violated
```

For persisted installation config, prefer candidates that are `FIT`.

Do not use endpoint smoke test as part of candidate enumeration. Smoke test should run only after final parameter selection.

---

## Final llama.cpp Resolution Algorithm

ctx_candidates for operator presets:

```text
speed:
  ctx_candidates: [4096, 2048]

balanced:
  ctx_candidates: [8192, 4096, 2048]

quality:
  ctx_candidates: [16384, 8192, 4096]
```

Pseudo-code:

```text
resolve_llamacpp_config(profile, preset, model, hardware):

  profile = llamacpp-cpu or llamacpp-gpu-offload

  for cache_type_k, cache_type_v in cache_candidates[preset]:

    kv_per_token_mb = estimate_kv_per_token(cache_type_k, cache_type_v, model)

    # Filter candidates to not exceed the model's maximum native context length
    filtered_ctx_candidates = [v for v in ctx_candidates[preset] if v <= model.max_context_length]
    if not filtered_ctx_candidates:
        filtered_ctx_candidates = [model.max_context_length]

    for ctx_size_per_slot in filtered_ctx_candidates:

      if profile == llamacpp-cpu:
          kv_placements = ["ram"]
      else if profile == llamacpp-gpu-offload and kv_offload_supported:
          kv_placements = ["vram", "ram"]
      else:
          kv_placements = ["ram"]

      for kv_placement in kv_placements:

        # Filter and sort batch candidates based on preset caps
        batch_candidates = [v for v in batch_size_candidates if v <= preset_batch_caps[preset]]
        sort batch_candidates descending

        for batch_size in batch_candidates:

          # Filter and sort ubatch candidates based on batch_size and preset caps
          ubatch_candidates = [v for v in ubatch_size_candidates if v <= min(preset_ubatch_caps[preset], batch_size)]
          sort ubatch_candidates descending

          for ubatch_size in ubatch_candidates:

            # 1. Evaluate baseline single-request configuration (parallel = 1)
            kv_cache_mb_baseline = ctx_size_per_slot * 1 * kv_per_token_mb

            if profile == llamacpp-cpu:
                gpu_layers = 0
                place weights in RAM
                place KV cache in RAM

            else if profile == llamacpp-gpu-offload:
                if kv_placement == "vram":
                    reserve VRAM for baseline KV cache
                    place KV cache in VRAM
                else:
                    place KV cache in RAM

                reserve VRAM for runtime overhead
                calculate remaining VRAM for model weights
                calculate gpu_layers
                place remaining weights in RAM

            resolve threads based on available CPU

            estimate baseline RAM and VRAM requirements (for parallel = 1)

            if baseline candidate is NOT FIT:
                continue  # Try next parameter combination

            # 2. Baseline fits! Resolve concurrency (parallel slots) from leftover memory
            preset_parallel_cap = {
                speed: 2,
                balanced: 4,
                quality: 1
            }[preset]

            if kv_placement == "vram":
                leftover_memory_mb = (available_vram_primary_gpu_mb / headroom_multiplier) - required_vram_baseline_mb
            else:
                leftover_memory_mb = (effective_ram_available_mb / headroom_multiplier) - required_ram_baseline_mb

            per_slot_kv_mb = ctx_size_per_slot * kv_per_token_mb
            additional_slots = floor(leftover_memory_mb / per_slot_kv_mb)
            parallel = min(preset_parallel_cap, 1 + max(0, additional_slots))

            # 3. Recalculate final memory requirements with resolved parallel slots
            # Fallback loop: if final fit fails due to minor rounding/precision, decrement parallel down to 1
            while parallel >= 1:
                final_kv_cache_mb = ctx_size_per_slot * parallel * kv_per_token_mb
                if kv_placement == "vram":
                    place final KV cache in VRAM
                else:
                    place final KV cache in RAM
                estimate final RAM and VRAM requirements

                if final_config is FIT:
                    return resolved_config

                parallel -= 1

  return RUNTIME_CONFIG_UNRESOLVABLE
```

`RUNTIME_CONFIG_UNRESOLVABLE` is an installation error for the selected preset.
It does not rewrite the Stage 4 catalog `fit_status`.

The final resolved config must include:

```text
backend_profile
preset
ctx_size_per_slot
server_ctx_size
parallel
cache_type_k
cache_type_v
kv_cache_placement
gpu_layers
threads
batch_size
ubatch_size
required_ram_mb
required_vram_per_gpu_mb
fit_status
```

The persisted config also includes the complete RuntimePolicyInput and
integrity_sha256 so the selection and hardware basis remain auditable.

## Persistence and Activation

After parameter resolution:

1. write config schema version 7 to data/config/llm_runtime.json;
2. generate llama_rag.args or vllm_rag.args from the Stage 6 catalog snapshot;
3. start only the selected runtime service;
4. stop the inactive runtime service;
5. restart rag-worker;
6. verify the selected runtime health.

Argument generation must not look up the selected artifact again in the live
catalog.

> [!NOTE]
> `required_ram_mb` and `required_vram_per_gpu_mb` stored in the final configuration represent the **raw physical memory requirements (without safety headroom multiplier)**. The 25% safety headroom multiplier (1.25x) is applied only during hardware fit evaluation.

---

## Important Notes

### Context and parallel are coupled

Do not calculate context and parallel independently.

Memory should be estimated using:

```text
ctx_size_per_slot * parallel
```

because each active slot requires KV cache capacity.

### GPU layers and context compete for VRAM

For GPU offload:

```text
more gpu_layers -> more VRAM used by weights
larger ctx_size -> more VRAM or RAM used by KV cache
higher parallel -> more KV cache memory
```

Therefore, the planner may reduce `gpu_layers` to preserve context length, especially for the `quality` preset.

### Multi-GPU support

For `llamacpp-gpu-offload`, the planner estimates memory and offloading limits based on the primary GPU (`GPU 0`) only. It does not calculate or configure multi-GPU tensor splitting (`--tensor-split`) for llama.cpp. 

### Presets express trade-offs

```text
speed:
  prioritizes single-request speed (maximizing GPU layers and batch size). Caps parallel low (cap = 2) to minimize scheduler overhead and resource contention.

balanced:
  chooses stable defaults with moderate memory pressure and balanced concurrency (cap = 4).

quality:
  sacrifices concurrency (forces parallel = 1) to preserve maximum context length and full-precision cache.
```
