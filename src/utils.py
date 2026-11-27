import ipaddress
import json
import re
from pathlib import Path
from typing import Any, Dict

import yaml

# sandbox: a personal test account, deployed from a laptop (config/aws/sandbox.yaml); never by CI
VALID_ENVS = {"dev", "prod", "sandbox"}

# https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html (the common subset),
# without dots: the bucket name is in its stack's name, where dots are not allowed (and AWS
# advises against them, as they break virtual-hosted-style HTTPS)
_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_SYNAPSE_PRINCIPAL = re.compile(r"^\d+$")

_BUCKET_LIST_FIELDS = ("synapse_owner_ids", "s3_user_arns", "s3_admin_arns", "deny_delete_arns")

_NEPTUNE_ENGINE_VERSION = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
_NEPTUNE_FIELDS = (
    "engine_version",
    "serverless_min_capacity",
    "serverless_max_capacity",
    "instance_count",
    "backup_retention_days",
    "preferred_backup_window",
    "preferred_maintenance_window",
    "deletion_protection",
)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_context_config(env_name: str, config_dir: str = "config/aws") -> Dict[str, Any]:
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
    for key in ("PROJECT_NAME", "SYNAPSE", "SYNC"):
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

    sidecar = config["SYNC"].get("sidecar_filename", "")
    if not sidecar or "/" in sidecar:
        raise ValueError(f"SYNC.sidecar_filename must be a file name, got '{sidecar}'")

    seen = set()
    for i, bucket in enumerate(config.get("BUCKETS") or []):
        where = f"BUCKETS[{i}]"
        _validate_bucket(where, bucket)
        if bucket["bucket_name"] in seen:
            raise ValueError(
                f"{where}.bucket_name '{bucket['bucket_name']}' is used by another bucket"
            )
        seen.add(bucket["bucket_name"])

    if (config.get("NEPTUNE") or {}).get("enabled"):
        _validate_neptune(config)


def _validate_bucket(where: str, bucket: Dict[str, Any]) -> None:
    if not bucket.get("bucket_name"):
        raise ValueError(f"{where}.bucket_name is required")
    if not _BUCKET_NAME.match(bucket["bucket_name"]):
        raise ValueError(
            f"{where}.bucket_name '{bucket['bucket_name']}' must match {_BUCKET_NAME.pattern}"
        )
    for field in _BUCKET_LIST_FIELDS:
        if not isinstance(bucket.get(field, []), list):
            raise ValueError(f"{where}.{field} must be a list")
    for owner in bucket.get("synapse_owner_ids", []):
        if not _SYNAPSE_PRINCIPAL.match(str(owner)):
            raise ValueError(f"{where}.synapse_owner_ids: '{owner}' is not a numeric Synapse ID")


def _validate_neptune(config: Dict[str, Any]) -> None:
    cidr = config.get("VPC_CIDR")
    try:
        network = ipaddress.IPv4Network(str(cidr))
    except ValueError:
        raise ValueError(
            f"VPC_CIDR must be an IPv4 network such as 10.0.0.0/24, got '{cidr}'"
        ) from None
    if not network.is_private:
        raise ValueError(f"VPC_CIDR must be a private range, got '{cidr}'")

    neptune = config["NEPTUNE"]
    for field in _NEPTUNE_FIELDS:
        if field not in neptune:
            raise ValueError(f"NEPTUNE.{field} is required")
    if not _NEPTUNE_ENGINE_VERSION.match(str(neptune["engine_version"])):
        raise ValueError(
            f"NEPTUNE.engine_version must look like 1.3.2.1, got '{neptune['engine_version']}'"
        )
    # https://docs.aws.amazon.com/neptune/latest/userguide/neptune-serverless-capacity-scaling.html
    low, high = neptune["serverless_min_capacity"], neptune["serverless_max_capacity"]
    if not 1 <= low <= high <= 128:
        raise ValueError(
            "NEPTUNE.serverless_min_capacity and serverless_max_capacity must be 1 to 128 NCU, "
            f"min no more than max, got {low} and {high}"
        )
    # One writer and up to 15 read replicas
    if not 1 <= neptune["instance_count"] <= 16:
        raise ValueError(f"NEPTUNE.instance_count must be 1 to 16, got {neptune['instance_count']}")
    if not isinstance(neptune.get("cluster_parameters") or {}, dict):
        raise ValueError("NEPTUNE.cluster_parameters must be a mapping")
