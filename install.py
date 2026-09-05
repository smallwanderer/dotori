#!/usr/bin/env python3
import argparse
import os
import sys
import subprocess
import platform
import shutil
import re
import json
import secrets
import time

import requests

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:
    Console = None
    Table = None


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

_console = Console() if Console else None

def print_header(title):
    if _console:
        _console.rule(f"[bold blue]{title}[/bold blue]")
    else:
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


def initialize_embedding_runtime_config(
    priority_preset="balanced",
    *,
    scope="production",
):
    # Keep the host installer runnable with the Python standard library only.
    # The container-side loader performs the full Pydantic catalog validation.
    from types import SimpleNamespace
    from llm_installation.embedding_config_store import (
        commit_active_embedding_runtime,
        write_embedding_runtime_generation,
    )

    catalog_root = (
        os.path.dirname(__file__)
        + "/app/llm_installation/embedding_catalog"
    )
    with open(
        catalog_root + "/models/baai/bge-m3.json",
        "r",
        encoding="utf-8",
    ) as model_file:
        model = json.load(model_file)
    with open(
        catalog_root + "/profiles/bgem3_hybrid/bge-m3.json",
        "r",
        encoding="utf-8",
    ) as profile_file:
        profile = json.load(profile_file)

    known_providers = {"bgem3_hybrid", "sentence_transformers", "openai_compatible"}
    known_stores = {
        "pgvector_chunk_1024": 1024,
        "pgvector_chunk_640": 640,
        "pgvector_chunk_768": 768,
        "pgvector_chunk_1536": 1536,
        "pgvector_chunk_384": 384,
    }

    if (
        profile.get("availability") != "supported"
        or priority_preset not in profile.get("presets", [])
        or profile.get("model_id") != model.get("id")
        or profile.get("provider") not in known_providers
        or profile.get("store") not in known_stores
        or int(profile.get("dimension", 0)) != int(model.get("dimension", 0))
        or known_stores.get(profile.get("store")) != int(profile.get("dimension", 0))
    ):
        raise RuntimeError(
            "The checked-in embedding catalog has no valid supported "
            f"entry for preset {priority_preset}."
        )
    resolved_entry = dict(model)
    resolved_entry.update(
        {
            key: value
            for key, value in profile.items()
            if key != "model_id"
        }
    )
    entry = SimpleNamespace(**resolved_entry)
    generation_id = (
        f"{scope}-embedding-{entry.id}-{entry.revision[:12]}"
    )
    write_embedding_runtime_generation(
        scope=scope,
        generation_id=generation_id,
        entry=entry,
    )
    commit_active_embedding_runtime(scope, generation_id)
    return entry

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
        if args.network_access_create:
            result = create_configuration_files()
            print_header("External Access Configuration")
            for path in result["created"]:
                print(f"{GREEN}• Created: {path}{RESET}")
            for path in result["preserved"]:
                print(f"{YELLOW}• Preserved existing file: {path}{RESET}")
            print("Edit these files before choosing Connect external access module.")
            return True
        if args.network_access_open:
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
        if args.network_access_connect:
            connect_external_access()
            print(f"{GREEN}• External access module connected.{RESET}")
            return True
        if args.network_access_disconnect:
            disconnect_external_access()
            print(f"{GREEN}• External access module disconnected. Local access remains available at {APP_URL}{RESET}")
            return True
        if args.network_access_status:
            report = external_access_status()
            if args.json_output:
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
    if _console:
        table = Table(title="docker_services")
        table.add_column("service")
        table.add_column("state")
        table.add_column("status")
        for service, state in connection["docker_services"].items():
            state_style = "green" if state.get("state") == "running" else "red"
            table.add_row(service, f"[{state_style}]{state.get('state', 'unknown')}[/{state_style}]", state.get("status", "-"))
        _console.print(table)
    else:
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
    configured_mode = env_settings.get("DOTORI_OPERATION_MODE", "").strip().lower()
    mode_aliases = {"rag": "1", "search": "2", "basic": "3"}
    if configured_mode in mode_aliases:
        return mode_aliases[configured_mode]

    deployment = read_deployment_plan("production")
    if deployment and deployment.get("mode") in mode_aliases:
        return mode_aliases[deployment["mode"]]

    # Compatibility with configurations written before DOTORI_OPERATION_MODE.
    # QUERY_UNDERSTANDING_LLM_ENABLED only gates the unrelated experimental
    # query-understanding feature, so it can't be used to infer the mode here;
    # fall back to whether an embedding model was configured at all.
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
                f"{YELLOW}[WARN] Dotori core services are healthy, but the "
                f"local LLM is unavailable. RAG answer generation remains disabled.{RESET}"
            )
            print(
                f"{YELLOW}• Document processing and hybrid search remain available.{RESET}"
            )
            print(
                f"{YELLOW}• Retry after freeing memory: "
                f"python3 install.py --retry-llm{RESET}"
            )

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


