"""CloudWatch alarms that Vanta's AWS tests look for, for the bucket and pipeline stacks.

Vanta reads an alarm's top-level `MetricName` and dimensions, so these metrics have no `label`:
a labelled metric makes CDK write the alarm in the `Metrics` (math) form, which doesn't count.
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

# A DLQ message nobody has redriven or purged for an hour. The DLQ's count alarm emails when it
# lands; this emails once more if it is still there. Vanta's "Messaging queue message age
# monitored" test wants an ApproximateAgeOfOldestMessage alarm on every queue, at most 5000 s
DLQ_UNHANDLED_SECONDS = 3600


def dlq_age_alarm(
    scope: Construct,
    construct_id: str,
    dlq: sqs.IQueue,
    what: str,
    alarm_topic: sns.ITopic | None,
) -> cw.Alarm:
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


# CDK's singleton functions, named by a fixed UUID: the AwsCustomResource provider
_SINGLETONS = {"AWS679f53fac002430cb0da5b7982bd2287": "AwsCustomResource"}


def _readable_name(stack: cdk.Stack, path: str) -> str:
    """`rapid-dev-bucket-example-AwsCustomResource`: no CDK singleton hash, no `::` or `/`."""
    for uuid, name in _SINGLETONS.items():
        path = path.replace(uuid, name)
    path = re.sub(r"[0-9a-f]{32}(?=/|$)", "", path)
    return re.sub(r"[^A-Za-z0-9]+", "-", f"{stack.stack_name}-{path}").strip("-")


@jsii.implements(cdk.IAspect)
class LambdaErrorAlarms:
    """An `Errors` alarm on every `AWS::Lambda::Function` in the app, each in its function's stack.

    Vanta's "Serverless function error rate monitored (AWS)" test passes a function when an alarm
    watches `AWS/Lambda` `Errors` with its `FunctionName`. Every function counts, CDK's own
    (e.g. the AwsCustomResource provider) included. Applied once to the app (app.py), this runs
    at synth, after every stack is built: a function added anywhere, in any stack, gets an alarm
    with no code change. Alarm and OK transitions both email `alarm_topic`.
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
