# infra/terraform

OCI provisioning for P2. One `VM.Standard.A1.Flex` at 4 OCPU / 24 GB running
Ubuntu 24.04 aarch64, with cloud-init installing k3s.

The Ampere A1 free tier is a capacity lottery. Every AD in `us-chicago-1`
currently answers `Out of capacity for shape VM.Standard.A1.Flex`, and manual
retries hit the OCI rate limit. `retry-apply.sh` is the mechanism that wins that
lottery: it cycles the ADs, backs off far enough to stay under the rate limit,
and exits the moment an instance launches.

```
main.tf                     data sources, NSG, instance
variables.tf                inputs and their validation
outputs.tf                  IP, AD, and copy-paste follow-up commands
versions.tf                 provider and auth
backend.tf                  remote state on OCI Object Storage (partial config)
backend.hcl.example         copy to backend.hcl, gitignored
cloud-init/k3s.yaml.tftpl   k3s bootstrap
retry-apply.sh              the capacity retry loop
terraform.tfvars.example    copy to terraform.tfvars
logs/                       one file per day, gitignored
```

## What this creates, and what it only reads

Created and owned here:

- a network security group in the existing VCN, plus its rules
- the compute instance and its boot volume

Read only, never created or modified:

- the VCN `evalgate-vcn` and its public subnet, both looked up by display name

The security rules go in an **NSG attached to the instance VNIC** rather than
into the subnet's security list. The VCN is shared, pre-existing infrastructure;
editing its security list would mean this configuration mutates something it
does not own, and `terraform destroy` would leave the VCN altered. The NSG is
additive and disappears cleanly with the instance.

## Network exposure

| Port | Source | Variable | Why |
|---|---|---|---|
| 22 | your address only | `admin_cidr` | The single administrative entry point, and the transport for the kube API tunnel |
| 80, 443 | the whole internet | `public_ingress_cidr` | The traefik ingress. Public **by design** — from P3 the eval gate is called by GitHub Actions runners, whose egress addresses rotate across a range too large to allowlist |
| 6443 | **nobody** | — no rule exists | The k3s API is not on the internet at all |

The two CIDRs are separate variables that never reference each other. Tightening
SSH cannot take the public API offline, and opening the web ports cannot widen
administrative access. `admin_cidr` has a validation rule rejecting `0.0.0.0/0`
outright, so world-open SSH takes a deliberate edit rather than a forgotten
default.

### Reaching the kube API

There is no ingress rule for 6443, so `kubectl` goes through an SSH tunnel. The
node's kubeconfig already points at `127.0.0.1:6443`, which is exactly what the
tunnel forwards, so it needs no editing:

```bash
eval "$(terraform output -raw fetch_kubeconfig_command)"   # once

eval "$(terraform output -raw kubectl_tunnel_command)"     # leave running
# in another terminal:
KUBECONFIG=~/.kube/evalgate.yaml kubectl get nodes -o wide
```

Close the tunnel and `kubectl` fails with connection refused. That is the
intended and verified behaviour: there is no second path to the API.

### Locked out because your IP changed? Do not panic

**You cannot lock yourself out of recovery, because recovery is a browser, not
SSH.** Edit the NSG rule from the OCI console anywhere:

> **Networking** → **Virtual cloud networks** → `evalgate-vcn` → **Network
> security groups** → `evalgate-k3s-nsg` → **Security rules** → edit the port 22
> ingress rule's source CIDR.

Then set `admin_cidr` in `terraform.tfvars` to the new value so the next
`terraform apply` does not revert your console edit. Get the current address
with `curl -s https://checkip.amazonaws.com`.

Never rebuild the instance to regain access. On this shape, replacing it means
re-entering the A1 capacity lottery with no guarantee of getting another.

## State backend

State lives in OCI Object Storage, not on a laptop. The instance took a capacity
lottery to obtain; if the only copy of `terraform.tfstate` were a local file,
losing it would mean terraform no longer knows the instance exists, and
recovering would be a hand-written `terraform import` for every resource. A
remote backend also has to exist before P3, when CI needs to read the same state.

