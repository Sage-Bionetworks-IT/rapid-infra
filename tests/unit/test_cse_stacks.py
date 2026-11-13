"""Client-side encryption: the bucket stack's encrypted bucket and key, the pipeline's steps."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sns as sns
from aws_cdk.assertions import Match, Template

from src.synapse_bucket_stack import SynapseBucketStack
from src.sync_pipeline_stack import SyncPipelineStack

from .test_stacks import ADMIN, BUCKET, SYNC

DECRYPTOR = "arn:aws:iam::333333333333:role/analysis"
CSE_BUCKET = {
    **BUCKET,
    "client_side_encryption": {
        "encrypted_bucket_name": "rapid-test-a-encrypted",
        "patterns": ["**/*.vcf"],
        "decrypt_arns": [DECRYPTOR],
    },
}
ENV = cdk.Environment(account="111111111111", region="us-east-1")


@pytest.fixture
def cse_bucket_template(app):
    # As app.py wires it: the topic and the Encrypt role come from the pipeline stack
    other = cdk.Stack(app, "test-pipeline-like", env=ENV)
    topic = sns.Topic(other, "AlarmTopic")
    encryptor = iam.Role(
        other, "Encryptor", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com")
    )
    stack = SynapseBucketStack(
        app,
        "test-bucket-c",
        bucket_config={**CSE_BUCKET, "s3_admin_arns": [ADMIN]},
        synapse_account_id="325565585839",
        synapse_service_account_id="1234567",
        data_store_tags={"DataClass": "internal"},
        alarm_topic=topic,
        cse_encryptor=encryptor,
        env=ENV,
    )
    return Template.from_stack(stack)


@pytest.fixture
def cse_pipeline_template(app):
    stack = SyncPipelineStack(
        app,
        "test-pipeline",
        buckets=[
            CSE_BUCKET,
            {**BUCKET, "id": "b", "bucket_name": "rapid-test-b", "synapse_project_name": "RAPID B"},
        ],
        sync_config=SYNC,
        s3_data_events_retention_days=30,
        env=ENV,
    )
    return Template.from_stack(stack)


def _encrypted_bucket_id(template):
    (bucket_id,) = template.find_resources(
        "AWS::S3::Bucket", {"Properties": {"BucketName": "rapid-test-a-encrypted"}}
    )
    return bucket_id


def _bucket_statements(template, bucket_id):
    (policy,) = template.find_resources(
        "AWS::S3::BucketPolicy", {"Properties": {"Bucket": {"Ref": bucket_id}}}
    ).values()
    return {s.get("Sid"): s for s in policy["Properties"]["PolicyDocument"]["Statement"]}


def _role_statements(template, function_id):
    (fn,) = [
        f
        for logical_id, f in template.find_resources("AWS::Lambda::Function").items()
        if logical_id.startswith(function_id)
    ]
    role = fn["Properties"]["Role"]["Fn::GetAtt"][0]
    return [
        s
        for p in template.find_resources("AWS::IAM::Policy").values()
        if {"Ref": role} in p["Properties"]["Roles"]
        for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]


def _definition(template):
    (sm,) = template.find_resources("AWS::StepFunctions::StateMachine").values()
    return json.loads(
        "".join(
            p if isinstance(p, str) else "aws"
            for p in sm["Properties"]["DefinitionString"]["Fn::Join"][1]
        )
    )


# ---------- bucket stack


def test_encrypted_bucket_is_private_versioned_and_retained(cse_bucket_template):
    cse_bucket_template.has_resource(
        "AWS::S3::Bucket",
        {
            "DeletionPolicy": "Retain",
            "Properties": Match.object_like(
                {
                    "BucketName": "rapid-test-a-encrypted",
                    "VersioningConfiguration": {"Status": "Enabled"},
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "BlockPublicPolicy": True,
                        "IgnorePublicAcls": True,
                        "RestrictPublicBuckets": True,
                    },
                    "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
                    "BucketEncryption": {
                        "ServerSideEncryptionConfiguration": [
                            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                        ]
                    },
                    "LifecycleConfiguration": {
                        "Rules": [
                            Match.object_like(
                                {"AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}
                            )
                        ]
                    },
                    "Tags": Match.array_with([{"Key": "DataClass", "Value": "internal"}]),
                }
            ),
        },
    )
    (props,) = [
        b["Properties"]
        for b in cse_bucket_template.find_resources("AWS::S3::Bucket").values()
        if b["Properties"].get("BucketName") == "rapid-test-a-encrypted"
    ]
    # Nothing in it starts a sync, and browsers never upload to it
    assert "NotificationConfiguration" not in props and "CorsConfiguration" not in props


def test_staging_bucket_is_unchanged(cse_bucket_template):
    cse_bucket_template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "BucketName": "rapid-test-a",
            "NotificationConfiguration": {"EventBridgeConfiguration": {"EventBridgeEnabled": True}},
        },
    )


def test_encrypted_bucket_policy(cse_bucket_template):
    statements = _bucket_statements(cse_bucket_template, _encrypted_bucket_id(cse_bucket_template))
    # Synapse reads (downloads, owner.txt) and never writes
    assert statements["SynapseRead"]["Action"] == "s3:GetObject"
    assert "325565585839" in json.dumps(statements["SynapseRead"]["Principal"])
    assert sorted(statements["SynapseList"]["Action"]) == [
        "s3:GetBucketLocation",
        "s3:ListBucket*",
    ]
    rendered = json.dumps(statements)
    assert "role/provider-a" not in rendered  # s3_user_arns: staging only
    assert ADMIN in json.dumps(statements["AdminAccess"]["Principal"])

    write = statements["DenyWriteExceptEncryptor"]
    assert write["Effect"] == "Deny" and write["Principal"] == {"AWS": "*"}
    assert sorted(write["Action"]) == ["s3:*MultipartUpload*", "s3:PutObject"]
    assert "owner.txt" in json.dumps(write["NotResource"])
    assert "Fn::ImportValue" in json.dumps(write["Condition"]["ArnNotEquals"]["aws:PrincipalArn"])

    delete = statements["DenyDelete"]
    assert delete["Action"] == "s3:DeleteObject*"
    assert delete["Condition"] == {"ArnNotEquals": {"aws:PrincipalArn": [ADMIN]}}

    sts = statements["DenySts"]
    assert sts["Action"] == "s3:*"
    assert sts["Condition"]["StringLike"] == {"aws:userid": "*:sts-*"}


def test_delete_denied_to_everyone_without_admins(app):
    other = cdk.Stack(app, "other", env=ENV)
    encryptor = iam.Role(
        other, "Encryptor", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com")
    )
    stack = SynapseBucketStack(
        app,
        "test-bucket-n",
        bucket_config=CSE_BUCKET,
        synapse_account_id="325565585839",
        synapse_service_account_id="1234567",
        cse_encryptor=encryptor,
        env=ENV,
    )
    template = Template.from_stack(stack)
    statements = _bucket_statements(template, _encrypted_bucket_id(template))
    assert "Condition" not in statements["DenyDelete"]
    assert "AdminAccess" not in statements


def test_cse_key_policy_binds_to_the_provider(cse_bucket_template):
    (key,) = cse_bucket_template.find_resources(
        "AWS::KMS::Key", {"DeletionPolicy": "Retain"}
    ).values()
    assert key["Properties"]["EnableKeyRotation"] is True
    cse_bucket_template.has_resource_properties(
        "AWS::KMS::Alias", {"AliasName": "alias/cse/rapid-test-a-encrypted"}
    )
    statements = {s.get("Sid"): s for s in key["Properties"]["KeyPolicy"]["Statement"]}
    this_provider = {"StringEquals": {"kms:EncryptionContext:rapid:provider": "a"}}
    assert statements["EncryptFiles"]["Action"] == "kms:GenerateDataKey"
    assert statements["EncryptFiles"]["Condition"] == this_provider
    assert "Fn::ImportValue" in json.dumps(statements["EncryptFiles"]["Principal"])
    assert statements["DecryptFiles"]["Action"] == "kms:Decrypt"
    assert statements["DecryptFiles"]["Condition"] == this_provider
    assert statements["DecryptFiles"]["Principal"] == {"AWS": DECRYPTOR}


def test_encrypted_bucket_has_an_owner_file(cse_bucket_template):
    bodies = [
        json.dumps(r["Properties"]["Create"])
        for r in cse_bucket_template.find_resources("Custom::AWS").values()
    ]
    assert len(bodies) == 2
    assert all("3333333,1234567" in b for b in bodies)


def test_cse_key_changes_email_the_alarm_topic(cse_bucket_template):
    rules = cse_bucket_template.find_resources(
        "AWS::Events::Rule", {"Properties": {"EventPattern": {"source": ["aws.kms"]}}}
    )
    assert len(rules) == 1 and next(iter(rules)).startswith("CseKeyChangeRule")


def test_cse_outputs(cse_bucket_template):
    outputs = cse_bucket_template.find_outputs("*")
    assert {"EncryptedBucketName", "CseKeyArn"} <= set(outputs)


def test_cse_needs_the_encryptor_role(app):
    with pytest.raises(ValueError, match="cse_encryptor"):
        SynapseBucketStack(
            app,
            "test-bucket-x",
            bucket_config=CSE_BUCKET,
            synapse_account_id="325565585839",
            synapse_service_account_id="1234567",
        )


# ---------- pipeline stack


def test_prepare_gets_the_cse_target(cse_pipeline_template):
    (fn,) = cse_pipeline_template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": "prepare.handler"}}
    ).values()
    targets = json.loads(fn["Properties"]["Environment"]["Variables"]["BUCKET_TARGETS"])
    assert targets["rapid-test-a"]["cse"] == {
        "bucket": "rapid-test-a-encrypted",
        "patterns": ["**/*.vcf"],
        "key_arn": "arn:aws:kms:us-east-1:111111111111:alias/cse/rapid-test-a-encrypted",
        "provider": "a",
    }
    assert targets["rapid-test-b"]["cse"] is None


def test_encrypt_role_reads_staging_and_writes_encrypted_only(cse_pipeline_template):
    statements = _role_statements(cse_pipeline_template, "EncryptFunction")
    by_resource = {json.dumps(s["Resource"]): s["Action"] for s in statements}
    assert by_resource[json.dumps("arn:aws:s3:::rapid-test-a/*")] == "s3:GetObject"
    assert sorted(by_resource[json.dumps("arn:aws:s3:::rapid-test-a-encrypted/*")]) == [
        "s3:AbortMultipartUpload",
        "s3:GetObject",
        "s3:PutObject",
    ]
    assert by_resource[json.dumps("arn:aws:s3:::rapid-test-a-encrypted")] == "s3:ListBucket"
    rendered = json.dumps(statements)
    assert "rapid-test-b" not in rendered  # not a CSE bucket
    assert "Delete" not in rendered and "kms:" not in rendered  # the key policy grants KMS


def test_delete_staged_role_only_deletes_staged_files(cse_pipeline_template):
    (statement,) = _role_statements(cse_pipeline_template, "DeleteStagedFunction")
    assert statement["Action"] == "s3:DeleteObject"
    assert statement["Resource"] == "arn:aws:s3:::rapid-test-a/*"


def test_sync_role_reads_encrypted_files(cse_pipeline_template):
    statements = _role_statements(cse_pipeline_template, "SyncFunction")
    (s3_read,) = [s for s in statements if s["Action"] == "s3:GetObject"]
    assert "arn:aws:s3:::rapid-test-a-encrypted/*" in s3_read["Resource"]


def test_encrypt_function(cse_pipeline_template):
    cse_pipeline_template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "encrypt.handler", "Timeout": 900, "MemorySize": 3008},
    )
    cse_pipeline_template.has_resource_properties(
        "AWS::Lambda::Function", {"Handler": "encrypt.delete_staged"}
    )


def test_item_processor_encrypts_indexes_then_deletes(cse_pipeline_template):
    definition = _definition(cse_pipeline_template)
    sync_files = definition["States"]["SyncFiles"]
    # What the Encrypt and Sync Lambdas read from each item
    assert sync_files["ItemSelector"] == {
        "bucket.$": "$.bucket",
        "key.$": "$$.Map.Item.Value.Key",
        "project_id.$": "$.storage.project_id",
        "storage_location_id.$": "$.storage.storage_location_id",
        "cse.$": "$.cse",
        "encrypted_storage_location_id.$": "$.storage.encrypted_storage_location_id",
    }
    processor = sync_files["ItemProcessor"]
    states = processor["States"]
    assert processor["StartAt"] == "NeedsEncryption"
    needs = states["NeedsEncryption"]
    assert needs["Choices"] == [{"Variable": "$.cse", "IsNull": False, "Next": "EncryptFile"}]
    assert needs["Default"] == "SyncFile"
    assert states["EncryptFile"]["Next"] == "SyncFile"
    assert states["SyncFile"]["Next"] == "Staged"
    assert states["Staged"]["Choices"] == [
        {"Variable": "$.staged", "IsPresent": True, "Next": "DeleteStaged"}
    ]
    assert states["Staged"]["Default"] == "FileDone"
    assert states["DeleteStaged"]["End"] is True
    # A file over the size limit fails at once
    retriers = [r["ErrorEquals"] for r in states["EncryptFile"]["Retry"]]
    assert retriers.index(["FileTooLargeError"]) < retriers.index(["States.TaskFailed"])


def test_trail_records_the_encrypted_buckets(cse_pipeline_template):
    (trail,) = cse_pipeline_template.find_resources("AWS::CloudTrail::Trail").values()
    rendered = json.dumps(trail["Properties"]["EventSelectors"])
    assert "rapid-test-a-encrypted" in rendered
    assert "rapid-test-a/" in rendered and "rapid-test-b/" in rendered


def test_lambda_errors_alarm_covers_the_encrypt_functions(cse_pipeline_template):
    names = {
        a["Properties"]["AlarmName"]
        for a in cse_pipeline_template.find_resources("AWS::CloudWatch::Alarm").values()
        if a["Properties"].get("MetricName") == "Errors"
    }
    assert {
        "test-pipeline-EncryptFunction-errors",
        "test-pipeline-DeleteStagedFunction-errors",
    } <= names
