# PROGRESS

Current state of EvalGate. Read this first in every session. Update it before ending every session.

Three files, three jobs. BUILD_PLAN.md is the plan and rarely changes. DECISIONS.md is an append only log of rationale and measurements. This file is mutable current state and gets overwritten freely.

Last updated 2026-08-02.

---

## 1. Where we are

**P1.2 is prepared and verified but not trained. Dataset split, environment installed, memory envelope measured, smoke test green. The full run has deliberately not been started.**

| Phase | Status |
|---|---|
| P0 Bootstrap | Done 2026-07-31 |
| P1.1 Data | **Done 2026-08-02.** 1,900 answered, 1,854 valid (97.6%), $2.89 of $5.00. Hand review 92/96 = 95.8%, criteria 3 and 4 clean. Acceptance rule fired as written |
| P1.2 Training | **Probe done 2026-08-02, LR confirmed, long runs not started.** 600 iters at 5e-5: full-split val 5.4749 → 1.0056, monotonic, 81.6% drop. Schedule locked at 2,956 iters. Next is P1.3 against the probe adapter, then v1 and v2 back to back |
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
- **P1.2 training splits.** `training/artifacts/dataset/{train,valid,test}.jsonl` (1,478 / 140 / 140). Those three filenames are what `mlx_lm.lora --data <dir>` requires; `valid.jsonl` is not optional. mlx-lm chat format, teacher system prompt preserved, golden 96 excluded
- **`training/artifacts/recovery.jsonl`** is the paid state: 1,900 rows, 2.0 MB, sha256 `8641b0f6…`. Rebuild it with `dataset recovery-export` (needs Postgres); prove it with `dataset restore` (needs no Postgres). **`dataset restore --target training/artifacts/dataset` reconstructs the training inputs from committed files alone, so P1.2 never needs the database**
- **The splits are gitignored; `dataset_manifest.json` is the committed artifact.** 56 KB, and it carries per-split `question_id` lists plus a sha256 of each rendered file, so the split is reconstructible and a rebuild is provable. Rebuild with `dataset export` (needs Postgres), then `dataset verify` to confirm byte-identity against the manifest. Current digests: train `54b9e5c7…`, valid `c7b9458d…`, test `b5baa051…`
- **Do not re-add the rendered splits to git.** 23 MB, ~95% duplicated chunk text, and committing rendered upstream docs is what tripped GitHub push protection on a Grafana placeholder token on 2026-08-02
- `training/src/evalgate_training/dataset/build.py` builds them: 8% + 8% held out per (category, repo) cell, ordered by `sha256(DATASET_SPLIT_SEED | question_id)` with no RNG, so a rerun on the same population is byte-identical. `plan()` is pure and covered by `training/tests/test_dataset_split.py`
- **mlx-lm is in the `mlx` dependency group, not `dev` and not `training`'s dependencies.** MLX is Apple-Silicon-only and CI syncs on `ubuntu-24.04-arm`. Install locally with `uv sync --group mlx`; the platform marker makes it a no-op elsewhere
- **The hand review is complete and its artifacts are committed.** `training/artifacts/golden_review.jsonl` holds 96 judgment records (96 judged, 0 unjudged, no rejudgments) and `golden_review_summary.json` the derived rates: 92/96 = 95.8% overall; adversarial 24/24, comparison 22/24, factual 23/24, howto 23/24; grafana and pydantic 24/24, prometheus 23/24, fastapi 21/24. Failures by first failed criterion: completeness 2, groundedness 2, refusal validity 0, citation accuracy 0
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
- Training runs on this same M1 Pro through MLX. No second machine, no CUDA. **mlx-lm 0.31.3 / mlx 0.32.0 installed 2026-08-02** via `uv sync --group mlx`; Metal available, arm64, no PyTorch pulled in
- **Metal's recommended working set on this machine is 12.71 GB, not 16.** `mx.device_info()['max_recommended_working_set_size']`. Exceeding it does not fail — it swaps, and the run degrades to 20-45 s/step with `nan` losses that look like a numerical bug. Treat 12.71 GB as the real ceiling
- **Stop the dev Docker stack before P1.2 training, but after `dataset export`.** The export reads chunks and answers from Postgres, so the order is export first, then `down`. Once the retrieved contexts are baked into the split files nothing in training touches the database
- **The base model is downloaded.** `mlx-community/Qwen3-1.7B-bf16`, 3.2 GB in `~/.cache/huggingface/`, 594 GB free. The 8-bit variant is also cached from the precision comparison
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

