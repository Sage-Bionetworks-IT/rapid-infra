import pytest

import sync


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SIDECAR_FILENAME", "sidecar.json")
    monkeypatch.setenv("SYNAPSE_PAT_SECRET_ARN", "arn:secret")
    sync._reset()
    calls = {"login": 0, "resolve": [], "resolve_encrypted": [], "index": []}
    syn = object()

    def fake_synapse():
        if sync._syn is None:
            calls["login"] += 1
            sync._syn = syn
        return sync._syn

    def fake_resolve(s, project_name, bucket):
        assert s is syn
        calls["resolve"].append((project_name, bucket))
        if bucket == "broken":
            raise RuntimeError("Synapse 503")
        return "syn1", 42

    def fake_resolve_encrypted(s, bucket):
        assert s is syn
        calls["resolve_encrypted"].append(bucket)
        return 43

    def fake_index(s, s3, **kwargs):
        assert s is syn
        calls["index"].append(kwargs)
        if kwargs["key"].endswith("boom.csv"):
            raise RuntimeError("Synapse 503")
        return {"status": "created", "entity_id": "syn9"}

    monkeypatch.setattr(sync, "_synapse", fake_synapse)
    monkeypatch.setattr(sync, "resolve_synapse_storage", fake_resolve)
    monkeypatch.setattr(sync, "resolve_encrypted_location", fake_resolve_encrypted)
    monkeypatch.setattr(sync, "index_object", fake_index)
    yield calls
    sync._reset()


def _item(key):
    return {"bucket": "provider-a", "key": key, "project_id": "syn1", "storage_location_id": 42}


def test_resolve_storage(env):
    prepared = {
        "bucket": "provider-a",
        "prefix": "b/",
        "sidecar_key": "b/sidecar.json",
        "project_name": "RAPID A",
    }
    assert sync.resolve_storage(prepared, None) == {
        "project_id": "syn1",
        "storage_location_id": 42,
        "encrypted_storage_location_id": None,
    }
    assert env["resolve"] == [("RAPID A", "provider-a")]


def test_resolve_storage_error_resets_login(env):
    with pytest.raises(RuntimeError):
        sync.resolve_storage({"bucket": "broken", "project_name": "RAPID A"}, None)
    sync.resolve_storage({"bucket": "provider-a", "project_name": "RAPID A"}, None)
    assert env["login"] == 2


def test_indexes_a_file_with_the_given_location(env):
    assert sync.handler(_item("b/data.csv"), None) == {
        "key": "b/data.csv",
        "status": "created",
        "entity_id": "syn9",
    }
    assert env["index"] == [
        {
            "bucket": "provider-a",
            "key": "b/data.csv",
            "project_id": "syn1",
            "storage_location_id": 42,
            "content_md5": None,
        }
    ]
    assert env["resolve"] == []  # resolved once per execution, not per file


def test_indexes_an_encrypted_file_and_passes_staged_on(env):
    staged = {"bucket": "provider-a", "key": "b/data.vcf", "etag": '"e"'}
    item = {
        **_item("b/data.vcf.encrypted"),
        "bucket": "provider-a-enc",
        "content_md5": "ab" * 16,
        "staged": staged,
    }
    assert sync.handler(item, None) == {
        "key": "b/data.vcf.encrypted",
        "status": "created",
        "entity_id": "syn9",
        "staged": staged,
    }
    assert env["index"][0]["bucket"] == "provider-a-enc"
    assert env["index"][0]["content_md5"] == "ab" * 16


def test_resolve_storage_looks_up_the_encrypted_bucket_location(env):
    cse = {"bucket": "provider-a-enc", "patterns": ["*"], "key_arn": "k", "provider": "a"}
    result = sync.resolve_storage(
        {"bucket": "provider-a", "project_name": "RAPID A", "cse": cse}, None
    )
    assert result["encrypted_storage_location_id"] == 43
    assert env["resolve_encrypted"] == ["provider-a-enc"]


@pytest.mark.parametrize(
    "key, reason",
    [("b/", "folder marker"), ("b/sidecar.json", "sidecar"), ("b/nested/sidecar.json", "sidecar")],
)
def test_skips(env, key, reason):
    assert sync.handler(_item(key), None) == {"key": key, "status": "skipped", "reason": reason}
    assert env["index"] == []


def test_login_reused_while_warm(env):
    sync.handler(_item("b/1.csv"), None)
    sync.handler(_item("b/2.csv"), None)
    assert env["login"] == 1


def test_error_resets_login(env):
    sync.handler(_item("b/1.csv"), None)
    with pytest.raises(RuntimeError):
        sync.handler(_item("b/boom.csv"), None)
    sync.handler(_item("b/2.csv"), None)
    assert env["login"] == 2
