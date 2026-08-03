# PROGRESS

Current state of EvalGate. Read this first in every session. Update it before ending every session.

Three files, three jobs. BUILD_PLAN.md is the plan and rarely changes. DECISIONS.md is an append only log of rationale and measurements. This file is mutable current state and gets overwritten freely.

Last updated 2026-08-03, while v1 was training. Last change: `./up.sh`, a single idempotent entrypoint from clone to live public API, verified as a 44 s no-op. P2's only remaining item is the model server.

---

## 1. Where we are

**v1 TRAINING IS RUNNING ON THIS MAC RIGHT NOW.** Launched 2026-08-02 18:00, 2,956 iters, ~12.6 s/iter, peak 11.030 GB against a 12.71 GB Metal working set, ETA ~04:20. Adapters land in `training/artifacts/adapters-v1/`, console log at `training/artifacts/v1_console.log`.

**While it runs, no memory-heavy local work.** No Docker, no Postgres, no model loading, no `dataset export` or `dataset verify`, no fuse, no llama-server. Swap is already at 91%, and swap pressure is what produced the `nan` on the first bf16 attempt — a competing process does not fail cleanly, it corrupts a 9-hour run in a way that reads like a numerical bug. Editing files, `ruff`, and `pytest` are fine.

| Phase | Status |
|---|---|
| P0 Bootstrap | Done 2026-07-31 |
| P1.1 Data | **Done 2026-08-02.** 1,900 answered, 1,854 valid (97.6%), $2.89 of $5.00. Hand review 92/96 = 95.8%, criteria 3 and 4 clean. Acceptance rule fired as written |
| P1.2 Training | **v1 RUNNING since 2026-08-02 18:00**, 2,956 iters at 5e-5, seq 6528 + grad-checkpoint, ETA ~04:20. Probe confirmed the LR (full-split val 5.4749 → 1.0056, 81.6% drop). **v2 deliberately deferred** until the harness is verified end to end against v1 |
| P1.3 Serving | **Pipeline proven 2026-08-03 against throwaway probe weights.** fuse → GGUF f16 → Q4_K_M (1.03 GB) → llama-server, arm64 native, 84 tok/s. Re-run against v1 when it exists |
| P1.4 Harness | **Code landed 2026-08-03 at `66cc2e6`, 194 tests passing.** *Verified:* `LlamaServerModel` speaks the protocol against a fake server built from real captured payloads; prompt renderer unified into `evalcore.prompt` and proven byte-identical. *Not verified:* anything against a live server — no real tokenization, no context arithmetic, no judge call. Judge model now decided (gpt-5.4-mini) but **never invoked** |
| P2 Infra | **One item left: the model server, blocked on v1.** Postgres on a 50 GB block volume, API public at `http://64.181.195.241`, kube-prometheus-stack with a committed 19-panel dashboard, and k6 recorded: **306,436 requests, 681 req/s, p95 192 ms, 0 errors.** The only P2 item left is the **model server**, which is blocked on v1 finishing — see the clause-by-clause reading below |
| P3 Automation | Gate workflow and threshold logic scaffolded 2026-08-01 |
| P4 Ingestion | Not started |
| P5 Analytics | Not started |
| P6 Optional | Not started |

**P2 is not closed.** Its exit criterion is "`terraform apply` goes from zero to
a running public API, dashboards live, k6 numbers recorded." Of BUILD_PLAN P2's
four build items:

| P2 item | Status |
|---|---|
| Terraform: VCN/subnet/NSG/instance, cloud-init k3s | **Done 2026-08-02** |
| Deploy **Postgres** with a block volume PVC | **Done 2026-08-03** |
| Deploy the **api** onto k3s | **Done 2026-08-03.** Public on port 80 via traefik, Postgres-backed, 2 replicas |
| Deploy the **quantized model server** onto k3s | Not started. Blocked on v1 finishing and the P1.3 pipeline re-running against real weights |
| kube-prometheus-stack + `/metrics` on FastAPI + committed dashboard JSON | **Done 2026-08-03.** Chart 88.1.3, 5 pods, 592 Mi actual. 19-panel dashboard committed, every query verified populated |
| k6 script, run and recorded | **Done 2026-08-03.** 306,436 requests, 681 req/s, p95 192 ms, p99 246 ms, 0 errors, all 4 pre-committed thresholds held |

### `./up.sh` — what is proven and what is not

Added 2026-08-03. Nine steps, composing scripts that already existed. **Verified
only on the no-op path**, and the distinction matters:

| Step | Exercised? |
|---|---|
| Preflight (tools, tfvars, backend.hcl, `~/.oci/config`, AWS profile, SSH keypair) | **Yes** — all pass on a configured machine. The *failure* branches are unexercised |
| `terraform init` + `apply` | **Yes, as a no-op**: `0 added, 0 changed, 0 destroyed`. Creating an instance from nothing is **not** exercised |
| Wait for SSH, wait for `bootstrap.done` | **Only the already-true case.** Both loops exited on the first attempt; the retry and timeout paths have never run against a booting node |
| Fetch kubeconfig, open SSH tunnel | **Yes.** Both branches — opening a tunnel, and reusing one that already exists |
| Node prep: volume, BuildKit, helm | **Only the already-installed case.** All three reported "already"; the install branches are unexercised here, though each ran for real when it was first written |
| Secret generation | **Only the "exists, leaving alone" branch.** Generation has never run through `up.sh` — the three secrets were created by hand earlier. The refuse-if-database-exists guard is likewise unexercised |
| Build on node | **Yes.** Runs every time; BuildKit cache hit, same digest |
| `apply.sh` ×3 | **Yes.** Every object `unchanged` |
| Verify against the public address | **Yes.** `/health` and `/ready` both 200 |

**So: zero-to-API is not verified.** A from-scratch run would need a fresh
tenancy, and this instance can never be destroyed to produce one — it is a
`VM.Standard.A1.Flex` that took days of capacity retries. What is verified is
that running it against a live system changes nothing: 44 s, exit 0, identical
pod uids, identical secret resourceVersions, `restartCount=0` across three
consecutive runs.

The unexercised branches are the ones that only fire on a fresh tenancy. They are
the same code paths that each ran successfully once, by hand, when the
corresponding piece was first built — but not through this wrapper, and not in
sequence.

### P2 exit criterion, clause by clause

BUILD_PLAN P2: *"`terraform apply` goes from zero to a running public API.
Dashboards live. k6 numbers recorded."*

