import json
from pathlib import Path

import aws_cdk as cdk
import pytest

ROOT = Path(__file__).parents[1]


@pytest.fixture
def app():
    """
    A CDK app with cdk.json's feature flags, as `cdk synth` uses, that skips asset bundling
    (no Docker, no pip) for template assertions.
    """
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    return cdk.App(context={**context, "aws:cdk:bundling-stacks": []})
