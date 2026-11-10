# rapid-infra

AWS CDK (Python) infrastructure for RAPID data ingestion into Synapse. Each data provider gets
its own S3 bucket, connected to its own Synapse project. Providers upload a folder of files,
then a `sidecar.json` in it; the sidecar's arrival indexes every file in the folder into the
provider's Synapse project, as Synapse files that point at the S3 objects (no data is copied).

This first version has no stacks yet: only the configuration, CI and the deploy path into each
environment's AWS account.

## Configuration

`config/base.yaml` is always loaded, then shallow-merged with `config/{dev,prod}.yaml` (the env
file replaces whole top-level keys). Synth validates it.

## Development

```console
uv venv --python 3.13 .venv && source .venv/bin/activate   # or python3.13 -m venv .venv
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install

python -m pytest tests/ -v               # unit tests, no Docker or AWS needed
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
  of both environments. Merging deploys the dev account.
- Promote with a `dev` → `prod` PR; merging deploys the prod account.
- Set the OIDC role ARNs in `deploy-dev.yaml` / `deploy-prod.yaml` first (TODO). The roles come
  from organizations-infra, and each account must be CDK-bootstrapped.
- Until the first stack, a deploy only checks that the OIDC role is assumed
  (`aws sts get-caller-identity`): the CDK CLI fails on an app without stacks.
- Since every stack deploys at once, ordering constraints between stacks need separate PRs,
  each deployed before the next merges.

### Sandbox

`config/sandbox.yaml` is a personal test environment (`rapid-sandbox-*` stacks) in the DNT dev
account, not RAPID's dev. It is the one environment deployed from a laptop, never by CI, and
its `AWS_ACCOUNT` pins it so credentials for any other account fail the deploy:

```console
cdk deploy --context env=sandbox --all --profile dnt-dev-admin
```

### Adding a staging environment

For a dev → stage → prod flow, e.g. to try real provider data before prod:

1. A stage AWS account and its GitHub OIDC deploy role (Sage IT)
2. `config/stage.yaml`
3. `"stage"` in `VALID_ENVS` (`src/utils.py`)
4. `.github/workflows/deploy-stage.yaml` (a copy of `deploy-dev.yaml`), a `test-stage` job in
   `pr-check.yaml`, and a `stage` branch
5. Promote `dev` → `stage` → `prod`
