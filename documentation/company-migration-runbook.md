# Company aws2tf full-inventory and networking migration runbook

## Purpose

This runbook explains the entire company workflow:

1. clone the reviewed aws2tf fork on a company laptop;
2. authenticate with a read-only AWS identity;
3. produce a validation-only inventory and candidate Terraform configuration;
4. understand exactly what aws2tf and Terraform do;
5. reconcile the generated inventory against AWS;
6. reshape the networking resources into a curated repository like the India
   networking repository; and
7. adopt resources into the company backend only after review.

The discovery repository and the final networking repository are intentionally
separate. aws2tf is a migration tool, not the long-term desired-state layout.

## The two repositories and their jobs

### 1. Company aws2tf fork

Use this repository to inspect the live account and produce migration evidence:

- discovered resource IDs and ARNs;
- candidate per-resource HCL;
- import blocks;
- Terraform plan diagnostics;
- dependency relationships; and
- a private run manifest identifying the account, Region, source revision, and
  credential source.

Do not use its generated local state as the company source of truth.

### 2. Company networking infrastructure repository

This is the durable repository modeled after the India team's layout. It owns:

- reusable network modules;
- account/environment composition;
- reviewed variables and `terraform.tfvars` files;
- the approved remote backend;
- CI plan/apply workflows;
- ownership boundaries; and
- controlled Terraform imports.

```text
aws2tf discovery output              company networking repository
-----------------------              -----------------------------
raw resource-oriented HCL     --->   reviewed module composition
temporary import blocks       --->   approved import/adoption plan
local plan diagnostics        --->   central backend and CI workflow
current-state evidence        --->   intentional desired state
```

## Before cloning on the company laptop

Obtain these values from the company platform or cloud team:

- company GitHub clone URL for this fork;
- expected 12-digit AWS account ID;
- AWS Region;
- approved read-only role, SSO permission set, or temporary access keys;
- final networking repository clone URL;
- backend bucket/table/workspace conventions; and
- the owner who approves imports.

Do not send AWS credentials through chat, email, tickets, Git commits, or shell
command arguments.

## Clone and install the company fork

Once the company remote exists:

```bash
git clone git@github.com:COMPANY_ORG/aws2tf.git
cd aws2tf
git switch company-safe-evaluation
```

Confirm the revision and remotes:

```bash
git status --short --branch
git log -1 --oneline
git remote -v
```

Keep AWS's public repository as the upstream remote:

```bash
git remote add upstream https://github.com/aws-samples/aws2tf.git
git fetch upstream
```

Install into an isolated Python environment:

```bash
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-company.lock
```

Verify the required tools:

```bash
aws --version
terraform version
jq --version
.venv/bin/python --version
```

Expected minimums are Python 3.12, AWS CLI 2.31.4, Terraform 1.12, and jq 1.6.

## AWS credential choices

`--profile` receives a profile name. It never receives an access key.

### Option A: named profile or SSO profile

The profile is a label stored in `~/.aws/config` or `~/.aws/credentials`, such
as `network-readonly`:

```bash
aws configure --profile network-readonly
```

The prompts request the key and secret without placing them in the command
itself. For company SSO, use the company-provided SSO setup and login:

```bash
aws sso login --profile network-readonly
```

Verify the identity:

```bash
aws sts get-caller-identity --profile network-readonly
```

Then pass only the profile name:

```bash
--profile network-readonly
```

### Option B: paste temporary credentials into the terminal environment

Use this when the company provides an access key, secret access key, and
possibly a session token. Start a Bash subshell so the secure prompt commands
work consistently on macOS and Linux:

```bash
bash
read -r -p "AWS access key ID: " AWS_ACCESS_KEY_ID
read -r -s -p "AWS secret access key: " AWS_SECRET_ACCESS_KEY
printf '\n'
read -r -s -p "AWS session token (press Enter if not supplied): " AWS_SESSION_TOKEN
printf '\n'
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
```

Characters entered for the secret and session token are hidden. These commands
do not put the credential values into shell history.

