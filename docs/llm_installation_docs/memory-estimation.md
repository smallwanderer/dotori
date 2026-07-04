# LLM Memory Estimation

## Purpose

This document defines the pool-neutral memory components calculated in Stage 3.
Placement into RAM and per-device VRAM is defined in
`stage3-resource-estimation.md`. FIT thresholds belong to Stage 4.

All memory values use MiB even though field names retain the established `_mb`
suffix. Intermediate values use full precision and stored component values are
rounded up with `ceil`.

## Baseline Inputs

```text
baseline_concurrency = 1
baseline_context_length = min(model_metadata.max_context_length, 4096)
baseline_kv_cache_dtype_bytes = 2
cpu_runtime_overhead_mb = 512
gpu_runtime_overhead_mb_per_gpu = 512
cuda_context_overhead_mb_per_gpu = 1500
```

The baseline uses an FP16-equivalent KV cache. Stage 7 may choose another cache
type, but it must perform its own final memory validation and must not rewrite
the catalog's baseline assessment.

Compute buffers are not added as a separate Stage 3 component because their
size depends on Stage 7 batch and runtime parameters. Stage 4's headroom
multiplier reserves capacity for these buffers. Do not apply that multiplier
inside Stage 3.

## Model Weights

For catalog metadata estimation:

```text
model_weights_mb = ceil(
    parameter_count_b * bytes_per_parameter * 1024
)
```

| Quantization | `bytes_per_parameter` |
|---|---:|
| `f32`, `fp32` | 4.00 |
| `f16`, `fp16`, `bf16` | 2.00 |
| `q8_0` | 1.05 |
| `q6_k` | 0.80 |
| `q5_k_m` | 0.68 |
| `q4_k_m`, `q4_0` | 0.58 |
| `q3_k_m` | 0.48 |
| `q2_k` | 0.37 |
| AWQ/GPTQ INT4 | 0.50 |

Unknown quantization uses `4.00` and adds an estimation warning. When exact
GGUF tensor metadata is available, its summed tensor byte size replaces the
catalog approximation and the result records `weight_source = "gguf-metadata"`.

## KV Cache

```text
kv_cache_mb_per_token = (
    num_hidden_layers
    * num_key_value_heads
    * head_dim
    * 2
    * baseline_kv_cache_dtype_bytes
) / (1024 * 1024)

kv_cache_mb = ceil(
    kv_cache_mb_per_token
    * baseline_context_length
    * baseline_concurrency
)
```

The factor `2` represents Key and Value. `head_dim` is
`model_metadata.head_dim` or, when divisible,
`hidden_size / num_attention_heads`. Missing `num_key_value_heads` means MHA and
uses `num_attention_heads`.

If the required dimensions remain invalid or unavailable, Stage 3 returns
`estimation_status = "FAILED"`. It must not use an unexplained empirical
constant for a catalog FIT decision.

## Pool-Neutral Result

```text
MemoryEstimate:
  model_weights_mb: integer
  kv_cache_mb_per_token: number
  baseline_kv_cache_mb: integer
  cpu_runtime_overhead_mb: 512
  gpu_runtime_overhead_mb_per_gpu: 512
  cuda_context_overhead_mb_per_gpu: 1500 or 0
  logical_total_memory_mb: integer
  weight_source: catalog | gguf-metadata
  warnings: list[string]
```

`cuda_context_overhead_mb_per_gpu` is `1500` only for a CUDA backend. It is zero
for CPU and non-CUDA accelerator backends.

`logical_total_memory_mb` is calculated after placement:

```text
logical_total_memory_mb =
    required_ram_mb + sum(required_vram_per_gpu_mb)
```

This combined value is for display only. Stage 4 evaluates RAM and each VRAM
pool separately.
