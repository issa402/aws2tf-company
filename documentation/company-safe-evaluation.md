# Company-safe aws2tf evaluation

## Decision

Use this checkout as an isolated discovery, candidate-HCL generation, and
coverage-comparison tool. Do not make its local Terraform state the company's
authoritative state, and do not copy generated files automatically into
`us-platform-infra`.

The company-owned migration pipeline remains the source of truth:

1. `inventory-network.sh` proves discovery completeness.
2. Ownership checks distinguish locally owned, RAM-visible, CloudFormation,
   and already-Terraform-managed resources.
3. Reviewed HCL and import blocks land in the company stack.
4. The company stack's central backend and approval workflow perform adoption.

`aws2tf` is a second opinion and an accelerator inside that process.

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
- requires an explicit profile, Region, and resource type;
- verifies the active account through STS before starting generation;
- rejects unrestricted `all`, `stack`, and broad `ec2` scans;
- uses a positive networking allowlist, so aliases or direct resource types
  outside the approved VPC/TGW/RAM/firewall/DNS/networking families fail closed;
- always adds upstream `--validate`, stopping before Terraform import apply;
- never adds `--accept`;
- writes a private `0600` run manifest with provenance.

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

## First safe pilot

Use a company-approved read-only SSO permission set. Never paste credentials
into a file or commit them.

First verify and preview the exact command:

```bash
.venv/bin/python company_safe.py \
  --expected-account 123456789012 \
  --profile NETWORK_READONLY \
  --region us-east-1 \
  --type aws_vpc \
  --id vpc-0123456789abcdef0 \
  --dry-run
```

If the account, Region, type, and ID are correct, remove `--dry-run` to run the
validation-only generation pipeline:

```bash
.venv/bin/python company_safe.py \
  --expected-account 123456789012 \
  --profile NETWORK_READONLY \
  --region us-east-1 \
  --type aws_vpc \
  --id vpc-0123456789abcdef0
```

This intentionally does not import the resource into local state. Review the
generated HCL, `plan1.json`, owner identifiers, and the run manifest first.

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
- Disable local secret/user-data/package extraction inside upstream itself,
  rather than relying only on wrapper scope blocking.
- Add the versioned offline inventory discovery adapter if offline generation
  becomes a supported company workflow.
- Produce a hash-verified dependency lock before publishing the company fork.

Do not run a full account scan or use write-capable credentials until these
controls have been reviewed and approved.
