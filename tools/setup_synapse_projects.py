"""
Set up the Synapse projects of a Synapse template (config/synapse/{env}.yaml) on the buckets the
CDK app deployed. CI runs this after every deploy (aws-deploy.yaml), with the deploy's outputs:
each $ref.<Output>.<bucket_name> in the template is that output of the stack of the bucket with
that BUCKETS bucket_name (the stack whose BucketName output it is), as a Terraform resource would
take another's output. A project's bucket, $ref.BucketName.<bucket_name>, also names the stack
that gives the service account.

Never create a provider's project by hand: until its upload destination is set, a new project
stores uploads in Synapse's default S3 bucket, where RAPID data must not go. For each project,
idempotently, as the sync service account:

1. The project, found by name or created (the service account becomes its administrator).
   Renaming it in the template makes a new project: rename it in Synapse first.
2. Its upload destination: a non-STS external S3 location on the bucket, where indexed files
   live, set as the project's ONLY upload location (replacing any other, including Synapse's
   default storage). Synapse checks the bucket's owner.txt lists the service account.
3. The STS "shim" folder (the project's sts_folder, default "sts"): empty, with an STS location
   on the whole bucket, from which contributors with edit and delete access get read-write STS
   credentials.
   Logs the STS folder's ID to send the provider.
4. Its sharing settings, made to match the project's acl, as CloudFormation does a stack's
   resources: each principal in it gets exactly its permission level, and every other principal
   (anyone shared by hand, public access) is removed, except the service account.
5. apply_project_template(): placeholder for the rest of the project setup.

It must run as the service account in the bucket stack's SynapseServiceAccountId output: only a
storage location's creator may create file handles in it, which is what the sync does.

    cdk deploy --context env=dev --all --outputs-file cdk-outputs.json
    SYNAPSE_AUTH_TOKEN=<service account PAT> python tools/setup_synapse_projects.py \\
        --template config/synapse/dev.yaml --cdk-outputs cdk-outputs.json [--project NAME]
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from synapseclient import Synapse  # noqa: E402
from synapseclient.core.exceptions import SynapseHTTPError  # noqa: E402
from synapseclient.models import (  # noqa: E402
    Folder,
    Project,
    StorageLocation,
    StorageLocationType,
    UserProfile,
)
from synapseclient.operations import find_entity_id  # noqa: E402

from src.synapse_template import (  # noqa: E402
    DEFAULT_STS_FOLDER,
    PERMISSIONS,
    load_template,
    parse_ref,
)

logger = logging.getLogger(__name__)


class SetupError(Exception):
    """A project's Synapse setup cannot proceed; rerunning will not help until it is fixed."""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--template", required=True, help="config/synapse/{env}.yaml")
    parser.add_argument(
        "--cdk-outputs", required=True, help="the JSON of cdk deploy --outputs-file"
    )
    parser.add_argument("--project", help="Only this project (its name in the template)")
    args = parser.parse_args(argv)

    projects = [
        p for p in load_template(args.template)["projects"] if args.project in (None, p["name"])
    ]
    if args.project and not projects:
        sys.exit(f"No project '{args.project}' in {args.template}")
    outputs = json.loads(Path(args.cdk_outputs).read_text())
    token = os.environ.get("SYNAPSE_AUTH_TOKEN")
    if not token:
        sys.exit("Set SYNAPSE_AUTH_TOKEN to the service account's Synapse PAT")

    syn = Synapse(skip_checks=True, silent=True)
    syn.login(authToken=token, silent=True)
    # No id or username: the logged-in user
    current_user = str(UserProfile().get(synapse_client=syn).id)

    results, failures = [], []
    for project in projects:
        try:
            bucket = bucket_outputs(outputs, parse_ref(project["bucket"]).bucket)
            results.append(setup_project(syn, current_user, resolve_refs(project, outputs), bucket))
        except (SetupError, SynapseHTTPError) as e:
            failures.append((project, e))
            logger.error("✗ %s: %s", project["name"], e)

    _report(args.template, results, failures)
    return 1 if failures else 0


def bucket_outputs(outputs: dict, bucket: str) -> dict:
    """The outputs of the stack that deployed the bucket (its BucketName output)."""
    stacks = [name for name, out in outputs.items() if out.get("BucketName") == bucket]
    if len(stacks) != 1:
        raise SetupError(
            f"Expected one deployed stack for bucket '{bucket}' in the CDK outputs, found "
            f"{stacks or 'none'}: is it a BUCKETS bucket_name, and was its stack deployed?"
        )
    return outputs[stacks[0]]


