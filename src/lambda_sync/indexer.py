"""
Index S3 objects into Synapse as external S3 file handles, without moving the data.

Ported from s3-synapse-sync (https://github.com/Sage-Bionetworks/s3-synapse-sync,
s3_synapse_sync/lambda_function.py at d309a37), Copyright 2020 Institute for Systems Biology,
licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0).

Changes from upstream:
- One entry point, index_object(), that takes a bucket/key instead of an S3 notification event,
  and reports whether it created, updated or left the file unchanged
- The project and its storage location are resolved once per sync, and the location must be an
  external S3 location on this bucket (upstream took the project's first upload location,
  whatever it pointed at). Both are looked up, never created: tools/setup_synapse_projects.py
  sets them up after each deploy (resolve_storage). Contributors upload with read-write STS
  credentials from a separate, empty "shim" folder whose STS location covers the whole bucket
  (also set up by the script); files are indexed at the project root, mirroring their S3 keys,
  on the project's non-STS location
- The ETag is used as the MD5 only when it is one (single-part, not SSE-KMS)
- Empty path segments (a//b) are skipped instead of creating unnamed folders
- A folder created concurrently by another worker is looked up, not updated
- No Minerva/Batch processing and no ObjectRemoved handling
- synapseclient's object models (synapseclient.models) instead of the Synapse client methods
  deprecated in 4.x. Model methods take keyword arguments only (their tracing wrapper rejects
  positional ones), and every call passes synapse_client explicitly

Creating the S3 file handle is still a REST call: no model creates external file handles.
"""

import base64
import hashlib
import json
import mimetypes
import posixpath

from synapseclient import Synapse
from synapseclient.core.exceptions import SynapseHTTPError
from synapseclient.models import File, Folder, Project, StorageLocation, StorageLocationType
from synapseclient.operations import find_entity_id

MD5_BLOCK_SIZE = 50 * 1024**2


class IndexingError(Exception):
    """Synapse is not set up to index this bucket; retrying will not help."""


def _status(error: SynapseHTTPError):
    return getattr(getattr(error, "response", None), "status_code", None)


def resolve_storage_location(syn: Synapse, project_id: str, bucket: str) -> int:
    """
    The project's non-STS upload storage location on this bucket that the service account
    created (the STS one, on the shim folder, is for contributors' credentials).
    Only a storage location's creator may create file handles in it, and only the creator can
    read it here, so a location someone else made on the same bucket does not count.
    """
    setting = Project(id=project_id).get_project_setting(setting_type="upload", synapse_client=syn)
    for location_id in (setting.locations if setting else None) or []:
        try:
            location = StorageLocation(storage_location_id=location_id).get(synapse_client=syn)
        except SynapseHTTPError:
            continue  # created by someone else, so not usable by the service account
        if (
            location.storage_type is StorageLocationType.EXTERNAL_S3
            and location.bucket == bucket
            and not location.sts_enabled
        ):
            return location.storage_location_id
    raise IndexingError(
        f"Project {project_id} has no upload storage location on s3://{bucket} created by this "
        "service account. tools/setup_synapse_projects.py sets it up"
    )


def resolve_storage(syn: Synapse, project_name: str, bucket: str) -> tuple[str, int]:
    """
    The bucket's Synapse project, by name, and its storage location on the bucket:
    (project_id, storage_location_id). Never creates either: a project made here, before its
    upload destination is set, would store uploads in Synapse's default storage. A missing one
    raises IndexingError, which is not retried.
    """
    project_id = find_entity_id(project_name, None, synapse_client=syn)
    if project_id is None:
        raise IndexingError(
            f"The service account sees no Synapse project named '{project_name}'. "
            "tools/setup_synapse_projects.py creates it (CI runs it after every deploy)"
        )
    return project_id, resolve_storage_location(syn, project_id, bucket)


