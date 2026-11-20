"""
Set up the Synapse projects of a Synapse template (config/synapse/{env}.yaml) on the buckets the
CDK app deployed. CI runs this after every deploy (aws-deploy.yaml), with the deploy's outputs:
each project's bucket is a BUCKETS bucket_name, and the stack whose BucketName output it is
gives the rest (the service account), as a Terraform resource would take another's output.

Never create a provider's project by hand: until its upload destination is set, a new project
stores uploads in Synapse's default S3 bucket, where RAPID data must not go. For each project,
idempotently, as the sync service account:

1. The project, found by name or created (the service account becomes its administrator).
   Renaming it in the template makes a new project: rename it in Synapse first.
2. Its upload destination: a non-STS external S3 location on the bucket, where indexed files
   live, set as the project's ONLY upload location (replacing any other, including Synapse's
   default storage). Synapse checks the bucket's owner.txt lists the service account.
3. apply_project_template(): placeholder for the rest of the project setup.

It must run as the service account in the bucket stack's SynapseServiceAccountId output: only a
storage location's creator may create file handles in it, which is what the sync does.

    cdk deploy --context env=dev --all --outputs-file cdk-outputs.json
    SYNAPSE_AUTH_TOKEN=<service account PAT> python tools/setup_synapse_projects.py \\
        --template config/synapse/dev.yaml --cdk-outputs cdk-outputs.json [--project NAME]
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import synapseclient  # noqa: E402
from synapseclient.core.exceptions import SynapseHTTPError  # noqa: E402
from synapseclient.models import (  # noqa: E402
    Project,
    StorageLocation,
    StorageLocationType,
    UserProfile,
)
from synapseclient.operations import find_entity_id  # noqa: E402

from src.synapse_template import load_template  # noqa: E402


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

    syn = synapseclient.Synapse(skip_checks=True, silent=True)
    syn.login(authToken=token, silent=True)
    me = str(UserProfile().get(synapse_client=syn).id)  # no id or username: the current user

    results, failures = [], []
    for project in projects:
        try:
            results.append(setup_project(syn, me, project, bucket_outputs(outputs, project)))
        except (SetupError, SynapseHTTPError) as e:
            failures.append((project, e))
            print(f"✗ {project['name']}: {e}", file=sys.stderr)

    _report(args.template, results, failures)
    return 1 if failures else 0


def bucket_outputs(outputs: dict, project: dict) -> dict:
    """The outputs of the stack that deployed the project's bucket (its BucketName output)."""
    stacks = [name for name, out in outputs.items() if out.get("BucketName") == project["bucket"]]
    if len(stacks) != 1:
        raise SetupError(
            f"Expected one deployed stack for bucket '{project['bucket']}' in the CDK outputs, "
            f"found {stacks or 'none'}: is it a BUCKETS bucket_name, and was its stack deployed?"
        )
    return outputs[stacks[0]]


def setup_project(syn, me: str, project: dict, bucket: dict) -> dict:
    name, bucket_name = project["name"], bucket["BucketName"]
    if me != str(bucket["SynapseServiceAccountId"]):
        raise SetupError(
            f"Logged in as Synapse user {me}, not the service account "
            f"{bucket['SynapseServiceAccountId']} in s3://{bucket_name}'s owner.txt: the sync "
            "could not use the storage location"
        )
    try:
        project_id = ensure_project(syn, name)
        # Straight after creation: nothing can be uploaded to the project before this
        location_id = ensure_only_location(syn, Project(id=project_id), bucket_name)
        apply_project_template(syn, project_id, project)
    except SynapseHTTPError as e:
        if _status(e) in (400, 403):
            raise SetupError(
                f"Could not connect s3://{bucket_name} to '{name}': {e}. Check the bucket's "
                "owner.txt lists the service account, and that the service account administers "
                "the project"
            ) from e
        raise
    print(f"✓ {name}: {project_id} on s3://{bucket_name}, upload location {location_id}")
    return {
        "name": name,
        "project_id": project_id,
        "bucket": bucket_name,
        "storage_location_id": location_id,
    }


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


def ensure_only_location(syn, container, bucket: str) -> int:
    """
    Make the service account's external S3 location on the bucket the container's only upload
    destination; returns its ID. StorageLocation.store() returns this user's existing identical
    location rather than a duplicate.
    """
    location_id = (
        StorageLocation(storage_type=StorageLocationType.EXTERNAL_S3, bucket=bucket)
        .store(synapse_client=syn)
        .storage_location_id
    )
    setting = container.get_project_setting(setting_type="upload", synapse_client=syn)
    if list((setting.locations if setting else None) or []) != [location_id]:
        # A destructive update: the given list replaces the container's locations
        container.set_storage_location(storage_location_id=location_id, synapse_client=syn)
    return location_id


def apply_project_template(syn, project_id: str, project: dict) -> None:
    """
    Placeholder for the rest of a provider project's setup, along the lines of HTAN2's
    https://github.com/ncihtan/htan2_project_setup:

    - teams, and project / folder permissions (a provider team, a RAPID admin team)
    - a versioned folder structure (e.g. ingest / staging / release) from a data model
    - JSON schema bindings, file views and curation tasks

    Until then the project is visible only to the service account; share it by hand.
    """


def _status(error: SynapseHTTPError):
    return getattr(getattr(error, "response", None), "status_code", None)


def _report(template: str, results: list, failures: list) -> None:
    """A table of every project, in the GitHub Actions job summary when run there."""
    lines = [
        f"### Synapse projects ({template})",
        "",
        "| Project | ID | Bucket | Upload location |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {r['name']} | {r['project_id']} | `{r['bucket']}` | {r['storage_location_id']} |"
        for r in results
    ]
    lines += [f"| {p['name']} | ✗ {e} | | |" for p, e in failures]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