def resolve_refs(value, outputs: dict):
    """The value with every $ref.<Output>.<bucket_name> in it replaced by that stack output."""
    if isinstance(value, dict):
        return {key: resolve_refs(item, outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_refs(item, outputs) for item in value]
    ref = parse_ref(value)
    if ref is None:
        return value
    stack = bucket_outputs(outputs, ref.bucket)
    if ref.output not in stack:
        raise SetupError(
            f"{value}: the stack of bucket '{ref.bucket}' has no output '{ref.output}' "
            f"(it has {', '.join(sorted(stack))})"
        )
    return stack[ref.output]


def setup_project(syn, current_user: str, project: dict, bucket: dict) -> dict:
    name, bucket_name = project["name"], bucket["BucketName"]
    sts_folder = project.get("sts_folder", DEFAULT_STS_FOLDER)
    if current_user != str(bucket["SynapseServiceAccountId"]):
        raise SetupError(
            f"Logged in as Synapse user {current_user}, not the service account "
            f"{bucket['SynapseServiceAccountId']} in s3://{bucket_name}'s owner.txt: the sync "
            "could not use the storage location"
        )
    try:
        project_id = ensure_project(syn, name)
        # Straight after creation: nothing can be uploaded to the project before this
        location_id = ensure_only_location(syn, Project(id=project_id), bucket_name)
        sts_folder_id = find_entity_id(sts_folder, project_id, synapse_client=syn)
        sts_folder_id = sts_folder_id or _create_folder(syn, sts_folder, project_id)
        sts_location_id = ensure_only_location(syn, Folder(id=sts_folder_id), bucket_name, sts=True)
        acl_changes = ensure_acl(syn, Project(id=project_id), project.get("acl", []), current_user)
        apply_project_template(syn, project_id, project)
    except SynapseHTTPError as e:
        if _status(e) in (400, 403):
            raise SetupError(
                f"Could not connect s3://{bucket_name} to '{name}': {e}. Check the bucket's "
                "owner.txt lists the service account, that the service account administers the "
                f"project, and that '{sts_folder}' is empty with no other storage location "
                "(Synapse only adds STS to such a folder)"
            ) from e
        raise
    logger.info(
        "✓ %s: %s on s3://%s, upload location %s; STS folder %s %s (location %s)",
        name,
        project_id,
        bucket_name,
        location_id,
        sts_folder,
        sts_folder_id,
        sts_location_id,
    )
    for change in acl_changes:
        logger.info("  sharing %s", change)
    logger.info(_contributor_help(sts_folder_id, bucket_name))
    return {
        "name": name,
        "project_id": project_id,
        "bucket": bucket_name,
        "storage_location_id": location_id,
        "sts_folder_id": sts_folder_id,
        "sts_location_id": sts_location_id,
    }


def _contributor_help(sts_folder_id: str, bucket: str) -> str:
    return f"""
  Send the provider the STS folder {sts_folder_id} (they need edit and delete access to it).
  Its read-write STS credentials upload to s3://{bucket}/<s3-folder>/; writing
  <s3-folder>/sidecar.json last indexes the folder into the project at <s3-folder>/.
  tools/contribute.py does this with the mock dataset (set its STS_FOLDER_ID).
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
                "it: rename the project in the template, or make the service account an "
                "administrator of that project"
            ) from e
        raise


def ensure_only_location(syn, container, bucket: str, sts: bool = False) -> int:
    """
    Make the service account's external S3 location on the bucket (STS-enabled or not) the
    container's only upload destination; returns its ID. StorageLocation.store() returns this
    user's existing identical location rather than a duplicate.
    """
    location_id = (
        StorageLocation(
            storage_type=StorageLocationType.EXTERNAL_S3, bucket=bucket, sts_enabled=sts or None
        )
        .store(synapse_client=syn)
        .storage_location_id
    )
    setting = container.get_project_setting(setting_type="upload", synapse_client=syn)
    if list((setting.locations if setting else None) or []) != [location_id]:
        # A destructive update: the given list replaces the container's locations
        container.set_storage_location(storage_location_id=location_id, synapse_client=syn)
    return location_id


def ensure_acl(syn, project, acl: list, current_user: str) -> list:
    """
    Make the project's sharing settings exactly the acl, plus the service account (whose admin
    access is what lets this script manage the project, so it is never changed). Returns the
    changes made, for the log.
    """
    wanted = {str(e["principal_id"]): PERMISSIONS[e["permission"]] for e in acl}
    if current_user in wanted:
        raise SetupError(
            f"The acl lists the service account {current_user}: it administers the project "
            "already, and changing its permissions would lock this setup out"
        )
    current = {
        str(e.principal_id): e.permissions
        for e in project.list_acl(synapse_client=syn).entity_acl
        if str(e.principal_id) != current_user
    }
    changes = [
        (principal, access)
        for principal, access in wanted.items()
        if sorted(current.get(principal, [])) != sorted(access)
    ] + [(principal, []) for principal in current if principal not in wanted]

    for principal, access in changes:
        try:
            # An empty access_type removes the principal from the ACL
            project.set_permissions(
                principal_id=int(principal), access_type=access, overwrite=True, synapse_client=syn
            )
        except SynapseHTTPError as e:
            if _status(e) in (400, 404):
                raise SetupError(
                    f"Could not share the project with {principal}: {e}. Is it a Synapse team "
                    "or user ID?"
                ) from e
            raise
    return [f"{p}: {','.join(a) if a else 'removed'}" for p, a in changes]


def _create_folder(syn, name: str, parent_id: str) -> str:
    folder = Folder(name=name, parent_id=parent_id, create_or_update=False)
    return folder.store(synapse_client=syn).id


def apply_project_template(syn, project_id: str, project: dict) -> None:
    """
    Placeholder for the rest of a provider project's setup, along the lines of HTAN2's
    https://github.com/ncihtan/htan2_project_setup:

    - teams, and folder permissions (project sharing is the template's acl, ensure_acl())
    - a versioned folder structure (e.g. ingest / staging / release) from a data model
    - JSON schema bindings, file views and curation tasks
    """
    pass


def _status(error: SynapseHTTPError):
    return getattr(getattr(error, "response", None), "status_code", None)


def _report(template: str, results: list, failures: list) -> None:
    """A table of every project, in the GitHub Actions job summary when run there."""
    lines = [
        f"### Synapse projects ({template})",
        "",
        "| Project | ID | Bucket | Upload location | STS folder |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {r['name']} | {r['project_id']} | `{r['bucket']}` | {r['storage_location_id']} "
        f"| {r['sts_folder_id']} (location {r['sts_location_id']}) |"
        for r in results
    ]
    lines += [f"| {p['name']} | ✗ {e} | | | |" for p, e in failures]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
