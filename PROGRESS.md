# PROGRESS

Current state of EvalGate. Read this first in every session. Update it before ending every session.

Three files, three jobs. BUILD_PLAN.md is the plan and rarely changes. DECISIONS.md is an append only log of rationale and measurements. This file is mutable current state and gets overwritten freely.

Last updated 2026-08-02.

---

## 1. Where we are

**P2 provisioning is done. The OCI node exists and runs k3s. P1.1 is down to its last step: the 96-case hand review.**

| Phase | Status |
|---|---|
| P0 Bootstrap | Done 2026-07-31 |
| P1.1 Data | **Teacher complete 2026-08-02, hand review pending.** All 1,900 answered, 97.6% valid, $2.89 of $5.00. Sample selected; 0 of 96 judged |
| P1.2 Training | Not started |
| P1.3 Serving | Not started |
| P1.4 Harness | Built against stubs 2026-08-01; needs a real endpoint at P1.3 |
| P2 Infra | **Provisioning done 2026-08-02.** Node live, k3s up, remote state. Deploys, monitoring, and k6 not started |
| P3 Automation | Gate workflow and threshold logic scaffolded 2026-08-01 |
| P4 Ingestion | Not started |
| P5 Analytics | Not started |
| P6 Optional | Not started |

**P2 is not closed.** BUILD_PLAN P2 also requires the api, Postgres on a block
volume PVC, and the model server deployed onto k3s; kube-prometheus-stack via
helm with `/metrics` on FastAPI and a committed Grafana dashboard JSON; and a k6
run with recorded numbers. Its exit criterion is "`terraform apply` goes from
zero to a running public API, dashboards live, k6 numbers recorded" — only the
first clause holds today. What is done is everything up to and including a
Ready k3s node reachable on 80/443.

## 2. Locked decisions

Project name EvalGate. Base model Qwen3 1.7B Instruct with thinking mode off. Task is grounded documentation QA over open source docs. Corpus repos are FastAPI, Pydantic, Prometheus, and Grafana. Teacher is gpt-5-mini through the Batch API. Judge is a different and stronger model, chosen at P1.4. Fine tuning runs on the M1 Pro through MLX as of 2026-08-01; the RTX 5080 Windows machine is out of the plan entirely. Everything is arm64 Apple Silicon in dev and arm64 Ampere in prod.

Full rationale for each of these lives in DECISIONS.md. Do not relitigate them here.

## 3. What exists right now

Fill this in as artifacts land. A new session should be able to read this section and know what is already built without grepping the tree.

