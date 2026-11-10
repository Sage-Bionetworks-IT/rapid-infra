"""
First step of a sync execution: turn a sidecar's S3 "Object Created" event into the folder to
sync and its Synapse project.

Input (the EventBridge event's detail):  {"bucket": {"name": ...}, "object": {"key": ...}, ...}
Output: {"bucket", "prefix", "sidecar_key", "project_name"}

The project comes from config (BUCKET_PROJECTS: bucket → project name), never from the
sidecar: anyone who can write to the bucket could otherwise point the service account at any
project it can edit.
"""

import json
import logging
import os
from urllib.parse import unquote_plus

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")


class InvalidSidecarError(Exception):
    """The sidecar's key or content is not acceptable; retrying will not help."""


def handler(event, _context):
    bucket = event["bucket"]["name"]
    key = event["object"]["key"]
    sidecar_name = os.environ["SIDECAR_FILENAME"]
    projects = json.loads(os.environ["BUCKET_PROJECTS"])

    if bucket not in projects:
        raise InvalidSidecarError(f"Bucket '{bucket}' has no Synapse project configured")

    folder, _, name = key.rpartition("/")
    if name != sidecar_name or not folder:
        # The rule only matches */{sidecar_name}; a root-level sidecar would sync the whole
        # bucket, and s3-synapse-sync never syncs root-level files anyway
        raise InvalidSidecarError(f"'{key}' is not a {sidecar_name} inside a folder")

    key, body = _read(bucket, key)
    try:
        sidecar = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise InvalidSidecarError(f"s3://{bucket}/{key} is not valid JSON: {e}") from e
    if not isinstance(sidecar, dict):
        raise InvalidSidecarError(f"s3://{bucket}/{key} must hold a JSON object")

    result = {
        "bucket": bucket,
        "prefix": key.rpartition("/")[0] + "/",
        "sidecar_key": key,
        "project_name": projects[bucket],
    }
    logger.info(json.dumps({"event": "sync_prepared", **result}))
    return result


def _read(bucket: str, key: str):
    """Read the sidecar, falling back to the URL-decoded key if the event's key is encoded."""
    try:
        return key, s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except s3.exceptions.NoSuchKey:
        decoded = unquote_plus(key)
        if decoded == key:
            raise
        return decoded, s3.get_object(Bucket=bucket, Key=decoded)["Body"].read()
