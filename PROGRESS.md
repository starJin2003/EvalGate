# PROGRESS

Current state of EvalGate. Read this first in every session. Update it before ending every session.

Three files, three jobs. BUILD_PLAN.md is the plan and rarely changes. DECISIONS.md is an append only log of rationale and measurements. This file is mutable current state and gets overwritten freely.

Last updated 2026-08-01.

---

## 1. Where we are

**Phase P1.1 Data, in progress.**

| Phase | Status |
|---|---|
| P0 Bootstrap | Done 2026-07-31 |
| P1.1 Data | In progress |
| P1.2 Training | Not started |
| P1.3 Serving | Not started |
| P1.4 Harness | Built against stubs 2026-08-01; needs a real endpoint at P1.3 |
| P2 Infra | Not started |
| P3 Automation | Gate workflow and threshold logic scaffolded 2026-08-01 |
| P4 Ingestion | Not started |
| P5 Analytics | Not started |
| P6 Optional | Not started |

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
- Top-ups backfill per (category, repo) cell using per-repo leak rates, not an aggregate gap. `questions trim` removes surplus from cells that overshoot
- Teacher regeneration capped at 2 attempts per row via `questions.teacher_attempts`, then the row is dropped
- Two dataset-poisoning guards in `teacher/validate.py`: `fabricated_absent_side` for one-sided comparisons, `answered_absent` for adversarial rows that did not refuse
- `config.decide_k()` holds the pre-committed retrieval-k rule; nothing about k is decided after seeing the measurement
- `training/artifacts/chunk_manifest.jsonl` and `ledger.json` committed. `training/.scratch/` holds vendored docs and is gitignored
- `packages/evalcore/` is the harness: `schema.py` (suite, case, thresholds, results), `scorers.py` (exact, regex, citation, refusal), `judge.py` (provider abstraction, SQLite cache keyed on case+output+rubric+judge version, full-jitter backoff), `runner.py` (`ModelClient` protocol plus `StubModel`/`EchoContextModel`), `diff.py` (case deltas and the breach logic), `report.py` (terminal, markdown PR comment, self-contained HTML), `loader.py`, `cli.py`
- `evalgate-eval` CLI: `build-suite` from the golden export, `run`, `gate`. Exit 1 from `gate` is the merge block
- `apps/api/` v0: register suites, submit runs, list runs, explicit baseline promotion per (suite, branch), `/diff`, and `/suites/{id}/gate` returning the verdict plus PR comment. `MemoryStore` now, Postgres DDL written in `store.py` for P2
- `.github/workflows/eval-gate.yml` scaffolded: builds the suite, runs the candidate, restores the judge cache, comments the diff (updating one sticky comment), uploads the report, fails on a breach
- `workers/`, `infra/terraform/`, `infra/k8s/`, `analytics/` are README placeholders and are not packaged yet

## 4. Environment state

- Dev machine Apple M1 Pro 16 GB, macOS 26. Docker runtime is Docker Desktop, with OrbStack installed but not selected
- **A native Postgres already owns 127.0.0.1:5432 on this machine.** A host-level listener shadows the container's port mapping, so `db init` fails with `role "evalgate" does not exist` against a perfectly healthy container. The dev stack therefore runs on **5433** via `POSTGRES_PORT=5433` in `.env`, with `DATABASE_URL` matching. The compose default stays 5432 for anyone else; this is per-machine config
- Training runs on this same M1 Pro through MLX. No second machine, no CUDA. `mlx-lm` is not installed yet; that happens at P1.2
- **Stop the dev Docker stack before P1.2 training.** The corpus is not needed once retrieved contexts are baked into the dataset, and 16 GB of unified memory is shared with macOS
- GitHub repo `starJin2003/EvalGate`, public, branch `main`, no branch protection yet since that is P3
- pre-commit hooks installed locally
- Accounts done. GitHub
- Accounts pending. Hugging Face token, OpenAI API key, OCI at P2, Databricks at P5, Cloudflare at P6
- `.env` keys in use. See `.env.example` for the current list

## 5. Blocked on Jinwoo

Anything waiting on a human goes here so a fresh session does not silently work around it.

- Hugging Face write token and OpenAI API key need to be in `.env` before any embedding, teacher, or judge call can run
- Golden set hand review, 80 to 100 cases, after the teacher batch completes

## 6. Known issues and deferred items

- OCI Pay As You Go may allocate only 2 OCPU and 12 GB rather than the expected 4 and 24. Verify in the console at P2 and shrink the stack if needed
- Kafka memory budget and single versus dual instance is deferred until that verification
- Databricks Free Edition external access path for pushing files and running dbt from outside is unverified. Check at the start of P5
- `pre-commit run --all-files` reported nothing before the first commit because the scaffold was untracked. Resolved, hooks engage from the first commit onward

## 7. Next action

Teacher batch part 1 of 2 is in flight. When it lands: `teacher collect`, then submit the remaining ~197, then `golden select` / `golden export` and hand review. P1.4 and the P3 gate are already built against stubs; P1.2 (MLX) and P1.3 (GGUF) are the next real work, and the only change P1.4 needs afterwards is pointing the runner at a real endpoint.

## 8. Session log

Newest first. Three to five lines each. What was attempted, what landed, what broke, what the next session needs to know.

**2026-08-01 P1.1 Data, question set final.** Per-repo leak rates measured by replaying stored batch outputs: Grafana 34.2%, Pydantic 27.5%, FastAPI 20.6%, Prometheus 19.0%. Grafana confirms leak tracks corpus density; Pydantic contradicts it with the smallest corpus and second-highest rate, driven by regular API naming. Aggregate top-ups had drifted adversarial off its split (FastAPI +10, Grafana -15); backfill is now per (category, repo) cell with per-repo overshoot, and it converged in one round. Final: 1,900 questions, all 16 cells exact. Ledger $0.1674 of $5.00. Still no teacher spend.

**2026-07-31 P1.1 Data, questions complete.** Corpus embedded (6,178 chunks, $0.0285, exactly as projected). Question generation stratified by repo into a [15%, 35%] band because the corpus is 68% Grafana; proportional sampling would have trained a Grafana model. 1,907 questions for $0.1363 across 3 batches, 0 failed calls. Headline finding: gpt-4.1-mini invents an adversarial symbol that actually exists in the corpus **26% of the time**, measured three times (26.0 / 26.7 / 13.3), so filling the refusal category costs 1.38x its nominal size. Two poisoning guards added and tested. Stopped before teacher spend; ledger at $0.1648 of $5.00.

**2026-07-31 P0 Bootstrap.** Scaffolded the uv workspace, pgvector dev stack, and arm64 CI. Two problems, both logged in DECISIONS.md. Three members all named their test file `test_smoke.py` and collided under pytest prepend import mode, fixed with `--import-mode=importlib`. CI run #1 green in 20 seconds total.