- `pyproject.toml` uv virtual workspace root, members `apps/*`, `packages/*`, and `training`, Python 3.12 pinned, ruff and pytest configured centrally
- `packages/evalcore/`, `packages/sdk/`, `apps/api/` package skeletons with src layout, `py.typed`, and smoke tests
- `docker-compose.dev.yml` running `pgvector/pgvector:pg16` with a healthcheck and an init SQL that creates the vector extension
- `.github/workflows/ci.yml` with two jobs, both on `ubuntu-24.04-arm`. `lint and tests` and `dev stack has pgvector`
- `.pre-commit-config.yaml`, `README.md`, `.env.example`, `.gitignore`, MIT `LICENSE`
- `training/` is a real workspace member exposing the `evalgate-training` CLI. Subpackages `corpus/` (fetch, parse, embed), `questions/` (allocate, prompts, generate, verify), `retrieval/` (the single shared search path), `teacher/` (prompts, batch, validate), `golden/` (select, export), plus `budget.py`, `db.py`, `openai_batch.py`, `config.py`
- Corpus parsed, embedded, and loaded: 6,178 chunks, 1.43M tokens, all with pgvector embeddings. FastAPI 986, Pydantic 382, Prometheus 600, Grafana 4,210
- 1,900 questions generated and verified in Postgres. factual 600, howto 500, comparison 400, adversarial 400. All 16 (category, repo) cells exactly on target; repo split 31.1 / 35.0 / 18.9 / 15.0
- **All 1,900 teacher answers collected**, `gpt-5-mini` via the Batch API in 3 shards (840 + 843 + 197) plus the 20 dry-run rows. 1,854 valid, 97.6%. Guards final: `answered_absent` 0 of 400 adversarial, `fabricated_absent_side` 6 of 400 comparison. Comparison refusal rate 36.0%
- `training/artifacts/golden_manifest.json` holds the 96-case hand-review sample, 6 per (category, repo) cell, seed `evalgate-golden-v1`, no shortfall cells. Rerunning `golden select` rewrites it byte-identically
- `golden/review.py` and `golden/review_server.py` are the hand-review tool. `golden review` serves a localhost two-pane UI, one case per screen, citation markers linked to the chunks stored on the row; verdicts append to `golden_review.jsonl` and `golden summary` writes `golden_review_summary.json`. No model calls anywhere in it
- Top-ups backfill per (category, repo) cell using per-repo leak rates, not an aggregate gap. `questions trim` removes surplus from cells that overshoot
- Teacher regeneration capped at 2 attempts per row via `questions.teacher_attempts`, then the row is dropped
- Two dataset-poisoning guards in `teacher/validate.py`: `fabricated_absent_side` for one-sided comparisons, `answered_absent` for adversarial rows that did not refuse
- `config.decide_k()` holds the pre-committed retrieval-k rule; nothing about k is decided after seeing the measurement
- `training/artifacts/chunk_manifest.jsonl` and `ledger.json` committed. `training/.scratch/` holds vendored docs and is gitignored
- `packages/evalcore/` is the harness: `schema.py` (suite, case, thresholds, results), `scorers.py` (exact, regex, citation, refusal), `judge.py` (provider abstraction, SQLite cache keyed on case+output+rubric+judge version, full-jitter backoff), `runner.py` (`ModelClient` protocol plus `StubModel`/`EchoContextModel`), `diff.py` (case deltas and the breach logic), `report.py` (terminal, markdown PR comment, self-contained HTML), `loader.py`, `cli.py`
- `evalgate-eval` CLI: `build-suite` from the golden export, `run`, `gate`. Exit 1 from `gate` is the merge block
- `apps/api/` v0: register suites, submit runs, list runs, explicit baseline promotion per (suite, branch), `/diff`, and `/suites/{id}/gate` returning the verdict plus PR comment. `MemoryStore` now, Postgres DDL written in `store.py` for P2
- `.github/workflows/eval-gate.yml` scaffolded: builds the suite, runs the candidate, restores the judge cache, comments the diff (updating one sticky comment), uploads the report, fails on a breach
- `infra/terraform/` is real as of P2. `versions.tf` (provider, `~/.oci/config` auth), `variables.tf`, `main.tf` (data-source lookups for the existing VCN and subnet, NSG plus rules, the instance), `outputs.tf`, `backend.tf` + `backend.hcl.example` (remote state), `cloud-init/k3s.yaml.tftpl`, `retry-apply.sh`, `terraform.tfvars.example`, and a committed `.terraform.lock.hcl` pinning oracle/oci 8.25.0
- `retry-apply.sh` is the A1 capacity retry loop: cycles availability domains by index, classifies failures as capacity / throttle / fatal, backs off 3 min between ADs and 15 min per cycle with exponential backoff from 30 min on a 429, logs every attempt with a UTC timestamp to gitignored `logs/`. Refuses to run below 4 OCPU without `--allow-downsize`, and exits 0 early if an instance is already in state
- `cloud-init/k3s.yaml.tftpl` installs k3s and opens the host iptables ports OCI's Ubuntu image blocks by default. POSIX sh only, because cloud-init runs `runcmd` under dash
- `infra/k8s/`, `workers/`, and `analytics/` are still README placeholders and are not packaged yet

## 4. Environment state

- Dev machine Apple M1 Pro 16 GB, macOS 26. Docker runtime is Docker Desktop, with OrbStack installed but not selected
- **A native Postgres already owns 127.0.0.1:5432 on this machine.** A host-level listener shadows the container's port mapping, so `db init` fails with `role "evalgate" does not exist` against a perfectly healthy container. The dev stack therefore runs on **5433** via `POSTGRES_PORT=5433` in `.env`, with `DATABASE_URL` matching. The compose default stays 5432 for anyone else; this is per-machine config
- Training runs on this same M1 Pro through MLX. No second machine, no CUDA. `mlx-lm` is not installed yet; that happens at P1.2
- **Stop the dev Docker stack before P1.2 training.** The corpus is not needed once retrieved contexts are baked into the dataset, and 16 GB of unified memory is shared with macOS
- GitHub repo `starJin2003/EvalGate`, public, branch `main`, no branch protection yet since that is P3
- pre-commit hooks installed locally
- Accounts done. GitHub, OCI, **OpenAI API key in `.env`** (all P1.1 paid stages have run against it; $2.89 of $5.00 spent)
- Accounts pending. Hugging Face token, Databricks at P5, Cloudflare at P6
- **The $1 OCI budget alert exists**, confirmed 2026-08-02, with two rules: actual spend at 1% and forecast at 100%. BUILD_PLAN section 5 row 6 is satisfied
- `.env` keys in use. See `.env.example` for the current list

