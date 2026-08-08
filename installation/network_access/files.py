from __future__ import annotations

import os
import shutil
from pathlib import Path


CONFIG_RELATIVE_PATH = Path("data/config/network_access")
PROVIDER_ENV_NAME = "provider.env"
NGINX_TEMPLATE_RELATIVE_PATH = Path("nginx/default.conf.template")
REQUIRED_ENV_KEYS = (
    "DOTORI_EXTERNAL_DOMAIN",
    "DOTORI_DJANGO_ALLOWED_HOSTS",
    "DOTORI_DJANGO_CSRF_TRUSTED_ORIGINS",
)


class ConfigurationError(ValueError):
    pass


def configuration_directory(project_root: Path | str = ".") -> Path:
    return Path(project_root).resolve() / CONFIG_RELATIVE_PATH


def create_configuration_files(project_root: Path | str = ".") -> dict[str, list[str]]:
    root = Path(project_root).resolve()
    template_root = Path(__file__).resolve().parent / "templates"
    config_root = configuration_directory(root)
    targets = {
        template_root / PROVIDER_ENV_NAME: config_root / PROVIDER_ENV_NAME,
        template_root / "nginx-direct-https.conf": config_root / NGINX_TEMPLATE_RELATIVE_PATH,
    }

    created: list[str] = []
    preserved: list[str] = []
    for source, target in targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            preserved.append(str(target))
            continue
        shutil.copyfile(source, target)
        created.append(str(target))

    provider_env = config_root / PROVIDER_ENV_NAME
    if provider_env.exists():
        try:
            os.chmod(provider_env, 0o600)
        except OSError:
            pass

    return {"created": created, "preserved": preserved}


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def validate_configuration(project_root: Path | str = ".") -> dict[str, str]:
    root = Path(project_root).resolve()
    config_root = configuration_directory(root)
    provider_env = config_root / PROVIDER_ENV_NAME
    nginx_template = config_root / NGINX_TEMPLATE_RELATIVE_PATH

    missing_files = [str(path) for path in (provider_env, nginx_template) if not path.is_file()]
    if missing_files:
        raise ConfigurationError(
            "Missing external access configuration file(s): " + ", ".join(missing_files)
        )

    values = read_env_file(provider_env)
    missing_values = [key for key in REQUIRED_ENV_KEYS if not values.get(key, "").strip()]
    if missing_values:
        raise ConfigurationError(
            f"Set {', '.join(missing_values)} in {provider_env} before connecting."
        )
    if not nginx_template.read_text(encoding="utf-8").strip():
        raise ConfigurationError(f"Nginx configuration is empty: {nginx_template}")
    if not (root / ".env").is_file():
        raise ConfigurationError(f"Base environment file is missing: {root / '.env'}")
    return values