OCI has no native Terraform backend, so this uses the standard **`s3` backend
against Object Storage's S3-compatible API**. Configuration is split: `backend.tf`
holds the parts that are true for anyone, and the gitignored `backend.hcl` holds
the bucket and namespace, which only exist in one tenancy.

### One-time setup

```bash
NS=$(oci os ns get --raw-output --query data)
TENANCY=<your tenancy OCID>

# Private, versioned. Versioning is the point: a corrupted or truncated state
# write is recoverable from a prior object version instead of being terminal.
oci os bucket create \
  --compartment-id "$TENANCY" --namespace "$NS" \
  --name evalgate-tfstate \
  --public-access-type NoPublicAccess \
  --versioning Enabled
```

The S3 API needs an S3-shaped credential, which is not the API signing key used
everywhere else. Create a **Customer Secret Key** — console path is *profile menu
→ My profile → Customer secret keys → Generate secret key*, or:

```bash
oci iam customer-secret-key create --user-id <your user OCID> \
  --display-name evalgate-tfstate
```

The secret is shown **once**. Put it in `~/.aws/credentials` (mode `600`), never
in the repo:

```ini
[evalgate-tfstate]
aws_access_key_id     = <the "id" field of the created key>
aws_secret_access_key = <the "key" field, shown only at creation>
```

The access key is the key object's `id`, which is unintuitive — it is not a
separate value shown next to the secret.

Then point the backend at it and initialise:

```bash
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required   # REQUIRED, see below

cp backend.hcl.example backend.hcl
$EDITOR backend.hcl                 # bucket, region, profile, namespace endpoint
terraform init -backend-config=backend.hcl
```

Every later `init` needs that same `-backend-config=backend.hcl` flag;
`retry-apply.sh` passes it automatically and refuses to run if the file is
missing.

### The one environment variable you must export

```bash
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required
```

Put it in your shell profile. Without it **every terraform command touching
state fails**, and the error tells you almost nothing useful:

```
403 SignatureDoesNotMatch: The secret key required to complete
authentication could not be found.
```

That points squarely at credentials, which will be perfectly fine. The real
cause is that the AWS SDK adds a streaming checksum trailer OCI does not
implement; on a write you sometimes get the honest version of the error,
`NotImplemented: AWS chunked encoding not supported`, but on a read it is
laundered into a signature failure.

Three things that do **not** fix it, all of which look like they should:

- `skip_s3_checksum = true` in the backend block — set, and insufficient
- `request_checksum_calculation = when_required` in `~/.aws/config` — fixes the
  `aws` CLI, but terraform's embedded SDK does not read it
- passing `--profile` or any credential change — the credential is not the problem

`retry-apply.sh` exports it, so the unattended path is safe. Manual `terraform`
commands are the exposure.

### Locking

`use_lockfile = true` enables S3-native locking: terraform writes a
`.tflock` object with a conditional PUT, so a second writer is rejected rather
than silently racing. Verified against OCI — two concurrent plans produce
`412 PreconditionFailed` on the loser, with the holder recorded:

```
Error acquiring the state lock
  ID:        98c7ec5b-...
  Operation: OperationTypePlan
  Who:       jin@...
```

This matters from P3, when a CI runner and a laptop can both hold state. If a
crash ever leaves a stale lock, `terraform force-unlock <ID>` clears it —
confirm nothing else is actually running first.

### If the bucket is lost

Object versioning is enabled, so overwritten state is recoverable from a prior
version rather than gone. The last local state before migration is kept at
`terraform.tfstate.migrated-to-oci` (gitignored) as a cold fallback. If both
were somehow lost, recovery is `terraform import` per resource — which is the
entire reason this backend exists, since the instance itself cannot simply be
rebuilt on demand.

### Why the flags in backend.tf

The `s3` backend assumes AWS, and every setting there is about it not being AWS:
`use_path_style` because OCI serves `/<bucket>/<key>` rather than a virtual-host
subdomain; `skip_s3_checksum` because the AWS SDK's integrity headers are
rejected by OCI's S3 layer and **every state write fails without it**; and the
`skip_*` validation flags because each one calls an AWS-only endpoint (STS, IMDS,
the region catalogue) that does not exist here.

