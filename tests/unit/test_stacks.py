import json

import aws_cdk as cdk
import pytest
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sns as sns
from aws_cdk.assertions import Match, Template

from src.synapse_template import load_template, project_names
from src.synapse_bucket_stack import SynapseBucketStack
from src.sync_pipeline_stack import SyncPipelineStack
from src.utils import load_context_config

BUCKET = {
    "bucket_name": "rapid-test-a",
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
    stack = SynapseBucketStack(
        app,
        "test-bucket-a",
        bucket_config=BUCKET,
        synapse_aws_account_id="325565585839",
        synapse_service_account_id="1234567",
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
        synapse_aws_account_id="325565585839",
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
            {**BUCKET, "bucket_name": "rapid-test-b"},
        ],
        project_names={"rapid-test-a": "RAPID A", "rapid-test-b": "RAPID B"},
        sync_config=SYNC,
        alarm_email="alerts@example.org",
    )
    return Template.from_stack(stack)


@pytest.fixture
def audited_pipeline_template(app):
    stack = SyncPipelineStack(
        app,
        "test-audited",
        buckets=[
            BUCKET,
            {**BUCKET, "bucket_name": "rapid-test-b"},
        ],
        project_names={"rapid-test-a": "RAPID A", "rapid-test-b": "RAPID B"},
        sync_config=SYNC,
        s3_data_events_retention_days=365,
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


def test_outputs_feed_the_synapse_setup(bucket_template):
    # tools/setup_synapse_projects.py reads these from cdk deploy --outputs-file
    outputs = bucket_template.find_outputs("*")
    assert {"BucketName", "SynapseServiceAccountId"} <= set(outputs)
    assert outputs["SynapseServiceAccountId"]["Value"] == "1234567"


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
    # Only through S3, and only for this bucket (Bucket Key: bucket ARN; without: object ARN)
    via_s3 = {
        "StringEquals": {"kms:ViaService": "s3.us-east-1.amazonaws.com"},
        "StringLike": {
            "kms:EncryptionContext:aws:s3:arn": [
                "arn:aws:s3:::rapid-test-a",
                "arn:aws:s3:::rapid-test-a/*",
            ]
        },
    }
    readwrite = statements["BucketReadWriteViaS3"]
    assert sorted(readwrite["Action"]) == ["kms:Decrypt", "kms:GenerateDataKey*"]
    assert readwrite["Condition"] == via_s3
    rendered = json.dumps(readwrite["Principal"])
    assert "325565585839" in rendered and "role/provider-a" in rendered and ADMIN in rendered
    pipeline = statements["PipelineReadViaS3"]
    assert pipeline["Action"] == "kms:Decrypt"
    assert pipeline["Condition"] == via_s3
    assert "Fn::ImportValue" in json.dumps(pipeline["Principal"])


def test_kms_key_changes_email_the_alarm_topic(kms_bucket_template):
    (key_id,) = kms_bucket_template.find_resources("AWS::KMS::Key")
    (rule,) = kms_bucket_template.find_resources(
        "AWS::Events::Rule", {"Properties": {"EventPattern": {"source": ["aws.kms"]}}}
    ).values()
    pattern = rule["Properties"]["EventPattern"]
    assert pattern["detail-type"] == ["AWS API Call via CloudTrail"]
    assert pattern["detail"]["resources"] == {"ARN": [{"Fn::GetAtt": [key_id, "Arn"]}]}
    assert {"PutKeyPolicy", "DisableKey", "ScheduleKeyDeletion", "CreateGrant"} <= set(
        pattern["detail"]["eventName"]
    )
    (target,) = rule["Properties"]["Targets"]
    assert "Fn::ImportValue" in json.dumps(target["Arn"])


def test_default_bucket_has_no_key_change_rule(bucket_template):
    assert not bucket_template.find_resources(
        "AWS::Events::Rule", {"Properties": {"EventPattern": {"source": ["aws.kms"]}}}
    )


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


@pytest.mark.parametrize("kms", [False, True])
@pytest.mark.parametrize("env", ["dev", "prod", "sandbox"])
def test_repo_configs_synthesize(app, env, kms):
    # As app.py builds them; with kms, the bucket stacks import the pipeline's reader roles
    config = load_context_config(env)
    if not config["BUCKETS"]:
        return
    data_events = config.get("S3_DATA_EVENTS") or {}
    pipeline = SyncPipelineStack(
        app,
        "pipeline",
        buckets=config["BUCKETS"],
        project_names=project_names(load_template(f"config/synapse/{env}.yaml"), config["BUCKETS"]),
        sync_config=config["SYNC"],
        s3_data_events_retention_days=(
            data_events.get("retention_days") if data_events.get("enabled") else None
        ),
    )
    for bucket in config["BUCKETS"]:
        SynapseBucketStack(
            app,
            f"bucket-{bucket['bucket_name']}",
            bucket_config={**bucket, "kms_encryption": kms},
            synapse_aws_account_id=config["SYNAPSE"]["aws_account_id"],
            synapse_service_account_id=config["SYNAPSE"]["service_account_id"],
            kms_key_users=pipeline.object_readers,
            alarm_topic=pipeline.alarm_topic,
        )
    app.synth()


def test_no_data_events_trail_by_default(pipeline_template):
    assert not pipeline_template.find_resources("AWS::CloudTrail::Trail")


def test_data_events_trail_records_only_provider_bucket_objects(audited_pipeline_template):
    (trail,) = audited_pipeline_template.find_resources("AWS::CloudTrail::Trail").values()
    props = trail["Properties"]
    assert props["TrailName"] == "test-audited-s3-data-events"
    assert props["EnableLogFileValidation"] is True
    assert props["IsMultiRegionTrail"] is False
    assert props["IncludeGlobalServiceEvents"] is False
    assert "Fn::GetAtt" in props["KMSKeyId"]
    (selector,) = props["EventSelectors"]
    assert selector["IncludeManagementEvents"] is False  # the org trail has those
    assert selector["ReadWriteType"] == "All"
    (resources,) = selector["DataResources"]
    assert resources["Type"] == "AWS::S3::Object"
    rendered = json.dumps(resources["Values"])
    assert "rapid-test-a/" in rendered and "rapid-test-b/" in rendered
    assert len(resources["Values"]) == 2


def test_data_events_logs_kept_a_year_and_undeletable(audited_pipeline_template):
    buckets = audited_pipeline_template.find_resources(
        "AWS::S3::Bucket", {"Properties": {"LifecycleConfiguration": Match.any_value()}}
    )
    (log_bucket,) = [
        b
        for b in buckets.values()
        if b["Properties"]["LifecycleConfiguration"]["Rules"][0]["ExpirationInDays"] == 365
    ]
    assert log_bucket["DeletionPolicy"] == "Retain"
    policies = audited_pipeline_template.find_resources("AWS::S3::BucketPolicy")
    statements = [
        s for p in policies.values() for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    (deny,) = [s for s in statements if s.get("Sid") == "DenyDeleteLogs"]
    assert deny["Effect"] == "Deny" and deny["Action"] == "s3:DeleteObject*"
    assert deny["Principal"] == {"AWS": "*"}


def test_data_events_log_key_lets_only_this_trail_encrypt(audited_pipeline_template):
    audited_pipeline_template.has_resource(
        "AWS::KMS::Key",
        {"DeletionPolicy": "Retain", "Properties": Match.object_like({"EnableKeyRotation": True})},
    )
    statements = _statements(audited_pipeline_template, "AWS::KMS::Key", "KeyPolicy")
    encrypt = statements["CloudTrailEncryptLogs"]
    assert encrypt["Principal"] == {"Service": "cloudtrail.amazonaws.com"}
    assert encrypt["Action"] == "kms:GenerateDataKey*"
    rendered = json.dumps(encrypt["Condition"])
    assert "aws:SourceArn" in rendered and "kms:EncryptionContext:aws:cloudtrail:arn" in rendered
    assert "trail/test-audited-s3-data-events" in rendered
