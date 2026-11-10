import copy

import pytest
import yaml

from src.utils import load_context_config, validate_config

VALID = {
    "PROJECT_NAME": "rapid",
}


def _with(**changes):
    config = copy.deepcopy(VALID)
    for path, value in changes.items():
        target = config
        *parents, leaf = path.split("__")
        for p in parents:
            target = target[int(p)] if p.isdigit() else target[p]
        target[int(leaf) if leaf.isdigit() else leaf] = value
    return config


def test_valid_config_passes():
    validate_config(copy.deepcopy(VALID))


@pytest.mark.parametrize("env", ["dev", "prod", "sandbox"])
def test_repo_configs_load(env):
    config = load_context_config(env)
    assert config["PROJECT_NAME"] == "rapid"


def test_env_overrides_base_per_top_level_key(tmp_path):
    (tmp_path / "base.yaml").write_text(yaml.safe_dump({**VALID, "ALARM_EMAIL": "base@x"}))
    (tmp_path / "dev.yaml").write_text(yaml.safe_dump({"ALARM_EMAIL": "dev@x"}))
    config = load_context_config("dev", config_dir=str(tmp_path))
    assert config["ALARM_EMAIL"] == "dev@x"
    assert config["PROJECT_NAME"] == VALID["PROJECT_NAME"]


def test_unknown_env_rejected():
    with pytest.raises(ValueError, match="Invalid environment"):
        load_context_config("stage")


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"AWS_ACCOUNT": "dnt-dev"}, "12-digit"),
    ],
)
def test_invalid_config_rejected(changes, message):
    with pytest.raises(ValueError, match=message):
        validate_config(_with(**changes))


def test_missing_project_name_rejected():
    with pytest.raises(ValueError, match="Missing config key PROJECT_NAME"):
        validate_config({})
