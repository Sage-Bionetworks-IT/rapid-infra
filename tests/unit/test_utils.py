import copy
import json

import pytest
import yaml

from src.utils import _deep_merge, load_context_config, validate_config

VALID = {
    "PROJECT_NAME": "rapid",
}


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
            {**VALID, "SHARED": "from_base", "SYNC": {"max_concurrency": 10, "retention": 90}},
        )
        _write(tmp_path / "dev.yaml", {"SHARED": "from_env", "SYNC": {"max_concurrency": 5}})
        assert load_context_config("dev", str(tmp_path)) == {
            **VALID,
            "SHARED": "from_env",
            "SYNC": {"max_concurrency": 5, "retention": 90},
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


class TestValidateConfig:
    def test_valid_config_passes(self):
        validate_config(copy.deepcopy(VALID))

    def test_missing_project_name_rejected(self):
        with pytest.raises(ValueError, match="Missing config key PROJECT_NAME"):
            validate_config({})

    def test_invalid_aws_account_rejected(self):
        with pytest.raises(ValueError, match="12-digit"):
            validate_config({**VALID, "AWS_ACCOUNT": "dnt-dev"})
