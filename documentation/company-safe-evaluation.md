# Company-safe aws2tf evaluation

## Decision

Use this checkout as an isolated full-account inventory, candidate-HCL
generation, and coverage-comparison tool. Do not make its local Terraform
state the company's authoritative state, and do not copy generated files
automatically into the target infrastructure repository.

The company-owned migration pipeline remains the source of truth:

1. A resource inventory proves discovery completeness.
2. Ownership checks distinguish locally owned, RAM-visible, CloudFormation,
   and already-Terraform-managed resources.
3. Reviewed HCL and import blocks land in the company stack.
4. The company stack's central backend and approval workflow perform adoption.

`aws2tf` is a second opinion and an accelerator inside that process. It can
inventory a whole account, but it cannot infer the company's intended module
boundaries, environment model, naming rules, or ownership decisions.

## Evaluated revision and local branch

- Upstream: `https://github.com/aws-samples/aws2tf.git`
- Evaluated commit: `0592fc501ba1a60d77c74409e2799414bbe15011`
- Local customization branch: `company-safe-evaluation`
- License: MIT-0

The upstream commit is newer than the latest tag and contains a November 2026
command-injection fix. Pin the commit; do not assume an older release tag has
the same fixes.

## What was changed locally

### Safe wrapper

`company_safe.py` adds boundaries that upstream does not provide:

- requires an expected 12-digit AWS account ID;
- requires either an explicit profile or terminal environment credentials, plus
  an explicit Region;
- requires either a scoped networking `--type` or an explicit
  `--full-inventory` choice;
- verifies the active account through STS before starting generation;
- retains a positive networking allowlist for small, scoped pilots;
- allows every upstream-supported resource family in explicit full-inventory
  mode;
- always adds upstream `--validate`, stopping before Terraform import apply;
- never adds `--accept`;
- enables best-effort payload suppression in selected aws2tf post-processing
  handlers, avoiding secondary secret-value lookups, SSM decryption calls, EC2
  user-data lookups, Lambda package/layer downloads, and Glue script copies;
- runs generation under a private `077` umask because the Terraform AWS
  provider can still read sensitive attributes while refreshing resources and
  plan JSON/binary plan artifacts must be treated as sensitive;
- writes a private `0600` run manifest with fork/upstream commits, dirty-tree
  status, and a worktree fingerprint.

Full inventory includes Terraform resource containers for Secrets Manager, SSM,
EC2, Lambda, Glue, and every other upstream-supported family. The suppression
guards reduce avoidable secondary downloads, but they are not a hard
no-sensitive-read boundary. Provider refresh and some AWS discovery APIs return
complete configuration: examples include SSM parameter values, Cognito client
secrets, launch-configuration user data, Glue connection properties, CloudFront
function code, ECS task definitions, and CodeBuild environment variables.
Generated directories, plan JSON, binary plans, logs, and any eventual state
must therefore be handled as sensitive data even with read-only credentials.

This is the unavoidable tradeoff for whole-account Terraform generation:
restricting those resource families would make the inventory incomplete. If the
company requires a strict metadata-only inventory, use AWS Config/Resource
Explorer or a purpose-built ID/ARN discovery adapter first; aws2tf cannot both
generate complete provider-backed HCL and guarantee that payload-bearing read
APIs are never called.

## What Terraform actually runs

Yes: the safe wrapper still runs Terraform against the current live AWS
account. Upstream performs one or more
`terraform plan -generate-config-out=...` operations to read remote resources,
generate candidate HCL, and detect dependencies. It then runs
`terraform validate` on the cleaned configuration.

No: the safe wrapper never reaches upstream's `wrapup()` path, where upstream
normally applies the saved plan to import resources into local Terraform state.
The wrapper always supplies `--validate`, and tests assert that `--accept` is
never supplied. This is live read-only discovery and planning, not adoption.

A clean plan proves that represented resources can be described consistently.
It does not prove that every AWS resource was discovered. Completeness requires
reconciling AWS inventory IDs to generated Terraform addresses.

## Why the output will not look like the India repository

The India networking repository is curated desired-state architecture: reusable
modules, environment-specific `terraform.tfvars`, provider/backend wiring, and
deployment workflows. `aws2tf` emits raw, resource-oriented candidate HCL and
import blocks. It does not automatically transform an existing account into
that repository design.

Use the generated output as migration input:

```text
live AWS account
  -> aws2tf full inventory + candidate HCL
  -> reconcile every resource ID
  -> classify by account/environment/domain
  -> refactor into company modules and tfvars
  -> import through the company's backend/workflow
  -> require a zero-create/change/destroy adoption plan
```

For the India-style shape, the human design step maps generated resources into
root composition (`main.tf`), reusable `modules/`, per-environment values under
`envs/`, and the existing CI workflow. Copying generated files directly would
produce a flat repository and preserve accidental current-state structure.

### Generator regressions

Tests now cover failures observed in the first company trial:

- a null `ipv6_ipam_pool_id` is removed with zero
  `ipv6_netmask_length`, keeping the provider's linked arguments symmetric;
- empty members in typed route objects are emitted as `null`, not invalid empty
  strings;
- validation-only runs exit successfully instead of returning failure after a
  successful validation.

The subnet cleanup already present upstream removes the conflicting
`availability_zone_id` and false `map_customer_owned_ip_on_launch` values seen
in the pasted failure.

## What the original error actually meant

The final `aws_route_table_association ... Not found` line was a secondary,
misleading dependency-loop message. The actionable Terraform provider errors
were in `generated/.../plan1.json`.

