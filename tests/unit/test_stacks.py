import json

import pytest
from aws_cdk.assertions import Match, Template

from src.synapse_bucket_stack import SynapseBucketStack
from src.utils import load_context_config

BUCKET = {
    "id": "a",
    "bucket_name": "rapid-test-a",
    "synapse_project_name": "RAPID A",
    "synapse_owner_ids": ["3333333"],
    "s3_user_arns": ["arn:aws:iam::111111111111:role/provider-a"],
    "deny_delete_arns": ["arn:aws:iam::111111111111:role/provider-a"],
}


@pytest.fixture
def bucket_template(app):
    stack = SynapseBucketStack(
        app,
        "test-bucket-a",
        bucket_config=BUCKET,
        synapse_account_id="325565585839",
        synapse_service_account_id="1234567",
    )
    return Template.from_stack(stack)


def _statements(template, resource_type):
    (policy,) = template.find_resources(resource_type).values()
    return {s.get("Sid"): s for s in policy["Properties"]["PolicyDocument"]["Statement"]}


def test_bucket_is_private_retained_and_sends_events(bucket_template):
    bucket_template.has_resource(
        "AWS::S3::Bucket",
        {
            "DeletionPolicy": "Retain",
            "Properties": Match.object_like(
                {
                    "BucketName": "rapid-test-a",
                    "NotificationConfiguration": {
                        "EventBridgeConfiguration": {"EventBridgeEnabled": True}
                    },
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "BlockPublicPolicy": True,
                        "IgnorePublicAcls": True,
                        "RestrictPublicBuckets": True,
                    },
                    "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
                }
            ),
        },
    )


def test_bucket_policy_grants_synapse_and_provider(bucket_template):
    statements = _statements(bucket_template, "AWS::S3::BucketPolicy")
    principals = statements["WriteAccess"]["Principal"]["AWS"]
    rendered = json.dumps(principals)
    assert "325565585839" in rendered and "role/provider-a" in rendered
    assert sorted(statements["WriteAccess"]["Action"]) == ["s3:*MultipartUpload*", "s3:*Object*"]
    assert statements["DenyDeleteObject"]["Effect"] == "Deny"
    assert "AdminAccess" not in statements  # no admins configured


def test_owner_file_includes_service_account(bucket_template):
    (owner,) = bucket_template.find_resources("Custom::AWS").values()
    create = json.dumps(owner["Properties"]["Create"])
    assert "owner.txt" in create and "3333333,1234567" in create


@pytest.mark.parametrize("env", ["dev", "prod", "sandbox"])
def test_repo_configs_synthesize(app, env):
    config = load_context_config(env)
    for bucket in config["BUCKETS"]:
        SynapseBucketStack(
            app,
            f"bucket-{bucket['id']}",
            bucket_config=bucket,
            synapse_account_id=config["SYNAPSE"]["aws_account_id"],
            synapse_service_account_id=config["SYNAPSE"]["service_account_id"],
        )
    app.synth()
