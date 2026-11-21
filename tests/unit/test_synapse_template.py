import pytest

from src.synapse_template import (
    Ref,
    find_refs,
    load_template,
    parse_ref,
    project_names,
    validate_template,
)
from src.utils import VALID_ENVS, load_context_config

A, B = "$ref.BucketName.rapid-test-a", "$ref.BucketName.rapid-test-b"


def test_load_template(tmp_path):
    path = tmp_path / "dev.yaml"
    path.write_text("projects:\n  - name: RAPID A\n    bucket: $ref.BucketName.rapid-test-a\n")
    assert load_template(path) == {"projects": [{"name": "RAPID A", "bucket": A}]}


def test_empty_template_has_no_projects(tmp_path):
    path = tmp_path / "prod.yaml"
    path.write_text("")
    assert load_template(path) == {"projects": []}


@pytest.mark.parametrize(
    "projects, message",
    [
        ("RAPID A", "projects must be a list"),
        (["RAPID A"], r"projects\[0\] must be a mapping"),
        ([{"bucket": A}], r"projects\[0\].name"),
        ([{"name": "  ", "bucket": A}], r"projects\[0\].name"),
        ([{"name": "a/b", "bucket": A}], r"projects\[0\].name"),
        ([{"name": "RAPID A"}], r"projects\[0\].bucket"),
        ([{"name": "RAPID A", "bucket": "Not A Bucket"}], r"projects\[0\].bucket"),
        # A bucket_name, not a reference to its stack
        ([{"name": "RAPID A", "bucket": "rapid-test-a"}], r"projects\[0\].bucket"),
        ([{"name": "RAPID A", "bucket": A, "sts_folder": "a/b"}], r"projects\[0\].sts_folder"),
        ([{"name": "RAPID A", "bucket": A, "sts_folder": "  "}], r"projects\[0\].sts_folder"),
        # A reference, but to another output
        (
            [{"name": "RAPID A", "bucket": "$ref.BucketArn.rapid-test-a"}],
            r"projects\[0\].bucket .* must be \$ref.BucketName",
        ),
        (
            [{"name": "RAPID A", "bucket": "$ref.BucketName.Not_A_Bucket"}],
            r"projects\[0\].bucket: .* must be \$ref.<Output>.<BUCKETS bucket_name>",
        ),
        # Malformed references are rejected wherever they are
        (
            [{"name": "RAPID A", "bucket": A, "notes": ["$ref.BucketArn"]}],
            r"projects\[0\].notes\[0\]: '\$ref.BucketArn' must be",
        ),
        (
            [
                {"name": "RAPID A", "bucket": A},
                {"name": "RAPID A", "bucket": B},
            ],
            r"projects\[1\].name 'RAPID A' is used by another project",
        ),
        (
            [
                {"name": "RAPID A", "bucket": A},
                {"name": "RAPID B", "bucket": A},
            ],
            r"projects\[1\].bucket 'rapid-test-a' is used by another project",
        ),
    ],
)
def test_invalid_template_rejected(projects, message):
    with pytest.raises(ValueError, match=message):
        validate_template({"projects": projects})


def _acl(*entries):
    return [{"name": "RAPID A", "bucket": A, "acl": list(entries)}]


def test_acl_accepted():
    validate_template(
        {
            "projects": _acl(
                {"principal_id": 3629180, "permission": "edit"},
                {"principal_id": "1", "permission": "admin"},
            )
        }
    )


@pytest.mark.parametrize(
    "projects, message",
    [
        ([{"name": "RAPID A", "bucket": A, "acl": {}}], r"acl must be a list"),
        (_acl(3629180), r"acl\[0\] must be a mapping"),
        (_acl({"principal_id": "team-a", "permission": "edit"}), r"acl\[0\].principal_id"),
        (_acl({"permission": "edit"}), r"acl\[0\].principal_id"),
        (_acl({"principal_id": True, "permission": "edit"}), r"acl\[0\].principal_id"),
        (_acl({"principal_id": 1, "permission": "write"}), r"acl\[0\].permission 'write'"),
        (_acl({"principal_id": 1}), r"acl\[0\].permission"),
        (
            _acl(
                {"principal_id": 1, "permission": "edit"},
                {"principal_id": "1", "permission": "view"},
            ),
            r"acl\[1\].principal_id '1' is listed twice",
        ),
    ],
)
def test_invalid_acl_rejected(projects, message):
    with pytest.raises(ValueError, match=message):
        validate_template({"projects": projects})


@pytest.mark.parametrize(
    "value, ref",
    [
        ("$ref.BucketName.rapid-test-a", Ref("BucketName", "rapid-test-a")),
        (
            "$ref.SynapseServiceAccountId.rapid-test-a",
            Ref("SynapseServiceAccountId", "rapid-test-a"),
        ),
        ("rapid-test-a", None),
        (3629180, None),
    ],
)
def test_parse_ref(value, ref):
    assert parse_ref(value) == ref


@pytest.mark.parametrize(
    "value",
    ["$ref", "$ref.BucketName", "$ref.BucketName.", "$ref..rapid-test-a", "$ref.Bucket-Name.a1b"],
)
def test_malformed_ref_rejected(value):
    with pytest.raises(ValueError, match="must be"):
        parse_ref(value)


def test_find_refs():
    value = {"bucket": A, "acl": [{"principal_id": 1}], "notes": ["x", B]}
    assert dict(find_refs(value, "p")) == {
        "p.bucket": Ref("BucketName", "rapid-test-a"),
        "p.notes[1]": Ref("BucketName", "rapid-test-b"),
    }


@pytest.mark.parametrize("env", sorted(VALID_ENVS))
def test_repo_templates_reference_configured_buckets(env):
    template = load_template(f"config/synapse/{env}.yaml")
    bucket_names = {b["bucket_name"] for b in load_context_config(env).get("BUCKETS") or []}
    for i, project in enumerate(template["projects"]):
        for where, ref in find_refs(project, f"projects[{i}]"):
            assert ref.bucket in bucket_names, f"{env}: {where} → {ref.bucket}"


def test_project_names_by_bucket_name():
    template = {"projects": [{"name": "RAPID A", "bucket": A}, {"name": "RAPID B", "bucket": B}]}
    buckets = [{"bucket_name": "rapid-test-a"}, {"bucket_name": "rapid-test-b"}]
    assert project_names(template, buckets) == {
        "rapid-test-a": "RAPID A",
        "rapid-test-b": "RAPID B",
    }


def test_every_bucket_needs_a_project():
    with pytest.raises(ValueError, match="BUCKETS rapid-test-b have no project"):
        project_names(
            {"projects": [{"name": "RAPID A", "bucket": A}]},
            [{"bucket_name": "rapid-test-a"}, {"bucket_name": "rapid-test-b"}],
        )