def _ensure_embedding_internal_token():
    # Backfill for installs whose .env predates EMBEDDING_INTERNAL_TOKEN, and
    # for fresh installs whose copied .env still has the .env.example
    # placeholder. Idempotent: never touches an already-generated token, so
    # this is safe to call on every install.py run.
    if not os.path.exists(ENV_FILE):
        return
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    match = re.search(r"^EMBEDDING_INTERNAL_TOKEN=(.*)$", content, re.MULTILINE)
    current = match.group(1).strip() if match else ""
    if current and current != "change-me":
        return
    write_env_file(ENV_FILE, ENV_FILE, {"EMBEDDING_INTERNAL_TOKEN": secrets.token_urlsafe(32)})
    print(f"{GREEN}• Generated EMBEDDING_INTERNAL_TOKEN for app <-> dotori-document authentication.{RESET}")


def _ensure_env_file_exists():
    if os.path.exists(ENV_FILE):
        _ensure_embedding_internal_token()
        return
    if os.path.exists(ENV_TEMPLATE_FILE):
        shutil.copy(ENV_TEMPLATE_FILE, ENV_FILE)
    else:
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write("# Generated by install.py\n")

    # First-time creation only: replace the placeholder secrets with random
    # values so a fresh install is never left with change-me credentials.
    write_env_file(ENV_FILE, ENV_FILE, {
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(50),
        "POSTGRES_USER": "dotori",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "EMBEDDING_INTERNAL_TOKEN": secrets.token_urlsafe(32),
    })
    print(f"{GREEN}• Generated {ENV_FILE} with a random Django secret key, PostgreSQL password, and embedding service token.{RESET}")


def handle_login_cli(mode, assume_yes=False):
    _ensure_env_file_exists()
    write_env_file(ENV_FILE, ENV_FILE, {"LOGIN_REQUIRED": "1" if mode == "enable" else "0"})
    if mode == "enable":
        print(f"{YELLOW}• Real sign-in will now be required for every request.{RESET}")
    else:
        print(f"{YELLOW}• Anonymous requests will auto-sign in to a local admin profile (created on first request).{RESET}")

    if not assume_yes:
        confirm = input("Restart Dotori services now to apply this change? (y/N): ").strip().lower()
        if confirm not in ("y", "yes"):
            print(f"{YELLOW}• Not restarted. Run 'python install.py --restart' when ready.{RESET}")
            return True
    paused = handle_pause_cli("production")
    return paused and run_services(_saved_operation_mode())


def handle_accounts_cli(mode):
    if mode == "list":
        ok, stdout, stderr = run_command(f"{COMPOSE_COMMAND} exec -T app python manage.py list_users")
        if ok:
            print(stdout)
        else:
            print(f"{RED}[ERROR] {stderr or 'Could not list accounts. Is the app service running?'}{RESET}")
        return ok
    return False