def index_object(
    syn: Synapse,
    s3,
    *,
    bucket: str,
    key: str,
    project_id: str,
    storage_location_id: int,
) -> dict:
    """
    Make s3://bucket/key the file {dirname(key)}/{basename(key)} under the project, creating
    folders as needed. A file with the same name and MD5 is left alone; a different MD5 adds a
    new version.

    Returns {"status": "created" | "updated" | "unchanged", "entity_id"}.
    """
    filename = posixpath.basename(key)
    if not filename:
        raise ValueError(f"'{key}' is a folder, not a file")

    head = s3.head_object(Bucket=bucket, Key=key)
    md5 = object_md5(s3, bucket, key, head)
    parent_id = ensure_folders(syn, project_id, posixpath.dirname(key))

    existing = None
    existing_id = find_entity_id(filename, parent_id, synapse_client=syn)
    if existing_id is not None:
        existing = File(id=existing_id, download_file=False).get(synapse_client=syn)
        if existing.file_handle and existing.file_handle.content_md5 == md5:
            return {"status": "unchanged", "entity_id": existing_id}

    file_handle = syn.restPOST(
        "/externalFileHandle/s3",
        json.dumps(
            {
                "concreteType": "org.sagebionetworks.repo.model.file.S3FileHandle",
                "fileName": filename,
                "contentSize": head["ContentLength"],
                "contentType": mimetypes.guess_type(filename, strict=False)[0]
                or head.get("ContentType"),
                "contentMd5": md5,
                "bucketName": bucket,
                "key": key,
                "storageLocationId": storage_location_id,
            }
        ),
        endpoint=syn.fileHandleEndpoint,
    )

    if existing is not None:
        # Point the existing entity at the new handle: a new version of the same file
        existing.data_file_handle_id = file_handle["id"]
        entity = existing.store(synapse_client=syn)
        return {"status": "updated", "entity_id": entity.id}

    # With a data file handle the model stores metadata only and uploads nothing. A conflict
    # (409, e.g. a nested folder's sidecar indexing the same file at once) fails the item and
    # its retry finds the file.
    entity = File(
        parent_id=parent_id,
        name=filename,
        data_file_handle_id=file_handle["id"],
        synapse_store=False,
        create_or_update=False,
    ).store(synapse_client=syn)
    return {"status": "created", "entity_id": entity.id}


def ensure_folders(syn: Synapse, project_id: str, dirname: str) -> str:
    """The Synapse ID of the folder at dirname under the project, creating missing folders."""
    parent_id = project_id
    for name in (n for n in dirname.split("/") if n):
        folder_id = find_entity_id(name, parent_id, synapse_client=syn)
        if folder_id is None:
            folder_id = _create_folder(syn, name, parent_id)
        parent_id = folder_id
    return parent_id


def _create_folder(syn: Synapse, name: str, parent_id: str) -> str:
    """
    Create the folder, or return the one a concurrent file worker just created. With
    create_or_update the store would update that folder instead, which fails with 412 when
    another worker changes it at the same moment (seen at 5 concurrent workers).
    """
    try:
        folder = Folder(name=name, parent_id=parent_id, create_or_update=False).store(
            synapse_client=syn
        )
        return folder.id
    except SynapseHTTPError as e:
        if _status(e) != 409:
            raise
    folder_id = find_entity_id(name, parent_id, synapse_client=syn)
    if folder_id is None:  # 409 for another reason; surface it, the item's retry starts over
        raise IndexingError(f"Folder '{name}' in {parent_id} conflicts but cannot be found")
    return folder_id


def object_md5(s3, bucket: str, key: str, head: dict) -> str:
    """
    The object's MD5 hex digest. The ETag is the MD5 only for a single-part upload that is not
    SSE-KMS encrypted; otherwise use a content-md5 the uploader stored in the object metadata,
    or read the object and compute it.
    """
    etag = head["ETag"].strip('"')
    if "-" not in etag and not head.get("ServerSideEncryption", "").startswith("aws:kms"):
        return etag
    if "content-md5" in head.get("Metadata", {}):
        return base64.b64decode(head["Metadata"]["content-md5"]).hex()
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    digest = hashlib.md5()
    for block in iter(lambda: body.read(MD5_BLOCK_SIZE), b""):
        digest.update(block)
    return digest.hexdigest()
