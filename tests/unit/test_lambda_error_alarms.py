"""The real app.py: every Lambda in every stack gets an Errors alarm that emails (Vanta)."""

import json
import runpy

import aws_cdk as cdk
import pytest

from tests.conftest import ROOT


def _synth_app(env, tmp_path, monkeypatch):
    """Synthesize app.py for `env` (no asset bundling) and return its templates by stack."""
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    app = cdk.App(
        context={**context, "env": env, "aws:cdk:bundling-stacks": []}, outdir=str(tmp_path)
    )
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(cdk, "App", lambda *args, **kwargs: app)
    runpy.run_path(str(ROOT / "app.py"))
    return {p.name: json.loads(p.read_text()) for p in tmp_path.glob("*.template.json")}


@pytest.mark.parametrize("env", ["dev", "prod", "sandbox"])
def test_every_lambda_in_the_app_has_an_errors_alarm_that_emails(env, tmp_path, monkeypatch):
    templates = _synth_app(env, tmp_path, monkeypatch)
    functions_seen = 0
    for name, template in templates.items():
        resources = template.get("Resources", {})
        functions = {k for k, r in resources.items() if r["Type"] == "AWS::Lambda::Function"}
        alarms = [
            r["Properties"]
            for r in resources.values()
            if r["Type"] == "AWS::CloudWatch::Alarm"
            and r["Properties"].get("Namespace") == "AWS/Lambda"
        ]
        alarmed = {
            d["Value"]["Ref"]
            for a in alarms
            for d in a["Dimensions"]
            if d["Name"] == "FunctionName"
        }
        assert alarmed == functions, name
        for alarm in alarms:
            assert alarm["MetricName"] == "Errors", name
            assert alarm["AlarmActions"] and alarm["OKActions"], name
        functions_seen += len(functions)
    # An env with buckets has the pipeline's Lambdas and each bucket stack's custom resource
    if templates:
        assert functions_seen >= 4
