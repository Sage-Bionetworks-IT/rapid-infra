import pytest

from src.synapse_template import load_template, validate_template
from src.utils import VALID_ENVS, load_context_config


def test_load_template(tmp_path):
    path = tmp_path / "dev.yaml"
    path.write_text("projects:\n  - name: RAPID A\n    bucket: a\n")
    assert load_template(path) == {"projects": [{"name": "RAPID A", "bucket": "a"}]}


def test_empty_template_has_no_projects(tmp_path):
    path = tmp_path / "prod.yaml"
    path.write_text("")
    assert load_template(path) == {"projects": []}


@pytest.mark.parametrize(
    "projects, message",
    [
        ("RAPID A", "projects must be a list"),
        (["RAPID A"], r"projects\[0\] must be a mapping"),
        ([{"bucket": "a"}], r"projects\[0\].name"),
        ([{"name": "  ", "bucket": "a"}], r"projects\[0\].name"),
        ([{"name": "a/b", "bucket": "a"}], r"projects\[0\].name"),
        ([{"name": "RAPID A"}], r"projects\[0\].bucket"),
        ([{"name": "RAPID A", "bucket": "Not An Id"}], r"projects\[0\].bucket"),
        (
            [{"name": "RAPID A", "bucket": "a"}, {"name": "RAPID A", "bucket": "b"}],
            r"projects\[1\].name 'RAPID A' is used by another project",
        ),
        (
            [{"name": "RAPID A", "bucket": "a"}, {"name": "RAPID B", "bucket": "a"}],
            r"projects\[1\].bucket 'a' is used by another project",
        ),
    ],
)
def test_invalid_template_rejected(projects, message):
    with pytest.raises(ValueError, match=message):
        validate_template({"projects": projects})


@pytest.mark.parametrize("env", sorted(VALID_ENVS))
def test_repo_templates_reference_configured_buckets(env):
    template = load_template(f"config/synapse/{env}.yaml")
    bucket_ids = {b["id"] for b in load_context_config(env).get("BUCKETS") or []}
    for project in template["projects"]:
        assert project["bucket"] in bucket_ids, f"{env}: {project['name']} → {project['bucket']}"
