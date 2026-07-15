import os
import sys
import platform
import shutil
import json
import math
import subprocess

# ANSI escape codes for colors and formatting
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"

def print_header(title):
    print(f"\n{BOLD}{BLUE}=== {title} ==={RESET}\n")

def run_command(cmd, shell=True):
    try:
        p = subprocess.Popen(
            cmd,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        stdout, stderr = p.communicate()
        return p.returncode == 0, stdout, stderr
    except Exception:
        return False, "", ""

def format_mb(value):
    if not value:
        return "-"
    if value >= 1024:
        gb = value / 1024
        return f"{gb:.0f}GB" if gb.is_integer() else f"{gb:.1f}GB"
    return f"{value}MB"

def _fallback_detect_system_ram_mb():
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
            return int(status.ullTotalPhys / 1024 / 1024)
        except Exception:
            return 0
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as meminfo:
            for line in meminfo:
                if line.startswith("MemTotal:"):
                    return int(int(line.split()[1]) / 1024)
    except OSError:
        return 0
    return 0

def _fallback_detect_hardware():
    os_name = platform.system()
    cpu_count = os.cpu_count() or 1
    ram_mb = _fallback_detect_system_ram_mb()
    
    gpu_detected = False
    gpu_names = []
    gpu_vram_list = []
    gpu_count = 0
    nvidia_ok, nvidia_out, _ = run_command("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits")
    if nvidia_ok and nvidia_out:
        gpu_detected = True
        for line in nvidia_out.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            gpu_parts = [part.strip() for part in line.split(",")]
            if gpu_parts:
                gpu_names.append(gpu_parts[0])
                try:
                    vram = int(gpu_parts[1]) if len(gpu_parts) > 1 else 0
                except ValueError:
                    vram = 0
                gpu_vram_list.append(vram)
        gpu_count = len(gpu_names)
        gpu_name = gpu_names[0] if gpu_count > 0 else "None"
        gpu_vram_mb = sum(gpu_vram_list)
    else:
        gpu_name = "None"
        gpu_vram_mb = 0

    docker_ok, docker_ver, _ = run_command("docker --version")
    
    return {
        "os": os_name,
        "docker_installed": docker_ok,
        "gpu_detected": gpu_detected,
        "gpu_count": gpu_count,
        "gpu_name": gpu_name,
        "gpu_names": gpu_names,
        "gpu_vram_mb": gpu_vram_mb,
        "gpu_vram_list": gpu_vram_list,
        "ram_mb": ram_mb,
    }

def _fallback_load_llm_models():
    catalog_dir = os.path.join(
        "app",
        "llm_installation",
        "catalog",
    )
    model_dir = os.path.join(catalog_dir, "models")
    artifact_dir = os.path.join(catalog_dir, "artifacts")
    models = []
    try:
        model_filenames = sorted(
            os.path.join(root, name)
            for root, _, files in os.walk(model_dir)
            for name in files
            if name.endswith(".json")
        )
        artifact_filenames = sorted(
            os.path.join(root, name)
            for root, _, files in os.walk(artifact_dir)
            for name in files
            if name.endswith(".json")
        )
    except OSError:
        return []
    model_payloads = {}
    for filename in model_filenames:
        try:
            with open(filename, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not payload.get("id"):
            continue
        model_payloads[payload["id"]] = payload

    for filename in artifact_filenames:
        try:
            with open(filename, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not payload.get("id"):
            continue
        model_payload = model_payloads.get(payload.get("model_id"))
        if not model_payload:
            continue
        artifact = payload.get("artifact") or {}
        metadata = model_payload.get("model_metadata") or {}
        policy = payload.get("policy") or {}
        parameters = metadata.get("parameter_count_b")
        artifact_format = str(artifact.get("format") or "").lower()
        quant = str(artifact.get("quant") or artifact.get("dtype") or "f32").lower()
        bytes_per_parameter = {
            "f32": 4.0,
            "fp32": 4.0,
            "f16": 2.0,
            "fp16": 2.0,
            "bf16": 2.0,
            "q8_0": 1.05,
            "q6_k": 0.80,
            "q5_k_m": 0.68,
            "q4_k_m": 0.58,
            "q4_0": 0.58,
            "q3_k_m": 0.48,
            "q2_k": 0.37,
            "awq": 0.5,
            "awq-int4": 0.5,
            "gptq": 0.5,
            "gptq-int4": 0.5,
        }.get(quant, 4.0)
        if not isinstance(parameters, (int, float)) or parameters <= 0:
            continue
        weight_mb = round(parameters * bytes_per_parameter * 1024)
        context_length = min(4096, int(metadata.get("max_context_length") or 4096))
        layers = int(metadata.get("num_hidden_layers") or 0)
        kv_heads = int(metadata.get("num_key_value_heads") or 0)
        head_dim = int(metadata.get("head_dim") or 0)
        if not head_dim:
            hidden_size = int(metadata.get("hidden_size") or 0)
            attention_heads = int(metadata.get("num_attention_heads") or 0)
            if hidden_size and attention_heads:
                head_dim = hidden_size // attention_heads
        if layers and kv_heads and head_dim:
            kv_cache_mb = max(
                1,
                round(context_length * layers * kv_heads * head_dim * 2 * 2 / 1024 / 1024),
            )
        else:
            kv_cache_mb = max(1, round(0.000008 * context_length * (parameters or 0) * 1024))
        runtime_overhead_mb = 512
        runtime = "vllm" if artifact_format in {"awq", "gptq", "safetensors"} else "llama.cpp"
        device = "GPU" if runtime == "vllm" else "CPU"
        # llama.cpp artifacts can serve from CPU-only or GPU-offload, unlike
        # vLLM which requires a GPU; reflect that in the displayed device label.
        device_label = "GPU" if runtime == "vllm" else "GPU/CPU"
        base_memory_mb = weight_mb + kv_cache_mb + runtime_overhead_mb
        if device == "GPU":
            min_memory_mb = base_memory_mb
            recommended_memory_mb = math.ceil(min_memory_mb * 1.25)
            required_ram_mb = runtime_overhead_mb
        else:
            min_memory_mb = base_memory_mb
            recommended_memory_mb = math.ceil(base_memory_mb * 1.25)
            required_ram_mb = base_memory_mb
        models.append(
            {
                "id": payload["id"],
                "model": payload.get("display_name") or payload["id"],
                "quant": artifact.get("quant", ""),
                "size": f"{parameters:g}B" if isinstance(parameters, (int, float)) else "-",
                "device": device_label,
                "min_mem_mb": min_memory_mb,
                "rec_mem_mb": recommended_memory_mb,
                "ram_mb": required_ram_mb,
                "backend": runtime,
                "runtime": runtime,
                "speed": "fast" if device == "GPU" else "standard",
                "priority": payload.get("priority", 0),
                "safety": policy.get("safety", "safe"),
                "context_length": context_length,
                "description": payload.get("description") or model_payload.get("description", ""),
                "notes": ", ".join(policy.get("tags") or []),
                "enabled": payload.get("enabled", True),
            }
        )
    return models

def _fallback_evaluate_install_model_fit(model, hardware):
    device = str(model.get("device", "CPU")).upper()
    ram_mb = int(hardware.get("ram_mb") or 0)
    gpu_vram_mb = int(hardware.get("gpu_vram_mb") or 0)
    gpu_count = int(hardware.get("gpu_count") or 0)
    min_mem_mb = int(model.get("min_mem_mb") or 0)
    rec_mem_mb = int(model.get("rec_mem_mb") or min_mem_mb)
    required_ram_mb = int(model.get("ram_mb") or rec_mem_mb)
    backend = str(model.get("backend", "")).lower()

    if model.get("safety") == "danger":
        return "RISKY"

    if device == "GPU":
        if not hardware.get("gpu_detected") or gpu_count == 0:
            return "NOFIT"
        if gpu_count >= 2:
            if gpu_vram_mb >= rec_mem_mb:
                return "FIT"
            if gpu_vram_mb >= min_mem_mb:
                return "RISKY"
            return "NOFIT"
        if gpu_vram_mb >= rec_mem_mb:
            return "FIT"
        if backend == "llama.cpp":
            if ram_mb < required_ram_mb:
                return "NOFIT"
            if gpu_vram_mb < 2048:
                return "RISKY"
            return "RISKY"
        else:
            if gpu_vram_mb < min_mem_mb:
                return "NOFIT"
            return "RISKY"

    if ram_mb < required_ram_mb:
        return "NOFIT"
    if ram_mb < rec_mem_mb:
        return "RISKY"
    return "FIT"

def detect_hardware():
    print_header("Hardware Detection")
    try:
        from llm_installation.runtime_probe import probe_server_runtime
        profile = probe_server_runtime()
        
        print(f"• OS: {BOLD}{profile.platform}{RESET}")
        print(f"• CPU Model: {BOLD}{profile.cpu_model or 'Unknown'}{RESET} ({profile.cpu_count} Cores)")
        print(f"• CPU Features: {BOLD}{', '.join(profile.cpu_features or ['None'])}{RESET}")
        print(f"• RAM: {BOLD}{format_mb(profile.ram_mb)}{RESET} (Available: {format_mb(profile.ram_available_mb)})")
        
        try:
            total, used, free = shutil.disk_usage(".")
            disk_free_gb = free / 1024 / 1024 / 1024
            print(f"• Disk Free: {BOLD}{disk_free_gb:.1f} GB{RESET}")
        except OSError:
            pass

        if profile.docker_available:
            print(f"• Docker: {BOLD}{GREEN}Available{RESET}")
        else:
            print(f"• Docker: {BOLD}{RED}Not detected (verify that Docker Desktop is running){RESET}")

        if profile.has_gpu:
            if profile.gpu_count > 1:
                gpu_details = ", ".join(f"{profile.gpu_name} ({format_mb(vram)})" for vram in (profile.gpu_vram_list or []))
                print(f"• GPU: {BOLD}{GREEN}{profile.gpu_count} GPUs detected ({gpu_details}){RESET}")
            else:
                print(f"• GPU: {BOLD}{GREEN}Detected ({profile.gpu_name}, {format_mb(profile.gpu_vram_mb)}){RESET}")
            if profile.gpu_compute_cap:
                print(f"• GPU Compute Cap: {BOLD}{profile.gpu_compute_cap}{RESET}")
            if profile.gpu_mem_bandwidth_gb_s:
                print(f"• GPU Bandwidth: {BOLD}{profile.gpu_mem_bandwidth_gb_s:.1f} GB/s{RESET}")
        else:
            print(f"• GPU: {BOLD}{YELLOW}Not detected (CPU mode will be used){RESET}")

        return {
            "os": profile.platform,
            "docker_installed": profile.docker_available,
            "gpu_detected": profile.has_gpu,
            "gpu_count": profile.gpu_count,
            "gpu_name": profile.gpu_name or "None",
            "gpu_names": [profile.gpu_name] * profile.gpu_count if profile.gpu_name else [],
            "gpu_vram_mb": profile.gpu_vram_mb,
            "gpu_vram_list": profile.gpu_vram_list or [],
            "ram_mb": profile.ram_mb,
        }
    except Exception:
        hw = _fallback_detect_hardware()
        print(f"• OS: {BOLD}{hw['os']}{RESET}")
        print(f"• CPU: {BOLD}{os.cpu_count() or 1} Cores{RESET}")
        print(f"• RAM: {BOLD}{format_mb(hw['ram_mb'])}{RESET}")
        try:
            total, used, free = shutil.disk_usage(".")
            disk_free_gb = free / 1024 / 1024 / 1024
            print(f"• Disk Free: {BOLD}{disk_free_gb:.1f} GB{RESET}")
        except OSError:
            pass
        if hw["docker_installed"]:
            print(f"• Docker: {BOLD}{GREEN}Available{RESET}")
        else:
            print(f"• Docker: {BOLD}{RED}Not detected (verify that Docker Desktop is running){RESET}")
        if hw["gpu_detected"]:
            print(f"• GPU: {BOLD}{GREEN}Detected ({hw['gpu_name']}, {format_mb(hw['gpu_vram_mb'])}){RESET}")
        else:
            print(f"• GPU: {BOLD}{YELLOW}Not detected (CPU mode will be used){RESET}")
        return hw

def detect_system_ram_mb():
    try:
        from llm_installation.runtime_probe import probe_server_runtime
        return probe_server_runtime().ram_mb
    except Exception:
        return _fallback_detect_system_ram_mb()

def load_llm_models():
    try:
        from llm_installation.catalog import get_rag_model_catalog
        from llm_installation.planner import assess_catalog_entry
        from llm_installation.runtime_probe import probe_server_runtime
        catalog = get_rag_model_catalog()
        profile = probe_server_runtime()
        models = []
        for entry in catalog:
            plan = assess_catalog_entry(entry, profile)
            if plan.memory_estimate is None or plan.memory_placement is None:
                continue
            policy = entry.policy
            parameters = entry.model_metadata.parameter_count_b
            models.append({
                "id": entry.id,
                "model": entry.display_name,
                "quant": entry.artifact.quant,
                "size": f"{parameters:g}B" if isinstance(parameters, (int, float)) else "-",
                "device": entry.device_label,
                "min_mem_mb": plan.memory_estimate.logical_total_memory_mb,
                "rec_mem_mb": max(plan.memory_placement.required_vram_per_gpu_mb, default=plan.memory_placement.required_ram_mb),
                "ram_mb": plan.memory_placement.required_ram_mb,
                "backend": plan.runtime,
                "runtime": plan.runtime,
                "speed": "fast" if plan.backend_profile != "llamacpp-cpu" else "standard",
                "priority": entry.priority,
                "safety": policy.safety,
                "context_length": plan.context_length,
                "description": entry.description,
                "notes": ", ".join(policy.tags),
                "enabled": entry.enabled,
                "_entry": entry,
            })
        return models
    except Exception:
        return _fallback_load_llm_models()

def evaluate_install_model_fit(model, hardware):
    try:
        from llm_installation.planner import assess_catalog_entry
        from llm_installation.runtime_probe import ServerRuntimeProfile
        
        profile = ServerRuntimeProfile(
            cpu_count=os.cpu_count() or 1,
            ram_mb=hardware.get("ram_mb") or 0,
            has_gpu=hardware.get("gpu_detected") or False,
            gpu_name=hardware.get("gpu_name") or "",
            gpu_vram_mb=hardware.get("gpu_vram_mb") or 0,
            gpu_count=hardware.get("gpu_count") or 0,
            gpu_vram_list=hardware.get("gpu_vram_list") or [],
            cuda_available=hardware.get("gpu_detected") or False,
            platform=hardware.get("os") or "",
        )
        
        entry = model.get("_entry")
        if not entry:
            from llm_installation.catalog import get_catalog_entry
            entry = get_catalog_entry(model.get("id"))
            
        if not entry:
            return "NOFIT"
            
        plan = assess_catalog_entry(entry, profile)
        return plan.fit_status
    except Exception:
        return _fallback_evaluate_install_model_fit(model, hardware)