def change_embedding_runtime_cli(
    *,
    priority_preset="balanced",
    scope="production",
):
    if _saved_operation_mode() == "3":
        print(
            f"{YELLOW}• Embedding runtime is disabled in Basic mode.{RESET}"
        )
        return False

    scope_cfg = SCOPE_CONFIG[scope]
    compose_command = f"docker compose -f {scope_cfg.compose_file}"
    print_header(f"Change Embedding Runtime ({scope})")

    # Document processing is paused while the candidate corpus is built. The active
    # pointer remains unchanged until the management command validates full
    # coverage.
    run_command(
        f"{compose_command} stop dotori-document"
    )
    command = (
        f"{compose_command} run --rm app python manage.py "
        f"change_embedding_runtime --scope {scope} "
        f"--preset {priority_preset} --activate"
    )
    ok = run_interactive_command(command)

    restart_ok, _stdout, restart_error = run_command(
        f"{compose_command} up --no-build -d --force-recreate "
        "app dotori-document"
    )
    if not ok:
        print(
            f"{RED}[ERROR] Embedding candidate failed. The previous active "
            f"runtime was preserved.{RESET}"
        )
        if not restart_ok:
            print(f"{RED}[ERROR] Worker recovery failed: {restart_error}{RESET}")
        return False
    if not restart_ok:
        print(f"{RED}[ERROR] Runtime activated but worker restart failed: {restart_error}{RESET}")
        rollback_ok = run_interactive_command(
            f"{compose_command} run --rm app python manage.py "
            f"rollback_embedding_runtime --scope {scope}"
        )
        if rollback_ok:
            run_command(
                f"{compose_command} up --no-build -d --force-recreate "
                "app dotori-document"
            )
            print(
                f"{YELLOW}• Previous embedding generation restored after "
                f"worker restart failure.{RESET}"
            )
        return False

    inspect_ok, stdout, stderr = run_command(
        f"{compose_command} exec -T app python manage.py "
        f"inspect_embedding_runtime --scope {scope}"
    )
    if inspect_ok:
        print(stdout)
    else:
        print(f"{RED}[ERROR] Runtime inspection failed: {stderr}{RESET}")
    return inspect_ok


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Dotori installation and operations CLI")
    parser.add_argument("--run", action="store_true", help="Start services using the saved configuration")
    parser.add_argument("--restart", action="store_true", help="Pause and restart services")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild images and restart the active runtime")
    parser.add_argument("--retry-llm", action="store_true", help="Retry starting the local LLM runtime")
    parser.add_argument("--stop", action="store_true", help="Pause services (containers preserved)")
    parser.add_argument("--shutdown", action="store_true", help="Fully remove containers and networks")
    parser.add_argument("--status", action="store_true", help="Show server status")
    parser.add_argument("--change-llm", action="store_true", help="Run the interactive LLM runtime selection wizard")
    parser.add_argument("--change-embedding", action="store_true", help="Build and activate a verified embedding runtime generation")
    parser.add_argument(
        "--embedding-priority",
        choices=["speed", "balanced", "quality"],
        default="balanced",
        help="Server-wide embedding catalog preset used with --change-embedding",
    )
    parser.add_argument("--remove-llm", action="store_true", help="Stop and remove the current LLM runtime")
    parser.add_argument("--login", choices=["enable", "disable"], help="Require real sign-in, or return to no-login personal mode")
    parser.add_argument("--accounts", choices=["list"], help="Manage local accounts")

    parser.add_argument("--scope", choices=list(SCOPE_CONFIG), default="production", help="Runtime scope to target")
    parser.add_argument("--json-output", action="store_true", help="Print machine-readable JSON output (--status, --network-access-status)")
    parser.add_argument("--skip-file-io", action="store_true", help="Skip the file I/O pipeline check in --status")
    parser.add_argument("--cluster-mode", action="store_true", help="Use cluster-mode detection with --change-llm")
    parser.add_argument("--keep-weights", action="store_true", help="Keep cached model weights when switching runtimes")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompts (--remove-llm, --login)")

    network = parser.add_argument_group("network access")
    network.add_argument("--network-access-create", action="store_true", help="Create external access configuration files")
    network.add_argument("--network-access-open", action="store_true", help="Open the external access configuration folder")
    network.add_argument("--network-access-connect", action="store_true", help="Connect the external access module")
    network.add_argument("--network-access-disconnect", action="store_true", help="Disconnect the external access module")
    network.add_argument("--network-access-status", action="store_true", help="Show external access status")

    return parser


