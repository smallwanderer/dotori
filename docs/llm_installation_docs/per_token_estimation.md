# Decode TPS Estimation

## Purpose

Stage 3 estimates output tokens per second for one active baseline request. It
is advisory catalog data, not a fit criterion or performance guarantee.

```text
baseline_concurrency = 1
estimated_decode_tps = round(raw_decode_tps, 1)
```

The estimator consumes `HardwareProfile`, `RAGModelCatalogEntry`,
`BackendResolution`, `MemoryEstimate`, and `MemoryPlacement`.

## Shared Weight Read Size

```text
effective_weight_read_gb =
    (model_weights_mb / 1024) * quant_runtime_penalty
```

| Quantization | Penalty |
|---|---:|
| `f32`, `fp32`, `f16`, `fp16`, `bf16` | 1.00 |
| `q8_0` | 1.05 |
| `q6_k` | 1.10 |
| `q5_k_m` | 1.12 |
| `q4_k_m`, `q4_0` | 1.15 |
| `q3_k_m` | 1.22 |
| `q2_k` | 1.30 |
| AWQ/GPTQ INT4 | 1.15 |
| unknown | 1.50 |

## GPU Decode

Only devices listed in `memory_placement.device_indices` participate.

```text
participating_devices = [
    device_by_index[index]
    for index in memory_placement.device_indices
]

bottleneck_bandwidth_gb_s = min(
    device.bandwidth_gb_s
    for device in participating_devices
)

gpu_only_tps = (
    bottleneck_bandwidth_gb_s
    / (effective_weight_read_gb / len(participating_devices))
    * tp_communication_factor
)
```

`tp_communication_factor` is `1.0` for one GPU and `0.85` for two or more GPUs.
This is an explicit heuristic constant, not a detected hardware property.

If any participating device has null or non-positive bandwidth, GPU TPS is null
and `TPS_BANDWIDTH_UNAVAILABLE` is added to estimation warnings.

## CPU Decode

```text
estimated_physical_cores = (
    physical_cpu_cores
    if physical_cpu_cores is not null
    else max(1, floor(cpu_count / 2))
)

cpu_only_tps = (
    estimated_physical_cores
    * cpu_feature_factor
    * cpu_quant_factor
    * 3.0
) / parameter_count_b
```

CPU feature factor uses the first matching feature in this order:

| Feature | Factor |
|---|---:|
| `amx` | 1.35 |
| `avx512` | 1.25 |
| `avx2` | 1.00 |
| `neon` | 0.80 |
| default | 0.60 |

```text
cpu_quant_factor = clamp(
    sqrt(0.58 / bytes_per_parameter),
    minimum=0.45,
    maximum=1.35,
)
```

When physical cores are estimated from logical cores,
`TPS_PHYSICAL_CORES_ESTIMATED` is added to warnings.

## Backend Result

- `llamacpp-cpu`: use `cpu_only_tps`.
- `vllm-cuda`: use `gpu_only_tps`.
- `llamacpp-gpu-offload`: interpolate using the Stage 3 weight placement.

```text
gpu_weight_ratio =
    sum(weight_vram_per_gpu_mb) / model_weights_mb

partial_offload_tps = 1 / (
    (gpu_weight_ratio / gpu_only_tps)
    + ((1 - gpu_weight_ratio) / cpu_only_tps)
)
```

If either required endpoint estimate is null, the partial-offload estimate is
also null. A null TPS does not change Stage 4 FIT status.

## Output

```text
PerformanceEstimate:
  estimated_decode_tps: number or null
  confidence: medium | low | unavailable
  warnings: list[string]
```

GPU bandwidth estimates have medium confidence. CPU and partial-offload
estimates have low confidence. Missing required measurements produce
`confidence = "unavailable"`.
