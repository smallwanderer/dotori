# Stage 3 Resource Estimation

## Purpose

Stage 3 converts an available, fixed `BackendResolution` into baseline memory,
placement, and decode-performance estimates. It does not select a backend and
does not assign FIT status.

## Input

```text
HardwareProfile
RAGModelCatalogEntry
BackendResolution where backend_status == AVAILABLE
```

Stage 3 does not run for an unavailable backend.

## Baseline

```text
baseline_context_length = min(model_metadata.max_context_length, 4096)
baseline_concurrency = 1
baseline_kv_cache_dtype_bytes = 2
headroom_multiplier = not applied in Stage 3
```

The baseline is independent of the operator's `speed`, `balanced`, or `quality`
preset. Stage 7 resolves preset-specific runtime parameters after selection.

## Output

```text
ResourceEstimation:
  estimation_status: ESTIMATED | FAILED
  baseline_context_length: integer
  baseline_concurrency: 1
  memory_estimate: MemoryEstimate or null
  memory_placement: MemoryPlacement or null
  performance_estimate: PerformanceEstimate or null
  disk_required_mb: integer or null
  failure_code: string or null
  warnings: list[string]

MemoryPlacement:
  device_indices: list[integer]
  tensor_parallel_size: integer
  gpu_layers: integer or null
  weight_ram_mb: integer
  weight_vram_per_gpu_mb: list[integer]
  kv_cache_ram_mb: integer
  kv_cache_vram_per_gpu_mb: list[integer]
  required_ram_mb: integer
  required_vram_per_gpu_mb: list[integer]
```

Per-GPU lists have exactly `len(gpu_probe_result.devices)` entries and align by
the device list's ascending index order. Unused devices contain zero. Every
`device_indices` value must identify a participating device.

```text
device_position_by_index = {
    device.index: position
    for position, device in enumerate(gpu_probe_result.devices)
}
```

Actual device indices are identities, not guaranteed list offsets.

`FAILED` requires null estimate, placement, performance, and disk requirement
fields. Stage 4 maps it to `NOFIT` using `failure_code`.

`failure_code` is one of:

- `INVALID_MODEL_METADATA`
- `MEMORY_ESTIMATION_FAILED`
- `NO_PARTICIPATING_DEVICE`
- `MEMORY_PLACEMENT_FAILED`

## Disk Requirement

Stage 3 resolves the catalog's raw artifact disk value before Stage 4:

```text
disk_multiplier = {
    gguf: 1.10,
    awq: 1.50,
    gptq: 1.50,
    safetensors: 1.50,
}[artifact.format]

minimum_disk_required_mb = ceil(
    artifact.download_size_mb * disk_multiplier
)

disk_required_mb = max(
    artifact.disk_required_mb,
    minimum_disk_required_mb,
)
```

The resolved value is always positive. Stage 4 must not read
`artifact.disk_required_mb` directly.

## Placement Rules

### llama.cpp CPU

```text
device_indices = []
tensor_parallel_size = 1
gpu_layers = 0
weight_ram_mb = model_weights_mb
weight_vram_per_gpu_mb = zeros(len(gpu_probe_result.devices))
kv_cache_ram_mb = baseline_kv_cache_mb
kv_cache_vram_per_gpu_mb = zeros(len(gpu_probe_result.devices))
required_ram_mb =
    weight_ram_mb + kv_cache_ram_mb + cpu_runtime_overhead_mb
required_vram_per_gpu_mb = zeros(len(gpu_probe_result.devices))
```

### vLLM CUDA

Use every CUDA device, ordered by device index. Stage 2 guarantees at least one.

```text
cuda_device_indices = [
    device.index
    for device in gpu_probe_result.devices
]  # gpu_probe_result is CUDA-capable when cuda_available == true

device_indices = cuda_device_indices
tensor_parallel_size = len(device_indices)
gpu_layers = null
weight_ram_mb = 0
kv_cache_ram_mb = 0

weight_vram_per_gpu_mb =
    split_evenly_mb(model_weights_mb, device_indices)
kv_cache_vram_per_gpu_mb =
    split_evenly_mb(baseline_kv_cache_mb, device_indices)

required_ram_mb = cpu_runtime_overhead_mb
required_vram_per_gpu_mb[i] =
    weight_vram_per_gpu_mb[i]
    + kv_cache_vram_per_gpu_mb[i]
    + gpu_runtime_overhead_mb_per_gpu
    + cuda_context_overhead_mb_per_gpu
```

`split_evenly_mb(total_mb, device_indices)` returns integer per-device values
whose sum is exactly `total_mb`: each device receives `floor(total_mb / n)` and
the first `total_mb % n` devices receive one additional MiB. Unused devices
receive zero.

Stage 3 does not add extra GPUs in response to a fit failure. The deterministic
all-CUDA-device group prevents Stage 4 from silently changing placement.

### llama.cpp GPU Offload

Use the lowest-index detected device only. Multi-GPU tensor splitting is not
part of the initial contract.

Baseline KV cache is placed in VRAM when
`llamacpp_kv_offload_supported == true`; otherwise it is placed in RAM.

```text
primary_gpu = devices[0]
device_indices = [primary_gpu.index]
tensor_parallel_size = 1
per_layer_weight_mb = model_weights_mb / num_hidden_layers

if llamacpp_kv_offload_supported == true:
    kv_cache_ram_mb = 0
    kv_cache_vram_mb = baseline_kv_cache_mb
else:
    kv_cache_ram_mb = baseline_kv_cache_mb
    kv_cache_vram_mb = 0

fixed_vram_mb =
    gpu_runtime_overhead_mb_per_gpu
    + accelerator_context_overhead_mb
    + kv_cache_vram_mb

weight_budget_mb = max(0, primary_gpu.free_vram_mb - fixed_vram_mb)
calculated_gpu_layers = floor(weight_budget_mb / per_layer_weight_mb)
gpu_layers = clamp(calculated_gpu_layers, minimum=1, maximum=num_hidden_layers)

weight_vram_mb = ceil(gpu_layers * per_layer_weight_mb)
weight_ram_mb = max(0, model_weights_mb - weight_vram_mb)
weight_vram_per_gpu_mb = zeros(len(devices))
primary_gpu_position = device_position_by_index[primary_gpu.index]
weight_vram_per_gpu_mb[primary_gpu_position] = weight_vram_mb
kv_cache_vram_per_gpu_mb = zeros(len(devices))
kv_cache_vram_per_gpu_mb[primary_gpu_position] = kv_cache_vram_mb

required_ram_mb =
    weight_ram_mb + kv_cache_ram_mb + cpu_runtime_overhead_mb
required_vram_primary_gpu_mb = fixed_vram_mb + weight_vram_mb
required_vram_per_gpu_mb = zeros(len(devices))
required_vram_per_gpu_mb[primary_gpu_position] = required_vram_primary_gpu_mb
```

The minimum of one layer is intentional. If even one layer does not fit, Stage
3 still reports its actual requirement and Stage 4 returns `NOFIT`; a
`llamacpp-gpu-offload` resolution must not degrade into CPU execution.

`accelerator_context_overhead_mb` is `1500` for CUDA and `0` for other
accelerator backends until a backend-specific non-CUDA constant is defined.

## Invariants

```text
model_weights_mb
    == weight_ram_mb + sum(weight_vram_per_gpu_mb)

baseline_kv_cache_mb
    == kv_cache_ram_mb + sum(kv_cache_vram_per_gpu_mb)

logical_total_memory_mb
    == required_ram_mb + sum(required_vram_per_gpu_mb)
```

All required memory values exclude Stage 4 safety headroom.
