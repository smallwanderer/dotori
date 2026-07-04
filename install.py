#!/usr/bin/env python3
import os
import sys
import subprocess
import platform
import shutil
import re
import json

# CLI Text Styles
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BLUE = "\033[34m"
RESET = "\033[0m"

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
        print(f"{RED}[에러] {e}{RESET}")
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
            print(f"{RED}[에러] 모델을 찾을 수 없습니다: {model_id}{RESET}")
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
    print_header("RAG LLM 운영 우선순위")
    print(f"{BOLD}[1] 속도 우선{RESET}")
    print("    - 현재 하드웨어에서 응답 지연이 낮은 모델과 실행값을 우선합니다.")
    print(f"{BOLD}[2] 균형{RESET}")
    print("    - 속도, 메모리 여유, 답변 품질을 균형 있게 조정합니다.")
    print(f"{BOLD}[3] 품질 우선{RESET}")
    print("    - 하드웨어 안전 범위 안에서 모델 품질과 Context를 우선합니다.")
    selected = input("선택 (기본값: 2): ").strip()
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
        print(f"{RED}[에러] {source_path} 템플릿 파일이 존재하지 않습니다.{RESET}")
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
    print(f"{GREEN}• {target_path} 설정 업데이트 완료.{RESET}")
    return True

def create_windows_launcher():
    launcher_path = "start.bat"
    code = """@echo off
title Dotori Dev Launcher
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    pause
    exit /b
)

python install.py --run
if %errorlevel% neq 0 (
    echo [ERROR] Failed to run launcher.
    pause
    exit /b
)
"""
    with open(launcher_path, "w", encoding="utf-8") as f:
        f.write(code)
    print(f"{GREEN}• Windows 더블클릭 실행기({launcher_path}) 생성 완료.{RESET}")

def initialize_llm_runtime_config(mode, priority_mode="balanced", cluster_mode=False):
    if mode != "1":
        print(f"{YELLOW}• Full 로컬 AI RAG 모드가 아니므로 LLM runtime 자동 감지를 건너뜁니다.{RESET}")
        return

    print_header("🧭 LLM Runtime Wizard 설정")
    cmd = (
        "docker compose -f docker-compose.dev.yml exec app "
        "python manage.py detect_llm_runtime --interactive"
    )
    if cluster_mode:
        cmd += " --cluster-mode"
    print(f"실행 명령어: {BOLD}{cmd}{RESET}\n")
    if run_interactive_command(cmd):
        if activate_selected_rag_runtime():
            print(f"{GREEN}• LLM runtime 설정 및 서비스 전환 완료!{RESET}")
            return
        print(f"{YELLOW}• runtime 설정은 저장됐지만 선택된 서비스 기동에 실패했습니다.{RESET}")
        return

    print(f"{YELLOW}• LLM runtime 설정에 실패했습니다. 서비스는 내장 catalog fallback으로 계속 동작합니다.{RESET}")


def selected_rag_runtime_service():
    config_path = os.path.join("data", "config", "llm_runtime.json")
    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            payload = json.load(config_file)
        runtime = str((payload.get("target") or {}).get("runtime") or "").lower()
    except (OSError, json.JSONDecodeError, AttributeError):
        runtime = ""
    return "vllm-rag" if runtime == "vllm" else "llama-rag"


def activate_selected_rag_runtime():
    runtime_service = selected_rag_runtime_service()
    inactive_service = "llama-rag" if runtime_service == "vllm-rag" else "vllm-rag"
    compose = ["docker", "compose", "-f", "docker-compose.dev.yml"]
    start_cmd = [*compose, "up", "--build", "-d", runtime_service]
    print(f"선택된 runtime 기동: {BOLD}{' '.join(start_cmd)}{RESET}")
    result = subprocess.run(start_cmd, check=False)
    if result.returncode != 0:
        return False
    subprocess.run([*compose, "stop", inactive_service], check=False)
    subprocess.run([*compose, "restart", "rag-worker"], check=False)
    return True

def run_services(mode, query_parser_choice, initialize_llm=False, rag_priority="balanced"):
    print_header("🚀 3. Dotori Docker 컨테이너 구동")
    
    # Base services that always run
    services = ["db", "redis", "app", "nginx"]
    
    if mode == "1":
        # Full AI Mode
        services += ["embedding-worker", "search-worker", "query-worker", "rag-worker"]
        if not initialize_llm:
            services.append(selected_rag_runtime_service())
        if query_parser_choice == "1":
            services += ["llama-query-parser"]
        elif query_parser_choice == "2":
            services += ["vllm-query-parser"]
    elif mode == "2":
        # Search AI Mode
        services += ["embedding-worker", "search-worker"]

    cmd = f"docker compose -f docker-compose.dev.yml up --build -d " + " ".join(services)
    print(f"구동 명령어: {BOLD}{cmd}{RESET}\n")
    
    print("컨테이너 빌드 및 백그라운드 실행을 시작합니다. 이 작업은 다소 시간이 걸릴 수 있습니다...")
    # run in real time
    process = subprocess.Popen(cmd, shell=True)
    process.communicate()
    
    if process.returncode == 0:
        if initialize_llm:
            initialize_llm_runtime_config(mode, priority_mode=rag_priority)
        print_header("🎉 구동 완료!")
        print(f"• {BOLD}웹 애플리케이션 접속 주소:{RESET} {GREEN}http://localhost:8888/{RESET}")
        print(f"• {BOLD}종료하시려면:{RESET} {YELLOW}docker compose -f docker-compose.dev.yml down{RESET} 을 실행하세요.")
    else:
        print(f"\n{RED}[에러] Docker 서비스 구동 실패. Docker 데스크톱이 켜져 있는지 확인해 주세요.{RESET}")