| Clause | Status |
|---|---|
| **one command goes from zero to a running public API** | **Met in substance, with one honest caveat.** `./up.sh` is that command and a stranger's path is now `./up.sh`. Two qualifications: the command is not literally `terraform apply` — the deploys compose *outside* terraform on purpose, because a failed `local-exec` provisioner taints its resource and terraform's remedy for a tainted resource is to destroy it, which on this project means the irreplaceable instance; and the **zero-to-API path is unverified**, because proving it needs a fresh tenancy and this instance can never be destroyed to produce one. What is verified is the no-op path. See the table above |
| **running public API** | **Met.** `http://64.181.195.241` serves `/health`, `/ready`, `/suites`, `/runs`, `/diff`, `/gate` from the internet, Postgres-backed, 2 replicas, 0 restarts through 306k requests |
| **Dashboards live** | **Met.** Grafana 13.1.1, dashboard `evalgate-p2` committed as JSON and loaded from a generated ConfigMap. 19 panels, every query verified returning data during the load window |
| **k6 numbers recorded** | **Met.** In DECISIONS.md: RPS, p95, p99, error rate, checks, node CPU and RAM idle vs under load, and the generator/system CPU split |
| **Record list: apply wall time** | **Met.** 41 s for the storage apply; earlier provisioning timings already logged |
| **Record list: k6 RPS, p95, error rate** | **Met.** 680.95 req/s, 192.26 ms, 0.00% |
| **Record list: node CPU and RAM idle vs load** | **Met.** 2,508 MB / load 0.17 idle → 2,626 MB / 74.9% CPU / load 9.57 under load |
| **Record list: PAYG allowance verification** | **Met 2026-08-02.** 4 OCPU / 24 GB confirmed real |

**What remains in P2 is one thing: the quantized model server**, which cannot be
deployed until v1 finishes and the P1.3 pipeline runs against real weights. That
is a Thread A dependency, not a Thread B gap.

**P2 is therefore not closed, but every clause that can be closed from here is.**
The two residual caveats on the entrypoint clause are both structural rather than
unfinished work: composing outside terraform is a deliberate decision recorded in
DECISIONS.md, and zero-to-API is unverifiable without a tenancy this project does
not have and should not create. Neither is a task waiting to be done.

**The "running public API" clause of the exit criterion now holds.**
`http://64.181.195.241` answers `/health`, `/ready`, `/suites`, `/runs`,
`/diff`, and `/gate` from the internet, and the gate returned a correct `fail`
verdict end to end through traefik, two pods, Postgres, and the block volume.
What remains for P2 is the model server, dashboards, and k6.

