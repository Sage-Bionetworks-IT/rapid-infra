# rapid-infra

This is the RAPID infra repo, where we deploy the AWS resources needed for RAPID at scale, with
the AWS CDK (Python).

RAPID data ingestion into Synapse is one of the resources deployed here. Each data provider gets
its own S3 bucket, connected to its own Synapse project. Providers upload a folder of files,
then a `sidecar.json` in it; the sidecar's arrival indexes every file in the folder into the
provider's Synapse project, as Synapse files that point at the S3 objects (no data is copied).

```
provider uploads s3://<provider-bucket>/<folder>/...files...
then writes      s3://<provider-bucket>/<folder>/sidecar.json   ← written LAST
  → S3 "Object Created" → EventBridge rule (*/sidecar.json, configured buckets only)
    → Step Functions  rapid-{env}-pipeline
        Prepare         validate the sidecar, take the bucket's Synapse project (from config)
        ResolveStorage  look up the project and its storage location on the bucket (set up
                        after each deploy by tools/setup_synapse_projects.py, never here)
        SyncFiles       Distributed Map over every object under <folder>/
                          → sync Lambda per object: Synapse folders for the key's path,
                            then an external S3 file handle + File entity
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
| `rapid-{env}-bucket-{id}` | One per `BUCKETS` entry: the provider's bucket, with Synapse's AWS account and the provider's ARNs in its bucket policy, `owner.txt`, CORS, and EventBridge notifications. Retained on stack deletion. Its Synapse project is set up after the deploy (below) |
| `rapid-{env}-pipeline` | The sidecar rule, state machine, Prepare and Sync Lambdas, Synapse PAT secret, results bucket and alarms. Shared by all buckets |

Adding a provider is a config change: add a `BUCKETS` entry to `config/{env}.yaml`.

### The provider's Synapse project

Never create a provider's Synapse project by hand: until its upload destination is set, a new
project stores uploads in Synapse's default S3 bucket, where RAPID data must not go. After every
deploy, CI's `synapse-setup` job (`aws-deploy.yaml`) runs `tools/setup_synapse_projects.py`,
which for each bucket, idempotently, as the sync service account:

1. Finds the project by `synapse_project_name`, or creates it. Renaming it in config makes a new
   project, so rename it in Synapse first.
2. Creates the bucket's external S3 storage location and makes it the project's **only** upload
   destination (replacing any other, including Synapse's default storage). Synapse checks the
   bucket's `owner.txt` for the service account, which is why this runs after the deploy.
3. Creates `SYNAPSE.project_folders` in the project.
4. Creates the empty STS "shim" folder (`sts_folder`, default `sts`), with an STS location on the
   whole bucket: contributors with edit access on it get read-write STS credentials to upload.
5. `apply_project_template()`: a placeholder for the rest of the project setup (teams and
   permissions, versioned folder structures, schema bindings, file views, curation tasks), along
   the lines of [htan2_project_setup](https://github.com/ncihtan/htan2_project_setup).

The job summary lists each bucket's project, upload location and STS folder IDs. A bucket that
fails does not stop the others; fix it and use "Re-run failed jobs". For the sandbox, run it
after the deploy:

```console
SYNAPSE_AUTH_TOKEN=<PAT> python tools/setup_synapse_projects.py --env sandbox [--bucket ID]
```

## Configuration

`config/base.yaml` is always loaded, then deep-merged with the environment's config,
`config/{env}.yaml` (`.yml` or `.json` also work, one file per env): the env file wins, nested
mappings merge key by key so an env file only lists the keys it changes, and lists are replaced.
Synth validates the result, and fails on bad IDs, duplicate bucket names, or two buckets
with the same Synapse project: each provider's project is its own.

```yaml
SYNAPSE:
  project_folders: []                # created in every provider project ("a/b" nests)

BUCKETS:
  - id: example                      # stack name suffix
    bucket_name: rapid-dev-example-data
    synapse_project_name: RAPID dev example  # created by the deploy; names are global
    sts_folder: sts                  # the project's STS shim folder
    synapse_owner_ids: []            # owner.txt; the service account is always added
    s3_user_arns: []                 # provider principals with read/write (cross-account OK)
    s3_admin_arns: []
    deny_delete_arns: []
    versioned: false
```

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

`config/sandbox.yaml` is a personal test environment (`rapid-sandbox-*` stacks) in the DNT dev
account, not RAPID's dev. It is the one environment deployed from a laptop, never by CI, and
its `AWS_ACCOUNT` pins it so credentials for any other account fail the deploy:

```console
cdk deploy --context env=sandbox --all --profile dnt-dev-admin
aws --profile dnt-dev-admin secretsmanager put-secret-value \
  --secret-id rapid-sandbox-pipeline/synapse-pat --secret-string '<PAT>'
```

### Adding a staging environment

For a dev → stage → prod flow, e.g. to try real provider data before prod:

1. A stage AWS account and its GitHub OIDC deploy role (Sage IT)
2. `config/stage.yaml` with its own `BUCKETS` (bucket names are global, so new names) and
   Synapse projects
3. `"stage"` in `VALID_ENVS` (`src/utils.py`)
4. `.github/workflows/deploy-stage.yaml` (a copy of `deploy-dev.yaml`) and a `stage` branch
5. Promote `dev` → `stage` → `prod`

### One-time setup per environment

1. **Synapse service account.** Set its user ID in `SYNAPSE.service_account_id`. It is written
   to every bucket's `owner.txt`, so it can create the bucket's Synapse storage location.
2. **Its PAT** (view, download and modify scopes), in two places: the `SYNAPSE_AUTH_TOKEN`
   repository secret, which `deploy-dev.yaml` and `deploy-prod.yaml` pass to the
   `synapse-setup` job, and, after the first deploy, the pipeline's secret for the sync Lambdas:
   ```console
   aws --profile <profile> secretsmanager put-secret-value \
     --secret-id rapid-dev-pipeline/synapse-pat --secret-string '<PAT>'
   ```
3. **Alarm email.** Set `ALARM_EMAIL` and confirm the SNS subscription email.

### Per provider bucket

1. Add the `BUCKETS` entry, with a `synapse_project_name` not used anywhere in Synapse, and
   merge; CI deploys the bucket stack, then sets up its Synapse project.
2. Share the project with the provider only (until `apply_project_template()` does it).

The pipeline never creates a project or storage location: each execution's `ResolveStorage`
step looks up the bucket's project by name and its storage location on the bucket. If either
is missing (the setup job failed, or has not run yet), the execution fails without retrying
and emails `ALARM_EMAIL`; rerun the setup job, then re-upload the sidecar.

### Watching a sync

```console
aws --profile <profile> stepfunctions list-executions --state-machine-arn \
  $(aws --profile <profile> cloudformation describe-stacks --stack-name rapid-dev-pipeline \
    --query "Stacks[0].Outputs[?OutputKey=='SyncStateMachineArn'].OutputValue" --output text)
```

Per-file results (`created` / `updated` / `unchanged` / `skipped`, or the error) are under
`runs/` in the `ResultsBucketName` output bucket. Lambda logs carry one
`{"event": "file_sync", ...}` line per file.