- ~~Hugging Face write token~~ **Cleared 2026-08-02.** Token is in `.env` and the base model pulled
- **Sequence-length sign-off, the one thing blocking the real P1.2 run.** Recommended `--max-seq-length 6528` with `--grad-checkpoint`: 0 of 1,758 rows truncated (max is 6,391), peak 10.86 GB at 8-bit or 12.55 GB at bf16 against a 12.71 GB ceiling. Not written into `config.py` — no default was set pending this call. See section 7 for the two open knobs that go with it
- Add `export AWS_REQUEST_CHECKSUM_CALCULATION=when_required` to the shell profile. Terraform state operations fail without it, with an error that blames credentials

## 6. Known issues and deferred items

- ~~STANDING RISK: questions and teacher answers exist only in the Postgres volume~~ **Closed 2026-08-02 by `training/artifacts/recovery.jsonl`.** 1,900 rows, 2.0 MB, committed: question, absent_symbol, split, retrieved chunk **ids**, answer, refused, citations, valid, validation_errors. No chunk text. Restore is proven, not asserted — `dataset restore` ran with Postgres stopped, re-parsed 6,178 chunks with 0 hash mismatches against `chunk_manifest.jsonl`, and rebuilt all three splits byte-identical to `dataset_manifest.json`. Six tests guard it in CI, including an 8-pattern secret sweep and the golden-96 join
- **Open question carried into the P1.2 eval, from the hand review: fastapi took 3 of the 4 failures at 21/24.** Per-repo n is 24 (+/- ~13 points), so this is a direction to check, not a measured gap. Look for it in the P1.2 eval; do not act on it in the data
- **Accepted risk carried into the P1.2 eval: comparison-avoidance bias.** The comparison category refuses 36.0% of the time and that mix was deliberately left as is (DECISIONS, 2026-08-02), so the trained model may decline comparisons it could answer. Measure it after training — if comparison refusals come out above the teacher's 36%, the mix is the first thing to change. All 5 comparison refusals in the sample were valid, so the teacher's own bias is toward under-refusing, not over-refusing
- ~~OCI Pay As You Go may allocate only 2 OCPU and 12 GB~~ **Resolved 2026-08-02. 4 OCPU / 24 GB launched and is running.** No shrink needed, and the 2 OCPU contingency is off the table unless the instance is ever lost
- Kafka memory budget and single versus dual instance was deferred until that verification. **The verification is done, so this decision is now unblocked** and can be made whenever P4 starts. It still needs a human
- **A1 capacity is a lottery and this instance is not replaceable on demand.** All three ADs were returning "Out of capacity" hours before it launched. Never `terraform destroy` or taint the instance to pick up a config change. `ignore_changes` covers the image OCID and `metadata["user_data"]` for exactly this reason; bootstrap fixes are applied to the live node over SSH and land in the template for the next build
- The retry loop's capacity and throttle branches have **never executed against a live OCI error**. It succeeded on attempt 1, so those paths are still only tested against recorded error strings and the dry-run harness. Treat the backoff timings as unvalidated if the loop is ever needed for real
- Databricks Free Edition external access path for pushing files and running dbt from outside is unverified. Check at the start of P5
- `pre-commit run --all-files` reported nothing before the first commit because the scaffold was untracked. Resolved, hooks engage from the first commit onward

## 7. Next action

Two independent threads.

**P1.2 v1 training run, blocked only on the sequence-length sign-off.** Everything upstream is done: splits written, environment installed, base model cached, smoke test green end to end at 9.1 GB peak with loss falling and no `nan`. The dev stack is already stopped. The run itself has deliberately not been started.

The command, once the length is agreed:

**The probe is done and 5e-5 is confirmed. Work is now strictly sequenced — nothing runs alongside a long run.**

