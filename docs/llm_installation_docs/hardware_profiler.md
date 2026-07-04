# Hardware Profiler

## Purpose

The Hardware Profiler collects server and container runtime information required for installation-time LLM fit evaluation.

It must not collect browser, client PC, document content, location, or personal usage data.

## Detection Scope

Stage 1 receives an explicit installation or reconfiguration request and produces
one `HardwareProfile`. The profile is the only hardware input to backend
selection, resource estimation, and fit evaluation.

`cluster_mode` and `configured_context_cap` are installation options. They are
not detected hardware and must not be stored as fields of `HardwareProfile`.

## HardwareProfile

Required fields:

| Field | Type | Meaning |
|---|---|---|
| `cpu_count` | integer | Logical CPU count available to the process. |
| `physical_cpu_cores` | integer or null | Detected physical CPU core count. |
| `cpu_model` | string or null | CPU model when detection succeeds. |
| `cpu_features` | list of strings | Detected instruction-set features. |
| `ram_mb` | integer | Host-visible total RAM. |
| `ram_available_mb` | integer | Host-visible currently available RAM. |
| `container_memory_limit_mb` | integer or null | Container memory limit; null means no finite limit was detected. |
| `effective_ram_available_mb` | integer | RAM pool used by fit evaluation. |
| `disk_total_mb` | integer | Total capacity of the filesystem containing `data/`. |
| `disk_free_mb` | integer | Free capacity of the filesystem containing `data/`. |
| `platform` | string | Server OS and architecture description. |
| `cuda_available` | boolean | Whether a usable CUDA execution path was detected. |
| `cuda_driver_version` | string or null | Installed NVIDIA driver version. |
| `driver_supported_cuda_version` | string or null | Highest CUDA version supported by the driver. |
| `gpu_probe_result` | `GpuProbeResult` | Canonical accelerator probe result. |
| `docker_available` | boolean | Whether Docker is callable. |
| `docker_compose_available` | boolean | Whether Docker Compose is callable. |
| `docker_services` | list of `DockerService` | Detected Compose services; empty when unavailable or none exist. |
| `llamacpp_kv_offload_supported` | boolean or null | Whether the selected llama.cpp build supports KV offload control. |
| `llamacpp_gpu_offload_supported` | boolean or null | Whether the installed llama.cpp build can use the detected accelerator backend. |
| `llamacpp_capability_source` | string or null | Evidence source for the llama.cpp capability result. |
| `probe_warnings` | list of strings | Non-fatal detection failures and unavailable measurements. |

`effective_ram_available_mb` is derived as follows:

```text
if container_memory_limit_mb is null:
    effective_ram_available_mb = ram_available_mb
else:
    effective_ram_available_mb = min(
        ram_available_mb,
        container_memory_limit_mb,
    )
```

Fit evaluation must use `effective_ram_available_mb`, not `ram_mb` or
`ram_available_mb` directly.

Each `DockerService` contains `name`, `service`, `state`, and `status` strings.
These values describe installation prerequisites and currently running runtime
services; they are not model-fit measurements.

## GPU Probe Result

`gpu_probe_result` contains:

- `backend`
  - `nvidia`
  - `rocm`
  - `torch`
  - `metal`
  - `none`
- `devices`

Each GPU device contains:

- `index`
- `name`
- `total_vram_mb`
- `free_vram_mb`
- `compute_capability` (nullable)
- `driver_version` (nullable)
- `uuid` (nullable)
- `pci_bus_id` (nullable)
- `pci_device_id` (nullable)
- `memory_clock_mhz` (nullable)
- `memory_bus_width_bits` (nullable)
- `bandwidth_gb_s` (nullable)
- `bandwidth_source`
- `bandwidth_confidence`

`devices` is the single source of truth for GPU count, names, VRAM, compute
capability, driver version, and bandwidth. Do not persist parallel aggregate
fields such as `has_gpu`, `gpu_count`, `gpu_name`, `gpu_vram_mb`,
`gpu_vram_free_mb`, `gpu_vram_list`, `gpu_vram_free_list`, `gpu_compute_cap`, or
`gpu_mem_bandwidth_gb_s` in `HardwareProfile`.

Consumers derive values when needed:

```text
devices = gpu_probe_result.devices
has_gpu = len(gpu_probe_result.devices) > 0
gpu_count = len(gpu_probe_result.devices)
gpu_vram_total_mb = sum(device.total_vram_mb for device in devices)
gpu_vram_free_total_mb = sum(device.free_vram_mb for device in devices)
```

Device order is ascending by `index`, and device indices must be unique.

## Unknown and Failed Measurements

Do not use `0` or an empty string to mean probe failure. A measurement that can
legitimately be unavailable uses null, and the reason is appended to
`probe_warnings`. Numeric zero is reserved for a successful measurement whose
value is actually zero.

Stage 1 must fail instead of producing a profile when any of these values cannot
be established:

- `cpu_count`
- `ram_mb`
- `ram_available_mb`
- `effective_ram_available_mb`
- `disk_free_mb`
- `gpu_probe_result.backend`
- `gpu_probe_result.devices`

Optional accelerator details such as memory bandwidth may be null. When a TPS
input is unavailable, Stage 3 returns null TPS with an estimation warning; it
must not invent a hardware measurement.

## Important Rule

Hardware probing is installation-time or explicit operator-command behavior.

Do not run the Hardware Profiler during normal RAG request processing.
