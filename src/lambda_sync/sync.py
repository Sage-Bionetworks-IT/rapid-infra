"""
The sync execution's Synapse steps. Two handlers in one bundle:

resolve_storage  once per execution, before the Map:
    Input:  {"bucket", "project_name", "cse"?, ...}
    Output: {"project_id", "storage_location_id", "encrypted_storage_location_id"}
    The bucket's project and its storage location on the bucket (indexer.resolve_storage),
    and with client-side encryption (cse) the location on the encrypted bucket (else None),
    looked up once rather than per file. Never created here: tools/setup_synapse_projects.py
    sets them up after every deploy; missing, it raises IndexingError (not retried).

handler          one item of the Distributed Map:
    Input:  {"bucket", "key", "project_id", "storage_location_id", "content_md5"?, "staged"?}
    Output: {"key", "status": "created" | "updated" | "unchanged" | "skipped", "entity_id"?,
             "reason"?, "staged"?}
    An encrypted file (the Encrypt step's output) carries its ciphertext MD5 and the staged
    plaintext it came from; "staged" is passed through for the DeleteStaged step.
    An exception fails the item; Step Functions retries it, then counts it against the map's
    tolerated failure percentage.
"""

import json
import logging
import os
import posixpath

import boto3
import synapseclient

from indexer import index_object, resolve_encrypted_location
from indexer import resolve_storage as resolve_synapse_storage

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
secrets = boto3.client("secretsmanager")

# One login per warm container, not per file. Cleared after any error, so a retry starts clean.
_syn = None


def resolve_storage(event, _context):
    bucket, project_name = event["bucket"], event["project_name"]
    try:
        syn = _synapse()
        project_id, location_id = resolve_synapse_storage(syn, project_name, bucket)
        encrypted_bucket = (event.get("cse") or {}).get("bucket")
        encrypted_location_id = (
            resolve_encrypted_location(syn, encrypted_bucket) if encrypted_bucket else None
        )
    except Exception:
        _reset()
        raise
    result = {
        "project_id": project_id,
        "storage_location_id": location_id,
        "encrypted_storage_location_id": encrypted_location_id,
    }
    logger.info(
        json.dumps(
            {"event": "storage_location", "bucket": bucket, "project_name": project_name, **result}
        )
    )
    return result


def handler(event, _context):
    bucket, key = event["bucket"], event["key"]
    filename = posixpath.basename(key)

    if not filename:
        return _log({"key": key, "status": "skipped", "reason": "folder marker"})
    if filename == os.environ["SIDECAR_FILENAME"]:
        # The trigger (or a nested folder's); it is not data
        return _log({"key": key, "status": "skipped", "reason": "sidecar"})

    try:
        result = index_object(
            _synapse(),
            s3,
            bucket=bucket,
            key=key,
            project_id=event["project_id"],
            storage_location_id=event["storage_location_id"],
            content_md5=event.get("content_md5"),
        )
    except Exception:
        _reset()
        logger.exception(json.dumps({"event": "file_sync", "key": key, "status": "error"}))
        raise
    if event.get("staged"):
        result["staged"] = event["staged"]
    return _log({"key": key, **result})


def _synapse() -> synapseclient.Synapse:
    global _syn
    if _syn is None:
        pat = secrets.get_secret_value(SecretId=os.environ["SYNAPSE_PAT_SECRET_ARN"])[
            "SecretString"
        ]
        syn = synapseclient.Synapse(
            skip_checks=True, cache_root_dir="/tmp/.synapseCache", silent=True
        )
        syn.login(authToken=pat, silent=True)
        _syn = syn
    return _syn


def _reset():
    global _syn
    _syn = None


def _log(result: dict) -> dict:
    logger.info(json.dumps({"event": "file_sync", **result}))
    return result
