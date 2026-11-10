import aws_cdk as cdk
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import custom_resources as cr
from constructs import Construct

from src.alarms import lambda_error_alarms

OWNER_FILE = "owner.txt"

# KMS API calls on a bucket's key that email the alarm topic. CloudFormation makes some of them
# (PutKeyPolicy) when a deploy changes the key, so an email outside a deploy is a manual change.
# S3 never needs a grant for SSE-KMS, so any CreateGrant is unexpected
KEY_CHANGE_EVENTS = [
    "CancelKeyDeletion",
    "CreateGrant",
    "DeleteAlias",
    "DisableKey",
    "DisableKeyRotation",
    "PutKeyPolicy",
    "ScheduleKeyDeletion",
    "UpdateAlias",
]


class SynapseBucketStack(cdk.Stack):
    """
    One S3 bucket usable as a Synapse external storage location
    (https://help.synapse.org/docs/Custom-Storage-Locations.2048327803.html).

    The CDK form of s3-synapse-sync's s3-synapse-sync-bucket.j2, minus its Lambda notification
    wiring: the bucket sends every event to EventBridge, and the sync pipeline's rule picks out
    sidecar uploads. That also removes the template's two-pass deploy.

    - Bucket policy: Synapse's AWS account may list the bucket and read/write objects, as
      Synapse needs for uploads/downloads through Synapse clients
    - owner.txt at the root, listing the Synapse users/teams allowed to create a storage
      location on the bucket (always including the sync service account)
    - CORS for browser uploads from synapse.org
    - The bucket is retained when the stack is deleted
    - `data_store_tags` (config DATA_STORE_TAGS, e.g. Vanta's data classification) on the bucket
    - Encryption: SSE-S3 by default. With `kms_encryption: true`, SSE-KMS under the bucket's own
      customer-managed key (rotated yearly, retained with the bucket), with an S3 Bucket Key,
      and a bucket policy that denies writes asking for any other encryption or key. The key
      policy lets the bucket's read/write principals and `kms_key_users` (the pipeline Lambdas
      that read objects) use it, only through S3 and only for this bucket's objects. Changes
      to the key email `alarm_topic`. Synapse's STS credentials have no KMS permissions, so
      STS contributors cannot write to such a bucket (README)
    - An Errors alarm on the stack's Lambda (the owner.txt custom resource), emailing
      `alarm_topic`: the pipeline's, imported from it. The pipeline imports nothing from here,
      so bucket stacks still come and go freely

    Its Synapse project is set up by tools/setup_synapse_projects.py, which CI runs after every
    deploy: never create it by hand, since a new project uploads to Synapse's default storage.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        bucket_config: dict,
        synapse_account_id: str,
        synapse_service_account_id: str,
        data_store_tags: dict | None = None,
        # Principals that read objects (the pipeline's Lambdas), for a kms_encryption bucket
        kms_key_users: list[iam.IPrincipal] | None = None,
        alarm_topic: sns.ITopic | None = None,
        lambda_errors_per_5_minutes: int = 1,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # The key is retained like the bucket: deleting it would make every object unreadable
        self.bucket_name = bucket_config["bucket_name"]
        self.key = None
        if bucket_config.get("kms_encryption", False):
            self.key = kms.Key(
                self,
                "EncryptionKey",
                alias=f"alias/{construct_id}",
                description=f"SSE-KMS key of s3://{bucket_config['bucket_name']}",
                enable_key_rotation=True,
                removal_policy=cdk.RemovalPolicy.RETAIN,
            )

        self.bucket = s3.Bucket(
            self,
            "Bucket",
            bucket_name=bucket_config["bucket_name"],
            encryption=s3.BucketEncryption.KMS if self.key else s3.BucketEncryption.S3_MANAGED,
            encryption_key=self.key,
            # One data key per bucket instead of a KMS call per object
            bucket_key_enabled=True if self.key else None,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            versioned=bool(bucket_config.get("versioned", False)),
            removal_policy=cdk.RemovalPolicy.RETAIN,
            cors=[
                s3.CorsRule(
                    id="SynapseCORSRule",
                    allowed_headers=["*"],
                    allowed_origins=["*"],
                    allowed_methods=[
                        s3.HttpMethods.GET,
                        s3.HttpMethods.POST,
                        s3.HttpMethods.PUT,
                        s3.HttpMethods.HEAD,
                    ],
                    max_age=3000,
                )
            ],
        )
        for key, value in (data_store_tags or {}).items():
            cdk.Tags.of(self.bucket).add(key, value)

        # Every S3 event goes to EventBridge, where the pipeline's rule picks out sidecars. Set on
        # the CloudFormation resource: `event_bridge_enabled=True` would add a custom-resource
        # Lambda to do the same thing.
        self.bucket.node.default_child.notification_configuration = (
            s3.CfnBucket.NotificationConfigurationProperty(
                event_bridge_configuration=s3.CfnBucket.EventBridgeConfigurationProperty(
                    event_bridge_enabled=True
                )
            )
        )

        # Synapse plus any extra read/write users get the same access
        rw_principals = [iam.AccountPrincipal(synapse_account_id)] + [
            iam.ArnPrincipal(arn) for arn in bucket_config.get("s3_user_arns", [])
        ]
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="ReadAccess",
                principals=rw_principals,
                actions=["s3:ListBucket*", "s3:GetBucketLocation"],
                resources=[self.bucket.bucket_arn],
            )
        )
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="WriteAccess",
                principals=rw_principals,
                actions=["s3:*Object*", "s3:*MultipartUpload*"],
                resources=[self.bucket.arn_for_objects("*")],
            )
        )

        admin_principals = [iam.ArnPrincipal(arn) for arn in bucket_config.get("s3_admin_arns", [])]
        if admin_principals:
            self.bucket.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="AdminAccess",
                    principals=admin_principals,
                    actions=["s3:*"],
                    resources=[self.bucket.bucket_arn, self.bucket.arn_for_objects("*")],
                )
            )

        deny_delete_arns = bucket_config.get("deny_delete_arns", [])
        if deny_delete_arns:
            self.bucket.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="DenyDeleteObject",
                    effect=iam.Effect.DENY,
                    principals=[iam.ArnPrincipal(arn) for arn in deny_delete_arns],
                    actions=["s3:*Delete*"],
                    resources=[self.bucket.arn_for_objects("*")],
                )
            )

        if self.key:
            self._enforce_kms(rw_principals + admin_principals, kms_key_users or [])
            if alarm_topic is not None:
                self._alarm_on_key_changes(bucket_config["bucket_name"], alarm_topic)

        # owner.txt proves bucket ownership to Synapse when a storage location is created. The
        # creator must be listed, and only the storage location's creator may create file
        # handles in it, so the sync service account is always included.
        owners = [str(o) for o in bucket_config.get("synapse_owner_ids", [])]
        if str(synapse_service_account_id) not in owners:
            owners.append(str(synapse_service_account_id))
        owner_file_params = {
            "Bucket": self.bucket.bucket_name,
            "Key": OWNER_FILE,
            "Body": ",".join(owners),
            "ContentType": "text/plain",
        }
        if self.key:
            # Synapse reads owner.txt with its own credentials to create a storage location.
            # It holds only Synapse IDs, so it stays SSE-S3 (exempt from the KMS-only policy)
            # rather than depending on Synapse's KMS permissions
            owner_file_params["ServerSideEncryption"] = "AES256"
        put_owner_file = cr.AwsSdkCall(
            service="S3",
            action="putObject",
            parameters=owner_file_params,
            # A new owner list replaces the resource, so the file is rewritten
            physical_resource_id=cr.PhysicalResourceId.of(f"owner.txt:{','.join(owners)}"),
        )
        owner_file = cr.AwsCustomResource(
            self,
            "SynapseOwnerFile",
            on_create=put_owner_file,
            on_update=put_owner_file,
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=[self.bucket.arn_for_objects(OWNER_FILE)]
            ),
            install_latest_aws_sdk=False,
        )
        # The policy must exist first: with enforce_ssl the bucket policy is created anyway
        owner_file.node.add_dependency(self.bucket.policy)

        # Last, so every Lambda in the stack is covered (Vanta)
        lambda_error_alarms(self, lambda_errors_per_5_minutes, alarm_topic)

        cdk.CfnOutput(self, "BucketName", value=self.bucket.bucket_name)
        cdk.CfnOutput(self, "BucketArn", value=self.bucket.bucket_arn)
        if self.key:
            cdk.CfnOutput(self, "KmsKeyArn", value=self.key.key_arn)

    def _enforce_kms(
        self, bucket_principals: list[iam.IPrincipal], key_users: list[iam.IPrincipal]
    ) -> None:
        """
        Key policy grants for everyone the bucket policy lets read or write, and bucket policy
        denials that keep every object (but owner.txt) under this bucket's key. A write without
        encryption headers gets the bucket default, so plain uploads still work.
        """
        # Through S3, for this bucket's objects only: S3's encryption context is the bucket ARN
        # with a Bucket Key, the object ARN without one. So a principal granted here cannot use
        # the key for another bucket, even one of its own pointed at this key. (The ARN from the
        # configured name: the bucket's own GetAtt would make key and bucket depend on each other)
        bucket_arn = f"arn:{self.partition}:s3:::{self.bucket_name}"
        via_s3 = {
            "StringEquals": {"kms:ViaService": f"s3.{self.region}.amazonaws.com"},
            "StringLike": {"kms:EncryptionContext:aws:s3:arn": [bucket_arn, f"{bucket_arn}/*"]},
        }
        self.key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="BucketReadWriteViaS3",
                principals=bucket_principals,
                actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                resources=["*"],
                conditions=via_s3,
            )
        )
        if key_users:
            self.key.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="PipelineReadViaS3",
                    principals=key_users,
                    actions=["kms:Decrypt"],
                    resources=["*"],
                    conditions=via_s3,
                )
            )

        objects = self.bucket.arn_for_objects("*")
        # s3:PutObject also covers CreateMultipartUpload and CopyObject
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyNonKmsEncryption",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:PutObject"],
                not_resources=[self.bucket.arn_for_objects(OWNER_FILE)],
                conditions={
                    "Null": {"s3:x-amz-server-side-encryption": "false"},
                    "StringNotEquals": {"s3:x-amz-server-side-encryption": "aws:kms"},
                },
            )
        )
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyOtherKmsKey",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:PutObject"],
                resources=[objects],
                conditions={
                    "Null": {"s3:x-amz-server-side-encryption-aws-kms-key-id": "false"},
                    "StringNotEquals": {
                        "s3:x-amz-server-side-encryption-aws-kms-key-id": self.key.key_arn
                    },
                },
            )
        )
        # SSE-C sends no x-amz-server-side-encryption header, so the first denial misses it.
        # (--sse aws:kms with no key ID gets the aws/s3 key, which DenyOtherKmsKey catches)
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenySseC",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:PutObject"],
                resources=[objects],
                conditions={
                    "Null": {"s3:x-amz-server-side-encryption-customer-algorithm": "false"}
                },
            )
        )

    def _alarm_on_key_changes(self, bucket_name: str, alarm_topic: sns.ITopic) -> None:
        """Email every KEY_CHANGE_EVENTS call on the key (CloudTrail, via the org trail)."""
        detail = events.EventField.from_path
        events.Rule(
            self,
            "KeyChangeRule",
            description=f"Email changes to the KMS key of s3://{bucket_name}",
            event_pattern=events.EventPattern(
                source=["aws.kms"],
                detail_type=["AWS API Call via CloudTrail"],
                detail={
                    "eventSource": ["kms.amazonaws.com"],
                    "eventName": KEY_CHANGE_EVENTS,
                    "resources": {"ARN": [self.key.key_arn]},
                },
            ),
            targets=[
                targets.SnsTopic(
                    alarm_topic,
                    message=events.RuleTargetInput.from_multiline_text(
                        f"KMS {detail('$.detail.eventName')} on the key of s3://{bucket_name}\n"
                        f"Key: {self.key.key_arn}\n"
                        f"By: {detail('$.detail.userIdentity.arn')}\n"
                        f"At: {detail('$.detail.eventTime')}\n"
                        f"Parameters: {detail('$.detail.requestParameters')}\n"
                        "Expected during a deploy that changes the key (PutKeyPolicy); "
                        "otherwise someone changed it by hand"
                    ),
                )
            ],
        )