1. **P1.3 pipeline against the 600-iter probe adapter**, with nothing else resident. Fuse, convert to GGUF, quantize Q4_K_M, serve with llama-server, and confirm the served model emits `[C1]`-style citations and refuses. The probe adapter is structurally valid for all of this; its *weights* are 0.41 epochs and must never become v1 or appear in a reported number. `training/.scratch/fused-probe/` already holds a fused 8-bit copy from the memory measurement.
2. **Then v1**, 2,956 iters, alone. **Then v2**, same schedule, alone.

**Why not in parallel — the earlier claim was wrong.** Training peaks at 11.030 GB against the 12.71 GB working set, leaving 1.68 GB. `mlx_lm.fuse` peaks at **3.311 GB** measured; llama-server at Q4_K_M with a 6,528 context needs **~2.1-2.4 GB** (~1.1-1.3 GB weights plus 0.75 GB KV cache, at 0.109 MB per token for 28 layers x 8 KV heads x 128 head_dim x 2 bytes x 2). Either alone is about double the headroom, and the failure mode is not a clean OOM — swap pressure is what produced the `nan` on the first bf16 attempt, so the cost is a corrupted 9-hour run that reads like a numerical bug.

**One network step before going offline.** `mlx_lm.fuse` resolves the model with `local_files_only=True` and demands a *complete* snapshot, while `load()` skips `.gitattributes` and `README.md`. Run `snapshot_download('mlx-community/Qwen3-1.7B-8bit')` once with network; fuse then works offline. Training itself is verified network-free.

**Checkpoint selection is pre-committed** (DECISIONS.md) and applies identically to v1 and v2: score every numbered checkpoint plus the final weights on the **full 140-row valid split** with `train eval-adapters`, take the lowest, break ties within 0.01 toward the earlier checkpoint. Never use the in-training number — it is a reshuffled 18% subsample.

The command for the long run, when it is time:

```bash
uv run python -m mlx_lm lora --model mlx-community/Qwen3-1.7B-8bit \
  --train --data training/artifacts/dataset \
  --max-seq-length 6528 --grad-checkpoint --mask-prompt \
  --batch-size 1 --grad-accumulation-steps 4 \
  --num-layers 16 --learning-rate 5e-5 \
  --iters 2956 --steps-per-eval 200 --val-batches 25 --save-every 200 \
  --adapter-path training/artifacts/adapters-v1
```

---

**How the probe went, for the record.** The LR was the one number with no evidence behind it, and getting it wrong costs 18 hours because v2 inherits whatever v1 uses. The probe varied **only `iters`**, and since the LR is constant, its 600 iterations *were* the first 1.7 hours of v1 rather than a simulation.

Launched with `nohup caffeinate -is env HF_HUB_OFFLINE=1 uv run evalgate-training train probe`. Ran 125.6 min, 10.1 s/step, peak 11.030 GB. The 25-batch val trajectory was 5.2696 → 1.1024 → **1.1462** → 0.9761, and that bump at iter 400 fired two clauses of the pre-committed rule at once, one demanding 2e-5 and the other 1e-4 — a genuine defect in the rule, logged as such.

Re-measured all four models on the **full** 140-row valid split, deterministically:

| checkpoint | val loss | vs prev | vs baseline |
|---|---|---|---|
| baseline | 5.4749 | | |
| 200 | 1.0871 | −4.3878 | 80.1% |
| 400 | 1.0446 | −0.0425 | 80.9% |
| 600 | 1.0056 | −0.0390 | **81.6%** |

Monotonic, and the bump inverts sign. **5e-5 confirmed; schedule unchanged at 2,956 iters.** The reason it was noise: `evaluate` passes no seed to `iterate_batches`, so each in-training eval scored a different ~18% subsample. The untrained baseline alone moves **0.2053** between the subsample and the full split — **4.7x the 0.0438 anomaly** that nearly cost an 18-hour retry.

