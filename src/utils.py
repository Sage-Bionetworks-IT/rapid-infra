import json
import re
from pathlib import Path
from typing import Any, Dict

import yaml

# sandbox: a personal test account, deployed from a laptop (config/sandbox.yaml); never by CI
VALID_ENVS = {"dev", "prod", "sandbox"}

_BUCKET_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
# https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html (the common subset)
_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_SYNAPSE_PROJECT = re.compile(r"^syn\d+$")
_SYNAPSE_PRINCIPAL = re.compile(r"^\d+$")

_BUCKET_LIST_FIELDS = ("synapse_owner_ids", "s3_user_arns", "s3_admin_arns", "deny_delete_arns")


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_context_config(env_name: str, config_dir: str = "config") -> Dict[str, Any]:
    """
    Load AWS CDK context configuration from a YAML or JSON file.

    Supports:
      - .yaml/.yml OR .json (but not both)
      - base.yaml deep-merged with the environment config (env wins; nested dicts are
        merged key by key, lists and scalars are replaced)
      - Only the environments in VALID_ENVS

    Raises:
      - ValueError if the environment is invalid, multiple config files exist, or the merged
        config is invalid
      - FileNotFoundError if the expected config file is not found
    """
    if env_name not in VALID_ENVS:
        raise ValueError(
            f"Invalid environment '{env_name}'. Must be one of: {', '.join(sorted(VALID_ENVS))}"
        )

    base_path = Path(config_dir) / "base.yaml"
    env_yaml = Path(config_dir) / f"{env_name}.yaml"
    env_yml = Path(config_dir) / f"{env_name}.yml"
    env_json = Path(config_dir) / f"{env_name}.json"

    base_config = _read_file(base_path) if base_path.exists() else {}

    env_files = [p for p in [env_yaml, env_yml, env_json] if p.exists()]
    if not env_files:
        raise FileNotFoundError(
            f"No config file found for environment '{env_name}' "
            f"in {config_dir}. Expected one of: {env_yaml}, {env_yml}, {env_json}"
        )
    if len(env_files) > 1:
        raise ValueError(
            f"Multiple config files found for environment '{env_name}': {env_files}. "
            f"Use only one (.yaml/.yml OR .json)."
        )

    config = _deep_merge(base_config, _read_file(env_files[0]))
    validate_config(config)
    return config


def _read_file(path: Path) -> Dict[str, Any]:
    """Read a YAML or JSON file into a dictionary."""
    with open(path, "r") as f:
        if path.suffix in (".yaml", ".yml"):
            return yaml.safe_load(f) or {}
        elif path.suffix == ".json":
            return json.load(f)
        else:
            raise ValueError(f"Unsupported config file type: {path.suffix}")


def validate_config(config: Dict[str, Any]) -> None:
    """Fail synth early, with the offending key, instead of at deploy time."""
    for key in ("PROJECT_NAME", "SYNAPSE"):
        if key not in config:
            raise ValueError(f"Missing config key {key}")

    service_account = str(config["SYNAPSE"].get("service_account_id", ""))
    if not _SYNAPSE_PRINCIPAL.match(service_account):
        raise ValueError(
            f"SYNAPSE.service_account_id must be a numeric Synapse user ID, got '{service_account}'"
        )

    account = config.get("AWS_ACCOUNT")
    if account is not None and not re.fullmatch(r"\d{12}", str(account)):
        raise ValueError(f"AWS_ACCOUNT must be a 12-digit AWS account ID, got '{account}'")

    seen = {"id": set(), "bucket_name": set(), "synapse_project_id": set()}
    for i, bucket in enumerate(config.get("BUCKETS") or []):
        where = f"BUCKETS[{i}]"
        _validate_bucket(where, bucket)
        for field, values in seen.items():
            if bucket[field] in values:
                # Each provider's bucket indexes into its own project, visible only to that
                # provider, so a project ID is as unique as the bucket itself
                raise ValueError(f"{where}.{field} '{bucket[field]}' is used by another bucket")
            values.add(bucket[field])


def _validate_bucket(where: str, bucket: Dict[str, Any]) -> None:
    for field in ("id", "bucket_name", "synapse_project_id"):
        if not bucket.get(field):
            raise ValueError(f"{where}.{field} is required")
    if not _BUCKET_ID.match(bucket["id"]):
        raise ValueError(f"{where}.id '{bucket['id']}' must match {_BUCKET_ID.pattern}")
    if not _BUCKET_NAME.match(bucket["bucket_name"]):
        raise ValueError(
            f"{where}.bucket_name '{bucket['bucket_name']}' is not a valid S3 bucket name"
        )
    if not _SYNAPSE_PROJECT.match(bucket["synapse_project_id"]):
        raise ValueError(
            f"{where}.synapse_project_id '{bucket['synapse_project_id']}' must look like syn123"
        )
    for field in _BUCKET_LIST_FIELDS:
        if not isinstance(bucket.get(field, []), list):
            raise ValueError(f"{where}.{field} must be a list")
    for owner in bucket.get("synapse_owner_ids", []):
        if not _SYNAPSE_PRINCIPAL.match(str(owner)):
            raise ValueError(f"{where}.synapse_owner_ids: '{owner}' is not a numeric Synapse ID")