def handle_pause_cli(scope="production"):
    print_header(f"Pause Dotori Services ({scope})")
    scope_cfg = SCOPE_CONFIG[scope]
    compose_command = f"docker compose -f {scope_cfg.compose_file}"

    ok, _stdout, stderr = run_command(
        f"{compose_command} --profile direct-https stop"
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
        f"{compose_command} --profile direct-https down"
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
    args = build_arg_parser().parse_args()

    network_result = handle_network_access_cli(args)
    if network_result is not None:
        if not network_result:
            sys.exit(1)
        return
    if args.change_llm:
        initialize_llm_runtime_config(
            "1",
            cluster_mode=args.cluster_mode,
            keep_weights=args.keep_weights,
        )
        return
    if args.change_embedding:
        ok = change_embedding_runtime_cli(
            priority_preset=args.embedding_priority,
            scope=args.scope,
        )
        sys.exit(0 if ok else 1)
    if args.remove_llm:
        remove_llm_runtime_cli(assume_yes=args.yes)
        return
    if args.shutdown:
        ok = handle_shutdown_cli(args.scope)
        sys.exit(0 if ok else 1)
    if args.stop:
        ok = handle_pause_cli(args.scope)
        sys.exit(0 if ok else 1)
    if args.status:
        handle_server_status_cli(
            json_output=args.json_output,
            skip_file_io=args.skip_file_io,
            scope=args.scope,
        )
        return

    _ensure_env_file_exists()

    if args.login:
        ok = handle_login_cli(args.login, assume_yes=args.yes)
        sys.exit(0 if ok else 1)

    if args.accounts:
        ok = handle_accounts_cli(args.accounts)
        sys.exit(0 if ok else 1)

    if args.restart:
        paused = handle_pause_cli("production")
        ok = paused and run_services(_saved_operation_mode())
        sys.exit(0 if ok else 1)

    if args.retry_llm:
        if _saved_operation_mode() != "1":
            print(f"{YELLOW}• Local LLM retry is available only in Full Local AI RAG mode.{RESET}")
            return
        ok = run_services(_saved_operation_mode())
        sys.exit(0 if ok else 1)

    if args.rebuild:
        ok = run_services(
            _saved_operation_mode(),
            build_images=True,
            force_recreate=True,
            rebuild_runtime=True,
        )
        sys.exit(0 if ok else 1)

    # A normal run reuses existing images and the active runtime generation.
    if args.run:
        ok = run_services(_saved_operation_mode())
        sys.exit(0 if ok else 1)

    # Run Wizard
    detect_hardware()

    print_header("Select Operation Mode")
    print(f"{BOLD}[1] Full Local AI RAG Mode{RESET}")
    print("    - Run local LLM answer generation and embeddings.")
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
    rag_priority = "balanced"
    updates["DOTORI_OPERATION_MODE"] = {
        "1": "rag",
        "2": "search",
        "3": "basic",
    }[mode]

    if mode in ("1", "2"):
        # Optional: only needed for gated/access-restricted models on Hugging Face.
        print("\n" + "-" * 60)
        hf_token = input(
            "Hugging Face token (optional, only needed for gated models — press Enter to skip): "
        ).strip()
        updates["HF_TOKEN"] = hf_token

    if mode == "1":
        rag_priority = select_rag_priority()

    # Query understanding (QUERY_UNDERSTANDING_*) is an experimental, unfinished
    # feature that's off by default in .env.example regardless of operation
    # mode — the wizard no longer overrides it.

    # Write configs to the installation environment file.
    write_env_file(ENV_FILE, ENV_FILE, updates)
    if mode in ("1", "2"):
        embedding_entry = initialize_embedding_runtime_config(rag_priority)
        print(
            f"{GREEN}• Embedding runtime configured from catalog: "
            f"{embedding_entry.display_name} ({embedding_entry.id}){RESET}"
        )
    
    # Auto-generate Windows bat launcher
    create_windows_launcher()

    # Launch confirmation
    print("\n" + "-" * 60)
    launch = input("Start the Dotori Docker services now? (y/N): ").strip().lower()
    if launch in ("y", "yes"):
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
