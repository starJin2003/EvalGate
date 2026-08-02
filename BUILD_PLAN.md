# EvalGate Build Plan

Locked 2026-07-31. This file is the single brief for every Claude Code session. Read it fully before writing code. DECISIONS.md is the running log and lives next to this file. All web-sensitive facts in section 11 were verified on 2026-07-31.

## 1. What we are building

A platform that catches quality regressions in LLM apps and blocks bad deploys. A user registers an eval suite, the platform runs it daily, harvests failing production traces into the suite, shows case level diffs when scores drop, and blocks PR merges through a GitHub Actions check. Watched apps are PharmAgent, PromptFighter, and a LoRA model we fine tune ourselves for version to version regression demos.

The real objective is resume evidence. Every phase must end with measured numbers and documented decisions. Speed matters. The visible surface (README, live demo, diff reports, dashboards) matters more than internal polish.

## 2. Working agreement for Claude Code

1. Move fast. Prefer boring proven libraries. No speculative abstractions. Build the thinnest thing that passes the exit criteria, then iterate.
2. Never run git commands. At the end of every work block output a GIT block in exactly this shape so Jinwoo can paste it into the terminal.

```
GIT BLOCK
git add <specific files>
git commit -m "feat(scope): short message"
git push origin <branch>
```

3. Update DECISIONS.md in the same work block as the change it describes. Three entry kinds. Decisions with trade offs. Problems with root cause and fix. Measured numbers with context. One to three lines each. Numbers beat adjectives.
4. Ask before adding any new external service, paid tier, or heavyweight dependency. Total spend must stay at zero dollars.
5. Secrets never enter the repo. Use .env locally, GitHub Actions secrets in CI, Kubernetes secrets on OCI. Always ship .env.example.
6. arm64 is the only architecture. The dev laptop is an Apple M1 Pro and the server is OCI Ampere A1. Images built and tested locally run unchanged on the server. Never introduce an amd64-only dependency without flagging it.
7. A phase is done when a stranger can follow the README and reproduce the demo. Update README in the same phase, not later.
8. Decide freely inside a phase (library choice, schema details, file layout). Stop and ask only for the marked DECISION POINTs and for anything that costs money or touches accounts.

## 3. One time local setup (M1 Pro, 16 GB, macOS 26)

[YOU] run once.

```bash
# Homebrew if missing
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install uv node kubectl helm terraform k6 jq libpq llama.cpp
brew install --cask orbstack
```

Notes. uv manages Python versions and virtualenvs and the target is Python 3.12. OrbStack provides docker and docker compose, is free for personal use, and is light on Apple Silicon. Docker Desktop is a fine substitute. llama.cpp from brew serves quantized GGUF models locally during P1. Fine tuning runs on this same M1 Pro through MLX, so the whole project is arm64 Apple Silicon in dev and arm64 Ampere in prod, with no second machine and no CUDA anywhere.

## 4. Claude Code setup

[YOU] once.

1. In VS Code install the Claude Code extension published by Anthropic from the marketplace. It bundles its own CLI and needs VS Code 1.98 or newer.
2. First launch opens a browser sign in. Use the Claude Max account.
3. Open `~/Desktop/Dev/EvalGate` as the workspace root.

Conventions.

- CLAUDE.md at the repo root carries the standing rules and loads automatically every session.
- `.claude/settings.json` denies git as a backstop. The CLAUDE.md rule is the primary enforcement because Bash permission rules have been reported as unreliable.
- Start each phase in plan mode, review the plan, then execute.
- One phase per conversation. Kick off by pasting that phase's section from this file plus any DECISION POINT answers.

## 5. Accounts and web setup (product side)

[YOU] in the browser. Do each at the phase that needs it, not earlier.

