from pathlib import Path

import pytest
import yaml


pytestmark = pytest.mark.unit


COMPOSE_PATH = Path("/workspace/docker-compose.yml")
@pytest.fixture(scope="module")
def compose_config() -> dict:
    assert COMPOSE_PATH.is_file(), (
        "The test service must mount docker-compose.yml read-only at "
        f"{COMPOSE_PATH}."
    )
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def test_parser_and_embedding_images_install_only_their_own_dependencies(compose_config):
    services = compose_config["services"]
    embedding = services["embedding-executor"]
    parser = services["parser-executor"]

    assert embedding["image"] == "dotori-embedding:local"
    assert embedding["build"]["args"] == {
        "INSTALL_AI_DEPS": "1",
        "INSTALL_PARSER_DOCLING": "0",
        "INSTALL_TEST_DEPS": "0",
    }
    assert parser["image"] == "dotori-parser:local"
    assert parser["build"]["args"] == {
        "INSTALL_AI_DEPS": "0",
        "INSTALL_PARSER_DOCLING": "1",
        "INSTALL_TEST_DEPS": "0",
    }
    assert services["dotori-orchestrator"]["image"] == "dotori-orchestrator:local"
    assert services["dotori-orchestrator"]["build"]["args"]["INSTALL_AI_DEPS"] == "0"


def test_orchestrator_is_the_only_celery_consumer(compose_config):
    service = compose_config["services"]["dotori-orchestrator"]
    command = service["command"]

    assert service["environment"]["DOTORI_SERVICE_ROLE"] == "orchestrator"
    assert command[:6] == ["celery", "-A", "config", "worker", "-P", "threads"]
    assert command[command.index("-Q") + 1] == "parse,embed"
    assert "--hostname=orchestrator@%h" in command
    assert "--prefetch-multiplier=1" in command
    assert any(
        item.startswith("--concurrency=${ORCHESTRATOR_CONCURRENCY:-")
        for item in command
    )

    healthcheck = service["healthcheck"]
    assert healthcheck["test"][:3] == ["CMD", "python", "-c"]
    probe = healthcheck["test"][3]
    assert "app.control.ping" in probe
    assert "'orchestrator@' + os.uname().nodename" in probe
    assert "timeout=2" in probe
    assert healthcheck["interval"] == "10s"
    assert healthcheck["timeout"] == "5s"
    assert healthcheck["retries"] == 3
    assert healthcheck["start_period"] == "20s"


def test_executors_are_http_processes(compose_config):
    services = compose_config["services"]
    parser = services["parser-executor"]
    assert parser["environment"]["DOTORI_EXECUTOR_ROLE"] == "parser"
    assert parser["command"][:2] == ["gunicorn", "config.wsgi:application"]
    assert "/readyz" in parser["healthcheck"]["test"][3]

    embedding = services["embedding-executor"]
    assert embedding["environment"]["DOTORI_EXECUTOR_ROLE"] == "embedding"


def test_embedding_model_role_remains_single_process(compose_config):
    service = compose_config["services"]["embedding-executor"]
    command = service["command"]

    assert service["environment"]["DOTORI_SERVICE_ROLE"] == "embedding-model"
    assert command[:2] == ["bash", "-lc"]
    assert "DOTORI_EMBEDDING_MODEL_PROCESS=1" in command[2]
    assert "--workers 1" in command[2]
    assert service["healthcheck"]["test"][:3] == ["CMD", "python", "-c"]
    assert "/readyz" in service["healthcheck"]["test"][3]
