import base64
import hashlib
import io
import json
from types import SimpleNamespace

import pytest
from synapseclient.core.exceptions import SynapseHTTPError
from synapseclient.models import StorageLocationType

import indexer


def _http_error(status):
    return SynapseHTTPError(f"{status} error", response=SimpleNamespace(status_code=status))


class FakeSynapse:
    """
    In-memory Synapse behind fake synapseclient.models classes. Their methods are keyword-only,
    like the real ones (whose tracing wrapper rejects positional arguments). (patched into indexer below):
    entities by (parent, name), file handles, the project's upload setting and storage locations.
    It is also the `syn` passed to indexer, for the one REST call (creating S3 file handles).
    Projects and storage locations can only be read: the indexer must never create them.
    """

    fileHandleEndpoint = "https://file.example"

    def __init__(self):
        self.entities = {}  # (parent_id, name) -> {"id", "type", "handle_id", "versions"}
        self.handles = {}
        self.upload_locations = None  # None: no project setting
        self.locations = {}  # id -> {"bucket", "type"}
        self.readable = set()  # location ids this user created (the only ones it can get)
        self.folder_stores = []  # (name, create_or_update)
        self._next = 100

    def new_id(self):
        self._next += 1
        return f"syn{self._next}"

    def by_id(self, entity_id):
        return next(e for e in self.entities.values() if e["id"] == entity_id)

    def restPOST(self, uri, body, endpoint=None):
        assert uri == "/externalFileHandle/s3" and endpoint == self.fileHandleEndpoint
        handle = {**json.loads(body), "id": str(len(self.handles) + 1)}
        self.handles[handle["id"]] = handle
        return handle


class _World:
    current = None  # the FakeSynapse of the running test, set by the `syn` fixture


def client(synapse_client):
    assert synapse_client is _World.current, "every model call must pass synapse_client"
    return synapse_client


def fake_find_entity_id(name, parent, *, synapse_client):
    entity = client(synapse_client).entities.get((parent, name))
    return entity["id"] if entity else None


class FakeProject:
    def __init__(self, id):
        self.id = id

    def get_project_setting(self, *, setting_type, synapse_client):
        w = client(synapse_client)
        return None if w.upload_locations is None else SimpleNamespace(locations=w.upload_locations)


class FakeStorageLocation:
    def __init__(self, storage_location_id=None, storage_type=None, bucket=None):
        self.storage_location_id, self.storage_type, self.bucket = (
            storage_location_id,
            storage_type,
            bucket,
        )

    def get(self, *, synapse_client):
        w = client(synapse_client)
        if self.storage_location_id not in w.readable:
            raise _http_error(403)
        loc = w.locations[self.storage_location_id]
        self.storage_type, self.bucket = loc["type"], loc["bucket"]
        return self


class FakeFolder:
    def __init__(self, name, parent_id, create_or_update=True):
        self.name, self.parent_id, self.create_or_update, self.id = (
            name,
            parent_id,
            create_or_update,
            None,
        )

    def store(self, *, synapse_client):
        w = client(synapse_client)
        w.folder_stores.append((self.name, self.create_or_update))
        existing = w.entities.get((self.parent_id, self.name))
        if existing and not self.create_or_update:
            raise _http_error(409)
        if not existing:
            existing = {"id": w.new_id(), "type": "folder"}
            w.entities[(self.parent_id, self.name)] = existing
        self.id = existing["id"]
        return self


class FakeFile:
    def __init__(
        self,
        id=None,
        parent_id=None,
        name=None,
        data_file_handle_id=None,
        synapse_store=True,
        create_or_update=True,
        download_file=True,
    ):
        assert download_file is False or id is None, "indexer must never download"
        self.id, self.parent_id, self.name = id, parent_id, name
        self.data_file_handle_id, self.synapse_store = data_file_handle_id, synapse_store
        self.create_or_update, self.file_handle = create_or_update, None

    def get(self, *, synapse_client):
        w = client(synapse_client)
        entity = w.by_id(self.id)
        self.data_file_handle_id = entity["handle_id"]
        handle = w.handles[entity["handle_id"]]
        self.file_handle = SimpleNamespace(content_md5=handle["contentMd5"])
        return self

    def store(self, *, synapse_client):
        w = client(synapse_client)
        assert self.synapse_store is False or self.id, "external handles: synapse_store=False"
        if self.id:
            entity = w.by_id(self.id)
            if entity["handle_id"] != self.data_file_handle_id:
                entity.update(handle_id=self.data_file_handle_id, versions=entity["versions"] + 1)
            return self
        if (self.parent_id, self.name) in w.entities and not self.create_or_update:
            raise _http_error(409)
        entity = {
            "id": w.new_id(),
            "type": "file",
            "handle_id": self.data_file_handle_id,
            "versions": 1,
        }
        w.entities[(self.parent_id, self.name)] = entity
        self.id = entity["id"]
        return self


