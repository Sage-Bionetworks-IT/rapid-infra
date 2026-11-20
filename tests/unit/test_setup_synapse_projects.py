import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from synapseclient.core.exceptions import SynapseHTTPError

ROOT = Path(__file__).parents[2]
_spec = importlib.util.spec_from_file_location("setup", ROOT / "tools/setup_synapse_projects.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)

SERVICE_ACCOUNT = "1234567"
ADMIN = ["READ", "DOWNLOAD", "CREATE", "UPDATE", "DELETE", "CHANGE_PERMISSIONS", "CHANGE_SETTINGS"]
DEFAULT_SYNAPSE_STORAGE = 1


def _http_error(status):
    return SynapseHTTPError(f"{status} error", response=SimpleNamespace(status_code=status))


class FakeSynapse:
    """
    In-memory Synapse behind fake synapseclient.models classes (patched into the script below).
    Methods are keyword-only, like the real ones.
    """

    def __init__(self):
        self.projects = {}  # name -> id
        self.settings = {}  # project id -> upload locations (absent: none set)
        self.locations = {}  # bucket -> id
        self.user = SERVICE_ACCOUNT
        self.invisible_projects = set()  # names taken by projects the user cannot see
        self.acls = {}  # project id -> {principal id (str): access types}
        self.acl_writes = 0
        self.unknown_principals = set()
        self._next = 100

    def login(self, **kwargs):
        pass

    def create_project(self, name):
        if name in self.projects or name in self.invisible_projects:
            raise _http_error(409)
        self._next += 1
        self.projects[name] = f"syn{self._next}"
        # Synapse makes the creator an administrator
        self.acls[self.projects[name]] = {self.user: ADMIN}
        return self.projects[name]


# The fakes get the FakeSynapse as synapse_client, as main() passes its Synapse() client (the
# fixture makes that the FakeSynapse too)


def fake_find_entity_id(name, parent=None, *, synapse_client):
    assert parent is None, "only projects are looked up"
    return synapse_client.projects.get(name)


class FakeProject:
    def __init__(self, id=None, name=None, create_or_update=True):
        assert create_or_update is False or id is not None, "creates must not update"
        self.id, self.name = id, name

    def store(self, *, synapse_client):
        self.id = synapse_client.create_project(self.name)
        return self

    def get_project_setting(self, *, setting_type, synapse_client):
        locations = synapse_client.settings.get(self.id)
        return None if locations is None else SimpleNamespace(locations=locations)

    def set_storage_location(self, *, storage_location_id, synapse_client):
        synapse_client.settings[self.id] = [storage_location_id]

    def list_acl(self, *, synapse_client):
        acl = synapse_client.acls.get(self.id, {})
        return SimpleNamespace(
            entity_acl=[
                SimpleNamespace(principal_id=p, permissions=list(a)) for p, a in acl.items()
            ]
        )

    def set_permissions(self, *, principal_id, access_type, overwrite, synapse_client):
        assert overwrite is True, "the principal gets exactly its level"
        assert isinstance(principal_id, int)
        if principal_id in synapse_client.unknown_principals:
            raise _http_error(404)
        synapse_client.acl_writes += 1
        acl = synapse_client.acls.setdefault(self.id, {})
        if access_type:
            acl[str(principal_id)] = list(access_type)
        else:  # an empty access_type removes the principal
            acl.pop(str(principal_id), None)


class FakeStorageLocation:
    def __init__(self, *, storage_type, bucket):
        self.bucket = bucket

    def store(self, *, synapse_client):
        # Synapse returns the user's existing identical location
        locations = synapse_client.locations
        self.storage_location_id = locations.setdefault(self.bucket, 1000 + len(locations))
        return self


class FakeUserProfile:
    def get(self, *, synapse_client):
        return SimpleNamespace(id=synapse_client.user)


@pytest.fixture
def syn(monkeypatch):
    world = FakeSynapse()
    monkeypatch.setattr(setup, "find_entity_id", fake_find_entity_id)
    monkeypatch.setattr(setup, "Project", FakeProject)
    monkeypatch.setattr(setup, "StorageLocation", FakeStorageLocation)
    monkeypatch.setattr(setup, "UserProfile", FakeUserProfile)
    monkeypatch.setattr(setup.synapseclient, "Synapse", lambda **kwargs: world)
    monkeypatch.setenv("SYNAPSE_AUTH_TOKEN", "pat")
    return world


PROJECT = {"name": "RAPID A", "bucket": "rapid-test-a"}
# The bucket stacks' outputs
BUCKET = {"BucketName": "rapid-test-a", "SynapseServiceAccountId": SERVICE_ACCOUNT}
BUCKET_B = {**BUCKET, "BucketName": "rapid-test-b"}
PROJECT_B = {"name": "RAPID B", "bucket": "rapid-test-b"}


def _files(tmp_path, projects, outputs):
    template, cdk_outputs = tmp_path / "synapse.yaml", tmp_path / "cdk-outputs.json"
    template.write_text(yaml.safe_dump({"projects": projects}))
    cdk_outputs.write_text(json.dumps(outputs))
    return ["--template", str(template), "--cdk-outputs", str(cdk_outputs)]


def test_creates_the_project_on_the_deployed_bucket(syn):
    result = setup.setup_project(syn, SERVICE_ACCOUNT, PROJECT, BUCKET)

    project_id = syn.projects["RAPID A"]
    location_id = syn.locations["rapid-test-a"]
    assert result == {
        "name": "RAPID A",
        "project_id": project_id,
        "bucket": "rapid-test-a",
        "storage_location_id": location_id,
    }
    assert syn.settings[project_id] == [location_id]


def test_rerun_changes_nothing(syn):
    first = setup.setup_project(syn, SERVICE_ACCOUNT, PROJECT, BUCKET)
    projects, settings = dict(syn.projects), dict(syn.settings)

    assert setup.setup_project(syn, SERVICE_ACCOUNT, PROJECT, BUCKET) == first
    assert syn.projects == projects and syn.settings == settings


def test_existing_project_loses_every_other_upload_location(syn):
    project_id = syn.create_project("RAPID A")
    # Made by hand: Synapse's default storage, plus another bucket
    syn.settings[project_id] = [DEFAULT_SYNAPSE_STORAGE, 999]

    setup.setup_project(syn, SERVICE_ACCOUNT, PROJECT, BUCKET)

    assert syn.settings[project_id] == [syn.locations["rapid-test-a"]]


TEAM = 3629180
EDIT = ["READ", "DOWNLOAD", "CREATE", "UPDATE"]
SHARED = {**PROJECT, "acl": [{"principal_id": TEAM, "permission": "edit"}]}


def test_shares_the_project(syn):
    setup.setup_project(syn, SERVICE_ACCOUNT, SHARED, BUCKET)
    assert syn.acls[syn.projects["RAPID A"]] == {SERVICE_ACCOUNT: ADMIN, "3629180": EDIT}


def test_rerun_leaves_the_acl_alone(syn):
    setup.setup_project(syn, SERVICE_ACCOUNT, SHARED, BUCKET)
    setup.setup_project(syn, SERVICE_ACCOUNT, SHARED, BUCKET)
    assert syn.acl_writes == 1


def test_acl_is_made_to_match_the_template(syn):
    project_id = syn.create_project("RAPID A")
    # Shared by hand: the team with more than the template's level, someone else, the public
    syn.acls[project_id].update({"3629180": EDIT + ["DELETE"], "42": ["READ"], "273949": ["READ"]})

    setup.setup_project(syn, SERVICE_ACCOUNT, SHARED, BUCKET)

    assert syn.acls[project_id] == {SERVICE_ACCOUNT: ADMIN, "3629180": EDIT}


def test_entry_taken_off_the_acl_is_removed(syn):
    setup.setup_project(syn, SERVICE_ACCOUNT, SHARED, BUCKET)
    setup.setup_project(syn, SERVICE_ACCOUNT, PROJECT, BUCKET)  # no acl
    assert syn.acls[syn.projects["RAPID A"]] == {SERVICE_ACCOUNT: ADMIN}


def test_service_account_access_is_never_changed(syn):
    project_id = syn.create_project("RAPID A")
    syn.acls[project_id][SERVICE_ACCOUNT] = ADMIN + ["MODERATE"]
    setup.setup_project(syn, SERVICE_ACCOUNT, PROJECT, BUCKET)
    assert syn.acls[project_id] == {SERVICE_ACCOUNT: ADMIN + ["MODERATE"]}


def test_acl_must_not_list_the_service_account(syn):
    project = {**PROJECT, "acl": [{"principal_id": SERVICE_ACCOUNT, "permission": "view"}]}
    with pytest.raises(setup.SetupError, match="lists the service account"):
        setup.setup_project(syn, SERVICE_ACCOUNT, project, BUCKET)
    assert syn.acl_writes == 0


def test_acl_unknown_principal(syn):
    syn.unknown_principals.add(TEAM)
    with pytest.raises(setup.SetupError, match="share the project with 3629180"):
        setup.setup_project(syn, SERVICE_ACCOUNT, SHARED, BUCKET)


def test_name_taken_by_an_invisible_project(syn):
    syn.invisible_projects.add("RAPID A")
    with pytest.raises(setup.SetupError, match="cannot see it"):
        setup.setup_project(syn, SERVICE_ACCOUNT, PROJECT, BUCKET)
    assert syn.projects == {}


def test_must_run_as_the_bucket_stacks_service_account(syn):
    with pytest.raises(setup.SetupError, match="not the service account 1234567"):
        setup.setup_project(syn, "7654321", PROJECT, BUCKET)
    assert syn.projects == {}


@pytest.mark.parametrize(
    "outputs",
    [
        {},  # not deployed
        {"rapid-dev-bucket-rapid-test-b": BUCKET_B},  # another bucket
        {"rapid-dev-bucket-rapid-test-a": BUCKET, "other": BUCKET},  # ambiguous
    ],
)
def test_bucket_outputs_need_exactly_one_bucket_stack(outputs):
    with pytest.raises(setup.SetupError, match="one deployed stack for bucket 'rapid-test-a'"):
        setup.bucket_outputs(outputs, PROJECT)


def test_bucket_outputs_ignore_other_stacks():
    outputs = {
        "rapid-dev-bucket-rapid-test-a": BUCKET,
        "rapid-dev-bucket-rapid-test-b": BUCKET_B,
        "rapid-dev-pipeline": {},  # no BucketName
    }
    assert setup.bucket_outputs(outputs, PROJECT) == BUCKET


def test_main_sets_up_every_project_and_writes_the_job_summary(syn, monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    args = _files(
        tmp_path,
        [PROJECT, PROJECT_B],
        {"rapid-dev-bucket-rapid-test-a": BUCKET, "rapid-dev-bucket-rapid-test-b": BUCKET_B},
    )

    assert setup.main(args) == 0

    assert set(syn.projects) == {"RAPID A", "RAPID B"}
    assert "`rapid-test-a`" in summary.read_text()
    assert "`rapid-test-b`" in summary.read_text()


def test_main_continues_past_a_failing_project_and_fails(syn, tmp_path):
    # RAPID A's bucket stack was not deployed
    args = _files(
        tmp_path,
        [PROJECT, PROJECT_B],
        {"rapid-dev-bucket-rapid-test-b": BUCKET_B},
    )

    assert setup.main(args) == 1
    assert set(syn.projects) == {"RAPID B"}


def test_main_project_filter(syn, tmp_path):
    args = _files(tmp_path, [PROJECT], {"rapid-dev-bucket-rapid-test-a": BUCKET})
    with pytest.raises(SystemExit, match="No project 'RAPID Z'"):
        setup.main(args + ["--project", "RAPID Z"])
