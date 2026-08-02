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

## Prerequisites

```bash
brew install terraform oci-cli
oci setup config          # writes ~/.oci/config, then upload the public key
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
$EDITOR terraform.tfvars          # tenancy_ocid is the only required value
terraform init

./retry-apply.sh
```

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

Then pull the kubeconfig:

```bash
eval "$(terraform output -raw fetch_kubeconfig_command)"
```

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

**`ignore_changes` on the image OCID.** A new Ubuntu release must never destroy
and recreate an instance that took days of retries to obtain. Rebuilding onto a
newer image is a deliberate act, not a side effect of running `apply` again.

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
