import json

import pytest
from aws_cdk.assertions import Match, Template

from src.synapse_bucket_stack import SynapseBucketStack
from src.sync_pipeline_stack import SyncPipelineStack
from src.utils import load_context_config

BUCKET = {
    "id": "a",
    "bucket_name": "rapid-test-a",
    "synapse_project_name": "RAPID A",
    "synapse_owner_ids": ["3333333"],
    "s3_user_arns": ["arn:aws:iam::111111111111:role/provider-a"],
    "deny_delete_arns": ["arn:aws:iam::111111111111:role/provider-a"],
}
SYNC = {
    "sidecar_filename": "sidecar.json",
    "max_concurrency": 7,
    "tolerated_failure_percentage": 0,
    "results_retention_days": 30,
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


@pytest.fixture
def pipeline_template(app):
    stack = SyncPipelineStack(
        app,
        "test-pipeline",
        buckets=[
            BUCKET,
            {**BUCKET, "id": "b", "bucket_name": "rapid-test-b", "synapse_project_name": "RAPID B"},
        ],
        sync_config=SYNC,
        alarm_email="alerts@example.org",
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


def test_sidecar_rule_matches_configured_buckets(pipeline_template):
    pipeline_template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "EventPattern": {
                "source": ["aws.s3"],
                "detail-type": ["Object Created"],
                "detail": {
                    "bucket": {"name": ["rapid-test-a", "rapid-test-b"]},
                    "object": {"key": [{"suffix": "/sidecar.json"}]},
                },
            },
            "Targets": [
                Match.object_like({"InputPath": "$.detail", "DeadLetterConfig": Match.any_value()})
            ],
        },
    )


def test_only_the_synapse_lambdas_read_the_pat(pipeline_template):
    functions = pipeline_template.find_resources("AWS::Lambda::Function")
    handlers = {
        f["Properties"]["Handler"]
        for f in functions.values()
        if "SYNAPSE_PAT_SECRET_ARN" in f["Properties"].get("Environment", {}).get("Variables", {})
    }
    assert handlers == {"sync.handler", "sync.resolve_storage"}
    for f in functions.values():
        if f["Properties"]["Handler"].startswith("sync."):
            env = f["Properties"]["Environment"]["Variables"]
            assert env["PYTHONWARNINGS"] == "default::DeprecationWarning"


def test_prepare_gets_bucket_project_map(pipeline_template):
    pipeline_template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "prepare.handler",
            "Environment": {
                "Variables": {
                    "SIDECAR_FILENAME": "sidecar.json",
                    "BUCKET_PROJECTS": json.dumps(
                        {"rapid-test-a": "RAPID A", "rapid-test-b": "RAPID B"}
                    ),
                }
            },
        },
    )


def test_state_machine_maps_over_the_sidecar_folder(pipeline_template):
    (sm,) = pipeline_template.find_resources("AWS::StepFunctions::StateMachine").values()
    definition = json.loads(
        "".join(
            p if isinstance(p, str) else "aws"
            for p in sm["Properties"]["DefinitionString"]["Fn::Join"][1]
        )
    )
    states = definition["States"]
    assert definition["StartAt"] == "Prepare"
    assert states["Prepare"]["Next"] == "ResolveStorage"
    assert states["ResolveStorage"]["Next"] == "SyncFiles"
    assert states["ResolveStorage"]["ResultPath"] == "$.storage"
    # Setup errors fail at once: the first matching retrier wins, so the no-retry rule must come
    # before the catch-all
    retriers = [r["ErrorEquals"] for r in states["ResolveStorage"]["Retry"]]
    assert retriers.index(["IndexingError"]) < retriers.index(["States.TaskFailed"])
    assert states["ResolveStorage"]["Retry"][retriers.index(["IndexingError"])]["MaxAttempts"] == 0
    sync_files = states["SyncFiles"]
    assert sync_files["Type"] == "Map"
    assert sync_files["ItemReader"]["Resource"] == "arn:aws:states:::s3:listObjectsV2"
    assert sync_files["ItemReader"]["Parameters"] == {
        "Bucket.$": "$.bucket",
        "Prefix.$": "$.prefix",
    }
    assert sync_files["ItemSelector"] == {
        "bucket.$": "$.bucket",
        "project_id.$": "$.storage.project_id",
        "storage_location_id.$": "$.storage.storage_location_id",
        "key.$": "$$.Map.Item.Value.Key",
    }
    assert sync_files["MaxConcurrency"] == 7
    assert "ResultWriter" in sync_files


def test_state_machine_can_list_only_configured_buckets(pipeline_template):
    (sm,) = pipeline_template.find_resources("AWS::StepFunctions::StateMachine").values()
    role = sm["Properties"]["RoleArn"]["Fn::GetAtt"][0]
    statements = [
        s
        for p in pipeline_template.find_resources("AWS::IAM::Policy").values()
        if {"Ref": role} in p["Properties"]["Roles"]
        for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    (deny,) = [s for s in statements if s["Effect"] == "Deny"]
    assert deny["Action"] == "s3:ListBucket"
    assert "rapid-test-a" in json.dumps(deny["NotResource"])
    assert "rapid-test-b" in json.dumps(deny["NotResource"])


def test_failure_email(pipeline_template):
    pipeline_template.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "alerts@example.org"}
    )
    pipeline_template.has_resource_properties(
        "AWS::Events::Rule",
        {"EventPattern": Match.object_like({"source": ["aws.states"]})},
    )


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
    if config["BUCKETS"]:
        SyncPipelineStack(app, "pipeline", buckets=config["BUCKETS"], sync_config=config["SYNC"])
    app.synth()