| Knob | Value | mlx-lm default? |
|---|---|---|
| `--iters` | 600 probe / 2956 full (2 epochs) | no, default 1000 (a WikiSQL demo length) |
| `--batch-size` | 1 | no, default 4 — 2 needs 18.69 GB at seq 6528 |
| `--grad-accumulation-steps` | 4 | no, default 1 |
| `--num-layers` | 16 | **yes**, and it measures well here (10.98 GB) |
| LoRA rank / scale / dropout | 8 / 20.0 / 0.0 | **yes**, all three |
| `--learning-rate` | 5e-5 constant | no, default 1e-5; no schedule, for resume safety |
| `--steps-per-eval` / `--val-batches` | 200 / 25 | **yes** both |
| `--save-every` | 200 | no, default 100 |

Reproduce either number with `train trajectory` (reads the committed `probe_v1_losses.jsonl`) or `train eval-adapters` (full split, ~45 min for four arms).

**Lid, terminal, network — carries over to the long runs.** `nohup` means closing the terminal or ending the SSH session does not kill it. `caffeinate -is` prevents *idle* sleep only — **closing the lid still suspends the machine** on Apple Silicon without an external display, and the run suspends with it rather than dying, resuming on wake with the elapsed timings distorted. Leave the lid open and stay on mains power. `HF_HUB_OFFLINE=1` is set so the cached model and tokenizer are used without a network round trip; verified working. Postgres stays down throughout — the splits come from `dataset restore`.

**If it dies at hour 3.** `--save-every 200` writes `adapters.safetensors` plus numbered `0000200_adapters.safetensors` checkpoints, all kept. Resume with `--resume-adapter-file <newest numbered> --iters <remaining>`. This is a **warm restart, not an exact continuation**: mlx-lm restores adapter weights only — Adam moments reset to zero, the data iterator reshuffles, the counter starts over. Constant LR is chosen precisely so the schedule is not lost too. mlx-lm never selects a best checkpoint, so choose on val loss.

**What the 600-iter adapter is good for.** It is a **structurally valid, undertrained adapter** — correct shape, correct `adapter_config.json`, and confirmed loadable by `mlx_lm.fuse` (3.6 s, 1.7 GB fused output in `training/.scratch/fused-probe/`). Build the whole P1.3 pipeline against it: fuse → GGUF → Q4_K_M → `llama-server`, and confirm the served model emits `[C1]`-style citations and refuses. What it is **not** good for is any number that goes in a report — 600 iters is 0.41 epochs, no category is exercised enough for a per-category rate to mean anything, and it must never become v1. Build the pipeline against it; throw the weights away. **Correction: this work is sequenced before the long runs, not parallel with them — see the memory numbers above.**

Then `mlx_lm.fuse`, P1.3 (GGUF + llama.cpp), and P1.4, which is already built against stubs and needs only the runner pointed at a real endpoint. Two questions from the hand review travel into the P1.2 eval and are recorded in section 6: whether fastapi underperforms, and whether comparison refusals exceed the teacher's 36%.

