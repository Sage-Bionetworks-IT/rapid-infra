import json
import os
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest

# Lambda handlers import their siblings as top-level modules, as they do in the Lambda bundle
ROOT = Path(__file__).parents[1]
for _dir in (
    ROOT / "src" / "lambda_prepare",
    ROOT / "src" / "lambda_sync",
    ROOT / "src" / "lambda_encrypt",
):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

# The Lambda modules create boto3 clients when imported, which pytest does at collection, before
# any fixture runs. No real AWS call is ever made; these only let the clients be built.
os.environ.update(
    AWS_DEFAULT_REGION="us-east-1",
    AWS_ACCESS_KEY_ID="testing",
    AWS_SECRET_ACCESS_KEY="testing",
)
os.environ.pop("AWS_PROFILE", None)


@pytest.fixture
def app():
    """
    A CDK app with cdk.json's feature flags, as `cdk synth` uses, that skips asset bundling
    (no Docker, no pip) for template assertions.
    """
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    return cdk.App(context={**context, "aws:cdk:bundling-stacks": []})
