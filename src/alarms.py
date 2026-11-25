"""CloudWatch alarms required by Vanta's AWS tests.

Don't add a `label` to these metrics: CDK then writes a math alarm, which Vanta doesn't
recognize (it only checks the alarm's top-level `MetricName` and dimensions).
"""

import re

import aws_cdk as cdk
import jsii
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct, IConstruct

FIVE_MINUTES = cdk.Duration.minutes(5)

# Email if a DLQ message is still there after an hour (nobody redrove or purged it).
# Vanta requires an age alarm on every queue, at 5000 s or less.
DLQ_UNHANDLED_SECONDS = 3600


def dlq_age_alarm(
    scope: Construct,
    construct_id: str,
    dlq: sqs.IQueue,
    what: str,
    alarm_topic: sns.ITopic | None,
) -> cw.Alarm:
    """Alarm when `dlq`'s oldest message is an hour old. Use one for every new queue (Vanta)."""
    alarm = cw.Alarm(
        scope,
        construct_id,
        metric=dlq.metric_approximate_age_of_oldest_message(
            statistic="Maximum", period=FIVE_MINUTES
        ),
        threshold=DLQ_UNHANDLED_SECONDS,
        evaluation_periods=1,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        alarm_description=f"{what} has sat in the DLQ for an hour: redrive or purge it",
    )
    if alarm_topic:
        alarm.add_alarm_action(cw_actions.SnsAction(alarm_topic))
    return alarm


# Lambda functions CDK creates itself have a UUID in their ID. Map it to a readable name.
_SINGLETONS = {"AWS679f53fac002430cb0da5b7982bd2287": "AwsCustomResource"}


def _readable_name(stack: cdk.Stack, path: str) -> str:
    """Turn a construct path into an alarm name, e.g. `rapid-dev-bucket-example-AwsCustomResource`.

    Replaces known CDK UUIDs with names, drops other 32-character hashes, and turns any other
    non-alphanumeric run (`/`, `::`) into `-`.
    """
    for uuid, name in _SINGLETONS.items():
        path = path.replace(uuid, name)
    path = re.sub(r"[0-9a-f]{32}(?=/|$)", "", path)
    return re.sub(r"[^A-Za-z0-9]+", "-", f"{stack.stack_name}-{path}").strip("-")


@jsii.implements(cdk.IAspect)
class LambdaErrorAlarms:
    """Add an `Errors` alarm to every Lambda function in the app.

    Required by Vanta ("Serverless function error rate monitored (AWS)"), which checks every
    function, including the ones CDK creates for you.

    It's a CDK Aspect: applied once in app.py, it runs at synth after all stacks are built, so new
    functions get an alarm automatically. Each alarm goes in its function's stack and emails
    `alarm_topic` when it fires and again when it recovers.
    """

    def __init__(self, errors_per_5_minutes: int = 1, alarm_topic: sns.ITopic | None = None):
        self.errors_per_5_minutes = errors_per_5_minutes
        self.alarm_topic = alarm_topic

    def visit(self, node: IConstruct) -> None:
        if not (
            cdk.CfnResource.is_cfn_resource(node)
            and node.cfn_resource_type == "AWS::Lambda::Function"
        ):
            return
        stack = cdk.Stack.of(node)
        path = node.node.path.removeprefix(f"{stack.node.path}/").removesuffix("/Resource")
        name = _readable_name(stack, path)
        alarm = cw.Alarm(
            stack,
            f"LambdaErrors-{name}",
            alarm_name=f"{name}-errors",
            metric=cw.Metric(
                namespace="AWS/Lambda",
                metric_name="Errors",
                dimensions_map={"FunctionName": node.ref},
                statistic="Sum",
                period=FIVE_MINUTES,
            ),
            threshold=self.errors_per_5_minutes,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description=(
                f"Lambda {stack.stack_name}/{path} had {self.errors_per_5_minutes}+ errors in"
                f" 5 minutes. Its logs: /aws/lambda/{node.ref}"
            ),
        )
        if self.alarm_topic:
            action = cw_actions.SnsAction(self.alarm_topic)
            alarm.add_alarm_action(action)
            alarm.add_ok_action(action)
