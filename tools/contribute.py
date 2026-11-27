"""
Contribute a directory of files to a RAPID provider bucket with Synapse STS credentials:

    pip install synapseclient boto3
    export SYNAPSE_AUTH_TOKEN=<personal access token>
    python tools/contribute.py --sts-folder syn123 ./my-batch data/my-batch

Uploads every file under ./my-batch to s3://<bucket>/data/my-batch/<relative path>, then
./my-batch/sidecar.json last: it tells the pipeline the folder is complete, and the folder is
indexed into the Synapse project at data/my-batch/. Needs edit and delete access to the
project's STS folder (syn123), which Synapse requires for read-write STS credentials.
"""

import argparse
import json
from pathlib import Path

import boto3
import synapseclient
from synapseclient.models import Folder

SIDECAR = "sidecar.json"
ACL = "bucket-owner-full-control"  # Synapse's STS credentials require it on every write


def contribute(syn, sts_folder_id: str, local_dir, s3_folder: str) -> str:
    """Upload local_dir to s3_folder, its sidecar.json last. Returns the folder's S3 URL."""
    local_dir = Path(local_dir)
    sidecar = local_dir / SIDECAR
    s3_folder = s3_folder.strip("/")

    # 1. Check what the pipeline would reject only after the whole upload
    if not s3_folder:
        raise ValueError("The S3 folder must not be the bucket root")
    if not sidecar.is_file():
        raise ValueError(f"{local_dir} has no {SIDECAR}; the pipeline only indexes with one")
    if not isinstance(json.loads(sidecar.read_text()), dict):
        raise ValueError(f"{sidecar} must hold a JSON object")

    # 2. Get read-write STS credentials from the STS folder (valid 12 hours)
    token = Folder(id=sts_folder_id).get_sts_storage_token(
        permission="read_write", output_format="json", synapse_client=syn
    )
    s3 = boto3.client(
        "s3",
        aws_access_key_id=token["accessKeyId"],
        aws_secret_access_key=token["secretAccessKey"],
        aws_session_token=token["sessionToken"],
    )
    bucket = token["bucket"]
    prefix = "/".join(p for p in [token.get("baseKey", "").strip("/"), s3_folder] if p)

    # 3. Upload every file but the sidecar, keeping its relative path
    files = sorted(p for p in local_dir.rglob("*") if p.is_file() and p != sidecar)
    for path in files:
        key = f"{prefix}/{path.relative_to(local_dir).as_posix()}"
        s3.upload_file(str(path), bucket, key, ExtraArgs={"ACL": ACL})
        print(f"uploaded s3://{bucket}/{key}")

    # 4. Upload the sidecar last: it tells the pipeline the folder is complete
    key = f"{prefix}/{SIDECAR}"
    s3.upload_file(
        str(sidecar), bucket, key, ExtraArgs={"ACL": ACL, "ContentType": "application/json"}
    )
    print(f"uploaded s3://{bucket}/{key}; indexing into {s3_folder}/ has started")
    return f"s3://{bucket}/{prefix}/"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--sts-folder", required=True, help="the project's STS folder synID")
    parser.add_argument("local_dir", help=f"the directory to upload, with a {SIDECAR}")
    parser.add_argument("s3_folder", help="where in the bucket, e.g. data/my-batch")
    args = parser.parse_args()

    syn = synapseclient.Synapse()
    syn.login()
    contribute(syn, args.sts_folder, args.local_dir, args.s3_folder)


if __name__ == "__main__":
    main()
