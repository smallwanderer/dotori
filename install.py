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


def configure_console_output():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


configure_console_output()

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
APP_URL = "http://127.0.0.1:8000/"

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
            cmd,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return (
            result.returncode == 0,
            (result.stdout or "").strip(),
            (result.stderr or "").strip(),
        )
    except Exception as e:
        return False, "", str(e)

def run_interactive_command(cmd):
    try:
        return subprocess.call(cmd, shell=True) == 0
    except Exception as e:
        print(f"{RED}[ERROR] {e}{RESET}")
        return False

# Append app directory to path to allow importing llm_installation/document_ai modules
sys.path.append(os.path.join(os.path.dirname(__file__), "app"))

from document_ai.services.rag_runtime_config import load_llm_runtime_config
from llm_installation.installer_adapter import (
    detect_hardware,
)
from llm_installation.cleanup import (
    cleanup_stale_runtime,
    extract_runtime_and_repo,
    remove_current_llm_runtime,
)
from llm_installation.runtime_lifecycle import (
    SCOPE_CONFIG,
    RuntimeLifecycleManager,
    build_runtime_spec,
)
from llm_installation.config_store import stage_legacy_runtime_generation
from llm_installation.runtime_probe import probe_docker_services
from installation.network_access import (
    ConfigurationError as NetworkConfigurationError,
    connect as connect_external_access,
    create_configuration_files,
    disconnect as disconnect_external_access,
    status as external_access_status,
)
from installation.network_access.files import configuration_directory, read_env_file as read_network_env_file
from installation.deployment import (
    ALL_WORKER_SERVICES,
    build_deployment_plan,
    compose_up_command,
    read_deployment_plan,
    write_deployment_plan,
)

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
    if os.path.exists(launcher_path):
        print(f"{GREEN}• Windows launcher is ready: {launcher_path}{RESET}")
    else:
        print(f"{YELLOW}• Windows launcher is missing: {launcher_path}{RESET}")


def handle_network_access_cli(args):
    try:
        if "--network-access-create" in args:
            result = create_configuration_files()
            print_header("External Access Configuration")
            for path in result["created"]:
                print(f"{GREEN}• Created: {path}{RESET}")
            for path in result["preserved"]:
                print(f"{YELLOW}• Preserved existing file: {path}{RESET}")
            print("Edit these files before choosing Connect external access module.")
            return True
        if "--network-access-open" in args:
            config_path = configuration_directory()
            if not config_path.exists():
                create_configuration_files()
            if platform.system() == "Windows":
                os.startfile(config_path)  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(config_path)])
            else:
                subprocess.Popen(["xdg-open", str(config_path)])
            return True
        if "--network-access-connect" in args:
            connect_external_access()
            print(f"{GREEN}• External access module connected.{RESET}")
            return True
        if "--network-access-disconnect" in args:
            disconnect_external_access()
            print(f"{GREEN}• External access module disconnected. Local access remains available at {APP_URL}{RESET}")
            return True
        if "--network-access-status" in args:
            report = external_access_status()
            if "--json-output" in args:
                print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print_header("External Access Status")
                print(f"mode: {report['mode']}")
                print(f"docker_available: {report['available']}")
                print(f"configured: {report['configured']}")
                print(f"nginx_running: {report['running']}")
                print(f"configuration: {report['configuration_directory']}")
            return True
    except (NetworkConfigurationError, RuntimeError, OSError) as exc:
        print(f"{RED}[ERROR] {exc}{RESET}")
        return False
    return None

