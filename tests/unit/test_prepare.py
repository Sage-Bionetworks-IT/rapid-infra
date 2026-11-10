import io
import json

import pytest

import prepare


class FakeS3:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SIDECAR_FILENAME", "sidecar.json")
    monkeypatch.setenv("BUCKET_PROJECTS", json.dumps({"provider-a": "RAPID A"}))


def _event(key, bucket="provider-a"):
    return {"bucket": {"name": bucket}, "object": {"key": key}}


def test_valid_sidecar(monkeypatch):
    monkeypatch.setattr(prepare, "s3", FakeS3({("provider-a", "batch1/sidecar.json"): b"{}"}))
    assert prepare.handler(_event("batch1/sidecar.json"), None) == {
        "bucket": "provider-a",
        "prefix": "batch1/",
        "sidecar_key": "batch1/sidecar.json",
        "project_name": "RAPID A",
    }


def test_project_comes_from_config_not_sidecar(monkeypatch):
    body = json.dumps({"synapse_project_name": "Someone else's project"}).encode()
    monkeypatch.setattr(prepare, "s3", FakeS3({("provider-a", "b/sidecar.json"): body}))
    assert prepare.handler(_event("b/sidecar.json"), None)["project_name"] == "RAPID A"


def test_url_encoded_key_falls_back_to_decoded(monkeypatch):
    monkeypatch.setattr(prepare, "s3", FakeS3({("provider-a", "my batch/sidecar.json"): b"{}"}))
    result = prepare.handler(_event("my+batch/sidecar.json"), None)
    assert result["prefix"] == "my batch/"
    assert result["sidecar_key"] == "my batch/sidecar.json"


@pytest.mark.parametrize(
    "event, message",
    [
        (_event("b/sidecar.json", bucket="unknown"), "no Synapse project"),
        (_event("sidecar.json"), "inside a folder"),
        (_event("b/other.json"), "inside a folder"),
    ],
)
def test_rejected_events(monkeypatch, event, message):
    monkeypatch.setattr(prepare, "s3", FakeS3({}))
    with pytest.raises(prepare.InvalidSidecarError, match=message):
        prepare.handler(event, None)


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b"\xff\xfe"])
def test_invalid_sidecar_content(monkeypatch, body):
    monkeypatch.setattr(prepare, "s3", FakeS3({("provider-a", "b/sidecar.json"): body}))
    with pytest.raises(prepare.InvalidSidecarError):
        prepare.handler(_event("b/sidecar.json"), None)
