"""
Contribute the mock FHIR dataset to a RAPID provider bucket with Synapse STS credentials, the
way the sync pipeline expects. Each run goes under <synapse username>/<timestamp>/, so runs
never clash. Needs edit and delete access to STS_FOLDER_ID (the dev project's sts folder; the
setup job prints every project's).

    pip install synapseclient boto3
    export SYNAPSE_AUTH_TOKEN=<personal access token>
    python tools/contribute.py
"""

import datetime
import tempfile
from pathlib import Path

import boto3
import synapseclient
import synapseutils
from synapseclient.models import Folder, UserProfile

STS_FOLDER_ID = "syn78799585"  # the project's `sts` folder
FOLDERS = ["syn78593045", "syn78593053", "syn78593202"]  # docs/, fhir/, model/
FILES = ["syn78592994", "syn78592993"]  # manifest.json, manifest.sig
SIDECAR_ID = "syn78813627"  # sidecar.json, uploaded last
ACL = "bucket-owner-full-control"  # Synapse's STS credentials require it on every write


def main():
    # 1. Log in and find out who is running this
    syn = synapseclient.Synapse()
    syn.login()
    username = UserProfile().get(synapse_client=syn).username
    run = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    s3_folder = f"{username}/{run}"  # also the files' path in the Synapse project

    # 2. Get read-write STS credentials from the STS folder (valid 12 hours)
    token = Folder(id=STS_FOLDER_ID).get_sts_storage_token(
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

    with tempfile.TemporaryDirectory() as tmp:
        local_dir = Path(tmp)

        # 3. Download the dataset: each folder into a subfolder of the same name, the
        #    files at the top, matching the paths in manifest.json
        #    (syncFromSynapse can leave a manifest.csv, so only upload what it returns)
        paths = []
        for folder_id in FOLDERS:
            name = syn.get(folder_id, downloadFile=False).name
            files = synapseutils.syncFromSynapse(
                syn, folder_id, path=str(local_dir / name), manifest="suppress"
            )
            paths += [Path(f.path) for f in files]
        for file_id in FILES:
            paths.append(Path(syn.get(file_id, downloadLocation=tmp).path))
        sidecar = Path(syn.get(SIDECAR_ID, downloadLocation=tmp).path)

        # 4. Upload every file but the sidecar, keeping its relative path
        for path in sorted(paths):
            key = f"{prefix}/{path.relative_to(local_dir).as_posix()}"
            s3.upload_file(str(path), bucket, key, ExtraArgs={"ACL": ACL})
            print(f"uploaded s3://{bucket}/{key}")

        # 5. Upload the sidecar last: it tells the pipeline the folder is complete
        key = f"{prefix}/sidecar.json"
        s3.upload_file(
            str(sidecar),
            bucket,
            key,
            ExtraArgs={"ACL": ACL, "ContentType": "application/json"},
        )
        print(f"uploaded s3://{bucket}/{key}; indexing into {s3_folder}/ has started")


if __name__ == "__main__":
    main()