Temporary access keys normally begin with `ASIA` and require
`AWS_SESSION_TOKEN`. Long-lived keys commonly begin with `AKIA` and may not have
a session token. Use only the company-approved read-only credentials.

Verify exactly which identity the environment resolves:

```bash
aws sts get-caller-identity
```

Then use:

```bash
--use-environment-credentials
```

The wrapper validates that the required variables exist but never prints or
stores their values. When finished, remove them from the shell:

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
exit
```

## Run a full inventory safely

### Step 1: confirm the account number yourself

For profile credentials:

```bash
aws sts get-caller-identity --profile network-readonly
```

For terminal environment credentials:

```bash
aws sts get-caller-identity
```

Copy the `Account` value. The wrapper requires it separately so a wrong profile
cannot silently scan another account.

### Step 2: dry-run with a named profile

```bash
.venv/bin/python company_safe.py \
  --expected-account 123456789012 \
  --profile network-readonly \
  --region us-east-1 \
  --full-inventory \
  --dry-run
```

### Step 2 alternative: dry-run with terminal credentials

```bash
.venv/bin/python company_safe.py \
  --expected-account 123456789012 \
  --use-environment-credentials \
  --region us-east-1 \
  --full-inventory \
  --dry-run
```

The dry-run still calls STS to verify the account but does not start aws2tf.

### Step 3: execute discovery and generation

Remove only `--dry-run` from the reviewed command:

```bash
.venv/bin/python company_safe.py \
  --expected-account 123456789012 \
  --use-environment-credentials \
  --region us-east-1 \
  --full-inventory