def _read_pending_generation(scope="production"):
    """Read the candidate (runtime, model_id, generation_id) that
    detect_llm_runtime --interactive just staged for this scope, without
    touching the active config."""
    pending_path = os.path.join("data", "config", "runtime_scopes", scope, "pending_generation.json")
    try:
        with open(pending_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload["runtime"], payload["model_id"], payload["generation_id"]
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _active_runtime_info(scope="production"):
    """Read (runtime, model_id, generation_id) from the scope's already-active
    persisted config, or None if nothing is configured yet."""
    payload = load_llm_runtime_config(scope=scope)
    target = payload.get("target") if isinstance(payload, dict) else None
    if not isinstance(target, dict):
        return None
    runtime = target.get("runtime")
    model_id = target.get("model")
    generation_id = target.get("generation_id")
    if not runtime or not model_id or not generation_id:
        return None
    return runtime, model_id, generation_id


def _runtime_start_info(scope="production"):
    info = _active_runtime_info(scope)
    if info:
        return info
    migrated = stage_legacy_runtime_generation(scope)
    if migrated:
        print(
            f"{YELLOW}• Migrated the existing flat LLM configuration to a "
            f"managed runtime generation: {migrated[2]}{RESET}"
        )
    return migrated


def initialize_llm_runtime_config(mode, priority_mode="balanced", cluster_mode=False, keep_weights=False, scope="production"):
    if mode != "1":
        print(f"{YELLOW}• Skipping LLM runtime detection because Full Local AI RAG mode is not selected.{RESET}")
        return False

    previous_payload = load_llm_runtime_config(scope=scope)

    print_header("LLM Runtime Setup")
    cmd = (
        f"{COMPOSE_COMMAND} exec app "
        "python manage.py detect_llm_runtime --interactive"
    )
    if cluster_mode:
        cmd += " --cluster-mode"
    print(f"Command: {BOLD}{cmd}{RESET}\n")
    if not run_interactive_command(cmd):
        print(f"{YELLOW}• LLM runtime setup failed. The service will continue with the built-in catalog fallback.{RESET}")
        return False

    pending = _read_pending_generation(scope)
    if pending is None:
        print(f"{YELLOW}• No candidate runtime generation was produced; nothing to activate.{RESET}")
        return False

    runtime, model_id, generation_id = pending
    spec = build_runtime_spec(scope, runtime, model_id, generation_id)
    result = RuntimeLifecycleManager().apply(spec)
    for message in result.messages:
        color = GREEN if result.ok else (YELLOW if result.rolled_back else RED)
        print(f"{color}• {message}{RESET}")

    if not result.ok:
        print(f"{RED}• Runtime activation failed{' (rolled back to the previous runtime)' if result.rolled_back else ''}.{RESET}")
        return False

    print(f"{GREEN}• LLM runtime configuration and service switch completed.{RESET}")
    new_info = extract_runtime_and_repo(load_llm_runtime_config(scope=scope))
    if new_info:
        print_header("Cleaning Up Previous Runtime")
        messages = cleanup_stale_runtime(
            previous_payload,
            *new_info,
            scope=scope,
            remove_weights=not keep_weights,
        )
        if messages:
            for message in messages:
                print(f"{GREEN}• {message}{RESET}")
        else:
            print(f"{YELLOW}• Nothing to clean up.{RESET}")
    return True


def remove_llm_runtime_cli(assume_yes=False):
    print_header("Remove LLM Runtime")
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


STATUS_SERVICES = ["db", "redis", "app", "nginx", *ALL_WORKER_SERVICES]
STATUS_PUBLISHED_PORTS = {"local_http": 8000, "external_http": 80, "external_https": 443}


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
    print_header("Connection")
    print(f"domain: {connection.get('domain') or '-'}")
    print(f"published_ports: {connection['published_ports']}")
    print("docker_services:")
    for service, state in connection["docker_services"].items():
        color = GREEN if state.get("state") == "running" else RED
        print(f"  {service}: {color}{state.get('state', 'unknown')}{RESET} ({state.get('status', '-')})")
    probe = connection["external_probe"]
    probe_color = GREEN if probe.get("ok") else YELLOW
    print(f"external_probe: {probe_color}{probe.get('message', '-')}{RESET} ({probe.get('elapsed_ms', 0)}ms)")

    deployment = report.get("deployment")
    if deployment:
        enabled_workers = [
            worker["compose_service"]
            for worker in deployment.get("workers", [])
            if worker.get("enabled")
        ]
        print_header("Deployment Plan")
        print(f"scope: {deployment.get('scope', '-')}")
        print(f"mode: {deployment.get('mode', '-')}")
        print(f"generation: {deployment.get('generation_id', '-')}")
        print(f"workers: {', '.join(enabled_workers) if enabled_workers else '-'}")

    runtime = report.get("runtime") or {}
    persisted_runtime = runtime.get("runtime_status") or {}
    if runtime:
        print_header("Local LLM Runtime")
        print(f"container: {runtime.get('container_name', '-')}")
        print(f"running: {runtime.get('running', False)} health={runtime.get('health') or '-'}")
        print(f"restart_count: {runtime.get('restart_count', 0)} oom_killed={runtime.get('oom_killed', False)}")
        if persisted_runtime:
            runtime_color = GREEN if persisted_runtime.get("status") == "healthy" else YELLOW
            print(
                f"status: {runtime_color}{persisted_runtime.get('status', '-')}{RESET} "
                f"reason={persisted_runtime.get('reason_code') or '-'}"
            )
            if persisted_runtime.get("message"):
                print(f"  {persisted_runtime['message']}")
            if persisted_runtime.get("retryable"):
                print(f"  recovery: {YELLOW}python3 install.py --retry-llm{RESET}")

    if not report["container_reachable"]:
        print(f"\n{YELLOW}[WARN] Could not reach the app container to collect feature status. "
              f"Is 'app' running?{RESET}")
        return

    features = report["features"]
    print_header("Feature Status")
    file_io = features["file_io"]
    print(f"file_io: enabled={file_io['enabled']}")
    if file_io.get("pipeline_check"):
        check = file_io["pipeline_check"]
        check_color = GREEN if check["ok"] else RED
        print(f"  pipeline_check: {check_color}{check['ok']}{RESET} ({check['message']}, {check['elapsed_ms']}ms)")
    embedding = features["embedding"]
    print(f"embedding: enabled={embedding['enabled']}")
    rag = features["rag"]
    print(
        f"rag: enabled={rag['enabled']} configured={rag.get('configured')} "
        f"available={rag.get('available')}"
    )

    print_header("Feature Detail")
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


def handle_server_status_cli(json_output=False, skip_file_io=False, scope="production"):
    print_header("Server Status")

    docker_services = probe_docker_services()
    docker_status = {
        entry["service"]: {"state": entry["state"], "status": entry["status"]}
        for entry in docker_services
        if entry.get("service") in set(STATUS_SERVICES)
    }
    # The RAG runtime container isn't part of the Compose project (see
    # runtime_lifecycle.py), so it doesn't show up in probe_docker_services();
    # check it directly instead.
    rag_status = RuntimeLifecycleManager().status(scope)
    docker_status[rag_status["container_name"]] = {
        "state": "running" if rag_status["running"] else "not running",
        "status": rag_status.get("health") or "-",
    }

    provider_env = configuration_directory() / "provider.env"
    network_settings = read_network_env_file(provider_env)
    domain = network_settings.get("DOTORI_EXTERNAL_DOMAIN", "")

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
        "deployment": read_deployment_plan(scope),
        "runtime": rag_status,
    }

    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return
    _print_server_status_report(report)


