# rapid-infra

This is the RAPID infra repo, where we deploy the AWS resources needed for RAPID at scale, with
the AWS CDK (Python).

RAPID data ingestion into Synapse is one of the resources deployed here. Each data provider gets
its own S3 bucket, connected to its own Synapse project. Providers upload a folder of files,
then a `sidecar.json` in it; the sidecar's arrival indexes every file in the folder into the
provider's Synapse project, as Synapse files that point at the S3 objects (no data is copied).

So far, only the provider buckets: the sync pipeline comes next.

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
config/synapse/dev.yaml       projects: [{name: RAPID dev provider 1, bucket: rapid-dev-provider-example}]
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
3. `apply_project_template()`: a placeholder for the rest of the project setup (provider and
   admin teams and permissions, folder structures, schema bindings, file views, curation tasks),
   along the lines of [htan2_project_setup](https://github.com/ncihtan/htan2_project_setup).

The job summary lists each project's ID, bucket and upload location. A project that fails (say,
its bucket stack is missing from the outputs) does not stop the others; fix it and use "Re-run
failed jobs".

## Configuration

Config is split by the system it configures:

- `config/aws/` (the CDK app): `base.yaml` is always loaded, then deep-merged with the
  environment's `config/aws/{env}.yaml` (`.yml` or `.json` also work, one file per env): the env
  file wins, nested mappings merge key by key so an env file only lists the keys it changes, and
  lists are replaced. Synth validates the result, and fails on bad IDs or duplicate bucket names.
- `config/synapse/{env}.yaml` (`tools/setup_synapse_projects.py`): one project per bucket,
  naming its `BUCKETS` `bucket_name`. A unit test checks every project's bucket is in the AWS config.

```yaml
# config/aws/dev.yaml
BUCKETS:
  - bucket_name: rapid-dev-provider-example  # globally unique; also the stack name suffix
    synapse_owner_ids: []            # owner.txt; the service account is always added
    s3_user_arns: []                 # provider principals with read/write (cross-account OK)
    s3_admin_arns: []
    deny_delete_arns: []
    versioned: false

# config/synapse/dev.yaml
projects:
  - name: RAPID dev provider 1       # Synapse project names are global
    bucket: rapid-dev-provider-example  # the bucket_name above
```

`owner.txt` (`synapse_owner_ids`) lists who may register a storage location on the bucket, the
service account always among them. Providers do not need to be in it: they only use the service
account's locations (Synapse uploads, STS credentials), which takes permission on the project,
not ownership of the bucket.

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
cdk synth --context env=dev
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
2. **Its PAT** (view, download and modify scopes) as the `SYNAPSE_AUTH_TOKEN` repository
   secret, which `deploy-dev.yaml` and `deploy-prod.yaml` pass to the `synapse-setup` job.

### Per provider bucket

1. Add the `BUCKETS` entry (`config/aws/{env}.yaml`) and its project
   (`config/synapse/{env}.yaml`), with a name not used anywhere in Synapse, and merge; CI deploys
   the bucket stack, then sets up its Synapse project.
2. Share the project with the provider only (until `apply_project_template()` does it).