**P2 remainder, independent of all of the above.** In rough order: deploy Postgres onto k3s with a block volume PVC; deploy `apps/api` (needs an arm64 image and `store.py`'s Postgres DDL wired up in place of `MemoryStore`); add `/metrics` to FastAPI and install kube-prometheus-stack via helm, sized to 24 GB; commit a Grafana dashboard JSON; write and run the k6 script and record RPS, p95, error rate, and node CPU/RAM idle versus loaded. The model server waits on P1.3 producing a GGUF.

Nothing in P2's remainder blocks P1.2, and P1.2 blocks nothing in P2 except the model server, which waits on P1.3's GGUF.

## 8. Session log

Newest first. Three to five lines each. What was attempted, what landed, what broke, what the next session needs to know.

**2026-08-02 Probe run; 5e-5 confirmed after the pre-committed rule turned out to be defective.** The probe went 125.6 min, 10.1 s/step, peak 11.030 GB, and its 25-batch val trajectory bumped upward at iter 400 (1.1024 → 1.1462 → 0.9761). That bump fired **two clauses of the pre-committed rule at once** — "any eval rises above its predecessor" demanding 2e-5, and "non-monotonic but never above baseline" demanding 1e-4. The clauses were never checked for disjointness; the second is a subset of the first for any curve that drops steeply on its first leg. Logged as a defect, along with the fact that the branch the collision made unavailable was the convenient one, which is the only reason it was safe to repair the rule after seeing the result. Before re-measuring, read the source rather than assuming: `evaluate` passes no `seed` to `iterate_batches`, so batch order comes from the global NumPy RNG that advances through every prior permutation — **each in-training eval scored a different ~18% subsample**, so 200-vs-400 was never a paired comparison. Pre-committed the full-split reading first, with the bands unchanged and made exhaustive and non-overlapping, plus a cap of one re-measurement so this could not become a search for a congenial number. Full split, all 140 rows, deterministic: **5.4749 → 1.0871 → 1.0446 → 1.0056, monotonic, 81.6% drop**. The bump inverts sign. **Branch (2) fired alone: 5e-5 confirmed, 2,956 iters unchanged.** The guard held in the strongest sense — 81.6% clears the 25% bar threefold, so no available choice of threshold changes the verdict. Best evidence for the noise diagnosis: the untrained baseline scores 5.2696 on a subsample and 5.4749 on the full split, a **0.2053 swing on an identical model, 4.7x the 0.0438 anomaly** that nearly bought an 18-hour retry. Also **withdrew the earlier "P1.3 in parallel" claim** — measured `mlx_lm.fuse` at 3.311 GB and llama-server at ~2.1-2.4 GB against 1.68 GB of headroom, so both are sequenced before the long runs; swap pressure is what produced the original `nan`, so the failure would look like a numerical bug rather than an OOM. Found one P1.3 blocker: `fuse` demands a complete HF snapshot and fails offline until `snapshot_download` is run once with network. Checkpoint selection is now pre-committed for both versions, on the full split, identically.

**2026-08-02 Probe harness built; 600-iter v1 probe ready to launch, not started.** The schedule was accepted with one change: tonight is a 600-iter probe at 5e-5, not the 2,956-iter run, because the LR was the one number with no evidence behind it and v2 inherits whatever v1 uses — a wrong LR costs 18 hours, not 9. The probe varies **only `iters`**; since the LR is constant, its 600 iterations are literally the first 1.7 hours of v1 rather than a proxy for them. **The reading rule is pre-committed in DECISIONS.md, written before the run started**, with explicit bands for right / too high / too low against the untrained baseline, and ambiguous cases resolving to too-low — the pressure at 8am is one-directional, since the alternative to "the curve looks fine" is redoing a matched 18-hour pair. Losses go to `probe_v1_losses.jsonl` via a thin wrapper around mlx-lm rather than being scraped from stdout: `mlx_lm.lora.run()` accepts a `training_callback` and then overwrites it with `get_reporting_callbacks()`, whose only backends are wandb, mlflow and tensorboard, so the wrapper mirrors `run()`'s ~20 lines and hands the callback to `train_model` directly. Validated the harness on 2 iters before handing it over — it ran offline under `HF_HUB_OFFLINE=1`, applied 5.000e-05, put LoRA on 16 layers for 4.981M trainable params, and wrote its numbered checkpoint. That check caught a real defect: mlx-lm reports val at `it - 1` and train at `it`, so joining the series on iteration would have silently dropped every training point from the morning report. **The 600-iter adapter is deliberately usable for P1.3 pipeline work** — structurally valid and GGUF-convertible, so fuse/convert/serve can be built against it in parallel with the real run — but it is 0.41 epochs and must never become v1 or appear in any reported number.

**2026-08-02 Recovery artifact built and proven; training schedule proposed.** `recovery.jsonl` is 1,900 rows and 2.0 MB — question, absent_symbol, split, retrieved chunk **ids**, answer, refused, citations, valid, validation_errors. All 1,900 rather than the 1,758 trainable: the 46 invalid rows are the evidence behind the 97.6% validity number and the 96 golden rows are the hand review's subject, and a backup that cannot reproduce the review has not backed up most of what P1.1 bought. **The restore is proven, not asserted.** With Postgres stopped, `dataset restore` re-parsed 6,178 corpus chunks, cross-checked every one against the committed `chunk_manifest.jsonl` (0 mismatches, 0 missing), and rebuilt all three splits **byte-identical** to `dataset_manifest.json`. That match is what turns "chunk text is free to regenerate" from a claim into a measurement. Side effect worth having: **P1.2 no longer needs Postgres at all**, since `dataset restore --target training/artifacts/dataset` reconstructs the training inputs from committed files. Both pre-commit checks came back clean — an 8-pattern secret sweep over every field of all 1,900 rows found zero matches (chunk ids are kept, chunk text never is, and no teacher answer quotes the placeholder), and the golden 96 are identifiable by `split`, match `golden_ids.json` exactly, and every hand-review verdict still joins. Six new tests guard all of it in CI, including the sweep, so the next corpus refresh that pulls in a placeholder credential fails a test instead of GitHub push protection. Suite is 178 passed, 0 skipped. **Schedule proposed and not started:** 2,956 iters = 2 epochs, batch 1 with 4-step accumulation, 16 layers at rank 8, 5e-5 constant, ~9 h per version and ~18 h for both. Two measurements settled open questions — batch 2 is disqualified because it needs 18.69 GB on a 6,528-token batch against a 12.71 GB ceiling, and layer count and rank turn out to be nearly free next to sequence length. v1 and v2 will hold optimizer steps identical so the refusal collapse is attributable to the mix; constant-size remixing was checked and is impossible, since all 1,758 valid rows are already allocated.

**2026-08-02 P1.2 storage reworked after GitHub push protection blocked the commit.** Secret scanning flagged a Grafana Project Service Account Token in `dataset/train.jsonl`. It is a **documentation placeholder**: one string, `glsa_iNValId…inva_5b582697`, whose body spells "invalid" repeated and whose 8 trailing hex chars are Grafana's format checksum derived from that body, shipping in public upstream Grafana docs. The scanner matches format, not entropy, and was right to fire. Checked the pushed tree before treating it as containment: **zero hits in every committed artifact**, including `review_export.jsonl` and `chunk_manifest.jsonl` — the latter stores `content_sha256` and never `content`. So nothing leaked; `review_export.jsonl` simply never sampled one of the 9 corpus chunks that carry the string, which was luck. Three other placeholders sat in the same data and would have been next: AWS's own `AKIAIOSFODNN7EXAMPLE` and the jwt.io example token, three times. **The fix is structural, not a redaction.** `training/artifacts/dataset/` is now gitignored and `dataset_manifest.json` is the committed artifact: 56 KB carrying per-split `question_id` lists plus a sha256 of each rendered file, against 23 MB of rendered text that is ~95% duplicated chunk content and rebuilds free in 14.7 s. Redacting the one string was rejected because it leaves the mechanism intact for the next corpus refresh. That broke the disjointness guard, which CI cannot run against gitignored files — it moved onto the manifest's id lists and **lost its `skipif` entirely**, so it now always runs, checking golden-leak, pairwise disjointness, and count agreement from committed data alone. Byte-agreement is a separate claim and went to a new `dataset verify`. Suite is 172 passed, 0 skipped. **The real cost of this change is that it removed an accidental backup: questions and teacher answers now exist only in the Postgres volume, and that is logged in section 6 as the project's largest standing risk with the ~2 MB artifact that would close it.** Not built — awaiting the go-ahead. Also unresolved: the 3 affected training rows were left in place, since the string is a public placeholder that no teacher answer quotes.

**2026-08-02 P1.2 prepared and verified. No training run started, by instruction.** Four things landed. **Splits:** 1,758 valid rows to train 1,478 / valid 140 / test 140, held out 8% + 8% per (category, repo) cell rather than globally so all 16 cells reach both held-out splits — the smallest cell has 52 rows and a global shuffle could have left it with zero test rows, which would hide exactly the per-category regression the split exists to catch. Assignment is `sha256(seed | question_id)`, no RNG, same scheme as `golden select` but a different seed so the two orderings do not correlate over the same ids. Format is mlx-lm **chat** format, not `prompt`/`completion`: `create_dataset` reads the latter first and `CompletionsDataset` silently rebuilds the turn as user+assistant only, which would have dropped the teacher system prompt where all six distilled rules live. `valid.jsonl` is a third split because `mlx_lm.lora` requires it, not because it was asked for. **Environment:** mlx-lm went into its own `mlx` dependency group behind a darwin/arm64 marker — the inverse of the usual arm64 problem, since MLX is Apple-Silicon-only and CI syncs on `ubuntu-24.04-arm`, where a hard dependency takes CI down; verified with `uv sync --locked --dry-run`. **The two findings that matter for the real run.** First, memory is governed by sequence length, not weight precision: at 6,528 tokens, bf16 → 8-bit saves 1.6 GB while `--grad-checkpoint` saves ~16 GB (26.66 → 10.86 GB), because the cost is the seq_len x 151,936 vocab logits tensor, not the 1.7B weights. The `nan` loss and 20-45 s/step in the first bf16 attempt were **swap**, not bf16 numerics — bf16 with checkpointing is clean — so the BUILD_PLAN fallback ladder would have cost precision for the wrong reason. Second, `iterate_batches` keeps `tokens[:max_seq_length]`, so truncation cuts the **answer**, not the retrieved chunks: the feared failure mode (answering from unseen material) cannot occur, and the real one is training on unterminated answers. At 4096 that would have hit 220 of 1,758 rows. **Smoke test** at 8-bit + checkpointing + 6528: 20 examples, 20 steps, peak 9.095 GB, ~10.5 s/step, train loss 5.256 → 1.843 and val 4.784 → 1.566, no `nan`. One incidental fix: `test_committed_artifacts_are_disjoint` had been skipping on a `dataset.jsonl` that was never generated, so the golden-leak guard had never run once; repointed at the split files it now executes, and the superseded flat exporter is deleted. **Blocked on one call: the sequence length. Nothing was written into `config.py`.**

**2026-08-02 P1.1 closed. Hand review 92/96 = 95.8%, and the pre-committed rule fired without amendment.** All 96 cases judged, no rejudgments. The result clears the `>= 90%` band, so the full valid set goes to P1.2 unchanged — no citation-target check, no re-validation of the 1,900 rows. The two most useful numbers are zeroes. **Criterion 4, citation accuracy: 0 failures across ~768 marker checks** — the one criterion automated validation structurally cannot check, because `uncited_sentence` is satisfied by *a* citation regardless of where it points, is the one that came back clean. **Criterion 3, refusal validity: 0 failures across all 31 refusals in the sample** (24 adversarial, 5 comparison, 1 factual, 1 howto), which settles the direction of the teacher's weakness: it **under-refuses, never over-refuses** — every decline was correct, and 3 of its 4 failures are cases where it should have declined and answered instead. All 4 failures land on criteria 1-2. Cases 50 and 52 had the second side of a two-part question absent from all 8 retrieved chunks and the teacher filled from the available side; case 2 looks like the same shape but is not — the second approach *was* retrieved, sitting uncited in C6, and the teacher substituted the wrong chunk, which no retrieval fix would catch; case 38 read a chunk backwards, naming both the spec and the migration guide authoritative where C1 says the spec is authoritative *and the guide may contain errors*. On the strength of criterion 3 the previously-open **comparison mix decision is now closed: leave 36.0% as is.** Trimming refusal rows would deepen the exact weakness the review found, comparison refusals are the only rows that teach declining under *partial* retrieval (adversarial rows turn on an absent symbol, not absent coverage of a present one), and the uneven mix is what makes a loss of refusal discipline visible as a per-category regression at P1.4. Comparison-avoidance bias is the accepted risk, to be measured after training rather than pre-empted in the data. Two things travel into P1.2 as open questions, both in section 6: that bias, and fastapi carrying 3 of 4 failures at 21/24 — n=24 per repo, so a direction to check, not a measurement. **P1.2 is now the head of the critical path; the only human blocker left is the Hugging Face token.**

**2026-08-02 P1.1 Data, teacher stage closed and the review tool built.** Two jobs. First, reconciling the docs with reality: PROGRESS claimed the teacher batch was part 1 of 2 with ~197 rows left to submit, when it had actually finished in **3** shards (840 + 843 + 197) some sessions earlier and nobody wrote it down. Every headline number was re-derived from Postgres and `ledger.json` rather than promoted from memory, and all six agreed: **1,900 of 1,900 answered, 1,854 valid (97.58%), `answered_absent` 0 of 400, `fabricated_absent_side` 6 of 400, comparison refusal 36.0%, ledger $2.8930 of $5.00.** The interesting one is the split by category — adversarial is 100% valid and `howto` is worst at 94.0%, and every one of `howto`'s 30 failures is an uncited sentence rather than a grounding failure, so citation discipline, not hallucination, is the only failure mode that survived at scale. Second, the review tool. `golden select` now balances **6 per (category, repo) cell** instead of 24 per category and writes a manifest before any judging starts, so the sample is a commitment rather than something that can shrink once the verdicts look bad; selection is a sha256 sort over a fixed seed with no RNG, verified byte-identical across reruns. `golden review` serves a two-pane localhost UI — answer on the left, the row's stored chunks on the right, every `[C3]` a link that opens its chunk — with verdicts appended to JSONL after each judgment so quitting mid-review costs nothing. It calls no model and deliberately hides the validator's own flags, since showing `uncited_sentence` before the reviewer reads the answer would anchor exactly the criterion being judged. The old localStorage review page is deleted rather than kept: two verdict stores that can disagree is worse than one. **P1.1's only remaining step is a human reading 96 cases.**

**2026-08-02 P2 Infra, provisioning and remote state.** Started P2 as the retry mechanism for the A1 capacity lottery, since all three us-chicago-1 ADs were refusing 4 OCPU / 24 GB and manual retries had tripped the OCI rate limit. Wrote the terraform (existing VCN and subnet looked up, never created; rules in an NSG on the instance VNIC so `destroy` leaves shared infra untouched) plus `retry-apply.sh`, which cycles ADs and separates capacity from throttle from fatal. It won on **attempt 1 in 37 s**, so the backoff paths are still untested against live errors — logged as such rather than treated as validation. **4 OCPU / 24 GB confirmed on PAYG**, which unblocks the deferred Kafka sizing decision. Booting it for real found two bugs no amount of static validation would have: `/opc/v2/vnics/` has no `publicIp` field, so cloud-init aborted 42 s in and k3s never installed; and `kubectl wait --all` fails instead of waiting when no node has registered yet. Both fixed, applied to the live node over SSH rather than by replacing an instance that cannot be re-obtained on demand. Then closed 6443 to the internet entirely (kubectl now tunnels over SSH), narrowed 22 to a `/32`, and split the public web ports into their own variable. Finally migrated state off the laptop into a versioned OCI Object Storage bucket with S3-native locking, verified by concurrent plans. **The one thing to remember: `export AWS_REQUEST_CHECKSUM_CALCULATION=when_required`, or every state operation fails as a fake credential error.** P2 provisioning is done; deploys, monitoring, and k6 are not.

**2026-08-01 P1.1 Data, question set final.** Per-repo leak rates measured by replaying stored batch outputs: Grafana 34.2%, Pydantic 27.5%, FastAPI 20.6%, Prometheus 19.0%. Grafana confirms leak tracks corpus density; Pydantic contradicts it with the smallest corpus and second-highest rate, driven by regular API naming. Aggregate top-ups had drifted adversarial off its split (FastAPI +10, Grafana -15); backfill is now per (category, repo) cell with per-repo overshoot, and it converged in one round. Final: 1,900 questions, all 16 cells exact. Ledger $0.1674 of $5.00. Still no teacher spend.

**2026-07-31 P1.1 Data, questions complete.** Corpus embedded (6,178 chunks, $0.0285, exactly as projected). Question generation stratified by repo into a [15%, 35%] band because the corpus is 68% Grafana; proportional sampling would have trained a Grafana model. 1,907 questions for $0.1363 across 3 batches, 0 failed calls. Headline finding: gpt-4.1-mini invents an adversarial symbol that actually exists in the corpus **26% of the time**, measured three times (26.0 / 26.7 / 13.3), so filling the refusal category costs 1.38x its nominal size. Two poisoning guards added and tested. Stopped before teacher spend; ledger at $0.1648 of $5.00.

**2026-07-31 P0 Bootstrap.** Scaffolded the uv workspace, pgvector dev stack, and arm64 CI. Two problems, both logged in DECISIONS.md. Three members all named their test file `test_smoke.py` and collided under pytest prepend import mode, fixed with `--import-mode=importlib`. CI run #1 green in 20 seconds total.