### OCI, as of 2026-08-02

- **Instance live.** `evalgate-k3s`, `VM.Standard.A1.Flex` at **4 OCPU / 24 GB**, 50 GB boot, `jSVO:US-CHICAGO-1-AD-1`, public `64.181.195.241`, private `10.0.0.94`, OCID `...xyefrpsq`. Image `Canonical-Ubuntu-24.04-aarch64-2026.06.29-0`
- **k3s v1.36.2+k3s1**, node Ready, 7/7 system pods healthy including traefik and svclb. containerd 2.3.2-k3s2, kernel 6.17.0-1018-oracle aarch64
- **The 4 OCPU / 24 GB PAYG allowance is confirmed real**, not the feared 2/12. This is the verification BUILD_PLAN section 12 made the Kafka sizing decision wait on, so that decision is now unblocked
- **Network exposure.** 22 open to the operator `/32` only (`admin_cidr`); 80 and 443 open to the internet via a separate `public_ingress_cidr`, deliberately public because P3's gate is called by GitHub Actions runners; **6443 has no ingress rule at all**. kubectl goes through an SSH tunnel: `terraform output kubectl_tunnel_command`
- **If your IP changes you are not locked out.** Edit the port 22 rule on the `evalgate-k3s-nsg` security group in the OCI console from any browser, then update `admin_cidr`. Never rebuild the instance to regain access; the shape is a capacity lottery
- **SSH key** is project-scoped at `~/.ssh/evalgate_ed25519`, not the machine default. No passphrase. Terraform reads only the `.pub` half
- Auth for the OCI provider comes from `~/.oci/config` (`oci setup config`). Nothing tenancy-specific is committed except through gitignored `terraform.tfvars`

### Terraform state, as of 2026-08-02

- **State is remote**, not local. OCI Object Storage bucket `evalgate-tfstate`, key `evalgate/p2/terraform.tfstate`, namespace `ax3mt2roo4ha`, private, **versioning enabled**. S3-native locking is on and verified
- OCI has no native Terraform backend; this is the `s3` backend against Object Storage's S3-compatible API. Config is split: `backend.tf` committed, `backend.hcl` gitignored (copy `backend.hcl.example`). Every `init` needs `-backend-config=backend.hcl`
- **`export AWS_REQUEST_CHECKSUM_CALCULATION=when_required` is mandatory.** Put it in your shell profile. Without it every state operation fails with `403 SignatureDoesNotMatch: The secret key required to complete authentication could not be found`, which points at credentials and is completely misleading — the real cause is a streaming checksum trailer OCI does not implement. `skip_s3_checksum` in the backend block and `request_checksum_calculation` in `~/.aws/config` both look like fixes and are not; the config-file key fixes the `aws` CLI only. `retry-apply.sh` exports it, so only manual `terraform` commands are exposed
- The S3 credential is an OCI **Customer Secret Key** (not the API signing key), stored in `~/.aws/credentials` under profile `evalgate-tfstate`, mode 600, never in the repo. Its access key is the key object's `id` field
- Newly created Customer Secret Keys take **~80 s** to become usable and fail with that same misleading 403 until they do
- Stale pre-migration local state kept as `infra/terraform/terraform.tfstate.migrated-to-oci`, gitignored, as a cold fallback

## 5. Blocked on Jinwoo

Anything waiting on a human goes here so a fresh session does not silently work around it.

