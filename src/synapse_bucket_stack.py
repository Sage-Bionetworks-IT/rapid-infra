import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import custom_resources as cr
from constructs import Construct

from src.alarms import lambda_error_alarms


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
        alarm_topic: sns.ITopic | None = None,
        lambda_errors_per_5_minutes: int = 1,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.bucket = s3.Bucket(
            self,
            "Bucket",
            bucket_name=bucket_config["bucket_name"],
            encryption=s3.BucketEncryption.S3_MANAGED,
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

        admin_arns = bucket_config.get("s3_admin_arns", [])
        if admin_arns:
            self.bucket.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="AdminAccess",
                    principals=[iam.ArnPrincipal(arn) for arn in admin_arns],
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

        # owner.txt proves bucket ownership to Synapse when a storage location is created. The
        # creator must be listed, and only the storage location's creator may create file
        # handles in it, so the sync service account is always included.
        owners = [str(o) for o in bucket_config.get("synapse_owner_ids", [])]
        if str(synapse_service_account_id) not in owners:
            owners.append(str(synapse_service_account_id))
        put_owner_file = cr.AwsSdkCall(
            service="S3",
            action="putObject",
            parameters={
                "Bucket": self.bucket.bucket_name,
                "Key": "owner.txt",
                "Body": ",".join(owners),
                "ContentType": "text/plain",
            },
            # A new owner list replaces the resource, so the file is rewritten
            physical_resource_id=cr.PhysicalResourceId.of(f"owner.txt:{','.join(owners)}"),
        )
        owner_file = cr.AwsCustomResource(
            self,
            "SynapseOwnerFile",
            on_create=put_owner_file,
            on_update=put_owner_file,
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=[self.bucket.arn_for_objects("owner.txt")]
            ),
            install_latest_aws_sdk=False,
        )
        # The policy must exist first: with enforce_ssl the bucket policy is created anyway
        owner_file.node.add_dependency(self.bucket.policy)

        # Last, so every Lambda in the stack is covered (Vanta)
        lambda_error_alarms(self, lambda_errors_per_5_minutes, alarm_topic)

        cdk.CfnOutput(self, "BucketName", value=self.bucket.bucket_name)
        cdk.CfnOutput(self, "BucketArn", value=self.bucket.bucket_arn)