**Newly created Customer Secret Keys are not usable immediately.** Propagation
takes a few minutes, and until it completes every request fails with
`SignatureDoesNotMatch: The secret key required to complete authentication could
not be found`, which reads like a wrong password rather than a delay. If the
credential is freshly minted, wait and retry before debugging it.

## Prerequisites

```bash
brew install terraform oci-cli
oci setup config          # writes ~/.oci/config, then upload the public key

# Project-scoped SSH key. Separate from the OCI API key, and deliberately not
# ~/.ssh/id_ed25519, so this stack neither depends on nor claims the machine's
# default identity. Terraform only ever reads the .pub half.
ssh-keygen -t ed25519 -f ~/.ssh/evalgate_ed25519 -N "" -C "evalgate-oci-k3s"
```

`oci setup config` prompts for the user OCID, tenancy OCID, region
(`us-chicago-1`), and offers to generate a keypair. **Leave the passphrase
empty** — the retry loop runs unattended and cannot answer a prompt. Then:

```bash
cat ~/.oci/oci_api_key_public.pem | pbcopy
# Console: profile menu, My profile, Tokens and keys, Add API key, paste.
oci iam availability-domain list --output table   # must print 3 rows
```

## Running it

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
# Two required values: tenancy_ocid, and admin_cidr as <your-ip>/32.
# admin_cidr rejects 0.0.0.0/0, so this is a deliberate choice, not a default.
$EDITOR terraform.tfvars

cp backend.hcl.example backend.hcl   # see "State backend" above
$EDITOR backend.hcl
terraform init -backend-config=backend.hcl
terraform validate

# Read-only. Resolves every data source and proves the VCN, subnet, image, and
# SSH key all exist, without calling LaunchInstance or touching the rate limit.
terraform plan