- Hugging Face write token needs to be in `.env` before P1.2 can pull the base model
- **Golden set hand review, 96 cases. This is the only thing standing between here and P1.2.** The sample is selected and the tool is built: `uv run evalgate-training golden review` opens case 1 at `http://127.0.0.1:8765`. Resumable, so it can be done in sittings; `golden summary` shows progress at any point.
  **The acceptance rule is pre-committed** in DECISIONS.md, recorded 2026-08-02 before case 1 was judged, so the result cannot move the threshold. Short form, on the **overall** rate only: **>= 90%** (>= 87 of 96) proceed to P1.2 unchanged; **80-89%** (77-86) proceed only if the failures concentrate in criterion 4 and only after building the pgvector citation-target check and re-validating all 1,900 rows, but stop and fix the teacher if they spread across criteria 1-3; **< 80%** (<= 76) do not train, regenerate the affected categories. Separately, any criterion 3 failure on a **comparison refusal** is a generation bug to fix before P1.2; if there are none, the 36% comparison refusal rate is honest behaviour and the mix stays a design choice.
  Do not apply the rule to per-category rates (n=24, +/- 12 to 20 points) and do not read per-cell rates at all (n=6, no information). The sample is drawn from valid rows only, so it measures data that already cleared automated validation
- Add `export AWS_REQUEST_CHECKSUM_CALCULATION=when_required` to the shell profile. Terraform state operations fail without it, with an error that blames credentials

## 6. Known issues and deferred items

- ~~OCI Pay As You Go may allocate only 2 OCPU and 12 GB~~ **Resolved 2026-08-02. 4 OCPU / 24 GB launched and is running.** No shrink needed, and the 2 OCPU contingency is off the table unless the instance is ever lost
- Kafka memory budget and single versus dual instance was deferred until that verification. **The verification is done, so this decision is now unblocked** and can be made whenever P4 starts. It still needs a human
- **A1 capacity is a lottery and this instance is not replaceable on demand.** All three ADs were returning "Out of capacity" hours before it launched. Never `terraform destroy` or taint the instance to pick up a config change. `ignore_changes` covers the image OCID and `metadata["user_data"]` for exactly this reason; bootstrap fixes are applied to the live node over SSH and land in the template for the next build
- The retry loop's capacity and throttle branches have **never executed against a live OCI error**. It succeeded on attempt 1, so those paths are still only tested against recorded error strings and the dry-run harness. Treat the backoff timings as unvalidated if the loop is ever needed for real
- Databricks Free Edition external access path for pushing files and running dbt from outside is unverified. Check at the start of P5
- `pre-commit run --all-files` reported nothing before the first commit because the scaffold was untracked. Resolved, hooks engage from the first commit onward

## 7. Next action

Two independent threads.

**P1.1, one step left and it needs a human.** The teacher stage is closed: 1,900 answers, 97.6% valid, $2.89 spent. `golden select` has run, so `artifacts/golden_manifest.json` fixes the 96 cases. The remaining step is the hand review itself:

```bash
docker compose -f docker-compose.dev.yml up -d      # the tool reads chunks from Postgres
uv run evalgate-training golden review              # opens case 1; quit and rerun anytime
uv run evalgate-training golden summary             # pass rate so far
```

Then `dataset export` and P1.2 begins. P1.4 and the P3 gate are already built against stubs; P1.2 (MLX) and P1.3 (GGUF) are the next real work, and the only change P1.4 needs afterwards is pointing the runner at a real endpoint.

