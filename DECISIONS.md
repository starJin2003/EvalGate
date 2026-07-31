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
| 2026-07-31 | Plan | GitHub Actions is the regression gate | Custom webhook service, GitLab CI | The gate is the PR check itself. Same integration point as promptfoo and DeepEval, so CI/CD is a product feature and not decoration |
| 2026-07-31 | Plan | OCI PAYG upgrade for hosting | Stay on Always Free, AWS or GCP free tiers | Always Free was cut to 2 OCPU 12 GB in June 2026. PAYG reportedly keeps 4 OCPU 24 GB at zero cost. Unofficial, so verify after upgrade and keep a 1 dollar budget alert |
| 2026-07-31 | Plan | Self hosted Kafka on k3s | Confluent Cloud, Redpanda Cloud | Apache Kafka is license free and Confluent credits burn out, which breaks an always-on demo. Ingestion is a product feature, so the layer earns its place |
| 2026-07-31 | Plan | Spark handles aggregation and drift only, judge rescoring stays on OCI workers | Run LLM judge inside Databricks | Free Edition outbound is restricted to trusted domains, so serverless compute cannot call LLM APIs |
| 2026-07-31 | Plan | Databricks Free Edition for the analytics layer | Snowflake trial | Snowflake has no perpetual free plan and trial data deletes after expiry, which kills a live demo. Free Edition is perpetual, serverless only, and non commercial, so billing lives in a separate component |
| 2026-07-31 | Plan | Operational store Postgres, analytics store Delta on Databricks | Single store either way | Standard lakehouse split. Each layer has an independent product reason, which defends against the logo wall critique |

## 2. Problems and fixes

| Date | Phase | Problem | Root cause | Fix |
|---|---|---|---|---|
| | | | | |

## 3. Stats

| Date | Phase | Metric | Value | Context |
|---|---|---|---|---|
| | | | | |

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
