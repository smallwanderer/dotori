from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from time import monotonic

import requests


@dataclass(frozen=True)
class ServerRuntimeProfile:
    cpu_count: int
    ram_mb: int
    has_gpu: bool
    gpu_name: str
    gpu_vram_mb: int
    cuda_available: bool
    platform: str
    gpu_count: int = 0
    gpu_vram_list: list[int] | None = None
    disk_free_mb: int = 0
    container_memory_limit_mb: int = 0
    cuda_driver_version: str = ""
    driver_supported_cuda_version: str = ""
    docker_available: bool = False
    docker_compose_available: bool = False
    docker_services: list[dict] | None = None
    disk_total_mb: int = 0
    ram_available_mb: int = 0
    gpu_vram_free_mb: int = 0
    gpu_vram_free_list: list[int] | None = None
    gpu_compute_cap: str = ""
    gpu_mem_bandwidth_gb_s: float = 0.0
    cpu_model: str = ""
    cpu_features: list[str] | None = None
    gpu_probe_result: GpuProbeResult | None = None
    cluster_mode: bool = False
    llamacpp_kv_offload_supported: bool = True
    llamacpp_gpu_offload_supported: bool | None = None
    llamacpp_capability_source: str = "bundled-image-default"
    physical_cpu_cores: int = 0
    configured_context_cap: int = 16384


@dataclass(frozen=True)
class EndpointStatus:
    base_url: str
    ok: bool
    check_name: str = ""
    status_code: int | None = None
    message: str = ""
    elapsed_ms: int = 0
    model_ids: list[str] | None = None
    model_present: bool | None = None


@dataclass(frozen=True)
class SmokeTestStatus:
    base_url: str
    model: str
    ok: bool
    status_code: int | None = None
    message: str = ""
    elapsed_ms: int = 0


@dataclass
class GpuDeviceInfo:
    index: int
    name: str
    total_vram_mb: int
    free_vram_mb: int
    compute_capability: str
    driver_version: str

    uuid: str | None = None
    pci_bus_id: str | None = None
    pci_device_id: str | None = None

    memory_clock_mhz: float | None = None
    memory_bus_width_bits: int | None = None
    bandwidth_gb_s: float | None = None
    bandwidth_source: str = "unknown"
    bandwidth_confidence: str = "low"


@dataclass
class GpuProbeResult:
    backend: str
    gpu_count: int
    devices: list[GpuDeviceInfo]

    @property
    def total_vram_mb(self) -> int:
        return sum(g.total_vram_mb for g in self.devices)

    @property
    def free_vram_mb(self) -> int:
        return sum(g.free_vram_mb for g in self.devices)


def _read_system_ram_mb() -> tuple[int, int]:
    total_ram = 0
    available_ram = 0
    if platform.system() == "Windows":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            total_ram = int(status.ullTotalPhys / 1024 / 1024)
            available_ram = int(status.ullAvailPhys / 1024 / 1024)
            return total_ram, available_ram
        except Exception:
            pass
    
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as meminfo:
            for line in meminfo:
                if line.startswith("MemTotal:"):
                    total_ram = int(int(line.split()[1]) / 1024)
                elif line.startswith("MemAvailable:"):
                    available_ram = int(int(line.split()[1]) / 1024)
                elif line.startswith("MemFree:") and not available_ram:
                    available_ram = int(int(line.split()[1]) / 1024)
            return total_ram, available_ram
    except OSError:
        pass

    return 0, 0


