"""
Client-side encryption of a staged file, for a bucket with `client_side_encryption`. Two handlers
in one bundle, both items of the sync execution's Distributed Map:

handler          before SyncFile, for every file of a CSE bucket:
    Input:  {"bucket", "key", "project_id", "storage_location_id", "encrypted_storage_location_id",
             "cse": {"bucket", "patterns", "key_arn", "provider"}}
    Output: the item SyncFile indexes. A file no pattern matches comes back unchanged (it is
            indexed from the staging bucket, as without CSE). A matching one is encrypted to
            s3://{cse.bucket}/{key}.encrypted and comes back as
            {"bucket": cse.bucket, "key": key + ".encrypted", "project_id",
             "storage_location_id": encrypted_storage_location_id, "content_md5"?,
             "staged": {"bucket", "key", "etag"}}

delete_staged    after SyncFile indexed an encrypted file:
    Input:  SyncFile's output, with "staged"
    Output: the input. Deletes the plaintext from the staging bucket, only if it is still the
            object that was encrypted (If-Match on its ETag): a file re-uploaded meanwhile is
            left for its own sidecar.

The ciphertext is an AWS Encryption SDK message (committing, signed algorithm suite), so
`aws-encryption-cli --decrypt` or tools/decrypt.py turns it back into the file. The data key is
generated under the provider's CSE KMS key, with the encryption context
{"rapid:provider", "rapid:bucket", "rapid:key"}; the key policy only allows that provider's
context.
"""

import hashlib
import json
import logging
import os
import posixpath
from pathlib import PurePosixPath

import aws_encryption_sdk
import boto3
from aws_encryption_sdk.identifiers import CommitmentPolicy
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

ENCRYPTED_SUFFIX = ".encrypted"
# The encrypted object's metadata (x-amz-meta-*): the staged object it was made from
SOURCE_ETAG = "source-etag"
SOURCE_SIZE = "source-size"
# Lambda's 15 minutes; larger files need a task without a time limit (README)
DEFAULT_MAX_BYTES = 10 * 1024**3

_client = aws_encryption_sdk.EncryptionSDKClient(
    commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT
)
# One provider per key, per warm container: it caches its KMS clients
_providers = {}


class FileTooLargeError(Exception):
    """The file is over MAX_BYTES; retrying will not help."""


def matches(key: str, patterns: list[str]) -> bool:
    """Whether the S3 key matches any glob ("**" spans folders, "*" does not)."""
    path = PurePosixPath(key)
    return any(path.full_match(p) for p in patterns)


def encryption_context(cse: dict, bucket: str, key: str) -> dict:
    return {"rapid:provider": cse["provider"], "rapid:bucket": bucket, "rapid:key": key}


def handler(event, _context):
    bucket, key, cse = event["bucket"], event["key"], event["cse"]
    filename = posixpath.basename(key)
    if (
        not filename
        or filename == os.environ["SIDECAR_FILENAME"]
        or not matches(key, cse["patterns"])
    ):
        return _log({**_unchanged(event), "encrypt": "not matched"}, key)

    head = s3.head_object(Bucket=bucket, Key=key)
    etag, size = head["ETag"], head["ContentLength"]
    max_bytes = int(os.environ.get("MAX_BYTES", DEFAULT_MAX_BYTES))
    if size > max_bytes:
        raise FileTooLargeError(
            f"s3://{bucket}/{key} is {size} bytes, over the {max_bytes} client-side encryption limit"
        )

    target_key = key + ENCRYPTED_SUFFIX
    item = {
        "bucket": cse["bucket"],
        "key": target_key,
        "project_id": event["project_id"],
        "storage_location_id": event["encrypted_storage_location_id"],
        "staged": {"bucket": bucket, "key": key, "etag": etag},
    }
    if _already_encrypted(cse["bucket"], target_key, etag):
        # A rerun (the plaintext was not deleted last time): the ciphertext would differ, and
        # Synapse would get a new version of an unchanged file
        return _log({**item, "encrypt": "unchanged"}, key)

    item["content_md5"] = encrypt_object(s3, cse, bucket, key, etag, size)
    return _log({**item, "encrypt": "encrypted"}, key)


def delete_staged(event, _context):
    staged = event["staged"]
    try:
        s3.delete_object(Bucket=staged["bucket"], Key=staged["key"], IfMatch=staged["etag"])
        status = "deleted"
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("PreconditionFailed", "NoSuchKey", "404"):
            raise
        # Replaced since it was encrypted (its own sidecar handles it), or already gone
        status = "kept: " + e.response["Error"]["Code"]
    logger.info(json.dumps({"event": "staged_delete", "key": staged["key"], "status": status}))
    return event


def encrypt_object(s3_client, cse: dict, bucket: str, key: str, etag: str, size: int) -> str:
    """
    Stream s3://bucket/key through the Encryption SDK into s3://{cse.bucket}/{key}.encrypted
    without holding the file in memory or /tmp. Returns the ciphertext's MD5 hex digest, which
    Synapse's file handle needs (a multipart upload's ETag is not one).
    """
    # If-Match: the body is the object whose ETag was checked and recorded
    body = s3_client.get_object(Bucket=bucket, Key=key, IfMatch=etag)["Body"]
    with _client.stream(
        mode="e",
        source=body,
        key_provider=_key_provider(cse["key_arn"]),
        encryption_context=encryption_context(cse, bucket, key),
    ) as encryptor:
        reader = _Md5Reader(encryptor)
        s3_client.upload_fileobj(
            reader,
            cse["bucket"],
            key + ENCRYPTED_SUFFIX,
            ExtraArgs={
                "ContentType": "application/octet-stream",
                "Metadata": {SOURCE_ETAG: etag, SOURCE_SIZE: str(size)},
            },
            # 10,000 parts of 16 MiB cover far more than MAX_BYTES
            Config=TransferConfig(multipart_chunksize=16 * 1024**2, max_concurrency=8),
        )
    return reader.hexdigest()


def _already_encrypted(bucket: str, key: str, etag: str) -> bool:
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    return head.get("Metadata", {}).get(SOURCE_ETAG) == etag


def _key_provider(key_arn: str):
    if key_arn not in _providers:
        _providers[key_arn] = aws_encryption_sdk.StrictAwsKmsMasterKeyProvider(key_ids=[key_arn])
    return _providers[key_arn]


def _unchanged(event: dict) -> dict:
    return {k: event[k] for k in ("bucket", "key", "project_id", "storage_location_id")}


def _log(item: dict, source_key: str) -> dict:
    logger.info(json.dumps({"event": "file_encrypt", "source_key": source_key, **item}))
    item.pop("encrypt")
    return item


class _Md5Reader:
    """A read()-only stream that hashes what passes through it."""

    def __init__(self, stream):
        self._stream = stream
        self._md5 = hashlib.md5()

    def read(self, size=-1):
        data = self._stream.read(size)
        self._md5.update(data)
        return data

    def hexdigest(self) -> str:
        return self._md5.hexdigest()
