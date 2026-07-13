#!/usr/bin/env python3
import os
import sys
import subprocess
import platform
import shutil
import re
import json
import time

import requests

# CLI Text Styles
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BLUE = "\033[34m"
RESET = "\033[0m"

ENV_FILE = ".env"
ENV_TEMPLATE_FILE = ".env.example"
COMPOSE_FILE = "docker-compose.yml"
COMPOSE_COMMAND = f"docker compose -f {COMPOSE_FILE}"
APP_URL = "http://localhost/"

# Windows Command Prompt might not support ANSI colors by default
if platform.system() == "Windows":
    os.system("")

def print_header(title):
    print("\n" + "=" * 60)
    print(f"{BOLD}{BLUE} {title} {RESET}")
    print("=" * 60)

def run_command(cmd, shell=True, capture_output=True):
    try:
        result = subprocess.run(
            cmd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return False, "", str(e)

def run_interactive_command(cmd):
    try:
        return subprocess.call(cmd, shell=True) == 0
    except Exception as e:
        print(f"{RED}[ERROR] {e}{RESET}")
        return False

# Append app directory to path to allow importing llm_installation_helper modules
sys.path.append(os.path.join(os.path.dirname(__file__), "app"))

from document_ai.llm_installation_helper.installer_adapter import (
    detect_hardware,
    detect_system_ram_mb,
    format_mb,
    load_llm_models,
    evaluate_install_model_fit,
)
from document_ai.llm_installation_helper.config_store import load_llm_runtime_config
from document_ai.llm_installation_helper.cleanup import (
    cleanup_stale_runtime,
    extract_runtime_and_repo,
    remove_current_llm_runtime,
)
from document_ai.llm_installation_helper.runtime_probe import probe_docker_services

def print_install_model_table(models, hardware):
    headers = ["#", "Model", "Quant", "Size", "Device", "Logical", "Pool Req", "RAM Req", "Backend", "Speed", "Safety", "Fit"]
    widths = [3, 24, 9, 6, 7, 9, 9, 7, 10, 8, 8, 7]
    line = " ".join(header.ljust(width) for header, width in zip(headers, widths))
    print(line.rstrip())
    print("-" * len(line.rstrip()))
    for index, model in enumerate(models, start=1):
        values = [
            str(index),
            str(model.get("model", model.get("id", ""))),
            str(model.get("quant", "")),
            str(model.get("size", "")),
            str(model.get("device", "")),
            format_mb(int(model.get("min_mem_mb") or 0)),
            format_mb(int(model.get("rec_mem_mb") or 0)),
            format_mb(int(model.get("ram_mb") or 0)),
            str(model.get("backend", "")),
            str(model.get("speed", "")),
            str(model.get("safety", "safe")),
            evaluate_install_model_fit(model, hardware),
        ]
        print(" ".join(value[:width].ljust(width) for value, width in zip(values, widths)).rstrip())

def filter_llm_models(models, query):
    normalized = (query or "").strip().lower()
    if not normalized:
        return models
    return [
        model
        for model in models
        if normalized in str(model.get("id", "")).lower()
        or normalized in str(model.get("model", "")).lower()
        or normalized in str(model.get("quant", "")).lower()
        or normalized in str(model.get("device", "")).lower()
        or normalized in str(model.get("backend", "")).lower()
    ]

def install_model_row(model, hardware, index):
    return {
        "index": index,
        "id": model.get("id"),
        "model": model.get("model"),
        "quant": model.get("quant"),
        "size": model.get("size"),
        "device": model.get("device"),
        "min_mem": format_mb(int(model.get("min_mem_mb") or 0)),
        "rec_mem": format_mb(int(model.get("rec_mem_mb") or 0)),
        "ram": format_mb(int(model.get("ram_mb") or 0)),
        "backend": model.get("backend"),
        "speed": model.get("speed"),
        "fit": evaluate_install_model_fit(model, hardware),
    }

def print_llm_model_detail(model, hardware, json_output=False):
    detail = {
        "id": model.get("id"),
        "model": model.get("model"),
        "quant": model.get("quant"),
        "size": model.get("size"),
        "device": model.get("device"),
        "min_mem": format_mb(int(model.get("min_mem_mb") or 0)),
        "rec_mem": format_mb(int(model.get("rec_mem_mb") or 0)),
        "ram": format_mb(int(model.get("ram_mb") or 0)),
        "backend": model.get("backend"),
        "speed": model.get("speed"),
        "fit": evaluate_install_model_fit(model, hardware),
        "safety": model.get("safety"),
        "description": model.get("description"),
        "notes": model.get("notes"),
    }
    if json_output:
        print(json.dumps(detail, ensure_ascii=False, indent=2, sort_keys=True))
        return
    for key, value in detail.items():
        print(f"{key}: {value}")

def handle_llm_catalog_cli(args):
    json_output = "--json-output" in args
    models = sorted(load_llm_models(), key=lambda item: int(item.get("priority") or 0), reverse=True)
    hardware = {
        "ram_mb": detect_system_ram_mb(),
        "gpu_detected": False,
        "gpu_count": 0,
        "gpu_name": "None",
        "gpu_names": [],
        "gpu_vram_mb": 0,
        "gpu_vram_list": [],
    }
    nvidia_ok, nvidia_out, _ = run_command("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits")
    if nvidia_ok and nvidia_out:
        gpu_names = []
        gpu_vram_list = []
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
        hardware["gpu_count"] = len(gpu_names)
        if hardware["gpu_count"] > 0:
            hardware["gpu_detected"] = True
            hardware["gpu_name"] = gpu_names[0]
            hardware["gpu_names"] = gpu_names
            hardware["gpu_vram_list"] = gpu_vram_list
            hardware["gpu_vram_mb"] = sum(gpu_vram_list)

    if "--show-llm" in args:
        index = args.index("--show-llm")
        model_id = args[index + 1] if index + 1 < len(args) else ""
        model = next((item for item in models if item.get("id") == model_id), None)
        if not model:
            print(f"{RED}[ERROR] Model not found: {model_id}{RESET}")
            return True
        print_llm_model_detail(model, hardware, json_output=json_output)
        return True

    query = ""
    if "--search-llm" in args:
        index = args.index("--search-llm")
        query = args[index + 1] if index + 1 < len(args) else ""
    rows = filter_llm_models(models, query)
    if json_output:
        print(
            json.dumps(
                {
                    "models": [
                        install_model_row(model, hardware, index)
                        for index, model in enumerate(rows, start=1)
                    ]
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print_install_model_table(rows, hardware)
    return True

def select_rag_priority():
    print_header("RAG LLM Priority")
    print(f"{BOLD}[1] Speed{RESET}")
    print("    - Prefer models and runtime settings with lower latency on this hardware.")
    print(f"{BOLD}[2] Balanced{RESET}")
    print("    - Balance speed, memory headroom, and response quality.")
    print(f"{BOLD}[3] Quality{RESET}")
    print("    - Prefer model quality and context length within safe hardware limits.")
    selected = input("Select an option (default: 2): ").strip()
    return {"1": "speed", "2": "balanced", "3": "quality"}.get(
        selected or "2",
        "balanced",
    )

def read_env_file(file_path):
    if not os.path.exists(file_path):
        return {}
    settings = {}
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                settings[key.strip()] = val.strip()
    return settings

def write_env_file(source_path, target_path, updates):
    if not os.path.exists(source_path):
        print(f"{RED}[ERROR] Template file not found: {source_path}{RESET}")
        return False
        
    with open(source_path, "r", encoding="utf-8") as f:
        content = f.read()

    for key, val in updates.items():
        # Replace existing key if present
        pattern = rf"^{key}=.*$"
        if re.search(pattern, content, re.MULTILINE):
            content = re.sub(pattern, f"{key}={val}", content, flags=re.MULTILINE)
        else:
            # Append if not present
            content += f"\n{key}={val}"

    with open(target_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"{GREEN}• Updated configuration: {target_path}{RESET}")
    return True

def create_windows_launcher():
    launcher_path = "start.bat"
    code = """@echo off
title Dotori Launcher
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    pause
    exit /b
)

:menu
cls
echo ============================================================
echo   Dotori Launcher
echo ============================================================
echo   [1] Install / Setup Wizard   (first run or reconfigure)
echo   [2] Start Dotori             (use saved settings)
echo   [3] Change LLM Model
echo   [4] View Available LLM Models
echo   [5] Stop Dotori Services
echo   [6] Remove LLM Runtime
echo   [7] Show Server Status
echo   [8] Exit
echo ============================================================
set "choice="
set /p choice="Select an option (1-8): "

if "%choice%"=="1" goto install
if "%choice%"=="2" goto run
if "%choice%"=="3" goto change_llm
if "%choice%"=="4" goto list_models
if "%choice%"=="5" goto stop
if "%choice%"=="6" goto remove_llm
if "%choice%"=="7" goto status
if "%choice%"=="8" exit /b
echo.
echo [ERROR] Invalid option: %choice%
pause
goto menu

:install
python install.py
if %errorlevel% neq 0 (
    echo [ERROR] Installation failed.
)
pause
goto menu

:run
python install.py --run
if %errorlevel% neq 0 (
    echo [ERROR] Failed to start Dotori.
)
pause
goto menu

:change_llm
python install.py --change-llm
if %errorlevel% neq 0 (
    echo [ERROR] Failed to change the LLM model.
)
pause
goto menu

:list_models
python install.py --list-llm-models
pause
goto menu

:stop
docker compose -f docker-compose.yml down
if %errorlevel% neq 0 (
    echo [ERROR] Failed to stop Dotori services. Is Docker Desktop running?
)
pause
goto menu

:remove_llm
python install.py --remove-llm
if %errorlevel% neq 0 (
    echo [ERROR] Failed to remove the LLM runtime.
)
pause
goto menu

:status
python install.py --status
pause
goto menu
"""
    with open(launcher_path, "w", encoding="utf-8") as f:
        f.write(code)
    print(f"{GREEN}• Created Windows launcher: {launcher_path}{RESET}")

def initialize_llm_runtime_config(mode, priority_mode="balanced", cluster_mode=False, keep_weights=False):
    if mode != "1":
        print(f"{YELLOW}• Skipping LLM runtime detection because Full Local AI RAG mode is not selected.{RESET}")
        return

    previous_payload = load_llm_runtime_config()

    print_header("🧭 LLM Runtime Setup")
    cmd = (
        f"{COMPOSE_COMMAND} exec app "
        "python manage.py detect_llm_runtime --interactive"
    )
    if cluster_mode:
        cmd += " --cluster-mode"
    print(f"Command: {BOLD}{cmd}{RESET}\n")
    if run_interactive_command(cmd):
        if activate_selected_rag_runtime():
            print(f"{GREEN}• LLM runtime configuration and service switch completed.{RESET}")
            new_info = extract_runtime_and_repo(load_llm_runtime_config())
            if new_info:
                print_header("🧹 Cleaning Up Previous Runtime")
                messages = cleanup_stale_runtime(
                    previous_payload,
                    *new_info,
                    remove_weights=not keep_weights,
                )
                if messages:
                    for message in messages:
                        print(f"{GREEN}• {message}{RESET}")
                else:
                    print(f"{YELLOW}• Nothing to clean up.{RESET}")
            return
        print(f"{YELLOW}• Runtime configuration was saved, but the selected service could not be started.{RESET}")
        return

    print(f"{YELLOW}• LLM runtime setup failed. The service will continue with the built-in catalog fallback.{RESET}")


def remove_llm_runtime_cli(assume_yes=False):
    print_header("🗑  Remove LLM Runtime")
    if not assume_yes:
        confirm = input(
            f"{YELLOW}This will stop and remove the runtime container and permanently delete "
            f"its cached model weights. Continue? (y/N): {RESET}"
        ).strip().lower()
        if confirm not in ("y", "yes"):
            print(f"{YELLOW}• Cancelled.{RESET}")
            return
    try:
        result = remove_current_llm_runtime()
    except Exception as exc:
        print(f"{RED}[ERROR] LLM runtime removal failed: {exc}{RESET}")
        sys.exit(1)
    for message in result["messages"]:
        print(f"{GREEN}• {message}{RESET}")
    print(f"{GREEN}• LLM runtime removal complete.{RESET}")


def selected_rag_runtime_service():
    config_path = os.path.join("data", "config", "llm_runtime.json")
    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            payload = json.load(config_file)
        runtime = str((payload.get("target") or {}).get("runtime") or "").lower()
    except (OSError, json.JSONDecodeError, AttributeError):
        runtime = ""
    return "vllm-rag" if runtime == "vllm" else "llama-rag"


STATUS_SERVICES = ["db", "redis", "app", "nginx", "embedding-worker", "search-worker", "rag-worker"]
# Fixed by docker-compose.yml's nginx service; not env-configurable today.
STATUS_PUBLISHED_PORTS = {"http": 80, "https": 443}


def _probe_external_app_url():
    started = time.monotonic()
    try:
        response = requests.get(APP_URL, timeout=3, allow_redirects=True)
        return {
            "ok": response.ok,
            "status_code": response.status_code,
            "message": "ok" if response.ok else response.reason,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    except requests.RequestException as exc:
        return {
            "ok": False,
            "message": str(exc)[:200],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }


def _print_server_status_report(report):
    connection = report["connection"]
    print_header("1. Connection")
    print(f"domain: {connection.get('domain') or '-'}")
    print(f"published_ports: {connection['published_ports']}")
    print("docker_services:")
    for service, state in connection["docker_services"].items():
        color = GREEN if state.get("state") == "running" else RED
        print(f"  {service}: {color}{state.get('state', 'unknown')}{RESET} ({state.get('status', '-')})")
    probe = connection["external_probe"]
    probe_color = GREEN if probe.get("ok") else YELLOW
    print(f"external_probe: {probe_color}{probe.get('message', '-')}{RESET} ({probe.get('elapsed_ms', 0)}ms)")

    if not report["container_reachable"]:
        print(f"\n{YELLOW}[WARN] Could not reach the app container to collect feature status. "
              f"Is 'app' running?{RESET}")
        return

    features = report["features"]
    print_header("2. Feature Status")
    file_io = features["file_io"]
    print(f"file_io: enabled={file_io['enabled']}")
    if file_io.get("pipeline_check"):
        check = file_io["pipeline_check"]
        check_color = GREEN if check["ok"] else RED
        print(f"  pipeline_check: {check_color}{check['ok']}{RESET} ({check['message']}, {check['elapsed_ms']}ms)")
    embedding = features["embedding"]
    print(f"embedding: enabled={embedding['enabled']}")
    rag = features["rag"]
    print(f"rag: enabled={rag['enabled']} configured={rag.get('configured')}")

    print_header("3. Feature Detail")
    print(f"embedding: model={embedding['model']} backend={embedding['backend']} "
          f"dimension={embedding['dimension']} sparse_enabled={embedding['sparse_enabled']}")
    if rag.get("configured"):
        print(f"rag: model={rag['model']} runtime={rag['runtime']} base_url={rag['base_url']} "
              f"priority_preset={rag['priority_preset']}")
        health = rag.get("health_status")
        if health:
            health_color = GREEN if health["ok"] else RED
            print(f"  health_status: {health_color}{health['ok']}{RESET} ({health['message']})")
    else:
        print(f"rag: {rag.get('message')}")


def handle_server_status_cli(json_output=False, skip_file_io=False):
    print_header("Server Status")

    docker_services = probe_docker_services()
    relevant_service_names = set(STATUS_SERVICES) | {selected_rag_runtime_service()}
    docker_status = {
        entry["service"]: {"state": entry["state"], "status": entry["status"]}
        for entry in docker_services
        if entry.get("service") in relevant_service_names
    }

    env_settings = read_env_file(ENV_FILE) if os.path.exists(ENV_FILE) else {}
    domain = env_settings.get("NGINX_SERVER_NAME", "")

    cmd = f"{COMPOSE_COMMAND} exec -T app python manage.py server_status --json-output"
    if skip_file_io:
        cmd += " --skip-file-io"
    ok, stdout, _stderr = run_command(cmd)
    container_report = None
    if ok:
        try:
            container_report = json.loads(stdout)
        except json.JSONDecodeError:
            container_report = None

    report = {
        "connection": {
            "domain": domain,
            "published_ports": STATUS_PUBLISHED_PORTS,
            "docker_services": docker_status,
            "external_probe": _probe_external_app_url(),
            "container": (container_report or {}).get("connection"),
        },
        "features": (container_report or {}).get("features"),
        "container_reachable": container_report is not None,
    }

    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return
    _print_server_status_report(report)


def activate_selected_rag_runtime():
    runtime_service = selected_rag_runtime_service()
    inactive_service = "llama-rag" if runtime_service == "vllm-rag" else "vllm-rag"
    compose = ["docker", "compose", "-f", COMPOSE_FILE]
    start_cmd = [*compose, "up", "--build", "--quiet-build", "--quiet-pull", "-d", runtime_service]
    print(f"Starting selected runtime: {BOLD}{' '.join(start_cmd)}{RESET}")
    result = subprocess.run(start_cmd, check=False)
    if result.returncode != 0:
        return False
    # Force a restart so a container that was already running picks up the
    # freshly written args file (a no-op `up -d` won't reload it).
    subprocess.run([*compose, "restart", runtime_service], check=False)
    subprocess.run([*compose, "rm", "-f", "-s", inactive_service], check=False)
    subprocess.run([*compose, "restart", "rag-worker"], check=False)
    return True

def run_services(mode, initialize_llm=False, rag_priority="balanced"):
    print_header("🚀 3. Start Dotori Docker Containers")

    # Base services that always run
    services = ["db", "redis", "app", "nginx"]

    if mode == "1":
        # Full AI Mode
        services += ["embedding-worker", "search-worker", "rag-worker"]
        if not initialize_llm:
            services.append(selected_rag_runtime_service())
    elif mode == "2":
        # Search AI Mode
        services += ["embedding-worker", "search-worker"]

    cmd = f"{COMPOSE_COMMAND} up --build --quiet-build --quiet-pull -d " + " ".join(services)
    print(f"Command: {BOLD}{cmd}{RESET}\n")

    print("Building and starting containers in the background. This may take a while...")
    print(f"{YELLOW}(pip/apt install output is condensed; full logs are shown automatically if a build fails){RESET}")
    # run in real time
    process = subprocess.Popen(cmd, shell=True)
    process.communicate()
    
    if process.returncode == 0:
        if initialize_llm:
            initialize_llm_runtime_config(mode, priority_mode=rag_priority)
        print_header("🎉 Startup Complete")
        print(f"• {BOLD}Web application:{RESET} {GREEN}{APP_URL}{RESET}")
        print(f"• {BOLD}To stop the services, run:{RESET} {YELLOW}{COMPOSE_COMMAND} down{RESET}")
    else:
        print(f"\n{RED}[ERROR] Failed to start Docker services. Verify that Docker Desktop is running.{RESET}")

def main():
    has_run_flag = "--run" in sys.argv
    if "--change-llm" in sys.argv:
        initialize_llm_runtime_config(
            "1",
            cluster_mode="--cluster-mode" in sys.argv,
            keep_weights="--keep-weights" in sys.argv,
        )
        return
    if "--remove-llm" in sys.argv:
        remove_llm_runtime_cli(assume_yes="--yes" in sys.argv)
        return
    if any(option in sys.argv for option in ("--list-llm-models", "--search-llm", "--show-llm")):
        handle_llm_catalog_cli(sys.argv[1:])
        return
    if "--status" in sys.argv:
        handle_server_status_cli(
            json_output="--json-output" in sys.argv,
            skip_file_io="--skip-file-io" in sys.argv,
        )
        return

    if not os.path.exists(ENV_FILE):
        if os.path.exists(ENV_TEMPLATE_FILE):
            shutil.copy(ENV_TEMPLATE_FILE, ENV_FILE)
        else:
            # Create a blank one if nothing exists
            with open(ENV_FILE, "w", encoding="utf-8") as f:
                f.write("# Generated by install.py\n")

    # If --run flag is passed, we check if setup is already complete and skip wizard
    if has_run_flag:
        env_settings = read_env_file(ENV_FILE)
        # Determine mode from env variables
        query_llm_enabled = env_settings.get("QUERY_LLM_ENABLED", "1")

        # Infer mode
        if query_llm_enabled == "0":
            # Check if embedding is enabled (we assume yes if we ran it, or we look at worker status)
            # Just look at backend
            mode = "2" if env_settings.get("EMBEDDING_MODEL") else "3"
        else:
            mode = "1"

        run_services(mode)
        return

    # Run Wizard
    detect_hardware()

    print_header("⚙️  2. Select Operation Mode")
    print(f"{BOLD}[1] Full Local AI RAG Mode{RESET}")
    print("    - Run local LLM answer generation, query analysis, and embeddings.")
    print()
    print(f"{BOLD}[2] Hybrid/Search AI Mode{RESET}")
    print("    - Run local embeddings and hybrid search without an answer-generation LLM.")
    print()
    print(f"{BOLD}[3] Basic Mode{RESET}")
    print("    - Run file input and output features without AI services.")
    print("-" * 60)
    
    mode = input("Select an option (default: 2): ").strip()
    if not mode:
        mode = "2"

    updates = {}
    embedding_choice = "1"
    rag_priority = "balanced"

    if mode in ("1", "2"):
        # Embedding Model Selection
        print("\n" + "-" * 60)
        print(f"{BOLD}Select an embedding model:{RESET}")
        print("1) BAAI/bge-m3 (default: high-quality hybrid search, moderate resources)")
        print("2) intfloat/multilingual-e5-small (fast and lightweight, recommended for CPU)")
        embedding_choice = input("Select an option (default: 1): ").strip()
        if not embedding_choice:
            embedding_choice = "1"

        if embedding_choice == "1":
            updates["EMBEDDING_MODEL"] = "BAAI/bge-m3"
            updates["EMBEDDING_BACKEND"] = "bgem3_hybrid"
            updates["EMBEDDING_DIMENSION"] = "1024"
            updates["EMBEDDING_SPARSE_ENABLED"] = "1"
        else:
            updates["EMBEDDING_MODEL"] = "intfloat/multilingual-e5-small"
            updates["EMBEDDING_BACKEND"] = "huggingface"
            updates["EMBEDDING_DIMENSION"] = "384"
            updates["EMBEDDING_SPARSE_ENABLED"] = "0"

    if mode == "1":
        # Full RAG options
        updates["QUERY_LLM_ENABLED"] = "1"
        updates["QUERY_PIPELINE_ENABLED"] = "1"

        rag_priority = select_rag_priority()

    else:
        # Disable Query LLM & Pipeline for lower modes
        updates["QUERY_LLM_ENABLED"] = "0"
        updates["QUERY_PIPELINE_ENABLED"] = "0"

    # Write configs to the installation environment file.
    write_env_file(ENV_FILE, ENV_FILE, updates)
    
    # Auto-generate Windows bat launcher
    create_windows_launcher()

    # Launch confirmation
    print("\n" + "-" * 60)
    launch = input("Start the Dotori Docker services now? (Y/n): ").strip().lower()
    if launch in ("", "y", "yes"):
        run_services(
            mode,
            initialize_llm=True,
            rag_priority=rag_priority,
        )
    else:
        print(f"\n{YELLOW}• Setup complete. To start the services later, run python install.py --run.{RESET}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Setup cancelled by the user.{RESET}")
        sys.exit(0)