The dependency loop could not progress because generated HCL failed provider
schema validation. It then reported every unresolved dependency as if it might
not exist. When this happens again, inspect error-level diagnostics in
`plan1.json` before deleting or re-importing resources.

## Runtime compatibility

| Component | Upstream requirement | Evaluated locally |
| --- | --- | --- |
| Python | 3.12+ | 3.12.3 — compatible |
| AWS CLI | 2.31.4+ | 2.32.31 — compatible |
| jq | 1.6+ | 1.7 — compatible |
| Terraform | 1.12+ | repo-local 1.12.0 checksum verified — compatible |
| Terraform AWS provider | generated project default 6.53.0 | signed 6.53.0 fixture initialized and validated |
| Python packages | minimum versions only | isolated `.venv`; exact evaluated versions in `requirements-company.lock` |

The system Snap package cannot start in this environment, so the compatibility
test used the checksum-verified binary under `.tools/terraform/1.12.0`. The
synthetic VPC/subnet/route-table regression fixture passes `terraform validate`
against the signed AWS provider 6.53.0. This proves local schema compatibility;
it does not replace a scoped, read-only plan against the real account.

## Installation

From the parent workspace:

```bash
git clone https://github.com/aws-samples/aws2tf.git
cd aws2tf
git switch -c company-safe-evaluation
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-company.lock
```

The clone in this workspace has already completed those steps.

## Full-account inventory

Use a company-approved read-only identity. Never paste credentials into a file,
command argument, Git commit, or run manifest. See the
[company migration runbook](company-migration-runbook.md) for secure profile and
terminal environment setup.

First verify the AWS identity and preview the exact command:

```bash
.venv/bin/python company_safe.py \
  --expected-account 123456789012 \
  --profile NETWORK_READONLY \
  --region us-east-1 \
  --full-inventory \
  --dry-run
```

If the account and Region are correct, remove `--dry-run` to run the full
validation-only generation pipeline:

```bash
.venv/bin/python company_safe.py \
  --expected-account 123456789012 \
  --profile NETWORK_READONLY \
  --region us-east-1 \
  --full-inventory
```

This intentionally does not import resources into local state. Review the
generated HCL, `plan1.json`, `validate2.json` when present, owner identifiers,
and the run manifest first. A large account can take substantial time and can
surface `AccessDenied` for services excluded by the read-only permission set.

For a smaller networking pilot, use the scoped form:

```bash
.venv/bin/python company_safe.py \
  --expected-account 123456789012 \
  --profile NETWORK_READONLY \
  --region us-east-1 \
  --type aws_vpc \
  --id vpc-0123456789abcdef0
```

## Review gates for every generated resource

1. The caller account equals the expected networking account.
2. The resource's `owner_id` or ARN account segment equals that account.
3. RAM-visible resources owned by another account are excluded.
4. Current ownership is known: CloudFormation, Terraform state, AWS-managed,
   or unmanaged.
5. `terraform fmt` and `terraform validate` pass.
6. The company-stack import plan proposes imports only, with zero additions,
   changes, or destructions.
7. Every inventory resource ID intended for migration is reconciled to exactly
   one company Terraform address.

A zero-diff plan proves correctness only for resources already represented in
that configuration. It does not prove inventory completeness.

## How to make this a real company fork

Create the fork in the approved company GitHub organization, then keep AWS's
repository as `upstream`:

```bash
git remote rename origin upstream
git remote add origin git@github.com:YOUR-COMPANY/aws2tf.git
git push -u origin company-safe-evaluation
```

Creating that remote fork and pushing are external changes and have not been
performed here.

For future upstream updates:

```bash
git fetch upstream
git rebase upstream/master
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m coverage run --source=company_safe \
  -m unittest discover -s tests
.venv/bin/python -m coverage report --fail-under=80
```

Re-run Terraform fixture validation before accepting the rebase. The cleanup
handlers are provider-schema-sensitive and can regress when Terraform AWS
provider schemas change.

## Offline inventory JSON support

Upstream does not read the existing inventory JSON. It is coupled to live
boto3 discovery and later asks the Terraform provider to read each live
resource. Adding `--inventory-dir` correctly is a larger feature, not a parser
flag.

The maintainable design is:

```text
LiveAwsDiscovery ---------+
                          +--> normalized resource model
InventoryJsonDiscovery ---+             |
                                        v
                              ownership filtering
                                        |
                                        v
                             HCL + import rendering
                                        |
                                        v
                             Terraform validation
```

Version the inventory schema and add one adapter per resource family. Start
with VPCs, subnets, route tables/associations/routes, TGWs, and RAM. The live
read-only plan remains mandatory for final proof because inventory JSON can be
stale and cannot reproduce every provider-computed value.

## Known work still required

- Add a general owner-account filter inside upstream's discovery boundary;
  the wrapper prevents apply but does not yet remove foreign-owned candidates.
- Surface provider diagnostics from `plan1.json` directly before the dependency
  loop emits its fallback message.
- Expand metadata-only regression coverage whenever upstream adds a new handler
  that downloads object bodies, credentials, code, or decrypted values.
- Add the versioned offline inventory discovery adapter if offline generation
  becomes a supported company workflow.
- Produce a hash-verified dependency lock before publishing the company fork.

Use only a company-approved read-only role. Read-only permissions can still
include sensitive read APIs, which is why metadata-only mode is enforced in
code rather than relying on the IAM role name.
