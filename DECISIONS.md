# DECISIONS.md

Running log for EvalGate. Append only, newest rows at the top of each table. Claude Code updates this in the same work block as the change it describes. Entries stay at one to three lines. This file is the raw material for resume bullets and interview answers, so concrete numbers and named trade offs beat prose.

Entry rules.
- Every design choice with a real alternative gets a Decisions row.
- Every bug or outage that cost more than a few minutes gets a Problems row with root cause and fix.
- Every benchmark, load test, quota check, and runtime measurement gets a Stats row with enough context to quote later.
- When an entry looks resume worthy, copy a one line draft into the Bullet candidates section immediately. Do not wait.

---

## 1. Decisions

| Date | Phase | Decision | Alternatives considered | Why |
|---|---|---|---|---|
| 2026-07-31 | P0 | CI runs on `ubuntu-24.04-arm` hosted runners | `ubuntu-latest` (amd64) | Free arm64 runners for public repos went GA August 2025, so CI matches the M1 Pro dev machine and OCI Ampere A1 prod. No `ubuntu-latest-arm` label exists, so the version is pinned explicitly. Fall back to `ubuntu-latest` with a logged reason if arm64 ever breaks setup-uv or an image |
| 2026-07-31 | P0 | uv workspace with a virtual root, members `apps/*` and `packages/*` | Poetry, pip-tools, single flat package | uv already manages the Python 3.12 toolchain, so one tool covers interpreter, venv, and lock. Virtual root means plain `uv sync` installs all three members plus dev tools, matching the CLAUDE.md command |
| 2026-07-31 | P0 | `pgvector/pgvector:pg16` image for the dev database | `postgres:16` plus building the extension in a custom Dockerfile | Upstream image publishes linux/arm64, verified against the local manifest, so there is no build step on the M1 and none on the arm64 CI runner |
| 2026-07-31 | P0 | CI machine-checks the P0 exit criterion in a second `compose` job | Trust the local run and only lint/test in CI | "docker compose up yields Postgres with pgvector" becomes an asserted query on `pg_extension` instead of a claim. Free on a public repo |
| 2026-07-31 | P0 | `workers/`, `training/`, `infra/*`, `analytics/` are README placeholders, not packages | Scaffold a `pyproject.toml` in each up front | Only three of the section 6 directories hold Python in P0. Each earns packaging in the phase that needs it |
| 2026-07-31 | Plan | GitHub Actions is the regression gate | Custom webhook service, GitLab CI | The gate is the PR check itself. Same integration point as promptfoo and DeepEval, so CI/CD is a product feature and not decoration |
| 2026-07-31 | Plan | OCI PAYG upgrade for hosting | Stay on Always Free, AWS or GCP free tiers | Always Free was cut to 2 OCPU 12 GB in June 2026. PAYG reportedly keeps 4 OCPU 24 GB at zero cost. Unofficial, so verify after upgrade and keep a 1 dollar budget alert |
| 2026-07-31 | Plan | Self hosted Kafka on k3s | Confluent Cloud, Redpanda Cloud | Apache Kafka is license free and Confluent credits burn out, which breaks an always-on demo. Ingestion is a product feature, so the layer earns its place |
| 2026-07-31 | Plan | Spark handles aggregation and drift only, judge rescoring stays on OCI workers | Run LLM judge inside Databricks | Free Edition outbound is restricted to trusted domains, so serverless compute cannot call LLM APIs |
| 2026-07-31 | Plan | Databricks Free Edition for the analytics layer | Snowflake trial | Snowflake has no perpetual free plan and trial data deletes after expiry, which kills a live demo. Free Edition is perpetual, serverless only, and non commercial, so billing lives in a separate component |
| 2026-07-31 | Plan | Operational store Postgres, analytics store Delta on Databricks | Single store either way | Standard lakehouse split. Each layer has an independent product reason, which defends against the logo wall critique |

## 2. Problems and fixes

| Date | Phase | Problem | Root cause | Fix |
|---|---|---|---|---|
| 2026-07-31 | P0 | `uv run pytest` failed collection with "import file mismatch" on 2 of 3 members | Each workspace member has its own `tests/test_smoke.py`. pytest's default prepend import mode derives module names from basenames, so three files collided as `test_smoke` | `--import-mode=importlib` in root `addopts`. Note the ini key is not `importmode`; `--strict-config` correctly rejected that first attempt |
| 2026-07-31 | P0 | `pre-commit run --all-files` reported "no files to check" for yaml, toml, and ruff | pre-commit only sees git-tracked files and the scaffold was not committed yet | Verified with an explicit `--files` list instead. Hooks run normally from the first commit onward |

## 3. Stats

| Date | Phase | Metric | Value | Context |
|---|---|---|---|---|
| 2026-07-31 | P0 | Dev stack cold start | 13.0 s | `docker compose up -d --wait` on M1 Pro, including the `pgvector/pgvector:pg16` pull. Warm start is the healthcheck interval only |
| 2026-07-31 | P0 | Dev database versions | Postgres 16.14, pgvector 0.8.6, linux/arm64 | Queried from the running container. Confirms the P0 exit criterion |
| 2026-07-31 | P0 | `uv sync` from cold cache | 19 packages, 949 ms prepare + 15 ms install | M1 Pro. uv-fetched CPython 3.12.12, ruff 0.16.1, pytest 9.1.1, pre-commit 4.6.1 |
| 2026-07-31 | P0 | Local lint plus test wall time | 3 tests in 0.01 s, ruff clean over 15 files | Baseline before any real code exists |

Minimum stats to capture per phase.
- P1. Training wall time on T4, eval seconds per case and per suite, judge tokens per case, v1 versus v2 score delta overall and per category, dataset sizes.
- P2. terraform apply wall time, k6 RPS and p95 and error rate, node CPU and RAM idle and loaded, PAYG allowance verification result.
- P3. Gate wall time on a PR, daily DAG runtime, regression threshold values.
- P4. Ingest throughput, consumer lag under load, promotion counts, failure cluster count and sizes.
- P5. Daily export volume, Spark and dbt job runtimes, which Free Edition quotas were approached.
- P6. Publish latency, Worker request counts.

## 4. Resume bullet candidates

Raw one liners drafted from rows above. Chassis tag in brackets. Rewrite happens later in resume-maintenance, not here.

- [SWE] (pending) Regression gate blocks PR merges on eval score drops with case level diff comments, measured gate wall time of ___.
- [AIML] (pending) LoRA fine tuned ___ on ___ examples, harness caught a ___ point regression between v1 and v2 across ___ golden cases.
- [DE] (pending) Kafka to Postgres to Delta pipeline moving ___ traces per day with Airflow orchestration and dbt marts.
