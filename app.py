import os

import aws_cdk as cdk

from src.synapse_bucket_stack import SynapseBucketStack
from src.sync_pipeline_stack import SyncPipelineStack
from src.utils import load_context_config

cdk_app = cdk.App()
env_name = cdk_app.node.try_get_context("env") or "dev"
config = load_context_config(env_name=env_name)
STACK_NAME_PREFIX = f"{config['PROJECT_NAME']}-{env_name}"

# From active credentials (OIDC in CI, AWS profile locally), unless the config pins an account:
# then credentials for any other account fail the deploy instead of deploying there
env = cdk.Environment(
    account=str(config.get("AWS_ACCOUNT") or os.environ.get("CDK_DEFAULT_ACCOUNT") or "") or None,
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

for key, value in (config.get("TAGS") or {}).items():
    cdk.Tags.of(cdk_app).add(key, value)

buckets = config.get("BUCKETS") or []
lambda_errors = (config.get("LAMBDA_ERROR_ALARMS") or {}).get("errors_per_5_minutes", 1)
data_events = config.get("S3_DATA_EVENTS") or {}

# One pipeline for all buckets. It refers to them by name (from config), not by cross-stack
# reference, so bucket stacks come and go without export/import ordering. Bucket stacks import
# its alarm topic; deleting an importing stack is always allowed.
if buckets:
    pipeline_stack = SyncPipelineStack(
        scope=cdk_app,
        construct_id=f"{STACK_NAME_PREFIX}-pipeline",
        buckets=buckets,
        sync_config=config["SYNC"],
        alarm_email=config.get("ALARM_EMAIL") or "",
        lambda_errors_per_5_minutes=lambda_errors,
        s3_data_events_retention_days=(
            data_events.get("retention_days") if data_events.get("enabled") else None
        ),
        env=env,
    )

# One stack per data provider bucket, so each can be added or retired on its own
for bucket in buckets:
    SynapseBucketStack(
        scope=cdk_app,
        construct_id=f"{STACK_NAME_PREFIX}-bucket-{bucket['id']}",
        bucket_config=bucket,
        synapse_account_id=config["SYNAPSE"]["aws_account_id"],
        synapse_service_account_id=config["SYNAPSE"]["service_account_id"],
        data_store_tags=config.get("DATA_STORE_TAGS"),
        # Read objects under a kms_encryption bucket's key (a role ARN import, only then)
        kms_key_users=pipeline_stack.object_readers,
        alarm_topic=pipeline_stack.alarm_topic,
        lambda_errors_per_5_minutes=lambda_errors,
        env=env,
    )

cdk_app.synth()
