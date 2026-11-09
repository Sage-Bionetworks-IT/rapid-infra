import json

import aws_cdk as cdk
import pytest
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sns as sns
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
ADMIN = "arn:aws:iam::222222222222:role/admin"
SYNC = {
    "sidecar_filename": "sidecar.json",
    "max_concurrency": 7,
    "tolerated_failure_percentage": 0,
    "results_retention_days": 30,
}


@pytest.fixture
def bucket_template(app):
    # The alarm topic lives in another stack (the pipeline's), as in app.py
    topic = sns.Topic(cdk.Stack(app, "test-topic"), "AlarmTopic")
    stack = SynapseBucketStack(
        app,
        "test-bucket-a",
        bucket_config=BUCKET,
        synapse_account_id="325565585839",
        synapse_service_account_id="1234567",
        data_store_tags={"DataClass": "internal"},
        alarm_topic=topic,
    )
    return Template.from_stack(stack)


@pytest.fixture
def kms_bucket_template(app):
    # As app.py wires it: the topic and the pipeline's reader roles come from another stack
    env = cdk.Environment(region="us-east-1")
    other = cdk.Stack(app, "test-pipeline-like", env=env)
    topic = sns.Topic(other, "AlarmTopic")
    reader = iam.Role(other, "Reader", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
    stack = SynapseBucketStack(
        app,
        "test-bucket-k",
        bucket_config={**BUCKET, "kms_encryption": True, "s3_admin_arns": [ADMIN]},
        synapse_account_id="325565585839",
        synapse_service_account_id="1234567",
        kms_key_users=[reader],
        alarm_topic=topic,
        env=env,
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


def _statements(template, resource_type, document="PolicyDocument"):
    (policy,) = template.find_resources(resource_type).values()
    return {s.get("Sid"): s for s in policy["Properties"][document]["Statement"]}


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


def _sse_default(template):
    (bucket,) = template.find_resources("AWS::S3::Bucket").values()
    (rule,) = bucket["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    return rule


def test_bucket_defaults_to_sse_s3_without_a_key(bucket_template):
    rule = _sse_default(bucket_template)
    assert rule["ServerSideEncryptionByDefault"] == {"SSEAlgorithm": "AES256"}
    assert "BucketKeyEnabled" not in rule
    assert not bucket_template.find_resources("AWS::KMS::Key")
    statements = _statements(bucket_template, "AWS::S3::BucketPolicy")
    assert not {"DenyNonKmsEncryption", "DenyOtherKmsKey", "DenySseC"} & set(statements)
    (owner,) = bucket_template.find_resources("Custom::AWS").values()
    assert "ServerSideEncryption" not in json.dumps(owner["Properties"]["Create"])


def test_kms_bucket_uses_its_own_retained_rotated_key(kms_bucket_template):
    (key_id,) = kms_bucket_template.find_resources("AWS::KMS::Key")
    kms_bucket_template.has_resource(
        "AWS::KMS::Key",
        {
            "DeletionPolicy": "Retain",
            "Properties": Match.object_like({"EnableKeyRotation": True}),
        },
    )
    kms_bucket_template.has_resource_properties(
        "AWS::KMS::Alias", {"AliasName": "alias/test-bucket-k"}
    )
    assert _sse_default(kms_bucket_template) == {
        "BucketKeyEnabled": True,
        "ServerSideEncryptionByDefault": {
            "KMSMasterKeyID": {"Fn::GetAtt": [key_id, "Arn"]},
            "SSEAlgorithm": "aws:kms",
        },
    }


def test_kms_key_policy_grants_bucket_principals_and_pipeline_via_s3_only(kms_bucket_template):
    statements = _statements(kms_bucket_template, "AWS::KMS::Key", "KeyPolicy")
    via_s3 = {"StringEquals": {"kms:ViaService": "s3.us-east-1.amazonaws.com"}}
    readwrite = statements["BucketReadWriteViaS3"]
    assert sorted(readwrite["Action"]) == ["kms:Decrypt", "kms:GenerateDataKey*"]
    assert readwrite["Condition"] == via_s3
    rendered = json.dumps(readwrite["Principal"])
    assert "325565585839" in rendered and "role/provider-a" in rendered and ADMIN in rendered
    pipeline = statements["PipelineReadViaS3"]
    assert pipeline["Action"] == "kms:Decrypt"
    assert pipeline["Condition"] == via_s3
    assert "Fn::ImportValue" in json.dumps(pipeline["Principal"])


def test_kms_bucket_denies_other_encryption_but_not_plain_writes(kms_bucket_template):
    (key_id,) = kms_bucket_template.find_resources("AWS::KMS::Key")
    statements = _statements(kms_bucket_template, "AWS::S3::BucketPolicy")
    other_sse = statements["DenyNonKmsEncryption"]
    other_key = statements["DenyOtherKmsKey"]
    sse_c = statements["DenySseC"]
    for deny in (other_sse, other_key, sse_c):
        assert deny["Effect"] == "Deny" and deny["Principal"] == {"AWS": "*"}
        assert deny["Action"] == "s3:PutObject"
    # Each denial needs its header present, so a write without one gets the bucket default
    assert other_sse["Condition"] == {
        "Null": {"s3:x-amz-server-side-encryption": "false"},
        "StringNotEquals": {"s3:x-amz-server-side-encryption": "aws:kms"},
    }
    assert other_key["Condition"] == {
        "Null": {"s3:x-amz-server-side-encryption-aws-kms-key-id": "false"},
        "StringNotEquals": {
            "s3:x-amz-server-side-encryption-aws-kms-key-id": {"Fn::GetAtt": [key_id, "Arn"]}
        },
    }
    assert sse_c["Condition"] == {
        "Null": {"s3:x-amz-server-side-encryption-customer-algorithm": "false"}
    }
    # owner.txt is the one object written as SSE-S3
    assert "owner.txt" in json.dumps(other_sse["NotResource"])
    (owner,) = kms_bucket_template.find_resources("Custom::AWS").values()
    assert '\\"ServerSideEncryption\\":\\"AES256\\"' in json.dumps(owner["Properties"]["Create"])
    kms_bucket_template.has_output("KmsKeyArn", Match.any_value())


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


def _alarms(template, metric_name):
    return {
        k: a["Properties"]
        for k, a in template.find_resources("AWS::CloudWatch::Alarm").items()
        if a["Properties"].get("MetricName") == metric_name
    }


def _assert_plain_lambda_errors_alarm(props, function_logical_id):
    # Vanta reads the top-level MetricName and the FunctionName dimension (no Metrics form)
    assert "Metrics" not in props
    assert props["Namespace"] == "AWS/Lambda"
    assert props["Statistic"] == "Sum"
    assert props["Period"] == 300
    assert props["Threshold"] == 1
    assert props["Dimensions"] == [{"Name": "FunctionName", "Value": {"Ref": function_logical_id}}]
    assert len(props["AlarmActions"]) == 1


def test_bucket_has_data_store_tags(bucket_template):
    bucket_template.has_resource_properties(
        "AWS::S3::Bucket",
        {"Tags": Match.array_with([{"Key": "DataClass", "Value": "internal"}])},
    )


def test_data_store_tags_only_on_the_bucket(bucket_template):
    for resource_type in ("AWS::Lambda::Function", "AWS::IAM::Role"):
        for resource in bucket_template.find_resources(resource_type).values():
            tags = resource["Properties"].get("Tags", [])
            assert {"Key": "DataClass", "Value": "internal"} not in tags


def test_bucket_lambda_has_errors_alarm_on_the_pipeline_topic(bucket_template):
    # Vanta "Serverless function error rate monitored": the owner.txt custom resource's Lambda
    (function_id,) = bucket_template.find_resources("AWS::Lambda::Function")
    (props,) = _alarms(bucket_template, "Errors").values()
    _assert_plain_lambda_errors_alarm(props, function_id)
    assert props["AlarmName"] == "test-bucket-a-AwsCustomResource-errors"
    assert "Fn::ImportValue" in props["AlarmActions"][0]


def test_every_pipeline_lambda_has_errors_alarm(pipeline_template):
    functions = pipeline_template.find_resources("AWS::Lambda::Function")
    alarms = _alarms(pipeline_template, "Errors").values()
    assert {a["Dimensions"][0]["Value"]["Ref"] for a in alarms} == set(functions)
    assert len(alarms) == len(functions) == 3
    for props in alarms:
        _assert_plain_lambda_errors_alarm(props, props["Dimensions"][0]["Value"]["Ref"])
        assert props["AlarmActions"][0]["Ref"].startswith("AlarmTopic")
    assert {a["AlarmName"] for a in alarms} == {
        "test-pipeline-PrepareFunction-errors",
        "test-pipeline-SyncFunction-errors",
        "test-pipeline-ResolveStorageFunction-errors",
    }


def test_rule_dlq_age_alarm(pipeline_template):
    # Vanta "Messaging queue message age monitored": every queue, at most 5000 s
    (props,) = _alarms(pipeline_template, "ApproximateAgeOfOldestMessage").values()
    assert props["Dimensions"][0]["Name"] == "QueueName"
    assert "SidecarRuleDLQ" in json.dumps(props["Dimensions"])
    assert props["Statistic"] == "Maximum"
    assert props["Threshold"] == 3600
    assert props["AlarmActions"][0]["Ref"].startswith("AlarmTopic")


@pytest.mark.parametrize("kms", [False, True])
@pytest.mark.parametrize("env", ["dev", "prod", "sandbox"])
def test_repo_configs_synthesize(app, env, kms):
    # As app.py builds them; with kms, the bucket stacks import the pipeline's reader roles
    config = load_context_config(env)
    if not config["BUCKETS"]:
        return
    pipeline = SyncPipelineStack(
        app, "pipeline", buckets=config["BUCKETS"], sync_config=config["SYNC"]
    )
    for bucket in config["BUCKETS"]:
        SynapseBucketStack(
            app,
            f"bucket-{bucket['id']}",
            bucket_config={**bucket, "kms_encryption": kms},
            synapse_account_id=config["SYNAPSE"]["aws_account_id"],
            synapse_service_account_id=config["SYNAPSE"]["service_account_id"],
            data_store_tags=config.get("DATA_STORE_TAGS"),
            kms_key_users=pipeline.object_readers,
            alarm_topic=pipeline.alarm_topic,
        )
    app.synth()