def _saved_operation_mode():
    env_settings = read_env_file(ENV_FILE)
    if env_settings.get("QUERY_LLM_ENABLED", "1") != "0":
        return "1"
    return "2" if env_settings.get("EMBEDDING_MODEL") else "3"


def run_services(
    mode,
    initialize_llm=False,
    rag_priority="balanced",
    *,
    build_images=False,
    force_recreate=False,
    rebuild_runtime=False,
):
    print_header("Starting Dotori Docker Containers")

    # A normal start always restores local-only access. External access is
    # enabled only through the explicit network access action.
    run_command(f"{COMPOSE_COMMAND} --profile direct-https stop nginx")

    manager = RuntimeLifecycleManager()

    # RAG workers are intentionally excluded from the initial plan. They are
    # started only after a resolved runtime has passed lifecycle validation.
    initial_plan = build_deployment_plan(mode, scope="production")
    services = list(initial_plan.enabled_services)
    if initial_plan.disabled_worker_services:
        run_command(
            f"{COMPOSE_COMMAND} stop "
            + " ".join(initial_plan.disabled_worker_services)
        )

    cmd = compose_up_command(
        COMPOSE_COMMAND,
        services,
        build_images=build_images,
        force_recreate=force_recreate,
    )
    print(f"Command: {BOLD}{cmd}{RESET}\n")

    if build_images:
        print("Building and starting containers in the background. This may take a while...")
        print(f"{YELLOW}(pip/apt install output is condensed; full logs are shown automatically if a build fails){RESET}")
    else:
        print("Starting containers from existing images without rebuilding...")
    # run in real time
    process = subprocess.Popen(cmd, shell=True)
    process.communicate()

    if process.returncode == 0:
        runtime_ready = False
        if initialize_llm:
            runtime_ready = initialize_llm_runtime_config(
                mode, priority_mode=rag_priority
            )
            if not runtime_ready:
                current = manager.status("production")
                runtime_ready = bool(
                    current.get("owned")
                    and current.get("running")
                    and current.get("health") == "healthy"
                    and _active_runtime_info("production")
                )
                if runtime_ready:
                    print(
                        f"{YELLOW}• Model setup did not produce a new runtime; "
                        f"continuing with the existing healthy runtime.{RESET}"
                    )
        elif initial_plan.mode == "rag":
            # Ordinary starts reuse the active generation and an existing
            # image. Rebuild is an explicit maintenance action.
            info = _runtime_start_info("production")
            if info:
                runtime, model_id, generation_id = info
                spec = build_runtime_spec("production", runtime, model_id, generation_id)
                result = manager.apply(spec) if rebuild_runtime else manager.resume(spec)
                runtime_ready = result.ok or (
                    result.rolled_back
                    and manager.status("production").get("health") == "healthy"
                )
                for message in result.messages:
                    color = GREEN if result.ok else (YELLOW if result.rolled_back else RED)
                    print(f"{color}• {message}{RESET}")
            else:
                print(f"{YELLOW}• No LLM runtime is configured yet; run --change-llm to select one.{RESET}")
        else:
            manager.stop("production", remove_container=False)

        runtime_spec = None
        if initial_plan.mode == "rag" and runtime_ready:
            info = _active_runtime_info("production")
            if info:
                runtime, model_id, generation_id = info
                runtime_spec = build_runtime_spec(
                    "production", runtime, model_id, generation_id
                )

        final_plan = build_deployment_plan(
            mode,
            scope="production",
            runtime=runtime_spec,
            network_access="local",
        )
        write_deployment_plan(final_plan)

        if initial_plan.mode == "rag" and not runtime_ready:
            print(
                f"{YELLOW}[WARN] Dotori core and search services are healthy, but the "
                f"local LLM is unavailable. RAG answer generation remains disabled.{RESET}"
            )
            print(
                f"{YELLOW}• Document processing and hybrid search remain available.{RESET}"
            )
            print(
                f"{YELLOW}• Retry after freeing memory: "
                f"python3 install.py --retry-llm{RESET}"
            )

        runtime_workers = [
            worker.compose_service
            for worker in final_plan.enabled_workers
            if worker.requires_runtime
        ]
        if runtime_workers:
            worker_cmd = f"{COMPOSE_COMMAND} up --no-build -d " + " ".join(runtime_workers)
            worker_ok, _stdout, worker_error = run_command(worker_cmd)
            if not worker_ok:
                print(f"{RED}[ERROR] Runtime is healthy, but the RAG worker failed to start: {worker_error}{RESET}")
                return False

        print_header("Startup Complete")
        print(f"• {BOLD}Web application:{RESET} {GREEN}{APP_URL}{RESET}")
        print(f"• {BOLD}Deployment mode:{RESET} {final_plan.mode} ({final_plan.generation_id})")
        if final_plan.mode == "rag" and not runtime_ready:
            print(f"• {BOLD}RAG answer generation:{RESET} {YELLOW}disabled (search-only fallback){RESET}")
        print(f"• {BOLD}To pause the services, run:{RESET} {YELLOW}python install.py --stop{RESET}")
        return True
    else:
        if build_images:
            detail = "Verify that Docker Desktop is running and review the build output."
        else:
            detail = (
                "Existing images could not be started. Run Install / Setup Wizard "
                "or Maintenance > Rebuild and Restart."
            )
        print(f"\n{RED}[ERROR] Failed to start Docker services. {detail}{RESET}")
        return False


