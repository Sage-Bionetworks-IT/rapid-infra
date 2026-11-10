import json

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

_RUNTIME = lambda_.Runtime.PYTHON_3_13
# synapseclient pulls in compiled wheels; build and run on one architecture
_ARCH = lambda_.Architecture.ARM_64

# Lambda service errors are retried by LambdaInvoke's default retrier (retry_on_service_exceptions)


class SyncPipelineStack(cdk.Stack):
    """
    Sidecar-triggered S3 → Synapse indexing for every bucket in config BUCKETS.

    A data provider uploads a folder of files, then writes ``{folder}/sidecar.json`` last:

        S3 "Object Created" (EventBridge, from each SynapseBucketStack bucket)
          → EventBridge rule: key ends with /sidecar.json, bucket is a configured one
            → Step Functions:
                Prepare         read + validate the sidecar, find the bucket's Synapse project
                                (by name, from config)
                ResolveStorage  the project's ID and its storage location on the bucket, looked
                                up, never created: tools/setup_synapse_projects.py sets them up
                                after every deploy. Missing: the execution fails at once
                SyncFiles       Distributed Map over ListObjectsV2(bucket, folder/)
                                  → one sync Lambda per object (indexer.index_object)
                Succeed         per-file results are written to the results bucket

    Re-uploading a sidecar re-runs the folder; unchanged files (same MD5) are left alone.
    Nested folders are included (ListObjectsV2 is recursive). Deleting from S3 does not delete
    from Synapse.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        buckets: list,
        sync_config: dict,
        alarm_email: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        sidecar_filename = sync_config["sidecar_filename"]
        bucket_names = [b["bucket_name"] for b in buckets]
        bucket_projects = {b["bucket_name"]: b["synapse_project_name"] for b in buckets}
        bucket_arns = [f"arn:{self.partition}:s3:::{name}" for name in bucket_names]
        object_arns = [f"{arn}/*" for arn in bucket_arns]

        # -------------------
        # Synapse service account PAT. Created with a placeholder value; set the real token
        # once after the first deploy (README: "Synapse service account").
        # -------------------
        self.pat_secret = secretsmanager.Secret(
            self,
            "SynapsePat",
            secret_name=f"{construct_id}/synapse-pat",
            description="Synapse personal access token of the S3 sync service account",
        )

        # -------------------
        # Prepare Lambda: sidecar event → {bucket, prefix, project_id}
        # -------------------
        self.prepare_fn = lambda_.Function(
            self,
            "PrepareFunction",
            runtime=_RUNTIME,
            architecture=_ARCH,
            handler="prepare.handler",
            code=lambda_.Code.from_asset("src/lambda_prepare"),
            environment={
                "SIDECAR_FILENAME": sidecar_filename,
                "BUCKET_PROJECTS": json.dumps(bucket_projects, sort_keys=True),
            },
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            log_group=logs.LogGroup(
                self, "PrepareFunctionLogs", retention=logs.RetentionDays.THREE_MONTHS
            ),
        )
        self.prepare_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"{arn}/*{sidecar_filename}" for arn in bucket_arns],
            )
        )

        # -------------------
        # Sync Lambda: one object → Synapse (indexer.py, ported from s3-synapse-sync)
        # -------------------
        synapse_env = {
            "SIDECAR_FILENAME": sidecar_filename,
            "SYNAPSE_PAT_SECRET_ARN": self.pat_secret.secret_arn,
            # Python hides DeprecationWarning by default; log synapseclient's (once per call site)
            # so a deprecation shows up before the major version that removes it
            "PYTHONWARNINGS": "default::DeprecationWarning",
        }
        bundling = cdk.BundlingOptions(
            image=_RUNTIME.bundling_image,
            platform="linux/arm64",
            command=[
                "bash",
                "-c",
                "pip install --no-cache-dir -r requirements.txt -t /asset-output"
                " && cp *.py /asset-output/"
                # synapseclient's wheel installs a sample home/runner/.synapseConfig data file
                " && rm -rf /asset-output/home",
            ],
        )
        self.sync_fn = lambda_.Function(
            self,
            "SyncFunction",
            runtime=_RUNTIME,
            architecture=_ARCH,
            handler="sync.handler",
            code=lambda_.Code.from_asset("src/lambda_sync", bundling=bundling),
            environment=synapse_env,
            # A multipart upload without a content-md5 header is read in full to compute its MD5
            timeout=cdk.Duration.minutes(15),
            memory_size=1024,
            log_group=logs.LogGroup(
                self, "SyncFunctionLogs", retention=logs.RetentionDays.THREE_MONTHS
            ),
        )
        self.pat_secret.grant_read(self.sync_fn)
        self.sync_fn.add_to_role_policy(
            iam.PolicyStatement(actions=["s3:GetObject"], resources=object_arns)
        )

        # Same bundle, once per execution: finds the project and its location on the bucket
        self.storage_fn = lambda_.Function(
            self,
            "ResolveStorageFunction",
            runtime=_RUNTIME,
            architecture=_ARCH,
            handler="sync.resolve_storage",
            code=lambda_.Code.from_asset("src/lambda_sync", bundling=bundling),
            environment=synapse_env,
            timeout=cdk.Duration.minutes(2),
            memory_size=512,
            log_group=logs.LogGroup(
                self, "ResolveStorageFunctionLogs", retention=logs.RetentionDays.THREE_MONTHS
            ),
        )
        self.pat_secret.grant_read(self.storage_fn)

        # -------------------
        # Per-run results (one JSON result per file), written by the Distributed Map
        # -------------------
        self.results_bucket = s3.Bucket(
            self,
            "ResultsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    expiration=cdk.Duration.days(sync_config["results_retention_days"])
                )
            ],
        )

        # -------------------
        # State machine
        # -------------------
        prepare = tasks.LambdaInvoke(
            self,
            "Prepare",
            lambda_function=self.prepare_fn,
            payload_response_only=True,
        )

        resolve_storage = tasks.LambdaInvoke(
            self,
            "ResolveStorage",
            lambda_function=self.storage_fn,
            payload_response_only=True,
            result_path="$.storage",
        )
        # The first matching retrier applies: missing setup (no project, or no location on the
        # bucket) fails at once; anything else (Synapse 5xx, throttling) is retried
        resolve_storage.add_retry(errors=["IndexingError"], max_attempts=0)
        resolve_storage.add_retry(
            errors=["States.TaskFailed"],
            interval=cdk.Duration.seconds(5),
            backoff_rate=2.0,
            max_attempts=3,
        )

        sync_file = tasks.LambdaInvoke(
            self,
            "SyncFile",
            lambda_function=self.sync_fn,
            payload_response_only=True,
        )
        # Synapse API errors (throttling, 5xx, a folder-creation race) are usually transient
        sync_file.add_retry(
            errors=["States.TaskFailed"],
            interval=cdk.Duration.seconds(5),
            backoff_rate=2.0,
            max_attempts=3,
        )

        sync_files = sfn.DistributedMap(
            self,
            "SyncFiles",
            item_reader=sfn.S3ObjectsItemReader(
                bucket_name_path=sfn.JsonPath.string_at("$.bucket"),
                prefix=sfn.JsonPath.string_at("$.prefix"),
            ),
            item_selector={
                "bucket": sfn.JsonPath.string_at("$.bucket"),
                "project_id": sfn.JsonPath.string_at("$.storage.project_id"),
                "storage_location_id": sfn.JsonPath.number_at("$.storage.storage_location_id"),
                "key": sfn.JsonPath.string_at("$$.Map.Item.Value.Key"),
            },
            max_concurrency=sync_config["max_concurrency"],
            tolerated_failure_percentage=sync_config["tolerated_failure_percentage"],
            # Without a writer, every file's result goes into the state output (256 KiB limit)
            result_writer_v2=sfn.ResultWriterV2(bucket=self.results_bucket, prefix="runs"),
            result_path="$.results",
        )
        sync_files.item_processor(sync_file)

        definition = (
            prepare.next(resolve_storage).next(sync_files).next(sfn.Succeed(self, "Synced"))
        )

        self.state_machine = sfn.StateMachine(
            self,
            "SyncStateMachine",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            state_machine_type=sfn.StateMachineType.STANDARD,
            timeout=cdk.Duration.hours(24),
            logs=sfn.LogOptions(
                destination=logs.LogGroup(
                    self, "SyncStateMachineLogs", retention=logs.RetentionDays.THREE_MONTHS
                ),
                level=sfn.LogLevel.ERROR,
            ),
        )
        # The item reader's bucket comes from state input, so CDK grants s3:ListBucket on "*".
        # Allow the configured buckets and deny every other one.
        self.state_machine.add_to_role_policy(
            iam.PolicyStatement(actions=["s3:ListBucket"], resources=bucket_arns)
        )
        self.state_machine.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY, actions=["s3:ListBucket"], not_resources=bucket_arns
            )
        )

        # -------------------
        # EventBridge: sidecar created → start an execution
        # -------------------
        self.rule_dlq = sqs.Queue(
            self, "SidecarRuleDLQ", retention_period=cdk.Duration.days(14), enforce_ssl=True
        )
        events.Rule(
            self,
            "SidecarCreatedRule",
            description=f"S3 → Synapse sync: a {sidecar_filename} landed in a data folder",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": bucket_names},
                    "object": {"key": [{"suffix": f"/{sidecar_filename}"}]},
                },
            ),
            targets=[
                targets.SfnStateMachine(
                    self.state_machine,
                    input=events.RuleTargetInput.from_event_path("$.detail"),
                    dead_letter_queue=self.rule_dlq,
                    retry_attempts=3,
                )
            ],
        )

        # -------------------
        # Alarms: an email per failed execution, and one if EventBridge can't start any
        # -------------------
        self.alarm_topic = sns.Topic(self, "AlarmTopic", enforce_ssl=True)
        if alarm_email:
            self.alarm_topic.add_subscription(subs.EmailSubscription(alarm_email))

        execution_arn = events.EventField.from_path("$.detail.executionArn")
        events.Rule(
            self,
            "SyncFailedRule",
            description="Email each failed, timed-out or aborted sync execution",
            event_pattern=events.EventPattern(
                source=["aws.states"],
                detail_type=["Step Functions Execution Status Change"],
                detail={
                    "stateMachineArn": [self.state_machine.state_machine_arn],
                    "status": ["FAILED", "TIMED_OUT", "ABORTED"],
                },
            ),
            targets=[
                targets.SnsTopic(
                    self.alarm_topic,
                    message=events.RuleTargetInput.from_multiline_text(
                        f"S3 → Synapse sync execution {events.EventField.from_path('$.detail.status')}\n"
                        f"Input: {events.EventField.from_path('$.detail.input')}\n"
                        f"Error: {events.EventField.from_path('$.detail.error')}\n"
                        f"Cause: {events.EventField.from_path('$.detail.cause')}\n"
                        f"https://{self.region}.console.aws.amazon.com/states/home?region={self.region}"
                        f"#/v2/executions/details/{execution_arn}"
                    ),
                )
            ],
        )
        cw.Alarm(
            self,
            "SidecarRuleDLQAlarm",
            metric=self.rule_dlq.metric_approximate_number_of_messages_visible(
                period=cdk.Duration.minutes(5)
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="EventBridge failed to start an S3 → Synapse sync execution",
        ).add_alarm_action(cw_actions.SnsAction(self.alarm_topic))

        cdk.CfnOutput(self, "SyncStateMachineArn", value=self.state_machine.state_machine_arn)
        cdk.CfnOutput(self, "SynapsePatSecretArn", value=self.pat_secret.secret_arn)
        cdk.CfnOutput(self, "ResultsBucketName", value=self.results_bucket.bucket_name)