The application database is **empty on purpose**: the verification data was
deleted after it was recorded. The tables exist, created by the app at startup.

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
- **`packages/evalcore/src/evalcore/prompt.py` is the single source for the grounded-QA prompt.** `SYSTEM`, `render_chunk_block`, `render_context`, `build_user_message`, `build_messages`. All three call sites route through it: `teacher.prompts` (the API calls), `dataset.build.to_example` (the training rows), and `model.LlamaServerModel` (eval). `training` depends on `evalcore` in the uv workspace; nothing depends on `training`. **Editing this file changes the training data** — `dataset verify` and `packages/evalcore/tests/test_prompt.py` both fail if you do
- **`packages/evalcore/src/evalcore/model.py` is the real `ModelClient`.** `LlamaServerModel(version, quantization)` → `ref` like `llama-server:v1:Q4_K_M`. Stdlib `urllib`, temperature 0, `chat_template_kwargs: {"enable_thinking": false}` explicit. Raises `ContextOverflowError` (carrying `n_prompt_tokens` and `n_ctx`) or `ModelError`; `run_case` turns both into per-case errors. 16 tests against a threaded `http.server` replaying payloads captured from the real llama-server
- `packages/evalcore/` is the harness: `schema.py` (suite, case, thresholds, results), `scorers.py` (exact, regex, citation, refusal), `judge.py` (provider abstraction, SQLite cache keyed on case+output+rubric+judge version, full-jitter backoff), `runner.py` (`ModelClient` protocol plus `StubModel`/`EchoContextModel`), `diff.py` (case deltas and the breach logic), `report.py` (terminal, markdown PR comment, self-contained HTML), `loader.py`, `cli.py`
- `evalgate-eval` CLI: `build-suite` from the golden export, `run`, `gate`. Exit 1 from `gate` is the merge block
- `apps/api/` v0: register suites, submit runs, list runs, explicit baseline promotion per (suite, branch), `/diff`, and `/suites/{id}/gate` returning the verdict plus PR comment. **Live on k3s since 2026-08-03**
- **`store.py` now has `PostgresStore` alongside `MemoryStore`.** psycopg 3 with a `ConnectionPool`; `body` JSONB is the source of truth and the scalar `version` / `model_ref` / `score` columns are a denormalised cache written from the model. `build_store()` picks Postgres when `DATABASE_URL` is set and memory otherwise, so CI and unit tests need no database. **Both implementations must raise the same exceptions** — `app.py` maps `KeyError`→404 and `ValueError`→400 — and `apps/api/tests/test_store.py` runs one parametrised set of assertions against both
- **The schema is created by the app at startup**, from the `SCHEMA` constant, under `pg_advisory_xact_lock`. The lock is required at 2 replicas: `CREATE TABLE IF NOT EXISTS` is not atomic against a concurrent creator. **This is not a migration system** — the first schema change after P4 has real data needs Alembic
- **`apps/api/Dockerfile` is a 3-stage build**: `builder` (uv sync, `--frozen --no-dev --no-editable --package evalgate-api`), `test` (adds dev deps and runs pytest in-cluster), `runtime`. Builder and runtime share the same `python:3.12-slim-bookworm` base because the copied venv is bound to its interpreter path. Build context is the **repo root**, not `apps/api/`, since evalcore is a workspace sibling
- **`apps/api` requires `psycopg[binary,pool]`.** The `pool` extra was added 2026-08-03; `uv.lock` was updated with `uv lock` **and never `uv sync`**, because syncing without `--group mlx` would delete mlx-lm out from under the live training run
- `.dockerignore` at the repo root excludes `training/.scratch` (9.5 GB) and `training/artifacts` (244 MB, actively written by the training run), plus every secret-bearing pattern
- `.github/workflows/eval-gate.yml` scaffolded: builds the suite, runs the candidate, restores the judge cache, comments the diff (updating one sticky comment), uploads the report, fails on a breach
- `infra/terraform/` is real as of P2. `versions.tf` (provider, `~/.oci/config` auth), `variables.tf`, `main.tf` (data-source lookups for the existing VCN and subnet, NSG plus rules, the instance), `outputs.tf`, `backend.tf` + `backend.hcl.example` (remote state), `cloud-init/k3s.yaml.tftpl`, `retry-apply.sh`, `terraform.tfvars.example`, and a committed `.terraform.lock.hcl` pinning oracle/oci 8.25.0
- `retry-apply.sh` is the A1 capacity retry loop: cycles availability domains by index, classifies failures as capacity / throttle / fatal, backs off 3 min between ADs and 15 min per cycle with exponential backoff from 30 min on a 429, logs every attempt with a UTC timestamp to gitignored `logs/`. Refuses to run below 4 OCPU without `--allow-downsize`, and exits 0 early if an instance is already in state
- `cloud-init/k3s.yaml.tftpl` installs k3s and opens the host iptables ports OCI's Ubuntu image blocks by default. POSIX sh only, because cloud-init runs `runcmd` under dash
- **`infra/terraform/storage.tf` is the Postgres disk.** `oci_core_volume.pgdata` (50 GB, VPU 10, `is_auto_tune_enabled = false`, `prevent_destroy`) plus a **paravirtualized** `oci_core_volume_attachment`. Both are strictly additive — the instance is only ever *read* (`.id`, `.availability_domain`), never modified. The AD comes from the instance rather than `local.availability_domain`, because that local is derived from `retry-apply.sh`'s AD cursor and can point somewhere the instance is not. Volume OCID is `terraform output pgdata_volume_id`
- **`infra/k8s/postgres/` is live.** `node-volume-setup.sh` (idempotent `mkfs.ext4` + `/etc/fstab` by `LABEL=pgdata` + mount at `/mnt/pgdata`; hard-stops rather than formatting a disk that already has a filesystem), then `00-namespace` / `10-storageclass` (`pgdata-local`, `no-provisioner`, `Retain`) / `20-pv` (static `local` PV over `/mnt/pgdata/data`, node-affine, `Retain`) / `30-pvc` / `40-statefulset` / `50-service` (ClusterIP `postgres:5432`), plus `apply.sh`
- **The init-SQL ConfigMap is generated, not committed.** `apply.sh` builds it from `infra/postgres/init/` — the same directory the dev compose stack mounts — so the cluster and the dev stack cannot drift on which extensions a fresh database gets
- **The Postgres password is a Kubernetes secret created out-of-band and is not in the repo.** `apply.sh` checks `secret/postgres-credentials` exists and **fails rather than creating it**; the create command is in `infra/k8s/README.md`. `POSTGRES_PASSWORD` is read only by `initdb` on a first boot, so rotating the secret alone does nothing — it needs `ALTER ROLE` too
- Running on the node: `postgres-0`, `pgvector/pgvector:pg16`, **PostgreSQL 16.14 `aarch64`, pgvector 0.8.6**, requests 250m/512Mi, limits 2 CPU/2Gi. **The database is empty** — no schema has been applied to it yet
- **`infra/k8s/api/` is live.** `node-build-setup.sh` (installs nerdctl 2.3.5 + buildkit 0.32.0 on the node and runs buildkitd as a systemd unit with a **containerd worker in the `k8s.io` namespace**), `build-on-node.sh` (streams the build context over SSH as a tar and builds there), `10-deployment` / `20-service` / `30-ingress`, and `apply.sh`
- **There is no registry.** Images live only in the node's containerd, in the `k8s.io` namespace — kubelet looks nowhere else, so an image built into `default` is invisible while `ctr images list` still shows it. Tag is explicit `:0.1.0` with `imagePullPolicy: IfNotPresent`; **never `:latest`**, which implicitly forces `Always` and sends kubelet to Docker Hub
- **Two secrets, both created out-of-band and neither in the repo**: `postgres-credentials` and `evalgate-api-token`. The API composes `DATABASE_URL` in the manifest from three individual `secretKeyRef` env vars — **not `envFrom`**, because k8s `$(VAR)` expansion cannot see variables injected that way
- **Writes are token-guarded, reads are open.** `PUT /suites`, `POST /runs`, `POST /baseline` need `Authorization: Bearer $EVALGATE_API_TOKEN`; `/health`, `/ready`, reads, and `/gate` do not. With no token configured writes return **503, not open access**
- **`evalgate_test` is a throwaway database on the same server** for the Postgres store tests, which `TRUNCATE` on teardown. The fixture aborts the run unless `current_database()` ends in `_test` — see the 2026-08-03 Problems row for why that guard exists
- **`infra/k8s/monitoring/` is live.** `node-helm-setup.sh` (helm **3.21.3**, deliberately not 4.x — the charts are tested against 3), `values.yaml`, `dashboards/evalgate.json`, `apply.sh`. helm runs **on the node**; release state lives in-cluster as secrets so the client location is irrelevant, but nothing avoidable runs on the M1
- **Nothing in the chart takes a default.** Explicit requests/limits on Prometheus (400Mi/1Gi), Grafana (128Mi/320Mi), **both Grafana sidecars** (48Mi/96Mi each — the chart ships them unbounded at 72Mi), kube-state-metrics, node-exporter, and the operator. Measured: **592 Mi actual against 784 Mi requested**
- **Disabled entirely:** `kubeControllerManager`, `kubeScheduler`, `kubeProxy`, `kubeEtcd` (k3s embeds them; their ServiceMonitors would be permanently `down`), `kubeApiServer` (works, but is the largest cardinality source and feeds nothing we need), **Alertmanager + `defaultRules`** (no receiver, none in budget), and Grafana's bundled dashboards (control-plane ones render empty given the above)
- **`kubelet.serviceMonitor.metricRelabelings` drops `apiserver_*`, `etcd_*`, `workqueue_*` and friends.** This is not redundant with the toggles above — **k3s serves all control-plane metrics from the same process the kubelet scrape hits**, so the toggles removed the scrape jobs and none of the series. Ingest went **53,201 → 16,192**
- **Prometheus storage is `local-path` on the boot volume**, `retention: 15d` **and** `retentionSize: 8GB`, scrape interval **15s**. The size cap is the only real guard: **local-path does not enforce PVC capacity**, so the 10Gi PVC is advisory. Measured 1,582 samples/s → ~273 MB/day → ~4.1 GB at 15 d
- **Grafana is ClusterIP only**, reached by port-forward over the SSH tunnel. Admin credential in the out-of-band `grafana-admin` secret. No TLS on a bare IP, so a public admin login would cross the wire in cleartext
- **`apps/api` exports Prometheus metrics on port 9000**, not on the public router — the Ingress maps :80 to :8000 only, so `/metrics` returns 404 from the internet and `up` from the ServiceMonitor. Labelled by **route template**, so a suite id can never become its own series
- **The in-progress gauge is hand-rolled**, because `prometheus-fastapi-instrumentator` constructs that one metric without `registry=` and it lands in the global default. Two tests pin the behaviour
- **`./up.sh` at the repo root is the single entrypoint**: clone + credentials → live public API, nine steps, idempotent. It **composes** the existing scripts — terraform apply, the three `node-*-setup.sh`, `build-on-node.sh`, then `postgres/` → `monitoring/` → `api/` apply scripts — and reimplements none of them. Deploys are deliberately **not** inside terraform: a failed `local-exec` provisioner taints its resource, and terraform's remedy for a taint is destroy-and-recreate, which on this project is the one instance that can never be replaced
- **`up.sh` exports `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` itself**, so the misleading 403 can no longer be reached by an operator who did not read DECISIONS.md. It also preflights the `~/.aws/credentials` profile named in `backend.hcl`, since a missing profile produces the identical error
- **Secret policy in `up.sh` is generate-if-absent, never rotate, never print.** Values reach kubectl through mode-600 files via `--from-file`, so they are absent from `ps`, shell history, stdout, and the repo. One refusal case: a missing `postgres-credentials` alongside an existing StatefulSet stops the run, because `POSTGRES_PASSWORD` is read only by `initdb` on a first boot and a new password would not match the database
- **`build-on-node.sh` no longer defaults to a hardcoded IP.** It derives the node from `terraform output ssh_command`, expanding the literal `~` terraform emits, and fails with instructions if neither is available — a hardcoded address worked on exactly one machine and would silently target nothing from a fresh clone
- **`infra/k6/` is the P2 load test.** `script.js` (committed thresholds, weighted read mix), `job.yaml` (k6 as a **Job in the cluster**, capped at 1 CPU, so cadvisor separates generator CPU from the API's), `seed.sh`, `run.sh`, `teardown.sh`. Run order is seed → run → teardown; they are separate scripts so a failed run leaves the fixture to inspect rather than deleting the evidence
- **The load test writes nothing.** It seeds 1 suite + 2 runs + 1 baseline through the authenticated API, loads read paths only, then deletes exactly those rows with `WHERE suite_id LIKE 'k6-%'` in FK order and **asserts the counts returned to the pre-run baseline**. No `TRUNCATE`, no `DROP`, no `CASCADE` — written that way because of the fixture incident on the same night
- **k6 pushes metrics to Prometheus** via `enableRemoteWriteReceiver` plus `K6_PROMETHEUS_RW_SERVER_URL`, so the load window stays on the dashboard within retention rather than only in a terminal. The k6 row's panels are empty when no run is in scope — that is expected, unlike a permanently empty panel
- `workers/` and `analytics/` are still README placeholders and are not packaged yet

## 4. Environment state

- Dev machine Apple M1 Pro 16 GB, macOS 26. Docker runtime is Docker Desktop, with OrbStack installed but not selected
- **A native Postgres already owns 127.0.0.1:5432 on this machine.** A host-level listener shadows the container's port mapping, so `db init` fails with `role "evalgate" does not exist` against a perfectly healthy container. The dev stack therefore runs on **5433** via `POSTGRES_PORT=5433` in `.env`, with `DATABASE_URL` matching. The compose default stays 5432 for anyone else; this is per-machine config
- **llama.cpp b10210 installed via Homebrew 2026-08-03.** All binaries `Mach-O 64-bit executable arm64`. The `convert_hf_to_gguf.py` script is not shipped by brew and comes from the source tarball in gitignored `training/.scratch/llamacpp/`; it runs under `uv run --no-project --with-requirements`, never in the project venv, because it pins numpy 1.26 / transformers 4.57 against the project's 2.5.1 / 5.14.1
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
- **Block storage: 100 GB of the 200 GB Always Free allowance used.** 50 GB boot (`/dev/sda`, VPU 10) plus 50 GB `evalgate-k3s-pgdata` (`/dev/sdb`, VPU 10, paravirtualized, AD-1), mounted at `/mnt/pgdata` by `LABEL=pgdata` with `_netdev,nofail`. **100 GB left** for the Prometheus TSDB and P4's Kafka. Verified from the limits API: `total-free-storage-gb` 200 per AD, `volume-count` 10000 — the free tier is capped in GB, not in number of volumes
- **Node at idle with Postgres, 2 API replicas, and monitoring (2026-08-03):** 2,508 MB used of 23,974 MB, 21,466 MB available, no swap, **load 0.17**. Boot disk 8.8 GB of 48 GB (19%), pgdata 71 MB of 49 GB (1%). Requests 1,948 Mi (8%) and 1,070m CPU (26%); CPU *limits* are 143%, which is deliberate overcommit
- **Memory is not the constraint on this node — CPU is.** With everything above running, 8% of memory requests are committed. Even a generous Airflow (2.5 GB) plus Kafka (4 GB) would stay under 40%. This unblocks BUILD_PLAN section 12's Kafka sizing question in the direction of "size it for CPU and disk, not RAM"
- **The node is now also the build machine.** nerdctl 2.3.5 and buildkit 0.32.0 installed 2026-08-03; `buildkitd.service` runs with `--containerd-worker-addr=/run/k3s/containerd/containerd.sock --containerd-worker-namespace=k8s.io` and `--oci-worker=false`. Builds are native aarch64 — no buildx, no QEMU. Build context lands in `/home/ubuntu/build/evalgate-api`
- **`http://64.181.195.241` is live and public.** traefik Ingress, HTTP only. TLS needs a DNS name — Let's Encrypt will not issue for a bare IP — so it waits for P6's Cloudflare item
- **The 4 OCPU / 24 GB PAYG allowance is confirmed real**, not the feared 2/12. This is the verification BUILD_PLAN section 12 made the Kafka sizing decision wait on, so that decision is now unblocked
- **Network exposure.** 22 open to the operator `/32` only (`admin_cidr`); 80 and 443 open to the internet via a separate `public_ingress_cidr`, deliberately public because P3's gate is called by GitHub Actions runners; **6443 has no ingress rule at all**. kubectl goes through an SSH tunnel: `terraform output kubectl_tunnel_command`
- **If your IP changes you are not locked out.** Edit the port 22 rule on the `evalgate-k3s-nsg` security group in the OCI console from any browser, then update `admin_cidr`. Never rebuild the instance to regain access; the shape is a capacity lottery
- **SSH key** is project-scoped at `~/.ssh/evalgate_ed25519`, not the machine default. No passphrase. Terraform reads only the `.pub` half
- Auth for the OCI provider comes from `~/.oci/config` (`oci setup config`). Nothing tenancy-specific is committed except through gitignored `terraform.tfvars`

### Terraform state, as of 2026-08-02

- **State is remote**, not local. OCI Object Storage bucket `evalgate-tfstate`, key `evalgate/p2/terraform.tfstate`, namespace `ax3mt2roo4ha`, private, **versioning enabled**. S3-native locking is on and verified
- OCI has no native Terraform backend; this is the `s3` backend against Object Storage's S3-compatible API. Config is split: `backend.tf` committed, `backend.hcl` gitignored (copy `backend.hcl.example`). Every `init` needs `-backend-config=backend.hcl`
- **`export AWS_REQUEST_CHECKSUM_CALCULATION=when_required` is mandatory.** Put it in your shell profile. Without it every state operation fails with `403 SignatureDoesNotMatch: The secret key required to complete authentication could not be found`, which points at credentials and is completely misleading — the real cause is a streaming checksum trailer OCI does not implement. `skip_s3_checksum` in the backend block and `request_checksum_calculation` in `~/.aws/config` both look like fixes and are not; the config-file key fixes the `aws` CLI only. `retry-apply.sh` and `up.sh` both export it, so only manual `terraform` commands run outside those two are exposed
- The S3 credential is an OCI **Customer Secret Key** (not the API signing key), stored in `~/.aws/credentials` under profile `evalgate-tfstate`, mode 600, never in the repo. Its access key is the key object's `id` field
- Newly created Customer Secret Keys take **~80 s** to become usable and fail with that same misleading 403 until they do
- Stale pre-migration local state kept as `infra/terraform/terraform.tfstate.migrated-to-oci`, gitignored, as a cold fallback

## 5. Blocked on Jinwoo

Anything waiting on a human goes here so a fresh session does not silently work around it.

- ~~Hugging Face write token~~ **Cleared 2026-08-02.** Token is in `.env` and the base model pulled
- ~~Sequence-length sign-off~~ **Cleared 2026-08-02. Approved at `--max-seq-length 6528` with `--grad-checkpoint`, and that is what v1 is running.** Now written into code as `config.TRAIN_MAX_SEQ_LENGTH` / `config.TRAIN_GRAD_CHECKPOINT` rather than living only in prose
- ~~Judge model selection~~ **Decided 2026-08-03: `gpt-5.4-mini`.** See section 6 for the one caveat that remains (re-verify pricing on the web before the first call)
- ~~Add `export AWS_REQUEST_CHECKSUM_CALCULATION=when_required` to the shell profile~~ **Cleared 2026-08-03.** `up.sh` exports it, so the one-command path no longer depends on the operator's profile. Still worth having in your profile for manual `terraform` calls outside the wrapper

## 6. Known issues and deferred items

- ~~STANDING RISK: questions and teacher answers exist only in the Postgres volume~~ **Closed 2026-08-02 by `training/artifacts/recovery.jsonl`.** 1,900 rows, 2.0 MB, committed: question, absent_symbol, split, retrieved chunk **ids**, answer, refused, citations, valid, validation_errors. No chunk text. Restore is proven, not asserted — `dataset restore` ran with Postgres stopped, re-parsed 6,178 chunks with 0 hash mismatches against `chunk_manifest.jsonl`, and rebuilt all three splits byte-identical to `dataset_manifest.json`. Six tests guard it in CI, including an 8-pattern secret sweep and the golden-96 join
- ~~OPEN: the judge model~~ **Decided 2026-08-03: `gpt-5.4-mini`**, differing from the teacher `gpt-5-mini` as required. $0.253/run sync against gpt-4.1's $0.616, leaving ~$1.60 of the $2.1070 balance for P3's daily DAG rather than ~$0.88. **Two things still true and easy to forget: no judge call has been made yet, and the pricing table in `config.py` was last verified 2026-07-31 — check it on the web before the first one.** The ledger is the enforcement, but a stale price makes the ceiling arithmetic wrong in the unsafe direction
- **Open question carried into the P1.2 eval, from the hand review: fastapi took 3 of the 4 failures at 21/24.** Per-repo n is 24 (+/- ~13 points), so this is a direction to check, not a measured gap. Look for it in the P1.2 eval; do not act on it in the data
- **Accepted risk carried into the P1.2 eval: comparison-avoidance bias.** The comparison category refuses 36.0% of the time and that mix was deliberately left as is (DECISIONS, 2026-08-02), so the trained model may decline comparisons it could answer. Measure it after training — if comparison refusals come out above the teacher's 36%, the mix is the first thing to change. All 5 comparison refusals in the sample were valid, so the teacher's own bias is toward under-refusing, not over-refusing
- ~~OCI Pay As You Go may allocate only 2 OCPU and 12 GB~~ **Resolved 2026-08-02. 4 OCPU / 24 GB launched and is running.** No shrink needed, and the 2 OCPU contingency is off the table unless the instance is ever lost
- Kafka memory budget and single versus dual instance was deferred until that verification. **The verification is done, so this decision is now unblocked** and can be made whenever P4 starts. It still needs a human
- **A1 capacity is a lottery and this instance is not replaceable on demand.** All three ADs were returning "Out of capacity" hours before it launched. Never `terraform destroy` or taint the instance to pick up a config change. `ignore_changes` covers the image OCID and `metadata["user_data"]` for exactly this reason; bootstrap fixes are applied to the live node over SSH and land in the template for the next build
- The retry loop's capacity and throttle branches have **never executed against a live OCI error**. It succeeded on attempt 1, so those paths are still only tested against recorded error strings and the dry-run harness. Treat the backoff timings as unvalidated if the loop is ever needed for real
- **OPEN: `store.py`'s startup DDL is not a migration system.** `CREATE TABLE IF NOT EXISTS` silently does nothing against an existing table, so the first schema *change* after P4 puts real trace data in Postgres needs Alembic. That is the trigger; before then, version tracking buys nothing
- **OPEN: there is no alerting.** Alertmanager and the chart's default rules are deliberately off — no receiver exists and none is in budget. Dashboards show problems only to someone looking at them. If P3's daily DAG needs to page, wire a receiver first, then re-enable
- **OPEN: `prometheus_tsdb_head_series` reads ~58k while real ingest is 16,192.** Stale pre-relabel series stay in the open 2-hour head block. Expect it to fall to ~16k on its own; use the per-job count, not head_series, when judging cardinality
- **OPEN: the public API is HTTP only.** TLS needs a DNS name, since Let's Encrypt will not issue for `64.181.195.241`. Writes are token-guarded so credentials do not cross the wire in the clear, but the bearer token itself does. Fix with P6's Cloudflare item, or earlier with a free `sslip.io`-style name plus cert-manager if P3 wants HTTPS
- **OPEN: the Postgres volume has no backup.** Three guards stop it being *deleted* — `prevent_destroy` on the terraform volume, `Retain` on both the PV and the StorageClass, and `node-volume-setup.sh` refusing to format a disk that already has a filesystem — but none of those is a backup, and corruption or a bad migration is not covered. The tenancy has **5 free OCI volume backups** available (`terraform output pgdata_volume_id`). Low urgency while the database is empty; this becomes real the moment P4 writes traces that are not in `recovery.jsonl`
- **The block volume's disk prep is not in cloud-init and cannot be.** `node-volume-setup.sh` runs by hand over SSH because `user_data` executes only at first boot and this instance will never boot fresh again. If the instance is ever lost, restoring means: launch, run the bootstrap, run that script, re-attach the volume, recreate the secret. Written down because it is exactly the step a rebuild would forget
- Databricks Free Edition external access path for pushing files and running dbt from outside is unverified. Check at the start of P5
- `pre-commit run --all-files` reported nothing before the first commit because the scaffold was untracked. Resolved, hooks engage from the first commit onward

## 7. Next action

Two independent threads. **Thread A (local, M1)** is blocked on wall-clock — v1 is
training and there is nothing to do but wait. **Thread B (OCI)** is where work can
actually proceed right now, and it costs the Mac nothing: everything runs on the
node through the SSH tunnel. See "Thread B" below.

### Thread A — v1 training on the M1

**v1 is running. The next action is to wait, then select its checkpoint.** Launched 2026-08-02 18:00 with exactly this command:

```bash
nohup caffeinate -is env HF_HUB_OFFLINE=1 \
  uv run python -m mlx_lm lora --model mlx-community/Qwen3-1.7B-8bit \
    --train --data training/artifacts/dataset \
    --max-seq-length 6528 --grad-checkpoint --mask-prompt \
    --batch-size 1 --grad-accumulation-steps 4 \
    --num-layers 16 --learning-rate 5e-5 \
    --iters 2956 --steps-per-eval 200 --val-batches 25 --save-every 200 \
    --adapter-path training/artifacts/adapters-v1 \
  > training/artifacts/v1_console.log 2>&1 &
```

Adapters at `training/artifacts/adapters-v1/`, console log at `training/artifacts/v1_console.log`. ~12.6 s/iter, peak 11.030 GB, ETA ~04:20. Leave the lid open and stay on mains: `caffeinate -is` blocks idle sleep but not lid-close suspend.

**When it finishes, in order:**

```bash
uv run evalgate-training train eval-adapters      # full 140-row valid split, ~45 min per 4 arms
```

Score **every** numbered checkpoint (200 … 2800) plus the final weights at 2956 on the **full 140-row valid split**, take the lowest, break ties within 0.01 toward the earlier checkpoint. That rule is pre-committed in DECISIONS.md and applies identically to v2. **Do not select on the in-training val numbers** — `evaluate` gets no seed, so each one scores a different reshuffled 18% subsample.

Then run the P1.3 pipeline (below) against the selected adapter and put the harness through a real end-to-end run: `build-suite` on the golden 96, `LlamaServerModel` against a live llama-server, and the first judge call on `gpt-5.4-mini`.

**v2 is deliberately deferred until that whole path is verified against v1.** Another 9 hours spent before knowing the harness can score a real model is 9 hours bet on untested plumbing; the v1→v2 comparison is only worth having once the instrument reading it works.

*(The in-training val prints for v1 reproduce the seedless-subsample artefact already logged — same non-monotonic shape at iter 400, offset from the probe's by ~0.22 on identical settings. That is the artefact, not a difference between runs, and neither series is a quality signal. The full-split re-measurement is the only number to read.)*

**P1.3, proven and repeatable.** Every stage ran against the probe adapter. Re-run verbatim on `adapters-v1` once v1's checkpoint is selected:

```bash
brew install llama.cpp                            # arm64 bottle, all binaries Mach-O arm64
uv run --group mlx python -c "from huggingface_hub import snapshot_download; \
  snapshot_download('mlx-community/Qwen3-1.7B-8bit')"      # once, with network, for fuse

uv run --group mlx python -m mlx_lm fuse --model mlx-community/Qwen3-1.7B-8bit \
  --adapter-path training/artifacts/adapters-v1 \
  --save-path training/.scratch/fused-v1-bf16 --dequantize          # 3.2 GB

cd training/.scratch/llamacpp/llama.cpp-b10210                       # tarball, not a clone
uv run --no-project --with-requirements requirements/requirements-convert_hf_to_gguf.txt \
  python convert_hf_to_gguf.py <fused dir> --outfile <out>.f16.gguf --outtype f16   # 3.44 GB

llama-quantize <out>.f16.gguf <out>.Q4_K_M.gguf Q4_K_M               # 1.03 GB, 12.5 s
llama-server -m <out>.Q4_K_M.gguf -c 8192 --host 127.0.0.1 --port 8080
```

Sizes: 1.8 GB base → 3.2 GB fused bf16 → 3.44 GB f16 GGUF → **1.03 GB Q4_K_M** (5.12 BPW, a 3.3x reduction). Scratch total 9.4 GB, 579 GB free. **Serving: 84.0 tok/s generation, ~1,000 tok/s prefill, ~4.5 s per real case** — so a 96-case suite is ~7 min of model time.

**Two conversion gotchas, both load-bearing.** The converter pins `numpy~=1.26.4` and `transformers==4.57.6` against the project's numpy 2.5.1 and transformers 5.14.1, so it **must** run under `uv run --no-project --with-requirements` — installing it into the venv would break mlx-lm. And `mlx_lm.fuse` resolves the model with `local_files_only=True` and demands a complete snapshot, so run `snapshot_download` once with network before going offline.

**Why P1.3 is not run in parallel with training — the earlier claim was wrong.** Training peaks at 11.030 GB against the 12.71 GB working set, leaving 1.68 GB. `mlx_lm.fuse` peaks at **3.311 GB** measured; llama-server at Q4_K_M with a 6,528 context needs **~2.1-2.4 GB** (~1.1-1.3 GB weights plus 0.75 GB KV cache, at 0.109 MB per token for 28 layers x 8 KV heads x 128 head_dim x 2 bytes x 2). Either alone is about double the headroom, and the failure mode is not a clean OOM — swap pressure is what produced the `nan` on the first bf16 attempt, so the cost is a corrupted 9-hour run that reads like a numerical bug.

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

### Thread B — P2 on OCI

Postgres, the API, monitoring, and k6 all landed 2026-08-03. **Thread B is now
blocked on Thread A**: the one remaining P2 build item is the model server, and
it needs v1's selected checkpoint through the P1.3 pipeline first.

**When v1 finishes**, in order:

1. Select the checkpoint (Thread A), run the P1.3 pipeline against it, get a
   Q4_K_M GGUF.
2. **Model server → k3s.** Reuse `infra/k6/`'s sibling pattern — `build-on-node.sh`
   is proven three times now. The GGUF is ~1 GB, so it needs a volume decision:
   `local-path` on the boot volume (39 GB free) rather than the block volume,
   same reasoning as the Prometheus TSDB — model weights are re-derivable from
   the adapters, the database is not.
3. Then decide what to do about the exit criterion's literal *"`terraform apply`
   goes from zero to a running public API"* clause. See the table in section 1;
   it is the one clause that does not hold as written.

Optionally, and cheaply: re-run k6 once the model server is on the node, to see
what the API's numbers look like when it is no longer the only thing competing
for 4 OCPU. The current figures were taken with the node otherwise quiet.

Bringing everything up, or confirming it is already up:

```bash
./up.sh          # idempotent, ~45 s against a live system, prints the URL
```

Reaching the cluster directly, every time:

```bash
cd infra/terraform && export AWS_REQUEST_CHECKSUM_CALCULATION=when_required
eval "$(terraform output -raw kubectl_tunnel_command)" &   # leave running
export KUBECONFIG=~/.kube/evalgate.yaml
kubectl -n evalgate get pod,svc,ingress,pvc
curl -sS http://64.181.195.241/ready
```

Grafana and Prometheus, both ClusterIP:

```bash
kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80    # then localhost:3000
kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d; echo
kubectl -n monitoring port-forward svc/monitoring-prometheus 9090:9090
```

Rebuilding and redeploying the API after a code change:

```bash
./infra/k8s/api/build-on-node.sh                      # ~24 s, native arm64 on the node
KUBECONFIG=~/.kube/evalgate.yaml EVALGATE_NODE_SSH="ssh -i ~/.ssh/evalgate_ed25519 ubuntu@64.181.195.241" \
  ./infra/k8s/api/apply.sh
kubectl -n evalgate rollout restart deployment/evalgate-api   # same tag, so force it
```

Four things a fresh session will otherwise get wrong.

- **Neither secret is in the repo.** Read them from the cluster:
  `kubectl -n evalgate get secret postgres-credentials -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d`,
  same shape for `evalgate-api-token`. If `postgres-credentials` is ever
  recreated you must also `ALTER ROLE` — `POSTGRES_PASSWORD` is read only by
  `initdb`, on a first boot.
- **The image tag never changes**, so `kubectl apply` alone will not restart pods
  after a rebuild. Use `rollout restart`.
- **Never point `EVALGATE_TEST_DATABASE_URL` at `evalgate`.** The store tests
  `TRUNCATE` on teardown. Use `evalgate_test`; the fixture now refuses anything
  whose name does not end in `_test`.
- Any `terraform plan` touching the instance must read **`0 to change, 0 to
  destroy`** before you apply — the A1 shape is a capacity lottery and the
  instance is not reobtainable.

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

**Which set the harness scores against: the golden 96, not the test 140.** They are different instruments. The golden 96 is the only set whose reference answers are **human-approved** (92/96 at hand review), it is balanced 6 per (category, repo) cell by construction so per-category comparisons are equally powered, and `build-suite` already builds from it. The test 140 is held out from training but its references are unreviewed teacher output carrying the same ~4% error the review measured — feeding those to an LLM judge as ground truth injects noise into exactly the comparison the gate depends on. Both sets are disjoint from training for **both** versions, so either is fair; only one has vetted references.

**Whether being the hand-reviewed sample makes the golden 96 right for v1 vs v2 — mostly right, with one real limit and one real risk.** Right, because it is the only place a judge can compare against an answer a human signed off, and because per-cell balance means the refusal category is as well sampled as any other. The limit is **n=24 per category (±12 to ±20 points)**, already recorded: BUILD_PLAN expects v2's refusal category to *collapse*, which is a large effect and detectable, but a subtle regression would not be. The risk is **selection pressure on a reused set** — the same 96 cases inform the gate, the demo, and any tuning, so over time the project fits them. Mitigation available at no cost: the test 140 needs no references for the **rule-based** scorers (citation validity, refusal correctness), so run those over all 140 as a larger confirmation set and keep the judge on the golden 96. That gets more power where references are irrelevant and vetted references where they are not.

**The P1.4 endpoint contract, now implemented.** `ModelClient` is one method — `generate(case: Case) -> str` plus a `ref` string. llama-server exposes an OpenAI-compatible `/v1/chat/completions` returning `choices[0].message.content`, plus `/health`, `/props` and a `timings` block. **The protocol needs no change.** What P1.4 must add:

- ✅ `LlamaServerModel` in `evalcore/model.py` — POST at `temperature 0`, `chat_template_kwargs: {"enable_thinking": false}` explicit, `ref` = `llama-server:<version>:<quantization>` so the diff report labels runs.
- ✅ **Prompt drift closed.** `evalcore.prompt` is now the one renderer for the teacher calls, the training rows and the eval client. Guarded by `test_prompt.py` in CI and by the split digests locally.
- ✅ Overflow: `ContextOverflowError` carrying `n_prompt_tokens`/`n_ctx`, caught by `run_case` into the existing per-case error path.
- **Still to do at P1.4:** point `cmd_run` at the client, and serve both versions as **two llama-server processes on different ports** (one model per process); `ref` distinguishes them.
- Thinking mode: with and without `chat_template_kwargs` both produced no `<think>` block, because the training targets already carry the empty `<think></think>`. It is passed explicitly anyway so the served prompt matches training exactly rather than relying on learned behaviour.

**The probe adapter is discarded.** It did its job: the pipeline ran end to end and the served model emits the trained shape. Three hand checks on **test-split** cases, qualitative only — it produced `[C1][C4][C5]`-style markers on every answer, it refused the adversarial case naming the absent symbol (`EnableSecureScraping`), and its factual answer matched the teacher's chunk content exactly. **None of that is a quality measurement and none of it is in DECISIONS.md as one** — these are 0.41-epoch weights, 4 cases, no per-category meaning. It must never become v1. `training/.scratch/fused-probe*`, `gguf/` and the probe adapters are all disposable; the pipeline, sizes, throughput, context decision and endpoint contract are what carry forward. **Correction: this work is sequenced before the long runs, not parallel with them — see the memory numbers above.**

Then `mlx_lm.fuse`, P1.3 (GGUF + llama.cpp), and P1.4, which is already built against stubs and needs only the runner pointed at a real endpoint. Two questions from the hand review travel into the P1.2 eval and are recorded in section 6: whether fastapi underperforms, and whether comparison refusals exceed the teacher's 36%.

**P2 remainder, independent of all of the above.** In rough order: deploy Postgres onto k3s with a block volume PVC; deploy `apps/api` (needs an arm64 image and `store.py`'s Postgres DDL wired up in place of `MemoryStore`); add `/metrics` to FastAPI and install kube-prometheus-stack via helm, sized to 24 GB; commit a Grafana dashboard JSON; write and run the k6 script and record RPS, p95, error rate, and node CPU/RAM idle versus loaded. The model server waits on P1.3 producing a GGUF.

Nothing in P2's remainder blocks P1.2, and P1.2 blocks nothing in P2 except the model server, which waits on P1.3's GGUF. **But while v1 is training, P2 work on this laptop is not free either** — anything that starts Docker or a local cluster competes for the same 16 GB. P2 work against the OCI node over SSH is fine.

## 8. Session log

Newest first. Three to five lines each. What was attempted, what landed, what broke, what the next session needs to know.

**2026-08-03 P1.4 client and prompt unification, written while v1 trains. Nothing run against a live server.** BUILD_PLAN P1.4 wants evalcore's schema/runner/scorers (including citation validity as a *rule-based* check), a cached judge client with 429 backoff, a case-level diff report grouped by category, apps/api v0, and arm64 Dockerfiles; its exit is the harness *detecting* the v1→v2 regression and naming the broken category and cases. Two pieces landed. **The prompt drift risk turned out to be worse than flagged:** `training` already had **two** renderers — `teacher.prompts.build_messages` for the API calls and `dataset.build.to_example` for the training rows — agreeing only by hand, and the eval client would have made three. Verified they were byte-identical (so no data is affected), then collapsed all three into `evalcore.prompt`. Chose the single-source route over fixture-pinning because a fixture only catches drift it happens to exercise. Guarded twice, in different places: `test_prompt.py` pins the bytes and fails in CI, which cannot see the gitignored splits, and `dataset_manifest.json`'s per-split sha256 fails locally over the real 1,758 rows. Used the digests as the acceptance test — after rewiring, train/valid/test still matched byte for byte, so the refactor is proven inert rather than assumed to be. **`LlamaServerModel`** implements the existing protocol unchanged: stdlib urllib, temperature 0, `enable_thinking: false` explicit, `ref` carrying version and quantization, and a dedicated `ContextOverflowError` separate from `ModelError` because overflow is a suite-configuration fault no retry fixes. Tested against a threaded `http.server` replaying payloads **transcribed from the real llama-server in P1.3**, including the overflow body verbatim; `run_case` turns overflow into a per-case error rather than a crash. Suite 178 → 194. Judge cost measured from real golden-set tokens rather than guessed: gpt-5.4-mini $0.253/run, gpt-4.1 $0.616/run, against $2.1070 remaining — **the judge choice is the one open decision and it needs a human.**

**2026-08-03 P1.3 serving pipeline proven end to end against throwaway probe weights.** BUILD_PLAN P1.3 wants GGUF + llama.cpp because MLX cannot run on OCI Ampere — this is the prod path, not a local convenience — plus Q4_K_M, an OpenAI-compatible endpoint, and both versions servable. All of it ran: fuse `--dequantize` → 3.2 GB bf16, `convert_hf_to_gguf.py` → 3.44 GB f16, `llama-quantize` → **1.03 GB Q4_K_M at 5.12 BPW** (3.3x reduction, 12.5 s), served on llama-server. Every brew binary is `Mach-O 64-bit executable arm64`; nothing pulls x86 and nothing runs under Rosetta. **Serving 84.0 tok/s generation, ~1,000 tok/s prefill, ~4.5 s per real case, so a 96-case suite is ~7 min of model time** — the P1.4 budget will be dominated by the judge, not the model. Two conversion traps, both caught before they could do damage: the converter pins numpy 1.26 and transformers 4.57 against the project's 2.5.1 and 5.14.1, so installing it into the venv would have broken mlx-lm on the eve of an 18-hour run — it runs under `uv run --no-project --with-requirements` instead, which also keeps torch out of `uv.lock` and out of CI; and `mlx_lm.fuse` demands a complete HF snapshot, so `snapshot_download` must run once with network before going offline. Settled `-c 8192` for serving: 6528 was a training *sequence* budget covering prompt plus answer, while the prompt alone reaches 6,357 tokens, and a 12,009-token prompt returns a clean **HTTP 400 `exceed_context_size_error` rather than silent truncation**, which is exactly what the harness needs. On P1.4: the `ModelClient` protocol needs no change, but flagged a real latent risk — the eval prompt has no shared source of truth with `to_example()` in the training package, so a re-implemented renderer could drift on a separator and shift every score silently. **The probe adapter and all its artifacts are discarded**; the three hand checks (citation markers present, refusal on the adversarial case, factual answer matching the teacher's chunk) are qualitative shape checks on 4 test-split cases and are deliberately absent from DECISIONS.md as quality numbers.

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