@pytest.fixture
def syn(monkeypatch):
    world = FakeSynapse()
    _World.current = world
    for name, fake in [
        ("find_entity_id", fake_find_entity_id),
        ("Project", FakeProject),
        ("StorageLocation", FakeStorageLocation),
        ("Folder", FakeFolder),
        ("File", FakeFile),
    ]:
        monkeypatch.setattr(indexer, name, fake)
    yield world
    _World.current = None


class FakeS3:
    def __init__(self, body=b"hello", etag=None, sse="AES256", metadata=None):
        self.body = body
        self.etag = etag or hashlib.md5(body).hexdigest()
        self.sse = sse
        self.metadata = metadata or {}
        self.gets = 0

    def head_object(self, Bucket, Key):
        return {
            "ETag": f'"{self.etag}"',
            "ContentLength": len(self.body),
            "ContentType": "binary/octet-stream",
            "ServerSideEncryption": self.sse,
            "Metadata": self.metadata,
        }

    def get_object(self, Bucket, Key):
        self.gets += 1
        return {"Body": io.BytesIO(self.body)}


def _index(syn, s3, key="batch1/sub/data.csv"):
    return indexer.index_object(
        syn, s3, bucket="provider-a", key=key, project_id="syn1", storage_location_id=42
    )


def _add_location(syn, location_id, bucket, mine=True, storage_type=None):
    syn.locations[location_id] = {
        "bucket": bucket,
        "type": storage_type or StorageLocationType.EXTERNAL_S3,
    }
    if mine:
        syn.readable.add(location_id)


# ---------- index_object


def test_creates_folders_and_file(syn):
    s3 = FakeS3()
    result = _index(syn, s3)

    assert result["status"] == "created"
    batch1 = syn.entities[("syn1", "batch1")]
    sub = syn.entities[(batch1["id"], "sub")]
    file = syn.entities[(sub["id"], "data.csv")]
    assert result["entity_id"] == file["id"]
    assert syn.handles[file["handle_id"]] == {
        "concreteType": "org.sagebionetworks.repo.model.file.S3FileHandle",
        "fileName": "data.csv",
        "contentSize": 5,
        "contentType": "text/csv",
        "contentMd5": hashlib.md5(b"hello").hexdigest(),
        "bucketName": "provider-a",
        "key": "batch1/sub/data.csv",
        "storageLocationId": 42,
        "id": "1",
    }
    assert s3.gets == 0  # single-part SSE-S3 ETag is the MD5


def test_unchanged_file_is_left_alone(syn):
    s3 = FakeS3()
    first = _index(syn, s3)
    assert _index(syn, s3) == {"status": "unchanged", "entity_id": first["entity_id"]}
    assert len(syn.handles) == 1


def test_changed_file_adds_a_version(syn):
    first = _index(syn, FakeS3(b"v1"))
    second = _index(syn, FakeS3(b"v2"))
    assert second == {"status": "updated", "entity_id": first["entity_id"]}
    entity = syn.by_id(first["entity_id"])
    assert entity["versions"] == 2
    assert syn.handles[entity["handle_id"]]["contentMd5"] == hashlib.md5(b"v2").hexdigest()


def test_concurrent_create_of_the_same_file_conflicts(syn):
    # Another execution created it between our lookup and our store: fail, and the retry
    # finds it
    real_find = indexer.find_entity_id

    def find_then_lose_race(name, parent, *, synapse_client):
        found = real_find(name, parent, synapse_client=synapse_client)
        if name == "data.csv" and found is None:
            syn.entities[(parent, name)] = {
                "id": "syn999",
                "type": "file",
                "handle_id": None,
                "versions": 1,
            }
        return found

    indexer.find_entity_id = find_then_lose_race
    with pytest.raises(SynapseHTTPError):
        _index(syn, FakeS3(), key="b/data.csv")


def test_empty_path_segments_are_skipped(syn):
    _index(syn, FakeS3(), key="batch1//data.csv")
    assert ("syn1", "") not in syn.entities
    assert syn.entities[(syn.entities[("syn1", "batch1")]["id"], "data.csv")]