def _probe_cpu_info() -> tuple[str, list[str]]:
    cpu_model = ""
    cpu_features = []
    try:
        if os.path.exists("/proc/cpuinfo"):
            with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
                for line in f:
                    if ":" in line:
                        key, val = [x.strip() for x in line.split(":", 1)]
                        if key.lower() in ("model name", "cpu"):
                            cpu_model = val
                        elif key.lower() in ("flags", "features"):
                            cpu_features = val.split()
    except Exception:
        pass
    
    if platform.system() == "Windows":
        if not cpu_model:
            try:
                res = subprocess.run(
                    ["wmic", "cpu", "get", "name"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=2,
                )
                if res.returncode == 0:
                    lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
                    if len(lines) > 1:
                        cpu_model = lines[1]
            except Exception:
                pass
        
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # 39: PF_AVX_INSTRUCTIONS_AVAILABLE
            # 40: PF_AVX2_INSTRUCTIONS_AVAILABLE
            # 41: PF_AVX512F_INSTRUCTIONS_AVAILABLE
            # 19: PF_ARM_NEON_INSTRUCTIONS_AVAILABLE
            if kernel32.IsProcessorFeaturePresent(39):
                cpu_features.append("avx")
            if kernel32.IsProcessorFeaturePresent(40):
                cpu_features.append("avx2")
            if kernel32.IsProcessorFeaturePresent(41):
                cpu_features.append("avx512f")
            if kernel32.IsProcessorFeaturePresent(19):
                cpu_features.append("neon")
        except Exception:
            pass
            
    return cpu_model, cpu_features


def _probe_physical_cpu_cores() -> int:
    if platform.system() == "Linux":
        core_ids: set[tuple[str, str]] = set()
        physical_id = "0"
        core_id = ""
        try:
            for line in open("/proc/cpuinfo", "r", encoding="utf-8"):
                if not line.strip():
                    if core_id:
                        core_ids.add((physical_id, core_id))
                    physical_id, core_id = "0", ""
                elif line.startswith("physical id"):
                    physical_id = line.split(":", 1)[1].strip()
                elif line.startswith("core id"):
                    core_id = line.split(":", 1)[1].strip()
            if core_id:
                core_ids.add((physical_id, core_id))
        except OSError:
            pass
        if core_ids:
            return len(core_ids)
    commands = (
        ["sysctl", "-n", "hw.physicalcpu"],
        ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum"],
    )
    for command in commands:
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            value = int(result.stdout.strip())
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if result.returncode == 0 and value > 0:
            return value
    return os.cpu_count() or 1


import platform
import re
import subprocess
from typing import Any


KNOWN_GPU_BANDWIDTH_BY_NAME_VRAM: dict[tuple[str, int | None], float] = {
    ("NVIDIA L4", 24): 300.0,
    ("NVIDIA A10", 24): 600.0,
    ("NVIDIA A10G", 24): 600.0,
    ("NVIDIA A40", 48): 696.0,
    ("NVIDIA RTX 3090", 24): 936.0,
    ("NVIDIA RTX 4090", 24): 1008.0,
    ("NVIDIA A100-PCIE-40GB", 40): 1555.0,
    ("NVIDIA A100-SXM4-80GB", 80): 2039.0,
    ("NVIDIA H100 PCIe", 80): 2000.0,
    ("NVIDIA H100 80GB HBM3", 80): 3350.0,
}

def _parse_int(value: str, default: int = 0) -> int:
    """
    Parses values like:
    - "24576"
    - "24576 MiB"
    - "[Not Supported]"
    - "N/A"
    """
    if not value:
        return default

    value = value.strip()
    if value in {"N/A", "[N/A]", "[Not Supported]", "Not Supported"}:
        return default

    match = re.search(r"-?\d+", value)
    if not match:
        return default

    try:
        return int(match.group(0))
    except ValueError:
        return default


def _parse_float(value: str, default: float = 0.0) -> float:
    if not value:
        return default

    value = value.strip()
    if value in {"N/A", "[N/A]", "[Not Supported]", "Not Supported"}:
        return default

    match = re.search(r"-?\d+(\.\d+)?", value)
    if not match:
        return default

    try:
        return float(match.group(0))
    except ValueError:
        return default

def _parse_rocm_memory_mb(line: str) -> int:
    raw = line.rsplit(":", 1)[-1].strip()
    match = re.search(r"\d+", raw)
    if not match:
        return 0

    value = int(match.group(0))
    lower = line.lower()

    if "(b)" in lower or "bytes" in lower:
        return int(value / 1024 / 1024)
    if "kib" in lower or "kb" in lower:
        return int(value / 1024)
    if "gib" in lower or "gb" in lower:
        return int(value * 1024)

    # rocm-smi 출력이 명확하지 않으면 MB로 간주
    return value

def _normalize_gpu_name(name: str) -> str:
    return " ".join(name.strip().split())


def _normalize_vram_gb(total_vram_mb: int | None) -> int | None:
    if not total_vram_mb:
        return None
    return round(total_vram_mb / 1024)


# TODO: pci.device_id 사용 고려
def _lookup_gpu_bandwidth_gb_s(
    gpu_name: str,
    total_vram_mb: int | None = None,
) -> float | None:
    """
    1순위: 정확한 이름 + VRAM 매칭
    2순위: 정확한 이름 매칭
    3순위: 부분 매칭 (VRAM이 다를 경우)
    """
    normalized = _normalize_gpu_name(gpu_name)
    vram_gb = _normalize_vram_gb(total_vram_mb)

    # 1. name + VRAM exact
    value = KNOWN_GPU_BANDWIDTH_BY_NAME_VRAM.get((normalized, vram_gb))
    if value is not None:
        return value

    # 2. name only exact
    for (known_name, _known_vram), bandwidth in KNOWN_GPU_BANDWIDTH_BY_NAME_VRAM.items():
        if known_name == normalized:
            return bandwidth

    # 3. partial fallback
    for (known_name, known_vram), bandwidth in KNOWN_GPU_BANDWIDTH_BY_NAME_VRAM.items():
        if known_vram is not None and vram_gb is not None and known_vram != vram_gb:
            continue
        if known_name in normalized or normalized in known_name:
            return bandwidth

    return None

def _estimate_bandwidth_gb_s(memory_clock_mhz: float, bus_width_bits: float) -> float:
    """
    Approximate theoretical memory bandwidth.

    bandwidth GB/s
    = memory_clock_MHz * 1e6 * 2 transfers/cycle * bus_width_bits / 8 / 1e9

    정리하면:
    = memory_clock_MHz * 2 * bus_width_bits / 8 / 1000
    """
    if memory_clock_mhz <= 0 or bus_width_bits <= 0:
        return 0.0

    return memory_clock_mhz * 2.0 * bus_width_bits / 8.0 / 1000.0


def _run_nvidia_smi_query(fields: list[str], timeout: int = 2) -> subprocess.CompletedProcess[str]:
    """
    NVIDIA SMI를 사용하여 GPU 정보를 쿼리
    """
    return subprocess.run(
        [
            "nvidia-smi",
            f"--query-gpu={','.join(fields)}",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _probe_nvidia_gpu_info() -> GpuProbeResult | None:
    """
    NVIDIA GPU 상세 정보 수집
    - index             : GPU 인덱스
    - name              : GPU 모델명
    - uuid              : GPU 고유 ID
    - pci.bus_id        : PCI 버스 ID
    - pci.device_id     : PCI 장치 ID
    - memory.total      : 총 VRAM
    - memory.free       : 사용 가능한 VRAM
    - compute_cap       : Compute Capability
    - clocks.max.memory : 최대 메모리 클럭
    - memory.bus_width  : 메모리 버스 폭
    - driver_version    : 드라이버 버전
    """
    
    query_with_bus_width = [
        "index",
        "name",
        "uuid",
        "pci.bus_id",
        "pci.device_id",
        "memory.total",
        "memory.free",
        "compute_cap",
        "clocks.max.memory",
        "memory.bus_width",
        "driver_version",
    ]

    query_without_bus_width = [
        "index",
        "name",
        "uuid",
        "pci.bus_id",
        "pci.device_id",
        "memory.total",
        "memory.free",
        "compute_cap",
        "clocks.max.memory",
        "driver_version",
    ]

    result = _run_nvidia_smi_query(query_with_bus_width)

    has_bus_width = True
    if result.returncode != 0 or not result.stdout.strip():
        result = _run_nvidia_smi_query(query_without_bus_width)
        has_bus_width = False

    if result.returncode != 0 or not result.stdout.strip():
        return None

    devices = []
    lines = result.stdout.strip().splitlines()

    for idx, line in enumerate(lines):
        parts = [part.strip() for part in line.split(",")]

        gpu_index = _parse_int(parts[0] if len(parts) > 0 else "", default=idx)
        name = parts[1] if len(parts) > 1 else ""
        uuid = parts[2] if len(parts) > 2 else ""
        pci_bus_id = parts[3] if len(parts) > 3 else ""
        pci_device_id = parts[4] if len(parts) > 4 else ""
        tot_vram = _parse_int(parts[5] if len(parts) > 5 else "")
        fr_vram = _parse_int(parts[6] if len(parts) > 6 else "")
        cc = parts[7].strip() if len(parts) > 7 else ""
        clock = _parse_float(parts[8] if len(parts) > 8 else "")

        if has_bus_width:
            bus_width = _parse_float(parts[9] if len(parts) > 9 else "")
            drv = parts[10].strip() if len(parts) > 10 else ""
        else:
            bus_width = 0.0
            drv = parts[9].strip() if len(parts) > 9 else ""

        # bandwidth resolver
        lookup_bandwidth = _lookup_gpu_bandwidth_gb_s(name, tot_vram)
        
        if lookup_bandwidth is not None:
            bw = lookup_bandwidth
            bandwidth_source = "known_gpu_table"
            bandwidth_confidence = "high"
        elif clock > 0 and bus_width > 0:
            bw = _estimate_bandwidth_gb_s(clock, bus_width)
            bandwidth_source = "nvidia_smi_estimate"
            bandwidth_confidence = "medium"
        else:
            bw = 0.0
            bandwidth_source = "unknown"
            bandwidth_confidence = "low"

        devices.append(GpuDeviceInfo(
            index=gpu_index,
            name=name,
            total_vram_mb=tot_vram,
            free_vram_mb=fr_vram,
            compute_capability=cc,
            driver_version=drv,
            uuid=uuid if uuid else None,
            pci_bus_id=pci_bus_id if pci_bus_id else None,
            pci_device_id=pci_device_id if pci_device_id else None,
            memory_clock_mhz=clock if clock > 0 else None,
            memory_bus_width_bits=int(bus_width) if bus_width > 0 else None,
            bandwidth_gb_s=bw if bw > 0 else None,
            bandwidth_source=bandwidth_source,
            bandwidth_confidence=bandwidth_confidence,
        ))

    if not devices:
        return None

    return GpuProbeResult(backend="nvidia", gpu_count=len(devices), devices=devices)


def _probe_amdgpu_driver_version() -> str:
    try:
        if os.path.exists("/sys/module/amdgpu/version"):
            with open("/sys/module/amdgpu/version", "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def _probe_gpu_info() -> GpuProbeResult:
    # 1. NVIDIA
    try:
        nvidia_info = _probe_nvidia_gpu_info()
        if nvidia_info is not None:
            return nvidia_info
    except Exception:
        pass

    # 2. AMD ROCm
    try:
        res = subprocess.run(
            ["which", "rocm-smi"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )

        if res.returncode == 0:
            res_mem = subprocess.run(
                ["rocm-smi", "--showmeminfo", "vram"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )

            if res_mem.returncode == 0:
                pending_totals: list[int] = []
                pending_frees: list[int] = []

                for line in res_mem.stdout.splitlines():
                    if "VRAM Total Memory" in line:
                        tot_mb = _parse_rocm_memory_mb(line)
                        pending_totals.append(tot_mb)
                    elif "VRAM Used Memory" in line:
                        used_mb = _parse_rocm_memory_mb(line)
                        pending_frees.append(used_mb)

                res_name = subprocess.run(
                    ["rocm-smi", "--showproductname"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=2,
                )
                gpu_name = "AMD GPU"
                if res_name.returncode == 0:
                    for line in res_name.stdout.splitlines():
                        if "Card Series" in line or "GPU[" in line:
                            gpu_name = line.split(":")[-1].strip()
                            break

                devices = []
                amd_driver = _probe_amdgpu_driver_version()
                for idx, tot_mb in enumerate(pending_totals):
                    used_mb = pending_frees[idx] if idx < len(pending_frees) else 0
                    free_mb = max(0, tot_mb - used_mb)
                    devices.append(GpuDeviceInfo(
                        index=idx,
                        name=gpu_name,
                        total_vram_mb=tot_mb,
                        free_vram_mb=free_mb,
                        compute_capability="gfx",
                        driver_version=amd_driver,
                        bandwidth_gb_s=None,
                        bandwidth_source="unknown",
                        bandwidth_confidence="low",
                    ))
                
                if devices:
                    return GpuProbeResult(backend="rocm", gpu_count=len(devices), devices=devices)

    except Exception:
        pass

    # 3. PyTorch CUDA fallback
    try:
        import torch

        if torch.cuda.is_available():
            gpu_count = torch.cuda.device_count()
            gpu_name = torch.cuda.get_device_name(0)
            devices = []
            major, minor = torch.cuda.get_device_capability(0)
            cc = f"{major}.{minor}"

            for i in range(gpu_count):
                gpu_name = torch.cuda.get_device_name(i)
                major, minor = torch.cuda.get_device_capability(i)
                cc = f"{major}.{minor}"

                try:
                    free_bytes, total_bytes = torch.cuda.mem_get_info(i)
                    tot_mb = int(total_bytes / 1024 / 1024)
                    free_mb = int(free_bytes / 1024 / 1024)
                except Exception:
                    prop = torch.cuda.get_device_properties(i)
                    tot_mb = int(prop.total_memory / 1024 / 1024)
                    free_mb = 0

                lookup_bandwidth = _lookup_gpu_bandwidth_gb_s(gpu_name, tot_mb)

                devices.append(
                    GpuDeviceInfo(
                        index=i,
                        name=gpu_name,
                        total_vram_mb=tot_mb,
                        free_vram_mb=free_mb,
                        compute_capability=cc,
                        driver_version="",
                        bandwidth_gb_s=lookup_bandwidth,
                        bandwidth_source="lookup" if lookup_bandwidth else "unknown",
                        bandwidth_confidence="high" if lookup_bandwidth else "low",
                    )
                )

            if devices:
                return GpuProbeResult(backend="torch", gpu_count=len(devices), devices=devices)

    except Exception:
        pass

    # 4. macOS Metal fallback
    if platform.system() == "Darwin":
        try:
            res = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )

            if res.returncode == 0:
                gpu_name = "Apple GPU"
                for line in res.stdout.splitlines():
                    if "Chipset Model" in line:
                        gpu_name = line.split(":")[-1].strip()
                        break

                lookup_bandwidth = _lookup_gpu_bandwidth_gb_s(gpu_name)
                device = GpuDeviceInfo(
                    index=0,
                    name=gpu_name,
                    total_vram_mb=0,
                    free_vram_mb=0,
                    compute_capability="metal",
                    driver_version="",
                    bandwidth_gb_s=lookup_bandwidth,
                    bandwidth_source="lookup" if lookup_bandwidth else "unknown",
                    bandwidth_confidence="high" if lookup_bandwidth else "low",
                )
                return GpuProbeResult(backend="metal", gpu_count=1, devices=[device])
        except Exception:
            pass

    return GpuProbeResult(backend="none", gpu_count=0, devices=[])


def _probe_driver_supported_cuda_version() -> str:
    """ CUDA compatibility version """
    try:
        result = subprocess.run(
            ["nvidia-smi"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""

    if result.returncode != 0:
        return ""

    match = re.search(r"CUDA Version:\s*([0-9]+(?:\.[0-9]+)*)", result.stdout)
    return match.group(1) if match else ""


def _read_container_memory_limit_mb() -> int:
    candidates = [
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ]
    for candidate in candidates:
        try:
            raw_value = open(candidate, "r", encoding="utf-8").read().strip()
        except OSError:
            continue
        if not raw_value or raw_value == "max":
            continue
        try:
            value = int(raw_value)
        except ValueError:
            continue
        if value <= 0 or value >= 1 << 60:
            continue
        return int(value / 1024 / 1024)
    return 0


def _command_available(command: list[str]) -> bool:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _probe_llamacpp_kv_offload() -> tuple[bool, str]:
    commands = []
    local_server = shutil.which("llama-server")
    if local_server:
        commands.append(([local_server, "--help"], "local-llama-server"))
    commands.append(
        (
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "llama-rag",
                "/app/llama-server",
                "--help",
            ],
            "running-llama-rag-service",
        )
    )
    for command, source in commands:
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        output = f"{result.stdout}\n{result.stderr}"
        if result.returncode == 0 and "--no-kv-offload" in output:
            return True, source
        if result.returncode == 0:
            return False, source
    return True, "bundled-image-default"


def _probe_docker_services() -> list[dict]:
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    if result.returncode != 0 or not result.stdout.strip():
        return []

    services = []
    raw_output = result.stdout.strip()
    try:
        parsed = json.loads(raw_output)
        if isinstance(parsed, list):
            candidates = parsed
        elif isinstance(parsed, dict):
            candidates = [parsed]
        else:
            candidates = []
    except json.JSONDecodeError:
        candidates = []
        for line in raw_output.splitlines():
            try:
                parsed_line = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed_line, dict):
                candidates.append(parsed_line)

    for service in candidates:
        if not isinstance(service, dict):
            continue
        services.append(
            {
                "name": service.get("Name") or service.get("name") or "",
                "service": service.get("Service") or service.get("service") or "",
                "state": service.get("State") or service.get("state") or "",
                "status": service.get("Status") or service.get("status") or "",
            }
        )
    return services


def _disk_usage_mb(path: str = "/data") -> tuple[int, int]:
    target = path if os.path.exists(path) else "."
    try:
        usage = shutil.disk_usage(target)
        return int(usage.total / 1024 / 1024), int(usage.free / 1024 / 1024)
    except OSError:
        return 0, 0


def probe_server_runtime() -> ServerRuntimeProfile:
    gpu_res = _probe_gpu_info()
    has_gpu = gpu_res.gpu_count > 0
    if has_gpu:
        first_gpu = gpu_res.devices[0]
        gpu_name = first_gpu.name
        total_vram = gpu_res.total_vram_mb
        free_vram = gpu_res.free_vram_mb
        gpu_count = gpu_res.gpu_count
        total_vram_list = [g.total_vram_mb for g in gpu_res.devices]
        free_vram_list = [g.free_vram_mb for g in gpu_res.devices]
        compute_cap = first_gpu.compute_capability
        bandwidth = first_gpu.bandwidth_gb_s or 0.0
        driver_version = first_gpu.driver_version
    else:
        gpu_name = ""
        total_vram = 0
        free_vram = 0
        gpu_count = 0
        total_vram_list = []
        free_vram_list = []
        compute_cap = ""
        bandwidth = 0.0
        driver_version = ""

    cpu_model, cpu_features = _probe_cpu_info()
    physical_cpu_cores = _probe_physical_cpu_cores()
    total_ram, available_ram = _read_system_ram_mb()
    total_disk, free_disk = _disk_usage_mb()
    
    docker_available = _command_available(["docker", "--version"])
    docker_compose_available = _command_available(["docker", "compose", "version"])
    
    kv_offload_supported, capability_source = _probe_llamacpp_kv_offload()
    return ServerRuntimeProfile(
        cpu_count=os.cpu_count() or 1,
        physical_cpu_cores=physical_cpu_cores,
        ram_mb=total_ram,
        ram_available_mb=available_ram,
        has_gpu=has_gpu,
        gpu_name=gpu_name,
        gpu_vram_mb=total_vram,
        gpu_vram_free_mb=free_vram,
        gpu_count=gpu_count,
        gpu_vram_list=total_vram_list,
        gpu_vram_free_list=free_vram_list,
        gpu_compute_cap=compute_cap,
        gpu_mem_bandwidth_gb_s=bandwidth,
        cuda_available=gpu_res.backend in {"nvidia", "torch"},
        platform=platform.platform(),
        disk_free_mb=free_disk,
        disk_total_mb=total_disk,
        llamacpp_kv_offload_supported=kv_offload_supported,
        llamacpp_capability_source=capability_source,
        cpu_model=cpu_model,
        cpu_features=cpu_features,
        container_memory_limit_mb=_read_container_memory_limit_mb(),
        cuda_driver_version=driver_version,
        driver_supported_cuda_version=(
            _probe_driver_supported_cuda_version()
            if gpu_res.backend in {"nvidia", "torch"} else ""
        ),
        docker_available=docker_available,
        docker_compose_available=docker_compose_available,
        docker_services=_probe_docker_services() if docker_compose_available else [],
        gpu_probe_result=gpu_res,
    )


def _elapsed_ms(started_at: float) -> int:
    return int((monotonic() - started_at) * 1000)


def _normalize_base_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3].rstrip("/")
    return normalized


def check_endpoint_health(base_url: str, *, timeout: int = 2) -> EndpointStatus:
    normalized = _normalize_base_url(base_url)
    if not normalized:
        return EndpointStatus(base_url="", ok=False, check_name="health", message="No endpoint URL configured.")

    started_at = monotonic()
    try:
        response = requests.get(f"{normalized}/health", timeout=timeout)
    except requests.RequestException as exc:
        return EndpointStatus(
            base_url=normalized,
            ok=False,
            check_name="health",
            message=str(exc)[:240],
            elapsed_ms=_elapsed_ms(started_at),
        )

    if 200 <= response.status_code < 300:
        return EndpointStatus(
            base_url=normalized,
            ok=True,
            check_name="health",
            status_code=response.status_code,
            message="ok",
            elapsed_ms=_elapsed_ms(started_at),
        )
    return EndpointStatus(
        base_url=normalized,
        ok=False,
        check_name="health",
        status_code=response.status_code,
        message=response.text[:240],
        elapsed_ms=_elapsed_ms(started_at),
    )


def check_openai_compatible_endpoint(
    base_url: str,
    *,
    expected_model: str = "",
    timeout: int = 2,
) -> EndpointStatus:
    normalized = _normalize_base_url(base_url)
    if not normalized:
        return EndpointStatus(base_url="", ok=False, check_name="models", message="No endpoint URL configured.")

    started_at = monotonic()
    try:
        response = requests.get(f"{normalized}/v1/models", timeout=timeout)
    except requests.RequestException as exc:
        return EndpointStatus(
            base_url=normalized,
            ok=False,
            check_name="models",
            message=str(exc)[:240],
            elapsed_ms=_elapsed_ms(started_at),
        )

    elapsed_ms = _elapsed_ms(started_at)
    if not (200 <= response.status_code < 300):
        return EndpointStatus(
            base_url=normalized,
            ok=False,
            check_name="models",
            status_code=response.status_code,
            message=response.text[:240],
            elapsed_ms=elapsed_ms,
        )

    try:
        payload = response.json()
    except ValueError:
        return EndpointStatus(
            base_url=normalized,
            ok=False,
            check_name="models",
            status_code=response.status_code,
            message="Model list response was not JSON.",
            elapsed_ms=elapsed_ms,
        )

    models = payload.get("data", []) if isinstance(payload, dict) else []
    model_ids = [
        item.get("id")
        for item in models
        if isinstance(item, dict) and item.get("id")
    ]
    model_present = expected_model in model_ids if expected_model else None
    ok = bool(model_ids) and (model_present is not False)
    if model_present is False:
        message = f"Model '{expected_model}' was not listed by endpoint."
    elif model_ids:
        message = f"Endpoint listed {len(model_ids)} model(s)."
    else:
        message = "Endpoint returned an empty model list."
    return EndpointStatus(
        base_url=normalized,
        ok=ok,
        check_name="models",
        status_code=response.status_code,
        message=message,
        elapsed_ms=elapsed_ms,
        model_ids=model_ids[:20],
        model_present=model_present,
    )


def smoke_test_chat_completion(
    base_url: str,
    model: str,
    *,
    timeout: int = 10,
) -> SmokeTestStatus:
    normalized = _normalize_base_url(base_url)
    if not normalized or not model:
        return SmokeTestStatus(
            base_url=normalized,
            model=model,
            ok=False,
            message="Missing endpoint URL or model.",
        )

    started_at = monotonic()
    try:
        response = requests.post(
            f"{normalized}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 4,
                "temperature": 0,
                "stream": False,
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return SmokeTestStatus(
            base_url=normalized,
            model=model,
            ok=False,
            message=str(exc)[:240],
            elapsed_ms=_elapsed_ms(started_at),
        )

    return SmokeTestStatus(
        base_url=normalized,
        model=model,
        ok=200 <= response.status_code < 300,
        status_code=response.status_code,
        message="ok" if 200 <= response.status_code < 300 else response.text[:240],
        elapsed_ms=_elapsed_ms(started_at),
    )
