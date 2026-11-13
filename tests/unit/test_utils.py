import copy
import json

import pytest
import yaml

from src.utils import _deep_merge, load_context_config, validate_config

VALID = {
    "PROJECT_NAME": "rapid",
    "SYNAPSE": {
        "aws_account_id": "325565585839",
        "service_account_id": "1234567",
        "project_folders": ["raw", "processed/level_1"],
    },
    "SYNC": {"sidecar_filename": "sidecar.json"},
    "BUCKETS": [
        {"id": "a", "bucket_name": "rapid-test-a", "synapse_project_name": "RAPID A"},
        {"id": "b", "bucket_name": "rapid-test-b", "synapse_project_name": "RAPID B"},
    ],
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


def _write(path, data):
    with open(path, "w") as f:
        if path.suffix == ".json":
            json.dump(data, f)
        else:
            yaml.dump(data, f)


class TestDeepMerge:
    def test_flat_merge(self):
        assert _deep_merge({"a": 1, "b": 2}, {"b": 3, "c": 4}) == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge_preserves_base_keys(self):
        base = {"SYNC": {"max_concurrency": 10, "results_retention_days": 90}}
        result = _deep_merge(base, {"SYNC": {"max_concurrency": 5}})
        assert result == {"SYNC": {"max_concurrency": 5, "results_retention_days": 90}}

    def test_lists_are_replaced(self):
        assert _deep_merge({"a": [1, 2]}, {"a": [3]}) == {"a": [3]}

    def test_does_not_mutate_base(self):
        base = {"a": {"b": 1}}
        _deep_merge(base, {"a": {"b": 2}})
        assert base == {"a": {"b": 1}}

    def test_override_dict_with_non_dict(self):
        assert _deep_merge({"a": {"b": 1}}, {"a": "string"}) == {"a": "string"}


class TestLoadContextConfig:
    @pytest.mark.parametrize("env", ["dev", "prod", "sandbox"])
    def test_repo_configs_load(self, env):
        config = load_context_config(env)
        assert config["PROJECT_NAME"] == "rapid"

    @pytest.mark.parametrize("invalid_env", ["stage", "production", "development", ""])
    def test_invalid_environment_rejected(self, invalid_env):
        with pytest.raises(ValueError, match=f"Invalid environment '{invalid_env}'"):
            load_context_config(invalid_env)

    def test_no_config_file_raises_file_not_found_error(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="No config file found for environment 'dev'"):
            load_context_config("dev", str(tmp_path))

    @pytest.mark.parametrize(
        "ext1, ext2", [(".yaml", ".json"), (".yaml", ".yml"), (".yml", ".json")]
    )
    def test_multiple_config_files_rejected(self, tmp_path, ext1, ext2):
        _write(tmp_path / f"dev{ext1}", VALID)
        _write(tmp_path / f"dev{ext2}", VALID)
        with pytest.raises(ValueError, match="Multiple config files found for environment 'dev'"):
            load_context_config("dev", str(tmp_path))

    @pytest.mark.parametrize("ext", [".yaml", ".yml", ".json"])
    def test_load_config_files(self, tmp_path, ext):
        data = {**VALID, "TAGS": {"Environment": "dev"}}
        _write(tmp_path / f"dev{ext}", data)
        assert load_context_config("dev", str(tmp_path)) == data

    def test_base_config_deep_merging(self, tmp_path):
        _write(
            tmp_path / "base.yaml",
            {**VALID, "SHARED": "from_base", "NESTED": {"concurrency": 10, "retention": 90}},
        )
        _write(tmp_path / "dev.yaml", {"SHARED": "from_env", "NESTED": {"concurrency": 5}})
        assert load_context_config("dev", str(tmp_path)) == {
            **VALID,
            "SHARED": "from_env",
            "NESTED": {"concurrency": 5, "retention": 90},
        }

    def test_no_base_config(self, tmp_path):
        _write(tmp_path / "dev.yaml", VALID)
        assert load_context_config("dev", str(tmp_path)) == VALID

    def test_empty_env_config(self, tmp_path):
        _write(tmp_path / "base.yaml", VALID)
        (tmp_path / "dev.yaml").write_text("")
        assert load_context_config("dev", str(tmp_path)) == VALID

    @pytest.mark.parametrize(
        "ext, content, exception",
        [
            (".yaml", "invalid: yaml: content: [unclosed", yaml.YAMLError),
            (".json", '{"invalid": json content}', json.JSONDecodeError),
        ],
    )
    def test_invalid_config_content(self, tmp_path, ext, content, exception):
        (tmp_path / f"dev{ext}").write_text(content)
        with pytest.raises(exception):
            load_context_config("dev", str(tmp_path))

    def test_merged_config_is_validated(self, tmp_path):
        _write(tmp_path / "dev.yaml", {"TAGS": {}})
        with pytest.raises(ValueError, match="Missing config key PROJECT_NAME"):
            load_context_config("dev", str(tmp_path))


def test_valid_config_passes():
    validate_config(copy.deepcopy(VALID))


def test_missing_project_name_rejected():
    with pytest.raises(ValueError, match="Missing config key PROJECT_NAME"):
        validate_config({})


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"BUCKETS__1__synapse_project_name": "RAPID A"}, "used by another bucket"),
        ({"BUCKETS__1__bucket_name": "rapid-test-a"}, "used by another bucket"),
        ({"BUCKETS__1__id": "a"}, "used by another bucket"),
        ({"BUCKETS__0__synapse_project_name": "a/b"}, "synapse_project_name"),
        ({"BUCKETS__0__sts_folder": "a/b"}, "sts_folder must be a folder name"),
        ({"BUCKETS__0__sts_folder": ""}, "sts_folder must be a folder name"),
        ({"SYNAPSE__project_folders": ["sts/x"]}, "must not create folders in 'sts'"),
        ({"SYNAPSE__project_folders": "raw"}, "project_folders must be a list"),
        ({"SYNAPSE__project_folders": ["raw//x"]}, "project_folders"),
        ({"SYNAPSE__project_folders": ["raw:x"]}, "project_folders"),
        ({"BUCKETS__0__bucket_name": "Not_A_Bucket"}, "not a valid S3 bucket name"),
        ({"BUCKETS__0__id": "Has Spaces"}, "must match"),
        ({"BUCKETS__0__synapse_owner_ids": ["jdoe"]}, "not a numeric Synapse ID"),
        ({"BUCKETS__0__s3_user_arns": "arn:aws:iam::1:root"}, "must be a list"),
        ({"SYNAPSE__service_account_id": "TODO"}, "service_account_id"),
        ({"SYNC__sidecar_filename": "a/sidecar.json"}, "sidecar_filename"),
        ({"AWS_ACCOUNT": "dnt-dev"}, "12-digit"),
        ({"BUCKETS__0__sts_folder": "a/b"}, "sts_folder"),
        ({"BUCKETS__0__sts_folder": "  "}, "sts_folder"),
        ({"BUCKETS__0__kms_encryption": "yes"}, "kms_encryption"),
        ({"S3_DATA_EVENTS": {"enabled": True}}, "retention_days"),
        ({"S3_DATA_EVENTS": {"enabled": True, "retention_days": 0}}, "retention_days"),
    ],
)
def test_invalid_config_rejected(changes, message):
    with pytest.raises(ValueError, match=message):
        validate_config(_with(**changes))


def test_missing_bucket_field_rejected():
    config = copy.deepcopy(VALID)
    del config["BUCKETS"][0]["synapse_project_name"]
    with pytest.raises(ValueError, match=r"BUCKETS\[0\].synapse_project_name is required"):
        validate_config(config)


def test_data_events_off_needs_no_retention():
    validate_config(_with(S3_DATA_EVENTS={"enabled": False}))


@pytest.mark.parametrize("env, enabled", [("dev", True), ("prod", True), ("sandbox", False)])
def test_data_events_on_for_rapid_off_for_sandbox(env, enabled):
    data_events = load_context_config(env)["S3_DATA_EVENTS"]
    assert bool(data_events.get("enabled")) is enabled
    if enabled:
        assert data_events["retention_days"] == 365
