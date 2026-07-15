import subprocess
from pathlib import Path

import pytest

from installation.network_access import (
    ConfigurationError,
    connect,
    create_configuration_files,
    disconnect,
    status,
    validate_configuration,
)


pytestmark = pytest.mark.unit


def _write_valid_provider_env(project_root: Path) -> None:
    provider_env = project_root / "data/config/network_access/provider.env"
    provider_env.write_text(
        "\n".join(
            [
                "DOTORI_EXTERNAL_DOMAIN=docs.example.com",
                "DOTORI_DJANGO_ALLOWED_HOSTS=docs.example.com",
                "DOTORI_DJANGO_CSRF_TRUSTED_ORIGINS=https://docs.example.com",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (project_root / ".env").write_text("DJANGO_SECRET_KEY=test\n", encoding="utf-8")


def _completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def test_create_configuration_files_preserves_operator_edits(tmp_path):
    first = create_configuration_files(tmp_path)
    provider_env = tmp_path / "data/config/network_access/provider.env"
    provider_env.write_text("OPERATOR_EDIT=keep\n", encoding="utf-8")

    second = create_configuration_files(tmp_path)

    assert len(first["created"]) == 2
    assert second["created"] == []
    assert len(second["preserved"]) == 2
    assert provider_env.read_text(encoding="utf-8") == "OPERATOR_EDIT=keep\n"


def test_validate_configuration_reports_empty_required_values(tmp_path):
    create_configuration_files(tmp_path)
    (tmp_path / ".env").write_text("DJANGO_SECRET_KEY=test\n", encoding="utf-8")

    with pytest.raises(ConfigurationError) as exc_info:
        validate_configuration(tmp_path)

    assert "DOTORI_EXTERNAL_DOMAIN" in str(exc_info.value)
    assert "provider.env" in str(exc_info.value)


def test_connect_uses_provider_file_without_prompting(tmp_path):
    create_configuration_files(tmp_path)
    _write_valid_provider_env(tmp_path)
    commands = []

    def runner(args, **kwargs):
        commands.append(args)
        return _completed(args)

    connect(tmp_path, runner=runner)

    assert len(commands) == 1
    command = commands[0]
    assert command[:4] == ["docker", "compose", "-f", "docker-compose.yml"]
    assert "data/config/network_access/provider.env" in command
    assert command[-2:] == ["app", "nginx"]


def test_connect_failure_restores_local_app(tmp_path):
    create_configuration_files(tmp_path)
    _write_valid_provider_env(tmp_path)
    commands = []

    def runner(args, **kwargs):
        commands.append(args)
        if len(commands) == 1:
            return _completed(args, returncode=1, stderr="nginx failed")
        return _completed(args)

    with pytest.raises(RuntimeError, match="nginx failed"):
        connect(tmp_path, runner=runner)

    assert commands[1][-2:] == ["stop", "nginx"]
    assert commands[2][-1] == "app"
    assert "--force-recreate" in commands[2]
    assert "--wait" in commands[2]
    assert "data/config/network_access/provider.env" not in commands[2]


def test_disconnect_stops_nginx_and_recreates_only_app(tmp_path):
    commands = []

    def runner(args, **kwargs):
        commands.append(args)
        return _completed(args)

    disconnect(tmp_path, runner=runner)

    assert commands[0][-2:] == ["stop", "nginx"]
    assert commands[1][-3:] == ["rm", "-f", "nginx"]
    assert commands[2][-1] == "app"
    assert "--force-recreate" in commands[2]
    assert "--wait" in commands[2]
    assert all("db" not in command for command in commands)
    assert all("redis" not in command for command in commands)


def test_status_distinguishes_prepared_and_running(tmp_path):
    create_configuration_files(tmp_path)

    stopped = status(
        tmp_path,
        runner=lambda args, **kwargs: _completed(args, stdout=""),
    )
    running = status(
        tmp_path,
        runner=lambda args, **kwargs: _completed(args, stdout="nginx\n"),
    )

    assert stopped["configured"] is True
    assert stopped["mode"] == "local"
    assert running["running"] is True
    assert running["mode"] == "external"
