import re
from pathlib import Path
from typing import Any, Dict

import yaml

# sandbox: a personal test account, deployed from a laptop (config/sandbox.yaml); never by CI
VALID_ENVS = {"dev", "prod", "sandbox"}


def load_context_config(env_name: str, config_dir: str = "config") -> Dict[str, Any]:
    """
    Load config/base.yaml shallow-merged with config/{env_name}.yaml (env wins per top-level
    key), then validate it.

    Raises ValueError for an unknown env or an invalid config, FileNotFoundError when the env
    file is missing.
    """
    if env_name not in VALID_ENVS:
        raise ValueError(
            f"Invalid environment '{env_name}'. Must be one of: {', '.join(sorted(VALID_ENVS))}"
        )

    base_path = Path(config_dir) / "base.yaml"
    env_path = Path(config_dir) / f"{env_name}.yaml"
    if not env_path.exists():
        raise FileNotFoundError(f"No config file for environment '{env_name}': {env_path}")

    base = _read_yaml(base_path) if base_path.exists() else {}
    config = {**base, **_read_yaml(env_path)}
    validate_config(config)
    return config


def _read_yaml(path: Path) -> Dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def validate_config(config: Dict[str, Any]) -> None:
    """Fail synth early, with the offending key, instead of at deploy time."""
    for key in ("PROJECT_NAME",):
        if key not in config:
            raise ValueError(f"Missing config key {key}")

    account = config.get("AWS_ACCOUNT")
    if account is not None and not re.fullmatch(r"\d{12}", str(account)):
        raise ValueError(f"AWS_ACCOUNT must be a 12-digit AWS account ID, got '{account}'")