def _scope_arg(args, default="production"):
    if "--scope" not in args:
        return default
    index = args.index("--scope")
    value = args[index + 1] if index + 1 < len(args) else default
    if value not in SCOPE_CONFIG:
        print(f"{RED}[ERROR] Unknown scope: {value}. Expected one of: {', '.join(SCOPE_CONFIG)}{RESET}")
        sys.exit(1)
    return value


def handle_pause_cli(scope="production"):
    print_header(f"Pause Dotori Services ({scope})")
    scope_cfg = SCOPE_CONFIG[scope]
    compose_command = f"docker compose -f {scope_cfg.compose_file}"

    ok, _stdout, stderr = run_command(
        f"{compose_command} --profile direct-https --profile managed-rag stop"
    )
    runtime_ok = RuntimeLifecycleManager().stop(scope, remove_container=False)

    if ok and runtime_ok:
        print(f"{GREEN}• Dotori services paused. Containers, images, and model cache were preserved.{RESET}")
        return True
    detail = stderr or (
        "the managed LLM runtime could not be paused"
        if not runtime_ok
        else "is Docker Desktop running?"
    )
    print(f"{RED}[ERROR] Failed to pause Dotori services: {detail}{RESET}")
    return False


def handle_shutdown_cli(scope="production"):
    print_header(f"Full Shutdown ({scope})")
    scope_cfg = SCOPE_CONFIG[scope]
    compose_command = f"docker compose -f {scope_cfg.compose_file}"

    saved_plan = read_deployment_plan(scope)
    planned_workers = []
    if saved_plan:
        planned_workers = [
            worker.get("compose_service")
            for worker in saved_plan.get("workers", [])
            if worker.get("enabled") and worker.get("compose_service")
        ]
    worker_services = planned_workers or list(ALL_WORKER_SERVICES)
    run_command(f"{compose_command} stop " + " ".join(worker_services))
    runtime_ok = RuntimeLifecycleManager().stop(scope, remove_container=True)
    ok, _stdout, stderr = run_command(
        f"{compose_command} --profile direct-https --profile managed-rag down"
    )

    inspect_ok, count_out, _ = run_command(
        f'docker network inspect {scope_cfg.network_name} --format "{{{{len .Containers}}}}"'
    )
    if inspect_ok and count_out.strip() == "0":
        run_command(f"docker network rm {scope_cfg.network_name}")

    if ok and runtime_ok:
        print(f"{GREEN}• Dotori containers and networks were removed. Data, images, configuration, and model cache were preserved.{RESET}")
    else:
        detail = stderr or (
            "the managed LLM runtime could not be removed"
            if not runtime_ok
            else "is Docker Desktop running?"
        )
        print(f"{RED}[ERROR] Full shutdown failed: {detail}{RESET}")
    return ok and runtime_ok