```

Repeat once per account and Region. AWS resources are Region-scoped except for
global services, and one run is not a multi-account organization inventory.

## What each wrapper argument means

| Argument | Meaning |
| --- | --- |
| `--expected-account` | Independent guardrail containing the exact target account ID. |
| `--profile` | Name of an AWS CLI profile; never a credential value. |
| `--use-environment-credentials` | Use the standard AWS variables already exported in the terminal. |
| `--region` | Region used by boto3, AWS CLI, and the Terraform AWS provider. |
| `--full-inventory` | Explicitly request every resource family supported by upstream aws2tf. |
| `--type` | Smaller networking-only pilot instead of full inventory. |
| `--id` | One resource ID for a supported scoped type. It cannot accompany full inventory. |
| `--dry-run` | Verify identity, write the manifest, and print the command without starting aws2tf. |

`--profile` and `--use-environment-credentials` are mutually exclusive. Likewise,
choose either `--full-inventory` or `--type`.

## What happens during a full run

```text
1. Validate command arguments
2. Validate the credential source
3. Call STS GetCallerIdentity
4. Stop if the account differs from --expected-account
5. Record fork/upstream revisions and worktree provenance
6. Start upstream aws2tf with --validate
7. Discover resources through boto3
8. Write Terraform import blocks
9. Run terraform plan -generate-config-out against live AWS
10. Rewrite provider-generated HCL and resolve dependencies
11. Repeat plans when new dependencies appear
12. Run terraform validate
13. Exit before upstream wrapup() and terraform apply
```

The wrapper always adds upstream `--validate`. In this fork, that means no
`terraform apply` and no state adoption. `--accept` is not supplied.

## Output and what to inspect

The main run directory is normally:

```text
generated/tf-ACCOUNT-REGION/
```

Important artifacts include:

| Artifact | Why it matters |
| --- | --- |
| `aws_*__*.tf` | Candidate provider-generated and cleaned resource HCL. |
| `import__*.tf` | Temporary import blocks mapping AWS IDs to Terraform addresses. |
| `plan1.json` | First provider plan diagnostics and generated-config failures. |
| `plan2.json` | Later dependency/cleanup plan diagnostics when produced. |
| `validate2.json` | Machine-readable validation failure details when produced. |
| `imported/` | Intermediate generated/import files. |
| `notimported/` | Resources aws2tf could not safely materialize. |
| `company-runs/*.json` | Account, Region, command, source commits, dirty-tree fingerprint, and credential-source metadata. |

Do not commit generated directories, plan files, logs, state, downloaded code,
or inventories into a normal source repository. They may contain sensitive
configuration even when the AWS identity is read-only.

## Read-only does not mean non-sensitive

Read-only prevents AWS mutation. It does not prevent APIs from returning
sensitive configuration. Complete Terraform generation can cause AWS APIs or
the provider to read SSM parameter values, Cognito client secrets, launch
configuration user data, Glue connection properties, CloudFront function code,
ECS task definitions, and CodeBuild environment variables.

The fork suppresses several avoidable secondary downloads and creates files
under a private `077` mask. That is risk reduction, not a guarantee that only
metadata is read. Treat the entire output directory as sensitive.

If policy requires strict ID/ARN-only inventory, use AWS Config, Resource
Explorer, or a purpose-built inventory adapter. Strict metadata-only discovery
and complete provider-generated Terraform are different requirements.

## Proving inventory completeness

A successful Terraform plan proves only that represented resources can be
read. It does not prove everything was discovered.

Create a reconciliation table outside Git with at least:

| Field | Example purpose |
| --- | --- |
| account ID | Establish ownership boundary. |
| Region | Separate regional resources. |
| AWS service/type | Map to a Terraform resource and target module. |
| resource ID/ARN | Stable reconciliation key. |
| owner account | Exclude RAM-visible or foreign-owned resources. |
| current owner | Terraform, CloudFormation, CDK, AWS-managed, or unmanaged. |
| aws2tf address | Find the generated HCL/import block. |
| target address | Final module address in the networking repository. |
| migration status | discovered, reviewed, modeled, imported, verified, exception. |

For every in-scope networking resource, require exactly one result:

- mapped to a final Terraform address;
- documented as externally managed;
- documented as foreign/RAM-visible;
- documented as AWS-managed; or
- recorded as an approved exception.

## Building the India-style networking repository

The India repository is curated desired state rather than raw generated HCL. A
useful target shape is:

```text
company-network-infra/
├── .github/
│   └── workflows/
│       ├── plan.yml
│       └── deploy.yml
├── envs/
│   ├── cit/
│   │   └── terraform.tfvars
│   ├── prod-ext/
│   │   └── terraform.tfvars
│   └── prod-int/
│       └── terraform.tfvars
├── modules/
│   ├── vpc/
│   ├── transit-gateway/
│   ├── inspection-vpc/
│   ├── network-firewall/
│   ├── endpoints/
│   ├── route-tables/
│   ├── security-groups/
│   ├── ram-sharing/
│   ├── dns/
│   └── flow-logs/
├── backend.tf
├── main.tf
├── outputs.tf
├── providers.tf
├── variables.tf
├── versions.tf
└── README.md
```

Do not create a module merely because one generated file exists. Modules should
represent stable company concepts and repeated lifecycle boundaries.

### Suggested networking domains

| Domain | Typical resources |
| --- | --- |
| VPC foundation | VPCs, subnets, DHCP options, NACLs, IGWs, NAT gateways. |
| Routing | Route tables, routes, route-table associations, prefix lists. |
| Transit | TGWs, route tables, propagations, associations, attachments. |
| Inspection | Inspection VPC, Network Firewall, firewall policies/rule groups. |
| Private access | VPC endpoints, endpoint services, PrivateLink, endpoint policies. |
| Shared services | RAM shares, resolver endpoints/rules, Route 53 private zones. |
| Security | Security groups/rules and network logging controls. |
| Observability | Flow logs, log groups, delivery roles, alarms. |

### Root composition versus modules versus tfvars

`main.tf` connects modules and expresses architecture:

```hcl
module "hub_network" {
  source = "./modules/vpc"

  name              = var.hub_network.name
  cidr_block        = var.hub_network.cidr_block
  availability_zones = var.hub_network.availability_zones
  subnets           = var.hub_network.subnets
  tags              = local.common_tags
}
```

The module implements reusable resource behavior. Environment tfvars supply
facts that differ between accounts:

```hcl
hub_network = {
  name               = "prod-external-hub"
  cidr_block         = "10.20.0.0/16"
  availability_zones = ["us-east-1a", "us-east-1b"]
  subnets = {
    inspection_a = { cidr = "10.20.0.0/24", az = "us-east-1a" }
    inspection_b = { cidr = "10.20.1.0/24", az = "us-east-1b" }
  }
}
```

Do not put secrets in tfvars. Use approved secret stores and data references.

## Migration sequence for each networking domain

1. Select one bounded domain, such as one VPC and its direct routing resources.
2. Reconcile all related AWS IDs from the inventory.
3. Identify resources owned by another account, CloudFormation, CDK, or an
   existing Terraform state.
4. Copy only the relevant attributes into the target module design.
5. Replace hardcoded account/Region values with variables or data sources.
6. Preserve tags, routing behavior, associations, and lifecycle semantics.
7. Run `terraform fmt` and `terraform validate` in the target repository.
8. Add reviewed import blocks mapping each AWS ID to its final module address.
9. Run a plan against the approved remote backend.
10. Accept only expected imports with zero create, update, or destroy actions.
11. Apply the import through the approved company workflow.
12. Run another plan and require no drift.
13. Remove temporary import blocks after company policy allows it.
14. Update the reconciliation table.

Do not migrate the whole account in one state-changing apply. Discovery can be
broad; adoption should be incremental and reviewable.

## Plan interpretation

There are two different plans:

### aws2tf discovery plan

This runs in the generated directory so the provider can read resources and
generate candidate HCL. It is evidence, not approval to adopt resources.

### Target repository adoption plan

This runs after resources have final module addresses and the company backend
is configured. It is the plan that must show only reviewed imports and no
unintended infrastructure changes.

A zero-diff result is meaningful only after every expected inventory ID has
been reconciled. A clean plan cannot reveal a resource missing from the code.

## Troubleshooting the dependency error

If aws2tf reports:

```text
ERROR: No change/progress in dependancies exiting...
ERROR: Not found ...
```

do not assume the AWS resource is missing. Inspect error diagnostics first:

```bash
jq -c 'select(."@level" == "error") | .diagnostic' plan1.json
```

Common causes include invalid generated arguments, provider schema conflicts,
missing read permissions, foreign-owned resources, or dependency addresses
that could not be generated. Preserve the run manifest and diagnostics when
asking for help, but redact sensitive values before sharing them.

## Updating the company fork from AWS upstream

Perform updates on a branch:

```bash
git fetch upstream
git switch -c update/aws2tf-YYYY-MM-DD
git rebase upstream/master
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m coverage run --source=company_safe -m unittest discover -s tests
.venv/bin/python -m coverage report --fail-under=80
```

Re-run the Terraform provider fixture and security review before merging. Pay
special attention to new code that downloads object bodies, secret values,
user data, function packages, task definitions, or other payloads.

## Final checklist

- [ ] Company fork revision is reviewed and pinned.
- [ ] Credentials are approved and read-only.
- [ ] STS account matches `--expected-account`.
- [ ] Correct Region is selected.
- [ ] Dry-run command is reviewed.
- [ ] Generated directory is stored only on approved encrypted storage.
- [ ] Credentials are removed from the shell after use.
- [ ] Every networking resource ID is reconciled.
- [ ] Foreign/RAM-visible resources are excluded or documented.
- [ ] Target code is reorganized into intentional modules and environment data.
- [ ] Target backend and CI workflow are approved.
- [ ] Adoption plans contain only reviewed imports.
- [ ] No unintended create, update, or destroy action is present.
- [ ] Post-import plan is clean.

## The core mental model

aws2tf answers: **"What Terraform-shaped configuration can be generated from
what exists in AWS right now?"**

The networking repository answers: **"What network architecture does the
company intentionally own and operate going forward?"**

The migration is the controlled process of reconciling the first answer into
the second without changing live infrastructure accidentally.
