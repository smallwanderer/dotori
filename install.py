#!/usr/bin/env python3
import os
import sys
import subprocess
import platform
import shutil
import re

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

def detect_hardware():
    print_header("시스템 사양 감지 (Hardware Detection)")
    
    # OS
    os_name = platform.system()
    print(f"• OS: {BOLD}{os_name} {platform.release()}{RESET}")
    
    # Docker Check
    docker_ok, docker_ver, _ = run_command("docker --version")
    if docker_ok:
        print(f"• Docker: {BOLD}{GREEN}설치됨 ({docker_ver}){RESET}")
    else:
        print(f"• Docker: {BOLD}{RED}감지되지 않음 (Docker Desktop이 실행 중인지 확인해 주세요.){RESET}")

    # GPU Check via nvidia-smi
    gpu_detected = False
    gpu_name = "None"
    nvidia_ok, nvidia_out, _ = run_command("nvidia-smi --query-gpu=name --format=csv,noheader")
    if nvidia_ok and nvidia_out:
        gpu_detected = True
        gpu_name = nvidia_out.split('\n')[0].strip()
        print(f"• GPU: {BOLD}{GREEN}NVIDIA GPU 감지됨 ({gpu_name}){RESET}")
    else:
        # Check torch CUDA (if torch is installed locally)
        try:
            import torch
            if torch.cuda.is_available():
                gpu_detected = True
                gpu_name = torch.cuda.get_device_name(0)
                print(f"• GPU: {BOLD}{GREEN}NVIDIA GPU 감지됨 (PyTorch: {gpu_name}){RESET}")
            else:
                print(f"• GPU: {BOLD}{YELLOW}NVIDIA GPU 없음 (CPU 모드로 구동됩니다.){RESET}")
        except ImportError:
            print(f"• GPU: {BOLD}{YELLOW}NVIDIA GPU 없음 / nvidia-smi 미작동 (CPU 모드 자동 설정){RESET}")

    return {
        "os": os_name,
        "docker_installed": docker_ok,
        "gpu_detected": gpu_detected,
        "gpu_name": gpu_name
    }

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

def run_services(mode, query_parser_choice):
    print_header("🚀 3. Dotori Docker 컨테이너 구동")
    
    # Base services that always run
    services = ["db", "redis", "app", "nginx"]
    
    if mode == "1":
        # Full AI Mode
        services += ["embedding-worker", "search-worker", "query-worker", "rag-worker", "llama-rag"]
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
        print_header("🎉 구동 완료!")
        print(f"• {BOLD}웹 애플리케이션 접속 주소:{RESET} {GREEN}http://localhost:8888/{RESET}")
        print(f"• {BOLD}종료하시려면:{RESET} {YELLOW}docker compose -f docker-compose.dev.yml down{RESET} 을 실행하세요.")
    else:
        print(f"\n{RED}[에러] Docker 서비스 구동 실패. Docker 데스크톱이 켜져 있는지 확인해 주세요.{RESET}")

def main():
    has_run_flag = "--run" in sys.argv
    
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
    print("사용자 컴퓨터 스펙에 맞춰 최적의 동작 모드를 선택해 주세요.\n")
    print(f"{BOLD}[1] Full 로컬 AI RAG 모드 (전체 활성화){RESET}")
    print("    - 로컬 LLM 답변 생성 + 로컬 AI 쿼리 분석 + 로컬 임베딩 모두 구동")
    print(f"    - {YELLOW}권장 스펙: 16GB+ RAM / NVIDIA GPU 보유자{RESET}")
    print()
    print(f"{BOLD}[2] Hybrid/Search AI 모드 (임베딩 및 의미론적 검색만 활성화){RESET}")
    print("    - 로컬 임베딩 및 하이브리드 검색만 사용 (답변 생성 LLM 미구동)")
    print(f"    - {GREEN}권장 스펙: 8GB+ RAM / CPU만 있는 일반 노트북 등{RESET}")
    print()
    print(f"{BOLD}[3] No AI 모드 (일반 웹 서버 전용 구동){RESET}")
    print("    - 모든 AI 기능(임베딩, LLM)을 끄고 가벼운 기본 키워드 검색용 사이트만 구동")
    print("    - 저사양 PC 및 리소스 최소화 목적")
    print("-" * 60)
    
    mode = input("선택 (기본값: 2): ").strip()
    if not mode:
        mode = "2"

    updates = {}
    embedding_choice = "1"
    query_parser_choice = "3"

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
        run_services(mode, query_parser_choice)
    else:
        print(f"\n{YELLOW}• 세팅 완료! 다음에 서비스를 켤 때는 start.bat 또는 python install.py --run 을 실행해 주세요.{RESET}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{YELLOW}사용자 요청으로 세팅이 중단되었습니다.{RESET}")
        sys.exit(0)