def main():
    has_run_flag = "--run" in sys.argv
    has_retry_llm_flag = "--retry-llm" in sys.argv
    network_result = handle_network_access_cli(sys.argv[1:])
    if network_result is not None:
        if not network_result:
            sys.exit(1)
        return
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
    if "--shutdown" in sys.argv:
        ok = handle_shutdown_cli(_scope_arg(sys.argv))
        sys.exit(0 if ok else 1)
    if "--stop" in sys.argv:
        ok = handle_pause_cli(_scope_arg(sys.argv))
        sys.exit(0 if ok else 1)
    if "--status" in sys.argv:
        handle_server_status_cli(
            json_output="--json-output" in sys.argv,
            skip_file_io="--skip-file-io" in sys.argv,
            scope=_scope_arg(sys.argv),
        )
        return

    if not os.path.exists(ENV_FILE):
        if os.path.exists(ENV_TEMPLATE_FILE):
            shutil.copy(ENV_TEMPLATE_FILE, ENV_FILE)
        else:
            # Create a blank one if nothing exists
            with open(ENV_FILE, "w", encoding="utf-8") as f:
                f.write("# Generated by install.py\n")

    if "--restart" in sys.argv:
        paused = handle_pause_cli("production")
        ok = paused and run_services(_saved_operation_mode())
        sys.exit(0 if ok else 1)

    if has_retry_llm_flag:
        if _saved_operation_mode() != "1":
            print(f"{YELLOW}• Local LLM retry is available only in Full Local AI RAG mode.{RESET}")
            return
        ok = run_services(_saved_operation_mode())
        sys.exit(0 if ok else 1)

    if "--rebuild" in sys.argv:
        ok = run_services(
            _saved_operation_mode(),
            build_images=True,
            force_recreate=True,
            rebuild_runtime=True,
        )
        sys.exit(0 if ok else 1)

    # A normal run reuses existing images and the active runtime generation.
    if has_run_flag:
        ok = run_services(_saved_operation_mode())
        sys.exit(0 if ok else 1)

    # Run Wizard
    detect_hardware()

    print_header("Select Operation Mode")
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
            build_images=True,
        )
    else:
        print(f"\n{YELLOW}• Setup complete. To start the services later, run python install.py --run.{RESET}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Setup cancelled by the user.{RESET}")
        sys.exit(0)
