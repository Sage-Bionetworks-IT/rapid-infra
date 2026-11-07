import datetime
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "tools"))
import contribute  # noqa: E402

NOW = datetime.datetime(2026, 11, 7, 12, 0, tzinfo=datetime.timezone.utc)


def _token(hours=12, bucket="provider-a", base_key=None, n=1):
    token = {
        "accessKeyId": f"AK{n}",
        "secretAccessKey": "secret",
        "sessionToken": "session",
        "bucket": bucket,
        "expiration": (NOW + datetime.timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
    }
    if base_key:
        token["baseKey"] = base_key
    return token


class FakeS3:
    def __init__(self, fail_keys=()):
        self.calls, self.fail_keys = [], set(fail_keys)
        self.lock = threading.Lock()

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        if key in self.fail_keys:
            raise RuntimeError("AccessDenied")
        with self.lock:
            self.calls.append(("upload", bucket, key, ExtraArgs))

    def put_object(self, **kwargs):
        with self.lock:
            self.calls.append(("put", kwargs["Bucket"], kwargs["Key"], {"ACL": kwargs["ACL"]}))
            self.sidecar = json.loads(kwargs["Body"])


def _credentials(s3, tokens):
    tokens = iter(tokens)
    fetches = []

    def fetch():
        fetches.append(1)
        return next(tokens)

    creds = contribute.StsCredentials(fetch, lambda **kw: s3, now=lambda: NOW)
    return creds, fetches


@pytest.fixture
def batch(tmp_path):
    root = tmp_path / "batch"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("a")
    (root / "sub" / "b.txt").write_text("bb")
    return root


def test_plan_mirrors_the_local_tree_under_the_s3_folder(batch):
    assert [k for _, k in contribute.plan(batch, "/data/batch1/")] == [
        "data/batch1/a.txt",
        "data/batch1/sub/b.txt",
    ]


@pytest.mark.parametrize("folder", ["", "/", "data/../x", "data//x", "./data"])
def test_plan_rejects_bad_s3_folders(batch, folder):
    with pytest.raises(ValueError, match="S3 folder"):
        contribute.plan(batch, folder)


def test_plan_refuses_a_local_sidecar(batch):
    (batch / "sub" / "sidecar.json").write_text("{}")
    with pytest.raises(ValueError, match="written by this tool"):
        contribute.plan(batch, "data/batch1")


def test_every_write_has_the_acl_and_the_sidecar_comes_last(batch):
    s3 = FakeS3()
    creds, _ = _credentials(s3, [_token()])
    result = contribute.contribute(creds, contribute.plan(batch, "data/batch1"), "data/batch1")

    assert all(extra == {"ACL": "bucket-owner-full-control"} for *_, extra in s3.calls)
    assert {c[2] for c in s3.calls[:-1]} == {"data/batch1/a.txt", "data/batch1/sub/b.txt"}
    assert s3.calls[-1][:3] == ("put", "provider-a", "data/batch1/sidecar.json")
    assert s3.sidecar["files"] == 2 and s3.sidecar["bytes"] == 3
    assert result == {
        "bucket": "provider-a",
        "files": 2,
        "bytes": 3,
        "sidecar": "data/batch1/sidecar.json",
    }


def test_no_sidecar_when_an_upload_fails(batch):
    s3 = FakeS3(fail_keys={"data/batch1/sub/b.txt"})
    creds, _ = _credentials(s3, [_token()])
    with pytest.raises(RuntimeError, match="1 of 2 uploads failed; no sidecar written"):
        contribute.contribute(
            creds, contribute.plan(batch, "data/batch1"), "data/batch1", log=lambda m: None
        )
    assert not any(c[0] == "put" for c in s3.calls)


def test_base_key_is_prefixed(batch):
    s3 = FakeS3()
    creds, _ = _credentials(s3, [_token(base_key="inbox/")])
    contribute.contribute(creds, contribute.plan(batch, "data/batch1"), "data/batch1")
    assert s3.calls[-1][2] == "inbox/data/batch1/sidecar.json"


def test_credentials_reused_until_close_to_expiry():
    s3 = FakeS3()
    creds, fetches = _credentials(s3, [_token(hours=12), _token(hours=12, n=2)])
    creds.client()
    creds.client()
    assert len(fetches) == 1


def test_credentials_refetched_when_expiring():
    s3 = FakeS3()
    creds, fetches = _credentials(s3, [_token(hours=0.5), _token(hours=12, n=2)])
    creds.client()  # first fetch: expires in 30 min, under REFRESH_SECONDS
    assert creds.token()["accessKeyId"] == "AK2"
    assert len(fetches) == 2