| # | Service | Phase | What to do | Cost |
|---|---|---|---|---|
| 1 | GitHub | P0 | Create public repo `evalgate`. In P3 add branch protection on main requiring the `eval-gate` check and add Actions secrets | Free |
| 2 | Hugging Face | P1 | Account plus a read and write token. Base model download and adapter upload | Free |
| 3 | OpenAI Platform | P1 | API key on the account holding roughly 14 dollars of credit. This is platform.openai.com credit, separate from ChatGPT Codex credits, which cannot be called from scripts. Keep the 20 dollar monthly cap | Existing credit |
| 4 | Google AI Studio | P1 fallback | Gemini API key as a fallback provider only. Read the live per project limits in the AI Studio dashboard rather than trusting published numbers | Free |
| 5 | Kaggle or Colab | P1 fallback | Capacity fallback only, if M1 memory would force truncating training sequences and compromise the data. Kaggle is a fixed 30 GPU hours per week on T4 or P100 | Free |
| 6 | Oracle Cloud OCI | P2 | Sign up, then upgrade to Pay As You Go. A card is required and the roughly 100 dollar temporary hold refunds. After upgrade verify the Ampere A1 free allowance actually shows 4 OCPU 24 GB, set a budget alert at 1 dollar, then create the instance | 0 target |
| 7 | Databricks Free Edition | P5 | Sign up with email OTP or Google. Do the LinkedIn verification to raise limits. Serverless only and non commercial only | Free |
| 8 | Cloudflare | P6 | Account for Workers and R2 | Free |
| 9 | Stripe | P6 | Account, stay in test or sandbox mode | Free |

## 6. Repo layout (target shape, adjust as needed)

```
evalgate/
  CLAUDE.md            standing instructions for Claude Code
  BUILD_PLAN.md        this file
  DECISIONS.md         running log
  README.md            architecture diagram, quickstart, badges
  apps/api/            FastAPI service (suites, runs, diffs, traces, promotion)
  packages/evalcore/   eval harness (runner, scorers, judge client, diff)
  packages/sdk/        pip installable trace client
  workers/             Kafka consumer, promotion worker, judge rescore worker
  training/            data prep, MLX LoRA scripts, GGUF convert and quantize scripts
  infra/terraform/     OCI provisioning
  infra/k8s/           manifests and helm values
  analytics/           export DAG helpers, Spark jobs, dbt project
  .github/workflows/   ci.yml, eval-gate.yml
  docker-compose.dev.yml
```

## 7. Phase plan

Each phase lists Build (Claude Code), [YOU] Setup (browser and terminal actions for Jinwoo), Exit criteria, and Record (numbers that must land in DECISIONS.md). Aggressive time guides assume full days.

### P0 Bootstrap (0.5 day)

Build.
- uv managed Python 3.12 monorepo per section 6. ruff, pytest, pre-commit.
- docker-compose.dev.yml with postgres:16 plus pgvector.
- .github/workflows/ci.yml running lint and tests.
- Seed CLAUDE.md (section 9), DECISIONS.md, README skeleton.

[YOU] Setup. Create the GitHub repo and push the scaffold.

Exit. `docker compose up` yields Postgres with pgvector. CI is green.

Record. Scaffold decisions only if any were non obvious.

### P1 Core (4 to 5 days)

Decided 2026-07-31. Base model Qwen3 1.7B Instruct, thinking mode off. Task is grounded documentation QA over open source docs. The model does not learn domain knowledge. It learns to answer only from supplied chunks, cite every claim, and refuse when the chunks do not contain the answer. This is a distillation of a RAG generation node into a small local model, and the corpus is the stack EvalGate itself runs on.

Corpus. Shallow clone four markdown native docs repos and parse only markdown. FastAPI, Pydantic, Prometheus, Airflow. Skip anything in SGML, RST that needs a toolchain, or templated HTML. Strip frontmatter, split on H2 and H3, keep repo name and file path and heading path and source URL anchor as metadata.

Providers. Teacher and judge both OpenAI, on the account with roughly 14 dollars of credit and a 20 dollar monthly cap. Gemini free tier is fallback only, because published free tier numbers disagree across sources and the live per project value is what actually applies. Teacher and judge must be different models so the judge is not scoring its own writing style.

