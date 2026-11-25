# rapid-infra

This is the RAPID infra repo, where we deploy the AWS resources needed for RAPID at scale, with
the AWS CDK (Python).

RAPID data ingestion into Synapse is one of the resources deployed here. Each data provider gets
its own S3 bucket, connected to its own Synapse project. Providers upload a folder of files,
then a `sidecar.json` in it; the sidecar's arrival indexes every file in the folder into the
provider's Synapse project, as Synapse files that point at the S3 objects (no data is copied).

Providers upload with **read-write STS credentials** that Synapse issues from an empty `sts`
"shim" folder in their project ([Synapse STS](https://docs.synapse.org/synapse-docs/compute-directly-on-data-in-synapse-or-s3)),
so they need a Synapse account with edit access to it, not AWS credentials.
`tools/contribute.py` does the whole contribution in one command.

```
provider (contribute.py, STS credentials from <project>/sts)
  uploads        s3://<provider-bucket>/<folder>/...files...
  then writes    s3://<provider-bucket>/<folder>/sidecar.json   ← written LAST
  → S3 "Object Created" → EventBridge rule (*/sidecar.json, configured buckets only)
    → Step Functions  rapid-{env}-pipeline
        Prepare         validate the sidecar, take the bucket's Synapse project (from config)
        ResolveStorage  look up the project and its (non-STS) storage location on the bucket
                        (set up after each deploy by tools/setup_synapse_projects.py, never here)
        SyncFiles       Distributed Map over every object under <folder>/
                          → sync Lambda per object: Synapse folders for the key's path,
                            then an external S3 file handle + File entity, at
                            <project>/<folder>/... (the same path as in S3)
    → per-file results in the results bucket; a failed execution emails ALARM_EMAIL
```

The skeleton follows Sage IT's
[aws-cdk-ecs-infra-template](https://github.com/Sage-Bionetworks-IT/aws-cdk-ecs-infra-template)
(config loading, CI workflows, pre-commit, dev container), without its ECS stacks.

AWS CDK projects need each account
[bootstrapped](https://docs.aws.amazon.com/cdk/v2/guide/getting_started.html#getting_started_bootstrap)
before deployment; Sage IT bootstraps every account in the AWS Organization when creating it.

## Stacks

| Stack | Purpose |
|---|---|
| `rapid-{env}-bucket-{bucket_name}` | One per `BUCKETS` entry: the provider's bucket, with Synapse's AWS account and the provider's ARNs in its bucket policy, `owner.txt`, CORS, and EventBridge notifications. Retained on stack deletion. Its Synapse project is set up after the deploy (below) |
| `rapid-{env}-pipeline` | The sidecar rule, state machine, Prepare and Sync Lambdas, Synapse PAT secret, results bucket and alarms. Shared by all buckets |

Adding a provider is a config change: a `BUCKETS` entry in `config/aws/{env}.yaml` and its
project in `config/synapse/{env}.yaml`.

### The provider's Synapse project

Never create a provider's Synapse project by hand: until its upload destination is set, a new
project stores uploads in Synapse's default S3 bucket, where RAPID data must not go. The CDK app
deploys the AWS resources; the Synapse resources are declared separately, in
`config/synapse/{env}.yaml`, and set up from the deploy's outputs, the way a Terraform resource
would take another's output:

```
config/aws/dev.yaml           BUCKETS: [{bucket_name: rapid-dev-provider-example, ...}]
  → cdk deploy --all --outputs-file cdk-outputs.json
      {"rapid-dev-bucket-rapid-dev-provider-example":
        {"BucketName": "rapid-dev-provider-example", "SynapseServiceAccountId": "..."}}
config/synapse/dev.yaml       projects: [{name: RAPID dev provider example,
                                            bucket: $ref.BucketName.rapid-dev-provider-example}]
  → tools/setup_synapse_projects.py --template config/synapse/dev.yaml --cdk-outputs cdk-outputs.json
```

After every deploy, CI's `synapse-setup` job (`aws-deploy.yaml`) runs the script with the deploy
job's outputs (an artifact; the job needs no AWS credentials). For each project, idempotently,
as the service account (the bucket stack's `SynapseServiceAccountId` output):

1. Finds the project by name, or creates it. Renaming it in the template makes a new project,
   so rename it in Synapse first.
2. Creates the external S3 storage location on the deployed bucket (`BucketName`) and makes it
   the project's **only** upload destination (replacing any other, including Synapse's default
   storage). Synapse checks the bucket's `owner.txt` for the service account, which is why this
   runs after the deploy.
3. Creates the empty STS "shim" folder (`sts_folder`, default `sts`), with an STS location on the
   whole bucket: contributors with edit access on it get read-write STS credentials to upload.
4. Makes the project's sharing settings match its `acl`, the way CloudFormation makes a stack
   match its template: each team or user in it gets exactly that permission level (`view`,
   `download`, `edit`, `edit_delete` or `admin`, as in Synapse's sharing dialog), and anyone else
   (shared by hand, public access) is removed. The service account's own access is never
   changed. Share projects in the template, not in Synapse: changes made by hand are undone.
5. `apply_project_template()`: a placeholder for the rest of the project setup (teams, folder
   permissions, folder structures, schema bindings, file views, curation tasks),
   along the lines of [htan2_project_setup](https://github.com/ncihtan/htan2_project_setup).

The job summary lists each project's ID, bucket, upload location and STS folder; the log also
has each project's `contribute.py` command for its provider. A project that fails (say,
its bucket stack is missing from the outputs) does not stop the others; fix it and use "Re-run
failed jobs".

## Configuration

Config is split by the system it configures:

- `config/aws/` (the CDK app): `base.yaml` is always loaded, then deep-merged with the
  environment's `config/aws/{env}.yaml` (`.yml` or `.json` also work, one file per env): the env
  file wins, nested mappings merge key by key so an env file only lists the keys it changes, and
  lists are replaced. Synth validates the result, and fails on bad IDs or duplicate bucket names.
- `config/synapse/{env}.yaml` (`tools/setup_synapse_projects.py`): one project per bucket,
  referring to its bucket's stack. `$ref.<Output>.<bucket_name>` is a CloudFormation output
  (`BucketName`, `BucketArn`, `SynapseServiceAccountId`) of the stack of that `BUCKETS`
  `bucket_name`, resolved from the deploy's outputs. A project's `bucket` must be
  `$ref.BucketName.<bucket_name>`. A unit test checks every reference names a bucket in the AWS
  config.

```yaml
# config/aws/dev.yaml
BUCKETS:
  - bucket_name: rapid-dev-provider-example  # globally unique; also the stack name suffix
    synapse_owner_ids: []            # owner.txt; the service account is always added
    s3_user_arns: []                 # optional: IAM principals with read/write (not needed with STS)
    s3_admin_arns: []
    deny_delete_arns: []
    versioned: false

# config/synapse/dev.yaml
projects:
  - name: RAPID dev provider example # Synapse project names are global
    bucket: $ref.BucketName.rapid-dev-provider-example  # the BucketName output of its stack
    sts_folder: sts                  # optional: the STS shim folder's name in the project
    acl:                             # optional: the project's sharing settings, exactly
      - principal_id: 3629180        # a Synapse team or user ID
        permission: edit             # view, download, edit, edit_delete or admin
```

`owner.txt` (`synapse_owner_ids`) lists who may register a storage location on the bucket, the
service account always among them. Providers do not need to be in it: they only use the service
account's locations (Synapse uploads, STS credentials), which takes permission on the project,
not ownership of the bucket.

## Contributing data (providers)

```console
pip install synapseclient boto3
export SYNAPSE_AUTH_TOKEN=<personal access token>     # or ~/.synapseConfig
python tools/contribute.py --sts-folder <sts folder synID> ./my-batch data/my-batch
```

This uploads every file under `./my-batch` to `s3://<bucket>/data/my-batch/…` and then writes
`data/my-batch/sidecar.json`, only if every upload succeeded. The files appear in the project at
`data/my-batch/…`. `--dry-run` lists what it would do.

To upload with other tools, get the credentials yourself
(`Folder(id=<sts>).get_sts_storage_token(permission="read_write", output_format="shell")`) and
note:

- **Every write needs the `bucket-owner-full-control` ACL** (`aws s3 cp … --acl
  bucket-owner-full-control`, boto3 `ExtraArgs={"ACL": "bucket-owner-full-control"}`, rclone
  `acl = bucket-owner-full-control`). Synapse's STS policy requires it; it is a no-op on our
  buckets, which own every object (`BucketOwnerEnforced`), but writes without it are denied.
  No bucket setting can supply it.
- Credentials last 12 hours and cover the **whole bucket**: anywhere can be written, and an
  overwrite or delete of an already-indexed file leaves Synapse pointing at changed or missing
  data.
- Write the sidecar last.
- Do not upload through the Synapse website into the `sts` folder: those files would land at
  keys Synapse chooses and live in the shim, not the index.

### Why a shim

Synapse only enables STS on a folder, never a project (`400 Can only enable STS on a folder`),
only on an empty folder, and with no other storage location. Files in an STS folder cannot
be moved to a folder with another storage location. So the `sts` folder only vends
credentials, and indexed files go elsewhere in the project on a non-STS location on the same
bucket: their Synapse path is their S3 key, and curators can reorganize them freely.

## The sidecar

The sidecar is the completion sentinel, like `manifest.ttl` in sagebrain-infra: write it after
every file in the folder is uploaded.

- Its key must be `<folder>/sidecar.json` (`SYNC.sidecar_filename`). A root-level sidecar is
  ignored.
- Its content must be a JSON object. No fields are required yet; the Synapse project is taken
  from config, never from the sidecar, so a provider cannot index into another project.
- Everything under `<folder>/` is synced, including subfolders. Sidecars and folder markers are
  skipped.
- Re-uploading the sidecar re-runs the folder. Files with an unchanged MD5 are left alone; a
  changed file becomes a new version of its Synapse entity.
- Deleting from S3 does **not** delete from Synapse.

## Indexing code

`src/lambda_sync/indexer.py` is ported from
[s3-synapse-sync](https://github.com/Sage-Bionetworks/s3-synapse-sync) (Apache-2.0; attribution
in the module docstring). Only the indexing core is taken: not its S3-notification handler,
Minerva/Batch processing or HTAN-specific configuration. It uses synapseclient's object models
(`synapseclient.models`), not the client methods deprecated in 4.x; the Synapse Lambdas run with
`PYTHONWARNINGS=default::DeprecationWarning`, so any new deprecation shows up in their logs. The
module docstring lists what changed.

## Development

The repo has a [dev container](https://containers.dev/) (`.devcontainer/`) with Python, Node,
the AWS CLI and the CDK CLI; open the folder in it from VS Code ("Dev Containers: Open Folder in
Container...") or in a GitHub Codespace. Outside it, `./tools/setup.sh` installs the CDK CLI,
creates `.venv` and installs the pre-commit hooks, or by hand:

```console
uv venv --python 3.13 .venv && source .venv/bin/activate   # or python3.13 -m venv .venv
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install

python -m pytest tests/ -v               # unit tests, no Docker or AWS needed
cdk synth --context env=dev              # needs Docker: bundles the sync Lambda for arm64
pre-commit run --all-files
```

The CDK CLI must be at least as new as `aws-cdk-lib` in `requirements.txt`
(`npx aws-cdk@latest synth ...` works if a global install is older).

## Deployment

Each environment has its own AWS account and a branch of the same name; CI deploys the branch
to its account, and nobody deploys from a laptop.

```
feature branch ──PR──▶ dev ──PR──▶ prod
                       │           │
              deploy-dev.yaml   deploy-prod.yaml     (cdk deploy --all)
```

- Branch from `dev`, open a PR into `dev`: `pr-check.yaml` runs tests, pre-commit and a synth
  of the dev environment. Merging deploys the dev account.
- Promote with a `dev` → `prod` PR; merging deploys the prod account (`deploy-prod.yaml`
  runs the tests and a prod synth before deploying).
- Each account's OIDC deploy role comes from organizations-infra and trusts only that account's
  branch (`dev` or `prod`). Each account must be CDK-bootstrapped.
- Since every stack deploys at once, ordering constraints between stacks need separate PRs,
  each deployed before the next merges.

### Sandbox

`config/aws/sandbox.yaml` is a personal test environment (`rapid-sandbox-*` stacks) in the DNT dev
account, not RAPID's dev. It is the one environment deployed from a laptop, never by CI, and
its `AWS_ACCOUNT` pins it so credentials for any other account fail the deploy:

```console
cdk deploy --context env=sandbox --all --profile dnt-dev-admin --outputs-file cdk-outputs.json
SYNAPSE_AUTH_TOKEN=<PAT> python tools/setup_synapse_projects.py \
  --template config/synapse/sandbox.yaml --cdk-outputs cdk-outputs.json
aws --profile dnt-dev-admin secretsmanager put-secret-value \
  --secret-id rapid-sandbox-pipeline/synapse-pat --secret-string '<PAT>'
```

### Adding a staging environment

For a dev → stage → prod flow, e.g. to try real provider data before prod:

1. A stage AWS account and its GitHub OIDC deploy role (Sage IT)
2. `config/aws/stage.yaml` with its own `BUCKETS` (bucket names are global, so new names) and
   `config/synapse/stage.yaml` with their projects
3. `"stage"` in `VALID_ENVS` (`src/utils.py`)
4. `.github/workflows/deploy-stage.yaml` (a copy of `deploy-dev.yaml`) and a `stage` branch
5. Promote `dev` → `stage` → `prod`

### One-time setup per environment

1. **Synapse service account.** Set its user ID in `SYNAPSE.service_account_id`
   (`config/aws/`). It is written to every bucket's `owner.txt`, so it can create the bucket's
   Synapse storage location, and output by every bucket stack for the Synapse setup.
2. **Its PAT** (view, download and modify scopes), as the `SYNAPSE_AUTH_TOKEN` secret of the
   environment's GitHub environment (`dev`, `prod`). The `synapse-setup` job logs in with it,
   and every deploy copies it into the pipeline's secret for the sync Lambdas
   (`<stack prefix>-pipeline/synapse-pat`) when it differs. To rotate it, update the GitHub
   secret and redeploy.
3. **Alarm email.** Set `ALARM_EMAIL` and confirm the SNS subscription email.

### Per provider bucket

1. Add the `BUCKETS` entry (`config/aws/{env}.yaml`) and its project
   (`config/synapse/{env}.yaml`), with a name not used anywhere in Synapse, and merge; CI deploys
   the bucket stack, then sets up its Synapse project, including the empty `sts` shim folder.
2. Share the project with the provider only (until `apply_project_template()` does it): edit
   access, or at least edit access to its `sts` folder.
3. Send the provider the `contribute.py` command the setup job prints in its log, with the
   `sts` folder's ID (also in the job summary).

The pipeline never creates a project or storage location: each execution's `ResolveStorage`
step looks up the bucket's project by name and its non-STS storage location on the bucket. If
either is missing (the setup job failed, or has not run yet), the execution fails without
retrying and emails `ALARM_EMAIL`; rerun the setup job, then re-upload the sidecar.

### Watching a sync

```console
aws --profile <profile> stepfunctions list-executions --state-machine-arn \
  $(aws --profile <profile> cloudformation describe-stacks --stack-name rapid-dev-pipeline \
    --query "Stacks[0].Outputs[?OutputKey=='SyncStateMachineArn'].OutputValue" --output text)
```

Per-file results (`created` / `updated` / `unchanged` / `skipped`, or the error) are under
`runs/` in the `ResultsBucketName` output bucket. Lambda logs carry one
`{"event": "file_sync", ...}` line per file.
