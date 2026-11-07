"""
Contribute a folder of files to a RAPID provider bucket, the way the sync pipeline expects:

    python contribute.py --sts-folder syn123 ./my-batch data/my-batch

1. Gets read-write STS credentials for the bucket from the provider project's STS folder
   (Synapse issues them to anyone with edit access to it; they last 12 hours and are refreshed
   here when under REFRESH_SECONDS remain)
2. Uploads every file under ./my-batch to s3://<bucket>/data/my-batch/<relative path>, with
   the bucket-owner-full-control ACL that Synapse's STS credentials require on every write
3. Only when every upload succeeded, writes data/my-batch/sidecar.json, which triggers the
   pipeline to index the folder into the Synapse project at data/my-batch/

Needs `pip install synapseclient boto3`. Logs in to Synapse with SYNAPSE_AUTH_TOKEN if set,
else synapseclient's default (~/.synapseConfig).
"""

import argparse
import datetime
import json
import posixpath
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ACL = "bucket-owner-full-control"
SIDECAR = "sidecar.json"
REFRESH_SECONDS = 3600


def plan(local_dir: Path, s3_folder: str, sidecar_name: str = SIDECAR) -> list:
    """(path, key relative to the bucket's base) for every file under local_dir."""
    folder = s3_folder.strip("/")
    if not folder or any(part in ("", ".", "..") for part in folder.split("/")):
        raise ValueError(f"S3 folder must be a relative path like data/my-batch, got '{s3_folder}'")
    if not local_dir.is_dir():
        raise ValueError(f"{local_dir} is not a directory")
    files = sorted(p for p in local_dir.rglob("*") if p.is_file())
    if not files:
        raise ValueError(f"{local_dir} has no files")
    planned = []
    for path in files:
        relative = path.relative_to(local_dir).as_posix()
        if posixpath.basename(relative) == sidecar_name:
            raise ValueError(
                f"{path}: {sidecar_name} is written by this tool after the upload; remove it"
            )
        planned.append((path, f"{folder}/{relative}"))
    return planned


class StsCredentials:
    """Read-write STS credentials, refetched from Synapse when close to expiry. Thread-safe."""

    def __init__(self, fetch, make_client, now=None):
        self._fetch, self._make_client = fetch, make_client
        self._now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self._lock = threading.Lock()
        self._token = None

    def _expiring(self) -> bool:
        if self._token is None:
            return True
        expires = datetime.datetime.fromisoformat(self._token["expiration"].replace("Z", "+00:00"))
        return (expires - self._now()).total_seconds() < REFRESH_SECONDS

    def token(self) -> dict:
        with self._lock:
            if self._expiring():
                self._token = self._fetch()
            return self._token

    def client(self):
        t = self.token()
        return self._make_client(
            aws_access_key_id=t["accessKeyId"],
            aws_secret_access_key=t["secretAccessKey"],
            aws_session_token=t["sessionToken"],
        )


def contribute(
    credentials: StsCredentials,
    planned: list,
    s3_folder: str,
    *,
    workers: int = 16,
    sidecar_name: str = SIDECAR,
    log=print,
) -> dict:
    """Upload the planned files, then the sidecar. Raises, writing no sidecar, if any fails."""
    token = credentials.token()
    bucket = token["bucket"]
    base = (token.get("baseKey") or "").strip("/")

    def full_key(key):
        return f"{base}/{key}" if base else key

    def upload(path, key):
        credentials.client().upload_file(str(path), bucket, full_key(key), ExtraArgs={"ACL": ACL})
        return path.stat().st_size

    total, failures = 0, []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(upload, path, key): key for path, key in planned}
        for done, future in enumerate(as_completed(futures), 1):
            try:
                total += future.result()
            except Exception as e:  # report every failure, not just the first
                failures.append((futures[future], e))
            if done % 500 == 0:
                log(f"  {done}/{len(planned)} uploaded")
    if failures:
        for key, error in failures[:20]:
            log(f"  FAILED {key}: {error}")
        raise RuntimeError(f"{len(failures)} of {len(planned)} uploads failed; no sidecar written")

    sidecar_key = full_key(f"{s3_folder.strip('/')}/{sidecar_name}")
    sidecar = {
        "files": len(planned),
        "bytes": total,
        "uploaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tool": "rapid-infra contribute.py",
    }
    credentials.client().put_object(
        Bucket=bucket,
        Key=sidecar_key,
        Body=json.dumps(sidecar, indent=2).encode(),
        ContentType="application/json",
        ACL=ACL,
    )
    return {"bucket": bucket, "files": len(planned), "bytes": total, "sidecar": sidecar_key}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--sts-folder", required=True, help="Synapse ID of the project's STS folder"
    )
    parser.add_argument("local_dir", type=Path, help="Folder of files to upload")
    parser.add_argument("s3_folder", help="Destination folder in the bucket, e.g. data/my-batch")
    parser.add_argument("--workers", type=int, default=16, help="Files uploaded at once")
    parser.add_argument("--region", default="us-east-1", help="The bucket's AWS region")
    parser.add_argument("--dry-run", action="store_true", help="List what would be uploaded")
    args = parser.parse_args(argv)

    try:
        planned = plan(args.local_dir, args.s3_folder)
    except ValueError as e:
        sys.exit(str(e))
    if args.dry_run:
        for path, key in planned:
            print(f"{path} → {key}")
        print(f"{len(planned)} files, then {args.s3_folder.strip('/')}/{SIDECAR}")
        return

    import os

    import boto3
    import synapseclient
    from synapseclient.models import Folder

    syn = synapseclient.Synapse(skip_checks=True, silent=True)
    token = os.environ.get("SYNAPSE_AUTH_TOKEN")
    syn.login(authToken=token, silent=True) if token else syn.login(silent=True)
    folder = Folder(id=args.sts_folder)
    credentials = StsCredentials(
        fetch=lambda: folder.get_sts_storage_token(
            permission="read_write", output_format="json", synapse_client=syn
        ),
        make_client=lambda **creds: boto3.client("s3", region_name=args.region, **creds),
    )
    result = contribute(credentials, planned, args.s3_folder, workers=args.workers)
    print(
        f"Uploaded {result['files']} files ({result['bytes']} bytes) to s3://{result['bucket']}/"
        f"{args.s3_folder.strip('/')}/ and wrote {result['sidecar']}; indexing has started."
    )


if __name__ == "__main__":
    main()
