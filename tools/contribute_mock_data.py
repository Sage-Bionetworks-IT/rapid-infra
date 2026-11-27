"""
Contribute the mock FHIR dataset to the dev provider project with contribute.py. Each run goes
under <synapse username>/<timestamp>/, so runs never clash.

    pip install synapseclient boto3
    export SYNAPSE_AUTH_TOKEN=<personal access token>
    python tools/contribute_mock_data.py
"""

import datetime
import tempfile
from pathlib import Path

import synapseclient
import synapseutils
from contribute import contribute
from synapseclient.models import UserProfile

STS_FOLDER_ID = "syn78799585"  # the dev project's `sts` folder
FOLDERS = ["syn78593045", "syn78593053", "syn78593202"]  # docs/, fhir/, model/
FILES = ["syn78592994", "syn78592993", "syn78813627"]  # manifest.json, manifest.sig, sidecar.json


def main():
    # 1. Log in and find out who is running this
    syn = synapseclient.Synapse()
    syn.login()
    username = UserProfile().get(synapse_client=syn).username
    run = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")

    with tempfile.TemporaryDirectory() as tmp:
        local_dir = Path(tmp)

        # 2. Download the dataset: each folder into a subfolder of the same name, the files at
        #    the top, matching the paths in manifest.json
        for folder_id in FOLDERS:
            name = syn.get(folder_id, downloadFile=False).name
            synapseutils.syncFromSynapse(
                syn, folder_id, path=str(local_dir / name), manifest="suppress"
            )
            # synapseclient 4.15 writes a manifest.csv even with manifest="suppress"
            (local_dir / name / "manifest.csv").unlink(missing_ok=True)
        for file_id in FILES:
            syn.get(file_id, downloadLocation=tmp)

        # 3. Upload it, the dataset's sidecar.json last
        contribute(syn, STS_FOLDER_ID, local_dir, f"{username}/{run}")


if __name__ == "__main__":
    main()
