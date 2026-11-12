"""
Set up each configured bucket's Synapse project. CI runs this after every deploy
(aws-deploy.yaml), once the bucket stacks, and so the buckets' owner.txt, exist.

Never create a provider's project by hand: until its upload destination is set, a new project
stores uploads in Synapse's default S3 bucket, where RAPID data must not go. For each bucket,
idempotently, as the sync service account:

1. The project, BUCKETS[].synapse_project_name: found by name, or created (the service account
   becomes its administrator). Renaming it in config makes a new project: rename it in
   Synapse first.
2. Its upload destination: a non-STS external S3 location on the bucket, where indexed files
   live, set as the project's ONLY upload location (replacing any other, including Synapse's
   default storage). Synapse checks owner.txt lists the service account.
3. SYNAPSE.project_folders, created in the project.
4. The STS "shim" folder, BUCKETS[].sts_folder (default "sts"): empty, with an STS location on
   the whole bucket, from which contributors with edit access get read-write STS credentials.
   With client_side_encryption, also the non-STS location on the encrypted bucket where
   encrypted files are indexed: on no project or folder, so nothing can be uploaded there
   through Synapse (unencrypted). The pipeline only looks it up.
5. apply_project_template(): placeholder for the rest of the project setup.

It must run as the sync service account (SYNAPSE.service_account_id): only a storage
location's creator may create file handles in it, which is what the sync does.

    SYNAPSE_AUTH_TOKEN=<service account PAT> python tools/setup_synapse_projects.py --env dev [--bucket ID]
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import synapseclient  # noqa: E402
from synapseclient.core.exceptions import SynapseHTTPError  # noqa: E402
from synapseclient.models import (  # noqa: E402
    Folder,
    Project,
    StorageLocation,
    StorageLocationType,
    UserProfile,
)
from synapseclient.operations import find_entity_id  # noqa: E402

from src.utils import DEFAULT_STS_FOLDER, VALID_ENVS, load_context_config  # noqa: E402


class SetupError(Exception):
    """A bucket's Synapse setup cannot proceed; rerunning will not help until it is fixed."""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", required=True, choices=sorted(VALID_ENVS))
    parser.add_argument("--bucket", help="Only this bucket id (config BUCKETS[].id)")
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    config = load_context_config(args.env)
    buckets = [b for b in config.get("BUCKETS") or [] if args.bucket in (None, b["id"])]
    if args.bucket and not buckets:
        sys.exit(f"No bucket '{args.bucket}' in config/{args.env}.yaml")
    token = os.environ.get("SYNAPSE_AUTH_TOKEN")
    if not token:
        sys.exit("Set SYNAPSE_AUTH_TOKEN to the service account's Synapse PAT")

    syn = synapseclient.Synapse(skip_checks=True, silent=True)
    syn.login(authToken=token, silent=True)
    check_service_account(syn, config["SYNAPSE"]["service_account_id"])

    folders = config["SYNAPSE"].get("project_folders") or []
    results, failures = [], []
    for bucket in buckets:
        try:
            results.append(setup_project(syn, bucket, folders))
        except (SetupError, SynapseHTTPError) as e:
            failures.append((bucket, e))
            print(f"✗ {bucket['id']}: {e}", file=sys.stderr)

    _report(args.env, results, failures)
    return 1 if failures else 0


def check_service_account(syn, service_account_id) -> None:
    me = str(UserProfile().get(synapse_client=syn).id)  # no id or username: the current user
    if me != str(service_account_id):
        sys.exit(
            f"Logged in as Synapse user {me}, not the service account {service_account_id} "
            "(SYNAPSE.service_account_id): the sync could not use the storage locations"
        )


def setup_project(syn, bucket: dict, folders: list) -> dict:
    name, bucket_name = bucket["synapse_project_name"], bucket["bucket_name"]
    sts_folder = bucket.get("sts_folder", DEFAULT_STS_FOLDER)
    try:
        project_id = ensure_project(syn, name)
        # Straight after creation: nothing can be uploaded to the project before this
        location_id = ensure_only_location(syn, Project(id=project_id), bucket_name, sts=False)
        ensure_folders(syn, project_id, folders)
        sts_folder_id = find_entity_id(sts_folder, project_id, synapse_client=syn)
        sts_folder_id = sts_folder_id or _create_folder(syn, sts_folder, project_id)
        sts_location_id = ensure_only_location(syn, Folder(id=sts_folder_id), bucket_name, sts=True)
        encrypted_bucket = (bucket.get("client_side_encryption") or {}).get("encrypted_bucket_name")
        encrypted_location_id = (
            ensure_location(syn, encrypted_bucket, sts=False) if encrypted_bucket else None
        )
        apply_project_template(syn, project_id, bucket)
    except SynapseHTTPError as e:
        if _status(e) in (400, 403):
            raise SetupError(
                f"Could not connect s3://{bucket_name} to '{name}': {e}. Check the bucket stack "
                "is deployed and its owner.txt lists the service account, that the service "
                f"account administers the project, and that '{sts_folder}' is empty with no "
                "other storage location (Synapse only adds STS to such a folder)"
            ) from e
        raise
    result = {
        "id": bucket["id"],
        "bucket": bucket_name,
        "project_id": project_id,
        "storage_location_id": location_id,
        "sts_folder_id": sts_folder_id,
        "sts_location_id": sts_location_id,
        "encrypted_storage_location_id": encrypted_location_id,
    }
    print(
        f"✓ {bucket['id']}: s3://{bucket_name} → {project_id} ('{name}'), upload location "
        f"{location_id}; STS folder {sts_folder} {sts_folder_id} (location {sts_location_id})"
    )
    print(_contributor_help(sts_folder_id, bucket_name))
    return result


