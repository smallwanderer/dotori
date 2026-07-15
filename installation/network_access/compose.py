from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Sequence

from .files import CONFIG_RELATIVE_PATH, PROVIDER_ENV_NAME, validate_configuration


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run(
    args: Sequence[str],
    *,
    project_root: Path,
    runner: Runner,
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(args),
        cwd=project_root,
        check=False,
        text=True,
        capture_output=True,
    )


def _compose_prefix(*, external: bool) -> list[str]:
    command = ["docker", "compose", "-f", "docker-compose.yml", "--env-file", ".env"]
    if external:
        command.extend(["--env-file", str(CONFIG_RELATIVE_PATH / PROVIDER_ENV_NAME)])
    return command


def _command_error(action: str, result: subprocess.CompletedProcess[str]) -> RuntimeError:
    detail = (result.stderr or result.stdout or "unknown error").strip()
    return RuntimeError(f"{action} failed: {detail}")


def connect(
    project_root: Path | str = ".",
    *,
    runner: Runner = subprocess.run,
) -> None:
    root = Path(project_root).resolve()
    validate_configuration(root)
    command = _compose_prefix(external=True) + [
        "--profile",
        "direct-https",
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        "60",
        "--force-recreate",
        "app",
        "nginx",
    ]
    result = _run(command, project_root=root, runner=runner)
    if result.returncode == 0:
        return

    _run(
        _compose_prefix(external=False) + ["--profile", "direct-https", "stop", "nginx"],
        project_root=root,
        runner=runner,
    )
    _run(
        _compose_prefix(external=False)
        + ["up", "-d", "--wait", "--wait-timeout", "60", "--force-recreate", "app"],
        project_root=root,
        runner=runner,
    )
    raise _command_error("External access connection", result)


def disconnect(
    project_root: Path | str = ".",
    *,
    runner: Runner = subprocess.run,
) -> None:
    root = Path(project_root).resolve()
    stop_result = _run(
        _compose_prefix(external=False) + ["--profile", "direct-https", "stop", "nginx"],
        project_root=root,
        runner=runner,
    )
    _run(
        _compose_prefix(external=False) + ["--profile", "direct-https", "rm", "-f", "nginx"],
        project_root=root,
        runner=runner,
    )
    app_result = _run(
        _compose_prefix(external=False)
        + ["up", "-d", "--wait", "--wait-timeout", "60", "--force-recreate", "app"],
        project_root=root,
        runner=runner,
    )
    if stop_result.returncode != 0 and "no such service" not in stop_result.stderr.lower():
        raise _command_error("External access shutdown", stop_result)
    if app_result.returncode != 0:
        raise _command_error("Local app restoration", app_result)


def status(
    project_root: Path | str = ".",
    *,
    runner: Runner = subprocess.run,
) -> dict[str, object]:
    root = Path(project_root).resolve()
    config_root = root / CONFIG_RELATIVE_PATH
    configured = (config_root / PROVIDER_ENV_NAME).is_file() and (
        config_root / "nginx/default.conf.template"
    ).is_file()
    result = _run(
        _compose_prefix(external=False)
        + ["--profile", "direct-https", "ps", "--status", "running", "--services", "nginx"],
        project_root=root,
        runner=runner,
    )
    available = result.returncode == 0
    running = available and "nginx" in result.stdout.split()
    return {
        "available": available,
        "configured": configured,
        "running": running,
        "mode": "external" if running else ("local" if available else "unavailable"),
        "configuration_directory": str(config_root),
        "error": "" if available else (result.stderr or result.stdout).strip(),
    }
