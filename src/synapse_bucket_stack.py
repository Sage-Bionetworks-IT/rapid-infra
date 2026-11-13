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
from src.utils import cse_key_alias

OWNER_FILE = "owner.txt"

# aws:userid of a Synapse STS session: "<role ID>:<session name>", the session name being
# sts-<user id>-<entity id> (Synapse's StsManagerImpl). Matched by session name because Synapse's
# temporary-credentials role is created per Synapse release, under a generated name and ID
STS_SESSION_USERID = "*:sts-*"

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
    - Synapse STS credentials (how providers contribute) are write-only: Synapse issues them
      with s3:GetObject, so the bucket policy denies their sessions any object read and any
      pre-signed request. Data leaves only through Synapse downloads, under Synapse's access
      controls, not through credentials that cover the whole bucket
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
    - With `client_side_encryption`, this bucket is the staging bucket for the files its
      `patterns` match: the pipeline encrypts them (AWS Encryption SDK, under a KMS key of its
      own) into a second, encrypted bucket, indexes the ciphertext in Synapse and deletes the
      plaintext. Only the pipeline's Encrypt role writes the encrypted bucket; Synapse reads
      it; `decrypt_arns` may decrypt (README: "Client-side encryption")
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
        # The pipeline's Encrypt role, for a client_side_encryption bucket
        cse_encryptor: iam.IRole | None = None,
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

        self._deny_sts_reads(synapse_account_id)

        if self.key:
            self._enforce_kms(rw_principals + admin_principals, kms_key_users or [])
            if alarm_topic is not None:
                self._alarm_on_key_changes(
                    "KeyChangeRule",
                    self.key,
                    f"the KMS key of s3://{bucket_config['bucket_name']}",
                    alarm_topic,
                )

        # owner.txt proves bucket ownership to Synapse when a storage location is created. The
        # creator must be listed, and only the storage location's creator may create file
        # handles in it, so the sync service account is always included.
        owners = [str(o) for o in bucket_config.get("synapse_owner_ids", [])]
        if str(synapse_service_account_id) not in owners:
            owners.append(str(synapse_service_account_id))
        # Synapse reads owner.txt with its own credentials to create a storage location. It
        # holds only Synapse IDs, so on a KMS bucket it stays SSE-S3 (exempt from the KMS-only
        # policy) rather than depending on Synapse's KMS permissions
        self._owner_file("SynapseOwnerFile", self.bucket, owners, sse_s3=bool(self.key))

        self.encrypted_bucket = None
        self.cse_key = None
        if bucket_config.get("client_side_encryption"):
            self._client_side_encryption(
                bucket_config,
                synapse_account_id,
                cse_encryptor,
                owners,
                data_store_tags or {},
                alarm_topic,
            )

        # Last, so every Lambda in the stack is covered (Vanta)
        lambda_error_alarms(self, lambda_errors_per_5_minutes, alarm_topic)

        cdk.CfnOutput(self, "BucketName", value=self.bucket.bucket_name)
        cdk.CfnOutput(self, "BucketArn", value=self.bucket.bucket_arn)
        if self.key:
            cdk.CfnOutput(self, "KmsKeyArn", value=self.key.key_arn)

    def _owner_file(self, construct_id: str, bucket: s3.Bucket, owners: list[str], sse_s3: bool):
        params = {
            "Bucket": bucket.bucket_name,
            "Key": OWNER_FILE,
            "Body": ",".join(owners),
            "ContentType": "text/plain",
        }
        if sse_s3:
            params["ServerSideEncryption"] = "AES256"
        put_owner_file = cr.AwsSdkCall(
            service="S3",
            action="putObject",
            parameters=params,
            # A new owner list replaces the resource, so the file is rewritten
            physical_resource_id=cr.PhysicalResourceId.of(f"owner.txt:{','.join(owners)}"),
        )
        owner_file = cr.AwsCustomResource(
            self,
            construct_id,
            on_create=put_owner_file,
            on_update=put_owner_file,
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=[bucket.arn_for_objects(OWNER_FILE)]
            ),
            install_latest_aws_sdk=False,
        )
        # The policy must exist first: with enforce_ssl the bucket policy is created anyway
        owner_file.node.add_dependency(bucket.policy)

    def _client_side_encryption(
        self,
        bucket_config: dict,
        synapse_account_id: str,
        encryptor: iam.IRole | None,
        owners: list[str],
        data_store_tags: dict,
        alarm_topic: sns.ITopic | None,
    ) -> None:
        """
        The encrypted bucket and the KMS key its files are encrypted under (README:
        "Client-side encryption"). The pipeline's Encrypt role is the only writer; Synapse
        reads, to serve downloads of the (encrypted) files it indexes from here.
        """
        if encryptor is None:
            raise ValueError("client_side_encryption needs the pipeline's cse_encryptor role")
        admin_principals = [iam.ArnPrincipal(arn) for arn in bucket_config.get("s3_admin_arns", [])]
        cse = bucket_config["client_side_encryption"]
        provider = bucket_config["id"]
        self.encrypted_bucket_name = cse["encrypted_bucket_name"]

        # Retained like the bucket: deleting it would make every encrypted file unreadable
        self.cse_key = kms.Key(
            self,
            "CseKey",
            alias=cse_key_alias(self.encrypted_bucket_name),
            description=f"Client-side encryption key of s3://{self.encrypted_bucket_name}",
            enable_key_rotation=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        # The Encryption SDK passes its encryption context to KMS: the key only serves this
        # provider's files. Called directly, not through S3, so no kms:ViaService
        this_provider = {"StringEquals": {"kms:EncryptionContext:rapid:provider": provider}}
        self.cse_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="EncryptFiles",
                principals=[encryptor],
                actions=["kms:GenerateDataKey"],
                resources=["*"],
                conditions=this_provider,
            )
        )
        decryptors = [iam.ArnPrincipal(arn) for arn in cse.get("decrypt_arns", [])]
        if decryptors:
            self.cse_key.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="DecryptFiles",
                    principals=decryptors,
                    actions=["kms:Decrypt"],
                    resources=["*"],
                    conditions=this_provider,
                )
            )

        # SSE-S3: the objects are ciphertext already
        self.encrypted_bucket = s3.Bucket(
            self,
            "EncryptedBucket",
            bucket_name=self.encrypted_bucket_name,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            # An overwritten encrypted file stays recoverable
            versioned=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            lifecycle_rules=[
                # An Encrypt Lambda that times out mid-upload leaves its parts behind
                s3.LifecycleRule(abort_incomplete_multipart_upload_after=cdk.Duration.days(7))
            ],
        )
        for key, value in data_store_tags.items():
            cdk.Tags.of(self.encrypted_bucket).add(key, value)

        bucket = self.encrypted_bucket
        objects = bucket.arn_for_objects("*")
        synapse = iam.AccountPrincipal(synapse_account_id)
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="SynapseList",
                principals=[synapse],
                actions=["s3:ListBucket*", "s3:GetBucketLocation"],
                resources=[bucket.bucket_arn],
            )
        )
        # Downloads, and owner.txt when the storage location is created. Never writes: files
        # reach this bucket only through the Encrypt step
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="SynapseRead",
                principals=[synapse],
                actions=["s3:GetObject"],
                resources=[objects],
            )
        )
        if admin_principals:
            bucket.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="AdminAccess",
                    principals=admin_principals,
                    actions=["s3:*"],
                    resources=[bucket.bucket_arn, objects],
                )
            )
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyWriteExceptEncryptor",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:PutObject", "s3:*MultipartUpload*"],
                # owner.txt is written by this stack's custom resource
                not_resources=[bucket.arn_for_objects(OWNER_FILE)],
                conditions={"ArnNotEquals": {"aws:PrincipalArn": encryptor.role_arn}},
            )
        )
        # s3:DeleteObject* also covers DeleteObjectVersion
        keep = {}
        if admin_principals:
            keep = {"ArnNotEquals": {"aws:PrincipalArn": [p.arn for p in admin_principals]}}
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyDelete",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:DeleteObject*"],
                resources=[objects],
                conditions=keep or None,
            )
        )
        # Synapse STS sessions never get a location here; deny them outright all the same
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenySts",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:*"],
                resources=[bucket.bucket_arn, objects],
                conditions={
                    "StringEquals": {"aws:PrincipalAccount": synapse_account_id},
                    "StringLike": {"aws:userid": STS_SESSION_USERID},
                },
            )
        )

        self._owner_file("EncryptedOwnerFile", bucket, owners, sse_s3=False)
        if alarm_topic is not None:
            self._alarm_on_key_changes(
                "CseKeyChangeRule",
                self.cse_key,
                f"the client-side encryption key of s3://{self.encrypted_bucket_name}",
                alarm_topic,
            )
        cdk.CfnOutput(self, "EncryptedBucketName", value=bucket.bucket_name)
        # Decrypting (strict mode) names the key by its ARN
        cdk.CfnOutput(self, "CseKeyArn", value=self.cse_key.key_arn)

    def _deny_sts_reads(self, synapse_account_id: str) -> None:
        """
        Make Synapse STS credentials write-only on this bucket. Synapse's read_write STS session
        allows s3:GetObject on the whole bucket (our STS location has no base key), which would
        let any contributor read every provider file, outside Synapse's access controls, and
        mint pre-signed URLs for others. Synapse's own sessions (downloads, workers) are not
        STS sessions, so they are unaffected.
        """
        sts_sessions = {
            "StringEquals": {"aws:PrincipalAccount": synapse_account_id},
            "StringLike": {"aws:userid": STS_SESSION_USERID},
        }
        # s3:GetObject* also covers HeadObject
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyStsRead",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:GetObject*"],
                resources=[self.bucket.arn_for_objects("*")],
                conditions=sts_sessions,
            )
        )
        # A pre-signed URL would hand an STS session's access (now writes) to whoever holds it
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyStsPresigned",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:*"],
                resources=[self.bucket.bucket_arn, self.bucket.arn_for_objects("*")],
                conditions={
                    "StringEquals": {
                        **sts_sessions["StringEquals"],
                        "s3:authType": "REST-QUERY-STRING",
                    },
                    "StringLike": sts_sessions["StringLike"],
                },
            )
        )

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

    def _alarm_on_key_changes(
        self, construct_id: str, key: kms.IKey, label: str, alarm_topic: sns.ITopic
    ) -> None:
        """Email every KEY_CHANGE_EVENTS call on the key (CloudTrail, via the org trail)."""
        detail = events.EventField.from_path
        events.Rule(
            self,
            construct_id,
            description=f"Email changes to {label}",
            event_pattern=events.EventPattern(
                source=["aws.kms"],
                detail_type=["AWS API Call via CloudTrail"],
                detail={
                    "eventSource": ["kms.amazonaws.com"],
                    "eventName": KEY_CHANGE_EVENTS,
                    "resources": {"ARN": [key.key_arn]},
                },
            ),
            targets=[
                targets.SnsTopic(
                    alarm_topic,
                    message=events.RuleTargetInput.from_multiline_text(
                        f"KMS {detail('$.detail.eventName')} on {label}\n"
                        f"Key: {key.key_arn}\n"
                        f"By: {detail('$.detail.userIdentity.arn')}\n"
                        f"At: {detail('$.detail.eventTime')}\n"
                        f"Parameters: {detail('$.detail.requestParameters')}\n"
                        "Expected during a deploy that changes the key (PutKeyPolicy); "
                        "otherwise someone changed it by hand"
                    ),
                )
            ],
        )
