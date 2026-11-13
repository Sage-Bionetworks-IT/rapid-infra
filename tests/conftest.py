import json
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest

# Lambda code is imported as top-level modules, as it is in the Lambda bundle
ROOT = Path(__file__).parents[1]
for _dir in (ROOT / "src" / "lambda_sync",):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))


@pytest.fixture
def app():
    """
    A CDK app with cdk.json's feature flags, as `cdk synth` uses, that skips asset bundling
    (no Docker, no pip) for template assertions.
    """
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    return cdk.App(context={**context, "aws:cdk:bundling-stacks": []})