def _contributor_help(sts_folder_id: str, bucket: str) -> str:
    return f"""
  Send the provider (who needs edit access to {sts_folder_id}):
    python tools/contribute.py --sts-folder {sts_folder_id} <local-dir> <s3-folder>
  It uploads <local-dir> to s3://{bucket}/<s3-folder>/ with read-write STS credentials, then
  writes <s3-folder>/sidecar.json, which indexes the folder into the project at <s3-folder>/.
"""


def ensure_project(syn, name: str) -> str:
    project_id = find_entity_id(name, None, synapse_client=syn)
    if project_id:
        return project_id
    try:
        return Project(name=name, create_or_update=False).store(synapse_client=syn).id
    except SynapseHTTPError as e:
        if _status(e) == 409:
            # Project names are global: someone else's project, invisible to the service account
            raise SetupError(
                f"A Synapse project named '{name}' exists, but the service account cannot see "
                "it: pick another synapse_project_name, or make the service account an "
                "administrator of that project"
            ) from e
        raise


def ensure_location(syn, bucket: str, sts: bool) -> int:
    """
    The service account's external S3 location on the bucket. StorageLocation.store() returns
    this user's existing identical location rather than a duplicate. Synapse checks the
    bucket's owner.txt lists the service account.
    """
    return (
        StorageLocation(
            storage_type=StorageLocationType.EXTERNAL_S3, bucket=bucket, sts_enabled=sts or None
        )
        .store(synapse_client=syn)
        .storage_location_id
    )


def ensure_only_location(syn, container, bucket: str, sts: bool) -> int:
    """
    Make an external S3 location on the bucket the container's only upload destination;
    returns its ID.
    """
    location_id = ensure_location(syn, bucket, sts)
    setting = container.get_project_setting(setting_type="upload", synapse_client=syn)
    if list((setting.locations if setting else None) or []) != [location_id]:
        # A destructive update: the given list replaces the container's locations
        container.set_storage_location(storage_location_id=location_id, synapse_client=syn)
    return location_id


def ensure_folders(syn, project_id: str, paths: list) -> None:
    """Create each folder path ("a/b") under the project; existing folders are kept."""
    for path in paths:
        parent_id = project_id
        for name in (n for n in str(path).split("/") if n):
            parent_id = find_entity_id(name, parent_id, synapse_client=syn) or _create_folder(
                syn, name, parent_id
            )


def _create_folder(syn, name: str, parent_id: str) -> str:
    return (
        Folder(name=name, parent_id=parent_id, create_or_update=False).store(synapse_client=syn).id
    )


def apply_project_template(syn, project_id: str, bucket: dict) -> None:
    """
    Placeholder for the rest of a provider project's setup, along the lines of HTAN2's
    https://github.com/ncihtan/htan2_project_setup:

    - teams, and project / folder permissions (sharing the project with its provider)
    - a versioned folder structure (e.g. ingest / staging / release) from a data model
    - JSON schema bindings, file views and curation tasks

    Until then the project is visible only to the service account; share it by hand.
    """


def _status(error: SynapseHTTPError):
    return getattr(getattr(error, "response", None), "status_code", None)


def _report(env: str, results: list, failures: list) -> None:
    """A table of every bucket's project, in the GitHub Actions job summary when run there."""
    lines = [
        f"### Synapse projects ({env})",
        "",
        "| Bucket | Project | Upload location | STS folder |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| `{r['bucket']}` | {r['project_id']} | {r['storage_location_id']} "
        f"| {r['sts_folder_id']} (location {r['sts_location_id']}) |"
        for r in results
    ]
    lines += [f"| `{b['bucket_name']}` | ✗ {e} | | |" for b, e in failures]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