**P2 remainder, now unblocked by a running node.** In rough order: deploy Postgres onto k3s with a block volume PVC; deploy `apps/api` (needs an arm64 image and `store.py`'s Postgres DDL wired up in place of `MemoryStore`); add `/metrics` to FastAPI and install kube-prometheus-stack via helm, sized to 24 GB; commit a Grafana dashboard JSON; write and run the k6 script and record RPS, p95, error rate, and node CPU/RAM idle versus loaded. The model server waits on P1.3 producing a GGUF.

Nothing in P2's remainder blocks P1.1, and P1.1 does not block P2's remainder except for the model server.

## 8. Session log

Newest first. Three to five lines each. What was attempted, what landed, what broke, what the next session needs to know.

**2026-08-02 P1.1 Data, teacher stage closed and the review tool built.** Two jobs. First, reconciling the docs with reality: PROGRESS claimed the teacher batch was part 1 of 2 with ~197 rows left to submit, when it had actually finished in **3** shards (840 + 843 + 197) some sessions earlier and nobody wrote it down. Every headline number was re-derived from Postgres and `ledger.json` rather than promoted from memory, and all six agreed: **1,900 of 1,900 answered, 1,854 valid (97.58%), `answered_absent` 0 of 400, `fabricated_absent_side` 6 of 400, comparison refusal 36.0%, ledger $2.8930 of $5.00.** The interesting one is the split by category — adversarial is 100% valid and `howto` is worst at 94.0%, and every one of `howto`'s 30 failures is an uncited sentence rather than a grounding failure, so citation discipline, not hallucination, is the only failure mode that survived at scale. Second, the review tool. `golden select` now balances **6 per (category, repo) cell** instead of 24 per category and writes a manifest before any judging starts, so the sample is a commitment rather than something that can shrink once the verdicts look bad; selection is a sha256 sort over a fixed seed with no RNG, verified byte-identical across reruns. `golden review` serves a two-pane localhost UI — answer on the left, the row's stored chunks on the right, every `[C3]` a link that opens its chunk — with verdicts appended to JSONL after each judgment so quitting mid-review costs nothing. It calls no model and deliberately hides the validator's own flags, since showing `uncited_sentence` before the reviewer reads the answer would anchor exactly the criterion being judged. The old localStorage review page is deleted rather than kept: two verdict stores that can disagree is worse than one. **P1.1's only remaining step is a human reading 96 cases.**

**2026-08-02 P2 Infra, provisioning and remote state.** Started P2 as the retry mechanism for the A1 capacity lottery, since all three us-chicago-1 ADs were refusing 4 OCPU / 24 GB and manual retries had tripped the OCI rate limit. Wrote the terraform (existing VCN and subnet looked up, never created; rules in an NSG on the instance VNIC so `destroy` leaves shared infra untouched) plus `retry-apply.sh`, which cycles ADs and separates capacity from throttle from fatal. It won on **attempt 1 in 37 s**, so the backoff paths are still untested against live errors — logged as such rather than treated as validation. **4 OCPU / 24 GB confirmed on PAYG**, which unblocks the deferred Kafka sizing decision. Booting it for real found two bugs no amount of static validation would have: `/opc/v2/vnics/` has no `publicIp` field, so cloud-init aborted 42 s in and k3s never installed; and `kubectl wait --all` fails instead of waiting when no node has registered yet. Both fixed, applied to the live node over SSH rather than by replacing an instance that cannot be re-obtained on demand. Then closed 6443 to the internet entirely (kubectl now tunnels over SSH), narrowed 22 to a `/32`, and split the public web ports into their own variable. Finally migrated state off the laptop into a versioned OCI Object Storage bucket with S3-native locking, verified by concurrent plans. **The one thing to remember: `export AWS_REQUEST_CHECKSUM_CALCULATION=when_required`, or every state operation fails as a fake credential error.** P2 provisioning is done; deploys, monitoring, and k6 are not.

**2026-08-01 P1.1 Data, question set final.** Per-repo leak rates measured by replaying stored batch outputs: Grafana 34.2%, Pydantic 27.5%, FastAPI 20.6%, Prometheus 19.0%. Grafana confirms leak tracks corpus density; Pydantic contradicts it with the smallest corpus and second-highest rate, driven by regular API naming. Aggregate top-ups had drifted adversarial off its split (FastAPI +10, Grafana -15); backfill is now per (category, repo) cell with per-repo overshoot, and it converged in one round. Final: 1,900 questions, all 16 cells exact. Ledger $0.1674 of $5.00. Still no teacher spend.

**2026-07-31 P1.1 Data, questions complete.** Corpus embedded (6,178 chunks, $0.0285, exactly as projected). Question generation stratified by repo into a [15%, 35%] band because the corpus is 68% Grafana; proportional sampling would have trained a Grafana model. 1,907 questions for $0.1363 across 3 batches, 0 failed calls. Headline finding: gpt-4.1-mini invents an adversarial symbol that actually exists in the corpus **26% of the time**, measured three times (26.0 / 26.7 / 13.3), so filling the refusal category costs 1.38x its nominal size. Two poisoning guards added and tested. Stopped before teacher spend; ledger at $0.1648 of $5.00.

**2026-07-31 P0 Bootstrap.** Scaffolded the uv workspace, pgvector dev stack, and arm64 CI. Two problems, both logged in DECISIONS.md. Three members all named their test file `test_smoke.py` and collided under pytest prepend import mode, fixed with `--import-mode=importlib`. CI run #1 green in 20 seconds total.
