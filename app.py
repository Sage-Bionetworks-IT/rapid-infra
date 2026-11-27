import os

import aws_cdk as cdk

from src.neptune_stack import NeptuneStack
from src.network_stack import NetworkStack
from src.synapse_bucket_stack import SynapseBucketStack
from src.sync_pipeline_stack import SyncPipelineStack
from src.synapse_template import load_template, project_names
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
        construct_id=f"{STACK_NAME_PREFIX}-bucket-{bucket['bucket_name']}",
        bucket_config=bucket,
        synapse_aws_account_id=config["SYNAPSE"]["aws_account_id"],
        synapse_service_account_id=config["SYNAPSE"]["service_account_id"],
        env=env,
    )

# One pipeline for all buckets. It refers to them by name (from config), not by cross-stack
# reference, so bucket stacks come and go without export/import ordering.
if buckets:
    SyncPipelineStack(
        scope=cdk_app,
        construct_id=f"{STACK_NAME_PREFIX}-pipeline",
        buckets=buckets,
        project_names=project_names(load_template(f"config/synapse/{env_name}.yaml"), buckets),
        sync_config=config["SYNC"],
        alarm_email=config.get("ALARM_EMAIL") or "",
        env=env,
    )

# The graph database (as in sagebrain-infra): a Neptune cluster in its own VPC
if (config.get("NEPTUNE") or {}).get("enabled"):
    network_stack = NetworkStack(
        scope=cdk_app,
        construct_id=f"{STACK_NAME_PREFIX}-network",
        vpc_cidr=config["VPC_CIDR"],
        env=env,
    )
    NeptuneStack(
        scope=cdk_app,
        construct_id=f"{STACK_NAME_PREFIX}-neptune",
        vpc=network_stack.vpc,
        neptune_config=config["NEPTUNE"],
        data_store_tags=config.get("DATA_STORE_TAGS"),
        env=env,
    )

cdk_app.synth()