def test_unknown_extension_uses_s3_content_type(syn):
    _index(syn, FakeS3(), key="b/data.zzz-unknown")
    assert next(iter(syn.handles.values()))["contentType"] == "binary/octet-stream"


def test_folder_key_rejected(syn):
    with pytest.raises(ValueError):
        _index(syn, FakeS3(), key="batch1/")


def test_folders_are_created_without_create_or_update(syn):
    _index(syn, FakeS3(), key="a/b/data.csv")
    assert syn.folder_stores == [("a", False), ("b", False)]


def test_folder_created_concurrently_is_looked_up_not_updated(syn):
    real_find = indexer.find_entity_id
    raced = {}

    def find_then_lose_race(name, parent, *, synapse_client):
        found = real_find(name, parent, synapse_client=synapse_client)
        if name == "batch1" and not raced:
            # Another worker creates the folder between our lookup and our create
            raced["id"] = syn.new_id()
            syn.entities[(parent, name)] = {"id": raced["id"], "type": "folder"}
        return found

    indexer.find_entity_id = find_then_lose_race
    _index(syn, FakeS3(), key="batch1/data.csv")

    assert syn.folder_stores == [("batch1", False)]  # refused with 409, never updated
    assert syn.entities[("syn1", "batch1")]["id"] == raced["id"]
    assert syn.entities[(raced["id"], "data.csv")]


@pytest.mark.parametrize(
    "s3, reads",
    [
        (FakeS3(b"big", etag="abc-3"), 1),  # multipart, no metadata: computed
        (FakeS3(b"big", sse="aws:kms"), 1),  # KMS ETag is not the MD5
        (FakeS3(b"big", sse="aws:kms:dsse"), 1),
        (
            FakeS3(
                b"big",
                etag="abc-3",
                metadata={"content-md5": base64.b64encode(hashlib.md5(b"big").digest()).decode()},
            ),
            0,
        ),
    ],
)
def test_object_md5(s3, reads):
    head = s3.head_object(Bucket="b", Key="k")
    assert indexer.object_md5(s3, "b", "k", head) == hashlib.md5(b"big").hexdigest()
    assert s3.gets == reads


# ---------- storage locations


def test_resolve_storage_location_matches_bucket(syn):
    syn.upload_locations = [1, 7, 8, 9]
    _add_location(syn, 1, None, mine=False)  # Synapse default storage: not readable (403)
    _add_location(syn, 7, "other")
    _add_location(syn, 8, "provider-a", storage_type=StorageLocationType.EXTERNAL_GOOGLE_CLOUD)
    _add_location(syn, 9, "provider-a")
    assert indexer.resolve_storage_location(syn, "syn1", "provider-a") == 9


def test_resolve_storage_location_ignores_someone_elses(syn):
    syn.upload_locations = [9]
    _add_location(syn, 9, "provider-a", mine=False)
    with pytest.raises(indexer.IndexingError, match="no upload storage location"):
        indexer.resolve_storage_location(syn, "syn1", "provider-a")


@pytest.mark.parametrize("locations", [None, [7]])
def test_resolve_storage_location_missing(syn, locations):
    syn.upload_locations = locations
    _add_location(syn, 7, "other")
    with pytest.raises(indexer.IndexingError, match="no upload storage location"):
        indexer.resolve_storage_location(syn, "syn1", "provider-a")


def test_resolve_storage_finds_project_by_name_and_its_location(syn):
    syn.entities[(None, "RAPID A")] = {"id": "syn1", "type": "project"}
    syn.upload_locations = [9]
    _add_location(syn, 9, "provider-a")
    assert indexer.resolve_storage(syn, "RAPID A", "provider-a") == ("syn1", 9)


def test_resolve_storage_never_creates_the_project(syn):
    with pytest.raises(indexer.IndexingError, match="setup_synapse_projects.py"):
        indexer.resolve_storage(syn, "RAPID A", "provider-a")
    assert syn.entities == {}


@pytest.mark.parametrize("locations", [None, [1]])
def test_resolve_storage_never_creates_the_location(syn, locations):
    # No setting yet, or Synapse default storage (1): the setup script has not run
    syn.entities[(None, "RAPID A")] = {"id": "syn1", "type": "project"}
    syn.upload_locations = locations
    with pytest.raises(indexer.IndexingError, match="setup_synapse_projects.py"):
        indexer.resolve_storage(syn, "RAPID A", "provider-a")
    assert syn.upload_locations == locations and syn.locations == {}
