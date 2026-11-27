import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "tools"))
import contribute  # noqa: E402

TOKEN = {
    "accessKeyId": "AK",
    "secretAccessKey": "secret",
    "sessionToken": "session",
    "bucket": "provider-a",
}


class FakeS3:
    def __init__(self):
        self.uploads = []

    def upload_file(self, path, bucket, key, ExtraArgs):
        self.uploads.append((bucket, key, ExtraArgs))


@pytest.fixture
def s3(monkeypatch):
    s3, folders = FakeS3(), []

    class FakeFolder:
        def __init__(self, id):
            folders.append(id)

        def get_sts_storage_token(self, permission, output_format, synapse_client):
            assert permission == "read_write"
            return dict(TOKEN, **getattr(s3, "token_extra", {}))

    monkeypatch.setattr(contribute, "Folder", FakeFolder)
    monkeypatch.setattr(contribute.boto3, "client", lambda *a, **kw: s3)
    s3.folders = folders
    return s3


@pytest.fixture
def batch(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "nested" / "b.txt").write_text("b")
    (tmp_path / "sidecar.json").write_text(json.dumps({"name": "batch"}))
    return tmp_path


def test_uploads_every_file_then_the_sidecar_last(s3, batch):
    url = contribute.contribute(None, "syn123", batch, "/data/batch1/")
    assert s3.folders == ["syn123"]
    assert [key for _, key, _ in s3.uploads] == [
        "data/batch1/a.txt",
        "data/batch1/nested/b.txt",
        "data/batch1/sidecar.json",
    ]
    assert all(b == "provider-a" and x["ACL"] == contribute.ACL for b, _, x in s3.uploads)
    assert s3.uploads[-1][2]["ContentType"] == "application/json"
    assert url == "s3://provider-a/data/batch1/"


def test_keys_go_under_the_sts_base_key(s3, batch):
    s3.token_extra = {"baseKey": "base/"}
    contribute.contribute(None, "syn123", batch, "data/batch1")
    assert s3.uploads[-1][1] == "base/data/batch1/sidecar.json"


def test_needs_a_sidecar(s3, batch):
    (batch / "sidecar.json").unlink()
    with pytest.raises(ValueError, match="no sidecar.json"):
        contribute.contribute(None, "syn123", batch, "data/batch1")
    assert s3.folders == [] and s3.uploads == []


def test_sidecar_must_be_a_json_object(s3, batch):
    (batch / "sidecar.json").write_text("[]")
    with pytest.raises(ValueError, match="JSON object"):
        contribute.contribute(None, "syn123", batch, "data/batch1")
    assert s3.uploads == []


@pytest.mark.parametrize("folder", ["", "/"])
def test_refuses_the_bucket_root(s3, batch, folder):
    with pytest.raises(ValueError, match="bucket root"):
        contribute.contribute(None, "syn123", batch, folder)
    assert s3.uploads == []
