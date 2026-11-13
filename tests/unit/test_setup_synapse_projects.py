import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from synapseclient.core.exceptions import SynapseHTTPError

ROOT = Path(__file__).parents[2]
_spec = importlib.util.spec_from_file_location("setup", ROOT / "tools/setup_synapse_projects.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)

SERVICE_ACCOUNT = "1234567"
DEFAULT_SYNAPSE_STORAGE = 1


def _http_error(status):
    return SynapseHTTPError(f"{status} error", response=SimpleNamespace(status_code=status))


class FakeSynapse:
    """
    In-memory Synapse behind fake synapseclient.models classes (patched into the script below).
    Methods are keyword-only, like the real ones.
    """

    def __init__(self):
        self.entities = {}  # (parent_id, name) -> id; projects have parent None
        self.settings = {}  # entity id -> upload locations (absent: none set)
        self.locations = {}  # (bucket, sts) -> id
        self.user = SERVICE_ACCOUNT
        self.invisible_projects = set()  # names taken by projects the user cannot see
        self._next = 100

    def login(self, **kwargs):
        pass

    def create(self, name, parent_id):
        if (parent_id, name) in self.entities or (
            parent_id is None and name in self.invisible_projects
        ):
            raise _http_error(409)
        self._next += 1
        self.entities[(parent_id, name)] = f"syn{self._next}"
        return self.entities[(parent_id, name)]

    def children(self, parent_id):
        return {n: i for (p, n), i in self.entities.items() if p == parent_id}


def _fakes(world):
    def find_entity_id(name, parent=None, *, synapse_client):
        assert synapse_client is world
        return world.entities.get((parent, name))

    class Container:
        def __init__(self, id=None, name=None, parent_id=None, create_or_update=True):
            self.id, self.name, self.parent_id = id, name, parent_id
            assert create_or_update is False or id is not None

        def store(self, *, synapse_client):
            self.id = world.create(self.name, self.parent_id)
            return self

        def get_project_setting(self, *, setting_type, synapse_client):
            locations = world.settings.get(self.id)
            return None if locations is None else SimpleNamespace(locations=locations)

        def set_storage_location(self, *, storage_location_id, synapse_client):
            world.settings[self.id] = [storage_location_id]

    def is_sts(location_id):
        return next(sts for (_, sts), i in world.locations.items() if i == location_id)

    class Project(Container):
        def set_storage_location(self, *, storage_location_id, synapse_client):
            if is_sts(storage_location_id):
                raise _http_error(400)  # "Can only enable STS on a folder"
            super().set_storage_location(
                storage_location_id=storage_location_id, synapse_client=synapse_client
            )

    class Folder(Container):
        def set_storage_location(self, *, storage_location_id, synapse_client):
            if is_sts(storage_location_id) and world.children(self.id):
                raise _http_error(400)  # STS only on an empty folder
            super().set_storage_location(
                storage_location_id=storage_location_id, synapse_client=synapse_client
            )

    class StorageLocation:
        def __init__(self, *, storage_type, bucket, sts_enabled=None):
            self.bucket, self.sts = bucket, bool(sts_enabled)

        def store(self, *, synapse_client):
            key = (self.bucket, self.sts)
            self.storage_location_id = world.locations.setdefault(key, 1000 + len(world.locations))
            return self

    class UserProfile:
        def get(self, *, synapse_client):
            return SimpleNamespace(id=world.user)

    return {
        "find_entity_id": find_entity_id,
        "Project": Project,
        "Folder": Folder,
        "StorageLocation": StorageLocation,
        "UserProfile": UserProfile,
    }


@pytest.fixture
def syn(monkeypatch):
    world = FakeSynapse()
    for name, fake in _fakes(world).items():
        monkeypatch.setattr(setup, name, fake)
    # main()'s Synapse() client is the world itself, which the model fakes check they are given
    monkeypatch.setattr(setup.synapseclient, "Synapse", lambda **kwargs: world)
    monkeypatch.setenv("SYNAPSE_AUTH_TOKEN", "pat")
    return world


BUCKET = {"id": "a", "bucket_name": "rapid-test-a", "synapse_project_name": "RAPID A"}
FOLDERS = ["raw", "processed/level_1", "processed/level_2"]


def test_creates_project_on_the_bucket_with_folders_and_sts_shim(syn):
    result = setup.setup_project(syn, BUCKET, FOLDERS)

    project_id = syn.entities[(None, "RAPID A")]
    assert result["project_id"] == project_id
    index_location = syn.locations[("rapid-test-a", False)]
    sts_location = syn.locations[("rapid-test-a", True)]
    assert syn.settings[project_id] == [index_location]

    top = syn.children(project_id)
    assert set(top) == {"raw", "processed", "sts"}
    assert set(syn.children(top["processed"])) == {"level_1", "level_2"}
    assert syn.settings[top["sts"]] == [sts_location]
    assert syn.children(top["sts"]) == {}
    assert result["sts_folder_id"] == top["sts"]
    assert result["sts_location_id"] == sts_location


def test_rerun_changes_nothing(syn):
    first = setup.setup_project(syn, BUCKET, FOLDERS)
    entities, settings = dict(syn.entities), dict(syn.settings)

    assert setup.setup_project(syn, BUCKET, FOLDERS) == first
    assert syn.entities == entities and syn.settings == settings


def test_existing_project_loses_every_other_upload_location(syn):
    project_id = syn.create("RAPID A", None)
    # Made by hand: Synapse's default storage, plus another bucket
    syn.settings[project_id] = [DEFAULT_SYNAPSE_STORAGE, 999]

    setup.setup_project(syn, BUCKET, [])

    assert syn.settings[project_id] == [syn.locations[("rapid-test-a", False)]]


def test_custom_sts_folder_name(syn):
    setup.setup_project(syn, {**BUCKET, "sts_folder": "contribute"}, [])
    project_id = syn.entities[(None, "RAPID A")]
    assert set(syn.children(project_id)) == {"contribute"}


def test_non_empty_sts_folder_is_a_setup_error(syn):
    project_id = syn.create("RAPID A", None)
    syn.create("stray.txt", syn.create("sts", project_id))

    with pytest.raises(setup.SetupError, match="'sts' is empty"):
        setup.setup_project(syn, BUCKET, [])


def test_name_taken_by_an_invisible_project(syn):
    syn.invisible_projects.add("RAPID A")
    with pytest.raises(setup.SetupError, match="cannot see it"):
        setup.setup_project(syn, BUCKET, [])
    assert syn.entities == {}


def test_main_requires_the_service_account(syn, monkeypatch):
    syn.user = "7654321"
    with pytest.raises(SystemExit, match="not the service account"):
        setup.main(["--env", "sandbox"])
    assert syn.entities == {}


def test_main_sets_up_every_bucket_and_writes_the_job_summary(syn, monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    config = setup.load_context_config("sandbox")
    syn.user = config["SYNAPSE"]["service_account_id"]

    assert setup.main(["--env", "sandbox"]) == 0

    for bucket in config["BUCKETS"]:
        assert (None, bucket["synapse_project_name"]) in syn.entities
        assert f"`{bucket['bucket_name']}`" in summary.read_text()


def test_main_continues_past_a_failing_bucket_and_fails(syn, monkeypatch):
    two = {
        "PROJECT_NAME": "rapid",
        "SYNAPSE": {"service_account_id": SERVICE_ACCOUNT},
        "BUCKETS": [
            BUCKET,
            {**BUCKET, "id": "b", "bucket_name": "rapid-test-b", "synapse_project_name": "RAPID B"},
        ],
    }
    monkeypatch.setattr(setup, "load_context_config", lambda env: two)
    syn.invisible_projects.add("RAPID A")

    assert setup.main(["--env", "dev"]) == 1
    assert (None, "RAPID B") in syn.entities