Cost control is structural, not advisory. Judge responses cached on case plus output plus judge version. Teacher generation through the Batch API. A twenty item dry run measures real tokens before any full run. A cumulative token ceiling in the scripts that halts execution when crossed. Record actual spend.

P1.1 Data (1 day)
- Corpus parser producing chunks with metadata.
- Question generator. Roughly 1500 to 2000 questions across four categories. Factual lookup, how to, comparison, and adversarial refusal cases that ask about APIs or versions absent from the corpus.
- Retrieval over the chunks to attach context to each question. Reuse pgvector from P0.
- Teacher answers through Batch API.
- Golden eval set of 80 to 100 cases held out and hand reviewed by Jinwoo. Never used for training. Unreviewed teacher output must not become eval ground truth.

P1.2 Training (1 day)
- MLX on the M1 Pro. `mlx-lm` for LoRA, `mlx_lm.fuse` to merge the adapter, then the existing GGUF path. No second machine and no CUDA anywhere in the project.
- Do not assume a 4-bit base. Qwen3 1.7B is roughly 3.4 GB in bf16, and the 4-bit guidance circulating in the MLX community targets 9B and larger. Try bf16 LoRA first, fall back to 8-bit, then 4-bit. Record which precision actually fit and the peak memory it used.
- Measure the real token length distribution of the finished dataset and set max sequence length from it. Training samples run near 3000 tokens at k=8, so a guessed value either truncates context or wastes memory.
- Stop the dev Docker stack before training. The corpus is not needed once retrieved contexts are baked into the dataset, and 16 GB is shared with macOS.
- v1 trained on a balanced mix across all four categories.
- v2 trained on a mix that raises practical question weight and cuts refusal examples. Expected outcome is a higher overall score with the refusal category collapsing. This is a real data mix experiment, not a fabricated regression, and the cause is documented.
- Adapters pushed to Hugging Face.

P1.3 Serving (0.5 day)
- Unchanged by the MLX switch. MLX cannot run on OCI Ampere, so GGUF plus llama.cpp was already the serving path for both local and prod.
- Merge LoRA, convert to GGUF, quantize Q4_K_M.
- llama-server with an OpenAI compatible endpoint. Both versions servable so the harness can hit either.

P1.4 Harness (1.5 to 2 days)
- packages/evalcore. Suite and case schema, runner, scorers covering exact match, regex, citation validity as a rule based check, and LLM as judge.
- Judge client with a provider abstraction, exponential backoff on 429, and the response cache.
- Case level diff report in terminal and HTML showing old versus new output with judge rationale, grouped by category.
- apps/api v0. Register suite, trigger run, store results in Postgres, list runs, fetch a diff between two runs.
- Dockerfiles, arm64.

[YOU] Setup. Hugging Face account and token. OpenAI API key. Hand review the golden set. Before P1.2 training, stop the dev Docker stack with `docker compose -f docker-compose.dev.yml down` so the 16 GB is not shared with Postgres. Kaggle or Colab only as a capacity fallback, if M1 memory forces a sequence truncation that would compromise the training data.

Exit. The harness detects the v1 to v2 regression, the diff report names the broken category and the specific cases, and the README quickstart reproduces the run on the Mac against a downloaded GGUF.

Record. Corpus size in files and chunks. Dataset sizes per category. Training wall time and VRAM peak per version. Serving tokens per second for Q4 on the M1. Eval wall time per case and per suite. Judge tokens and dollars per run. v1 versus v2 score delta overall and per category. Actual total spend.

### P2 Infra (2 days)

[YOU] Setup. OCI flow from section 5 row 6. Generate an SSH keypair. Pick a home region and note that region choice is permanent per tenancy.

Build.
- infra/terraform. OCI provider, VCN, subnet, security list, one VM.Standard.A1.Flex instance at 4 OCPU 24 GB on Ubuntu 24.04 arm64, cloud-init installing k3s.
- Deploy api, Postgres with a block volume PVC, and the quantized model server onto k3s.
- kube-prometheus-stack via helm. Instrument FastAPI with /metrics. Commit a Grafana dashboard JSON.
- k6 script against the API. Run it and record.