./retry-apply.sh
```

Always `plan` before starting the loop. A misconfiguration caught here costs
seconds; the same mistake caught inside the loop is classified `fatal`, stops
the run, and wastes however long it took you to notice.

Leave it running. It is designed to be left alone for hours or days:

```bash
nohup ./retry-apply.sh > /dev/null 2>&1 &
tail -f logs/retry-$(date -u +%Y%m%d).log
```

| Flag | Default | Meaning |
|---|---|---|
| `--max-attempts N` | `0` (unlimited) | Stop after N attempts |
| `--attempt-delay S` | `180` | Seconds between ADs inside one cycle |
| `--cycle-delay S` | `900` | Extra seconds after a full pass over all ADs |
| `--allow-downsize` | off | Permit `ocpus < 4`. Requires a DECISIONS.md entry |
| `--dry-run` | off | Exercise the loop with no OCI calls |

`THROTTLE_DELAY_BASE` (1800), `MAX_DELAY` (7200), and the three above can also
be set as environment variables.

### The backoff

One full cycle is three attempts spaced 3 minutes apart, then a 15 minute pause
before starting over. That is roughly **4 attempts per hour**, deliberately slow.
The rate limit was already tripped by manual retries, and a tighter loop trades
a small gain in coverage for the chance of being throttled out of the game
entirely.

Errors are classified into three buckets and treated differently:

| Class | Matches | Response |
|---|---|---|
| `capacity` | `Out of capacity`, `OutOfHostCapacity` | Advance to the next AD, normal delay |
| `throttle` | `TooManyRequests`, `429`, `rate limit` | **Hold the AD**, back off exponentially from 30 min, capped at 2 h |
| `fatal` | `LimitExceeded`, `NotAuthenticated`, anything else | Stop and print the terraform output |

Two details that matter. Throttling does **not** advance the AD, because being
rate limited says nothing about whether the AD just tried had capacity; skipping
it would drop that AD from the rotation for no reason. And a service limit is
classified fatal rather than retried, because retrying it forever cannot succeed
and burns the rate-limit budget the capacity retries need.

Anything unrecognised is fatal, not retryable. A loop that retries unknown
errors forever looks like it is working while making no progress.

### After it succeeds

`terraform apply` returns when the instance launches, but cloud-init still has
a few minutes of work.

```bash
terraform output                                    # IP, AD, ssh command
eval "$(terraform output -raw bootstrap_status_command)"
```

`/var/lib/evalgate/bootstrap.done` appears only after k3s is up, the node is
Ready, and the kubeconfigs are written. Until then, tail
`/var/log/evalgate-bootstrap.log` on the node. The whole bootstrap is a single
`runcmd` block so a failure partway through cannot leave the `.done` marker
behind.

Then pull the kubeconfig and open a tunnel — see [Reaching the kube
API](#reaching-the-kube-api) above.

Record in DECISIONS.md: attempts and total elapsed time from the retry log,
which AD paid out, apply wall time, and the k3s version that landed.

## Design notes

**AD selection is an index, not a name.** `availability_domain_index` is cycled
by the wrapper and resolved against the AD list inside terraform, so the loop
never needs to know AD names up front and cannot desync from what the region
reports. It wraps modulo the AD count, so a single-AD region still applies.

**The image is resolved by name, not pinned to an OCID.** Image OCIDs are
per-region and rotate with every Canonical release. Filtering on the A1 shape is
what makes the result aarch64; "Minimal" builds are excluded because they omit
cloud-init modules used here.

**`ignore_changes` on the image OCID and on `metadata["user_data"]`.** A new
Ubuntu release must never destroy and recreate an instance that took days of
retries to obtain, and neither should editing the bootstrap. `user_data` runs
once, at first boot, so a running node cannot be affected by changing it — the
diff has no upside and one very large downside. Fixes go to the live node
directly (see below) and land in the template for the next build. The ignore is
scoped to that one map key, so `ssh_authorized_keys` still updates in place and
SSH key rotation remains a normal apply.

**Fixing the bootstrap on a running node.** Because cloud-init only runs at
first boot, a template fix does not reach an existing instance. Render the
`runcmd` block and run it over SSH rather than replacing the instance:

```bash
python3 -c "import yaml,sys; d=yaml.safe_load(open('/tmp/rendered.yaml')); print(d['runcmd'][0])" > /tmp/bootstrap.sh
scp -i ~/.ssh/evalgate_ed25519 /tmp/bootstrap.sh ubuntu@<ip>:/tmp/
ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<ip> 'sudo sh /tmp/bootstrap.sh'
```

The k3s install script and the port script are both re-runnable, so this is
safe to repeat.

**`create` timeout of 15 minutes**, below the 20 minute provider default, so a
stalled launch returns to the loop and moves to the next AD instead of parking
the retry cycle.

**Preconditions on VCN, subnet, and image** resolve before OCI is asked for
capacity, so a failed attempt fails on capacity alone and a typo'd VCN name
surfaces as a readable message instead of a capacity-shaped mystery. The subnet
data source deliberately lists the whole compartment and filters by VCN in
`locals`, because passing a null `vcn_id` to the provider produces an opaque
error before any precondition can run.

**The cloud-init `runcmd` block is POSIX sh, not bash.** cloud-init executes
runcmd through `/bin/sh`, which is dash on Ubuntu, and dash rejects `set -o
pipefail` with "Illegal option". Where pipefail would have mattered, the
`curl | sh` pipeline is split into a download and a run, so a failed fetch
cannot be silently piped into a shell.

**Host firewall.** OCI's Ubuntu images ship an iptables ruleset that rejects
everything except SSH, ahead of anything k3s adds. Without the ports opened in
cloud-init the cluster comes up and is unreachable, and the symptom looks like a
broken CNI. The NSG controls what reaches the host; the host rules control what
the host accepts.

## Do not downsize to work around capacity

4 OCPU / 24 GB is the Always Free allowance on a PAYG tenancy and what this
project is sized for. `Out of capacity` means the AD has no A1 hardware free
right now. It is not a signal that the request is too large.

Dropping to 2 OCPU / 12 GB is the documented BUILD_PLAN.md P2 contingency and a
DECISION POINT: it needs a human decision and a DECISIONS.md entry, because P4's
Kafka sizing depends on the number. `retry-apply.sh` refuses to run below 4 OCPU
without `--allow-downsize`, and warns in the log when that flag is used.
