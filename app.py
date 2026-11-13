import os

import aws_cdk as cdk

from src.synapse_bucket_stack import SynapseBucketStack
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

# One stack per data provider bucket, so each can be added or retired on its own
for bucket in buckets:
    SynapseBucketStack(
        scope=cdk_app,
        construct_id=f"{STACK_NAME_PREFIX}-bucket-{bucket['id']}",
        bucket_config=bucket,
        aws_account_id=config["SYNAPSE"]["aws_account_id"],
        synapse_service_account_id=config["SYNAPSE"]["service_account_id"],
        env=env,
    )

cdk_app.synth()