def main():
    has_run_flag = "--run" in sys.argv
    if "--change-llm" in sys.argv:
        initialize_llm_runtime_config("1", cluster_mode="--cluster-mode" in sys.argv)
        return
    if any(option in sys.argv for option in ("--list-llm-models", "--search-llm", "--show-llm")):
        handle_llm_catalog_cli(sys.argv[1:])
        return
    
    if not os.path.exists(".env.dev"):
        if os.path.exists(".env.dev.example"):
            shutil.copy(".env.dev.example", ".env.dev")
        elif os.path.exists(".env.example"):
            shutil.copy(".env.example", ".env.dev")
        else:
            # Create a blank one if nothing exists
            with open(".env.dev", "w", encoding="utf-8") as f:
                f.write("# Generated by install.py\n")

    # If --run flag is passed, we check if setup is already complete and skip wizard
    if has_run_flag:
        env_settings = read_env_file(".env.dev")
        # Determine mode from env variables
        query_llm_enabled = env_settings.get("QUERY_LLM_ENABLED", "1")
        query_parser_url = env_settings.get("QUERY_PARSER_BASE_URL", "")
        
        # Infer mode
        if query_llm_enabled == "0":
            # Check if embedding is enabled (we assume yes if we ran it, or we look at worker status)
            # Just look at backend
            mode = "2" if env_settings.get("EMBEDDING_MODEL") else "3"
        else:
            mode = "1"
            
        parser_choice = "1" if "llama-query-parser" in query_parser_url else "2"
        run_services(mode, parser_choice)
        return

    # Run Wizard
    hw = detect_hardware()

    print_header("⚙️  2. 서비스 작동 모드 선택 (Operation Mode)")
    print(f"{BOLD}[1] Full 로컬 AI RAG 모드 (전체 활성화){RESET}")
    print("    - 로컬 LLM 답변 생성 + 로컬 AI 쿼리 분석 + 로컬 임베딩 모두 구동")
    print()
    print(f"{BOLD}[2] Hybrid/Search AI 모드 (임베딩 및 의미론적 검색만 활성화){RESET}")
    print("    - 로컬 임베딩 및 하이브리드 검색만 사용 (답변 생성 LLM 미구동)")
    print()
    print(f"{BOLD}[3] 기본적인 모드{RESET}")
    print("    - 파일 입출력 기능만 사용 (AI 기능 미구동)")
    print("-" * 60)
    
    mode = input("선택 (기본값: 2): ").strip()
    if not mode:
        mode = "2"

    updates = {}
    embedding_choice = "1"
    query_parser_choice = "3"
    rag_priority = "balanced"

    if mode in ("1", "2"):
        # Embedding Model Selection
        print("\n" + "-" * 60)
        print(f"{BOLD}임베딩 모델을 선택해 주세요:{RESET}")
        print("1) BAAI/bge-m3 (기본값: 고품질 하이브리드 검색, 리소스 중간)")
        print("2) intfloat/multilingual-e5-small (빠르고 리소스 초경량, CPU 추천)")
        embedding_choice = input("선택 (기본값: 1): ").strip()
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
        print("\n" + "-" * 60)
        print(f"{BOLD}쿼리 파서(Query Parser) LLM 백엔드를 선택해 주세요:{RESET}")
        if hw["gpu_detected"]:
            print(f"1) llama-query-parser (llama.cpp - GGUF 모델 구동 / CPU 스레드 최적)")
            print(f"2) vllm-query-parser (vLLM - {GREEN}NVIDIA GPU 가속 추천{RESET})")
            query_parser_choice = input("선택 (기본값: 2): ").strip()
            if not query_parser_choice:
                query_parser_choice = "2"
        else:
            print(f"1) llama-query-parser (llama.cpp - GGUF 모델 구동 / {GREEN}CPU 추천{RESET})")
            print("2) vllm-query-parser (vLLM - GPU 필요하므로 비권장)")
            query_parser_choice = input("선택 (기본값: 1): ").strip()
            if not query_parser_choice:
                query_parser_choice = "1"

        updates["QUERY_LLM_ENABLED"] = "1"
        updates["QUERY_PIPELINE_ENABLED"] = "1"
        
        if query_parser_choice == "1":
            updates["QUERY_PARSER_BASE_URL"] = "http://llama-query-parser:8080"
        else:
            updates["QUERY_PARSER_BASE_URL"] = "http://vllm-query-parser:8080"

        rag_priority = select_rag_priority()
            
    else:
        # Disable Query LLM & Pipeline for lower modes
        updates["QUERY_LLM_ENABLED"] = "0"
        updates["QUERY_PIPELINE_ENABLED"] = "0"

    # Write configs to .env.dev
    write_env_file(".env.dev", ".env.dev", updates)
    
    # Auto-generate Windows bat launcher
    create_windows_launcher()

    # Launch confirmation
    print("\n" + "-" * 60)
    launch = input("지금 Dotori Docker 서비스를 바로 가동하시겠습니까? (Y/n): ").strip().lower()
    if launch in ("", "y", "yes"):
        run_services(
            mode,
            query_parser_choice,
            initialize_llm=True,
            rag_priority=rag_priority,
        )
    else:
        print(f"\n{YELLOW}• 세팅 완료! 다음에 서비스를 켤 때는 start.bat 또는 python install.py --run 을 실행해 주세요.{RESET}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{YELLOW}사용자 요청으로 세팅이 중단되었습니다.{RESET}")
        sys.exit(0)