Contingency. If the PAYG allowance verification shows only 2 OCPU 12 GB, shrink the instance, keep the 1B model, trim Kafka plans, and log the decision.

Exit. `terraform apply` goes from zero to a running public API. Dashboards live. k6 numbers recorded.

Record. Apply wall time. k6 RPS, p95, error rate. Node CPU and RAM at idle and under load. The PAYG allowance verification result.

### P3 Automation (1.5 days)

[YOU] Setup. GitHub Actions secrets (judge key, API endpoint). Branch protection on main requiring the `eval-gate` check. From this phase on, all work moves to feature branches and PRs so the gate has something to gate.

Build.
- Airflow 3.3 on k3s via the official helm chart, sized to the memory budget.
- Daily DAG per suite. Run eval, write scores, maintain a baseline table, flag drops.
- .github/workflows/eval-gate.yml. On PR, evaluate the candidate against the baseline from main, post a case diff comment on the PR, fail the check when the score drop exceeds a per suite threshold.
- Demo. One intentionally regressing PR that gets blocked and one fixed PR that passes. Screenshot both for the README.

Exit. A merge is actually blocked by a regression. The daily DAG runs green three days straight.

Record. Gate wall time. DAG runtime. Threshold config.

### P4 Ingestion (2 to 3 days)

Build.
- packages/sdk. Async fire and forget trace posting (input, output, latency, cost, metadata) with batching.
- Ingest endpoint to a Kafka topic. Kafka 4.x in KRaft mode, a single combined broker and controller on k3s with a hard memory cap. Consumer worker writes traces to Postgres.
- Promotion. Failed or low scoring traces become suite candidates through rules plus judge score, with a human approval list in the API and an auto promote flag.
- pgvector failure clustering. Embed failure traces, cluster, expose a cluster view that names failure types.
- Dogfood. Instrument PharmAgent and PromptFighter with the SDK.

DECISION POINT. Kafka memory budget, single versus dual instance, decided after the P2 allowance verification.

Exit. Traces flow end to end. A real failed case gets promoted and shows up in the next daily run. Replay from Kafka works.

Record. Ingest throughput under k6. Consumer lag under load. Promotion counts. Cluster count and sizes.

### P5 Analytics (2 days)

[YOU] Setup. Databricks Free Edition signup plus LinkedIn verification. Create a catalog, schema, and volume. At phase start verify the external access path (personal access token or OAuth) for pushing files and running dbt from outside.

Build.
- Daily export DAG. Postgres to parquet, upload to a Unity Catalog volume through the Databricks Files API, COPY INTO Delta tables.
- Spark jobs for non LLM metric recomputation, token statistics, drift analysis, and large aggregations. LLM judge rescoring stays on OCI workers because Free Edition outbound is restricted to trusted domains and cannot call LLM APIs.
- dbt marts for version over version trends. Primary route is a dbt task on a Databricks Lakeflow job. Fallback is local dbt Core with dbt-databricks if external auth verified.
- One drift chart on a Databricks dashboard or Grafana.

Exit. Yesterday's traces are queryable in Delta. Marts build daily. The drift chart exists.

Record. Export volume per day. Job runtimes. Which Free Edition quotas were approached (5 concurrent job tasks, one pipeline per type, one 2X-Small SQL warehouse).

### P6 Optional (1 day)

[YOU] Setup. Cloudflare account. Stripe account in test mode.

Build.
- Cloudflare Worker plus R2 public report page and a README score badge per suite.
- Stripe metered billing on API call volume as a fully separate component. No Databricks derived data may touch the paid path because Free Edition bans commercial use.
- Optional gRPC between API and workers.

Exit. The badge renders in the README from live data.

Record. Publish latency. Worker request counts.

## 8. Git workflow

Jinwoo runs all git. Claude Code only emits GIT blocks.

