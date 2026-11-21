"""
The Synapse template, config/synapse/{env}.yaml: the Synapse resources of an environment, as the
AWS config (config/aws/{env}.yaml) declares its AWS resources. Values may reference the
CloudFormation outputs of a bucket's stack as $ref.<Output>.<bucket_name>, with the bucket's
BUCKETS bucket_name; tools/setup_synapse_projects.py resolves them from the CDK outputs, the way
a Terraform resource takes another's output.

    projects:
      - name: RAPID dev provider a   # the Synapse project (names are global in Synapse)
        bucket: $ref.BucketName.rapid-dev-provider-a  # a BUCKETS[].bucket_name's stack
        acl:                         # optional: the project's sharing settings, exactly
          - principal_id: 3629180    # a Synapse team or user ID
            permission: edit         # a PERMISSIONS level

No CDK imports: the setup script loads this without the CDK app. The CDK app reads only the
project names (project_names), for the pipeline.
"""

import re
from pathlib import Path
from typing import Any, Dict, Iterator, NamedTuple, Optional, Tuple

import yaml

from src.utils import _BUCKET_NAME

# Synapse entity names: letters, numbers, spaces, underscores, hyphens, periods, plus signs,
# apostrophes and parentheses
_SYNAPSE_NAME = re.compile(r"^[A-Za-z0-9 _.+'()-]{1,256}$")
_SYNAPSE_PRINCIPAL = re.compile(r"^\d+$")

# $ref.<Output>.<bucket_name>: the output (a CloudFormation output's logical ID) of the stack of
# the BUCKETS entry with that bucket_name (which has no dots)
_REF = re.compile(r"^\$ref\.(?P<output>[A-Za-z0-9]+)\.(?P<bucket>[^.]+)$")

# The permission levels of Synapse's sharing settings dialog, as access types
PERMISSIONS = {
    "view": ["READ"],
    "download": ["READ", "DOWNLOAD"],
    "edit": ["READ", "DOWNLOAD", "CREATE", "UPDATE"],
    "edit_delete": ["READ", "DOWNLOAD", "CREATE", "UPDATE", "DELETE"],
    "admin": [
        "READ",
        "DOWNLOAD",
        "CREATE",
        "UPDATE",
        "DELETE",
        "CHANGE_PERMISSIONS",
        "CHANGE_SETTINGS",
        "MODERATE",
    ],
}


class Ref(NamedTuple):
    output: str
    bucket: str  # a BUCKETS bucket_name


def parse_ref(value: Any) -> Optional[Ref]:
    """The reference a value is, or None if it is not one ($ref...)."""
    if not isinstance(value, str) or not value.startswith("$ref"):
        return None
    match = _REF.match(value)
    if not match or not _BUCKET_NAME.match(match["bucket"]):
        raise ValueError(f"'{value}' must be $ref.<Output>.<BUCKETS bucket_name>")
    return Ref(match["output"], match["bucket"])


def find_refs(value: Any, where: str) -> Iterator[Tuple[str, Ref]]:
    """Every reference in a value, with where it is."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from find_refs(item, f"{where}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from find_refs(item, f"{where}[{i}]")
    else:
        try:
            ref = parse_ref(value)
        except ValueError as e:
            raise ValueError(f"{where}: {e}") from None
        if ref:
            yield where, ref


def load_template(path) -> Dict[str, Any]:
    """Load and validate a Synapse template; an empty file has no projects."""
    with open(Path(path)) as f:
        template = yaml.safe_load(f) or {}
    template.setdefault("projects", [])
    validate_template(template)
    return template


def validate_template(template: Dict[str, Any]) -> None:
    projects = template.get("projects")
    if not isinstance(projects, list):
        raise ValueError("projects must be a list")
    seen = {"name": set(), "bucket": set()}
    for i, project in enumerate(projects):
        where = f"projects[{i}]"
        if not isinstance(project, dict):
            raise ValueError(f"{where} must be a mapping")
        refs = dict(find_refs(project, where))
        name, bucket = project.get("name"), project.get("bucket")
        if not isinstance(name, str) or not name.strip() or not _SYNAPSE_NAME.match(name):
            raise ValueError(f"{where}.name '{name}' must match {_SYNAPSE_NAME.pattern}")
        bucket_ref = refs.get(f"{where}.bucket")
        if not bucket_ref or bucket_ref.output != "BucketName":
            raise ValueError(
                f"{where}.bucket '{bucket}' must be $ref.BucketName.<BUCKETS bucket_name>"
            )
        for field, value in (("name", name), ("bucket", bucket_ref.bucket)):
            if value in seen[field]:
                # One project per bucket: each provider's data indexes into its own project
                raise ValueError(f"{where}.{field} '{value}' is used by another project")
            seen[field].add(value)
        _validate_acl(where, project.get("acl", []))


def _validate_acl(where: str, acl: Any) -> None:
    if not isinstance(acl, list):
        raise ValueError(f"{where}.acl must be a list")
    principals = set()
    for j, entry in enumerate(acl):
        at = f"{where}.acl[{j}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{at} must be a mapping")
        principal, permission = entry.get("principal_id"), entry.get("permission")
        if isinstance(principal, bool) or not _SYNAPSE_PRINCIPAL.match(str(principal)):
            raise ValueError(f"{at}.principal_id '{principal}' must be a Synapse team or user ID")
        if permission not in PERMISSIONS:
            raise ValueError(
                f"{at}.permission '{permission}' must be one of {', '.join(PERMISSIONS)}"
            )
        if str(principal) in principals:
            raise ValueError(f"{at}.principal_id '{principal}' is listed twice")
        principals.add(str(principal))


def project_names(template: Dict[str, Any], buckets: list) -> Dict[str, str]:
    """
    {BUCKETS bucket_name: project name} for the CDK app, whose pipeline indexes each bucket's
    sidecars into its project: every bucket must have one.
    """
    names = {parse_ref(p["bucket"]).bucket: p["name"] for p in template["projects"]}
    missing = [b["bucket_name"] for b in buckets if b["bucket_name"] not in names]
    if missing:
        raise ValueError(
            f"BUCKETS {', '.join(missing)} have no project in the Synapse template "
            "(config/synapse/{env}.yaml): the pipeline would have nowhere to index them"
        )
    return {b["bucket_name"]: names[b["bucket_name"]] for b in buckets}
