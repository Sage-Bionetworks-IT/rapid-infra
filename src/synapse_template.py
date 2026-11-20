"""
The Synapse template, config/synapse/{env}.yaml: the Synapse resources of an environment, as the
AWS config (config/aws/{env}.yaml) declares its AWS resources. Each project names the bucket it
is connected to by its BUCKETS bucket_name; tools/setup_synapse_projects.py finds that bucket's
stack in the CDK outputs, the way a Terraform resource takes another's output.

    projects:
      - name: RAPID dev provider 1   # the Synapse project (names are global in Synapse)
        bucket: rapid-dev-provider-a # a BUCKETS[].bucket_name in config/aws/{env}.yaml

No CDK imports: the setup script loads this without the CDK app.
"""

import re
from pathlib import Path
from typing import Any, Dict

import yaml

from src.utils import _BUCKET_NAME

# Synapse entity names: letters, numbers, spaces, underscores, hyphens, periods, plus signs,
# apostrophes and parentheses
_SYNAPSE_NAME = re.compile(r"^[A-Za-z0-9 _.+'()-]{1,256}$")


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
        name, bucket = project.get("name"), project.get("bucket")
        if not isinstance(name, str) or not name.strip() or not _SYNAPSE_NAME.match(name):
            raise ValueError(f"{where}.name '{name}' must match {_SYNAPSE_NAME.pattern}")
        if not isinstance(bucket, str) or not _BUCKET_NAME.match(bucket):
            raise ValueError(f"{where}.bucket '{bucket}' must be a BUCKETS bucket_name")
        for field, value in (("name", name), ("bucket", bucket)):
            if value in seen[field]:
                # One project per bucket: each provider's data indexes into its own project
                raise ValueError(f"{where}.{field} '{value}' is used by another project")
            seen[field].add(value)