- Before P3, commit straight to main for speed.
- From P3, feature branches and PRs so the eval gate is exercised. Branch names `feat/<thing>`, `fix/<thing>`, `infra/<thing>`.
- Conventional commits. feat, fix, infra, docs, chore, test.
- Common paste-ready commands.

```bash
git checkout -b feat/<thing>
git add <files>
git commit -m "feat(scope): message"
git push -u origin feat/<thing>
# open PR on github.com, gate runs, merge when green
git checkout main && git pull
```

## 9. CLAUDE.md

Lives at the repo root and loads automatically every Claude Code session. It carries the standing rules (no git, DECISIONS.md logging, arm64, zero budget, secrets). Keep it short so it always fits. Do not duplicate this build plan into it.

## 10. Credibility artifacts (carried across all phases)

Live URL from P2 onward. Architecture diagram in the README from P1. DECISIONS.md maintained from P0. Per layer measured numbers per phase Record lists. One retrospective blog post after P5.

## 11. Verified facts, checked 2026-07-31

- Databricks Free Edition. Serverless only. Per account quotas include 5 concurrent job tasks, one SQL warehouse capped at 2X-Small, one active pipeline per type, one app, one vector search endpoint. Outbound internet from serverless compute is restricted to trusted domains, so no LLM API calls from Spark. Exceeding quota shuts compute for the rest of the day or month but data survives. Non commercial use only. LinkedIn verification can raise limits.
- OCI. Always Free Ampere A1 was cut to 2 OCPU 12 GB effective June 15 2026 with no announcement. Human support agents state PAYG tenancies keep 4 OCPU 24 GB free, but official docs do not distinguish account types. Treat 4 24 as unconfirmed until verified in the console after upgrade. Set a 1 dollar budget alert immediately.
- Apache Kafka. The 4.x line is KRaft only and ZooKeeper is removed. 4.2 went GA on 2026-02-17. Brokers require Java 17. A combined broker and controller node is the sanctioned small deployment shape.
- Apache Airflow. 3.3.0 released 2026-07-06. Requires Python 3.10 plus and 3.12 is recommended.
- Training compute. MLX, Apple's array framework for Apple Silicon. Runs on every Apple Silicon generation from M1 onward and uses the unified memory shared with the OS, so there is no separate VRAM budget. `mlx-lm` covers LoRA and QLoRA plus `mlx_lm.fuse` for merging adapters. On a 16 GB machine the memory pressure comes from sequence length rather than model size: Qwen3 1.7B is roughly 3.4 GB in bf16, while attention and optimizer state over ~3000-token samples is what actually decides whether a run fits. MLX is Apple-only and cannot serve on OCI Ampere, which is why GGUF plus llama.cpp remains the serving path. Fallbacks are Kaggle at a fixed 30 GPU hours per week on T4 or P100, and Colab free at a dynamic 15 to 30 hours per week, wanted only if M1 memory would force truncating training sequences.
- Gemini API free tier. Exists and needs no card, but published numbers disagree across sources and were cut sharply in December 2025. Pro moved behind billing and the free tier now covers Flash and Flash-Lite. Per model figures live in the AI Studio dashboard rather than static docs, and daily request caps bite long running jobs hardest. Treat it as a fallback provider, never as the plan. Free tier prompts and outputs may be used for training.
- Cloudflare free tier. Workers at 100K requests per day. R2 at 10 GB storage, 1M class A and 10M class B operations per month, zero egress.
- Claude Code. The VS Code extension bundles its own CLI, requires VS Code 1.98 or newer, and authenticates through the Claude account in a browser flow.

## 12. Open decisions

1. Kafka memory budget and single versus dual instance. Decide after the P2 allowance verification.

Closed 2026-07-31. Project name is EvalGate. Base model is Qwen3 1.7B Instruct. Task is grounded documentation QA over open source docs. Teacher and judge are both OpenAI with Gemini as fallback.

Closed 2026-08-01. Fine tuning runs on the M1 Pro through MLX. The RTX 5080 Windows machine is out of the plan entirely.
