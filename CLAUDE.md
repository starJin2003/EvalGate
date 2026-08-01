# EvalGate

LLM eval regression platform. It runs eval suites daily, harvests failing production traces into those suites, and blocks PR merges when scores drop. Full brief in BUILD_PLAN.md. Running log in DECISIONS.md.

## Commands

- deps: `uv sync`
- test: `uv run pytest`
- lint: `uv run ruff check . && uv run ruff format --check .`
- dev stack: `docker compose -f docker-compose.dev.yml up -d`

## Rules

1. Never run git. End every work block with a GIT BLOCK of paste-ready commands for the user to run.
2. Update DECISIONS.md in the same work block as the change it describes. Three entry kinds. Decisions with the alternative that was rejected. Problems with root cause and fix. Measured numbers with context. One to three lines each. Numbers over adjectives.
3. Zero dollar budget. Ask before adding any new external service, paid tier, or heavyweight dependency.
4. arm64 only. Dev machine is an Apple M1 Pro and prod is OCI Ampere A1. Flag any amd64-only dependency instead of adding it.
5. Secrets never enter the repo. `.env` locally, GitHub Actions secrets in CI, Kubernetes secrets on OCI. Always ship a `.env.example`.
6. Python 3.12, type hinted, ruff formatted. Thin and working beats clever and general.
7. Decide freely inside a phase. Stop and ask only at the DECISION POINTs marked in BUILD_PLAN.md and for anything touching money or accounts.
8. A phase is done when a stranger can follow the README and reproduce the demo. Update the README in the same phase.

## GIT BLOCK format

```
GIT BLOCK
git add .
git commit -m "feat(scope): short message"
git push origin <branch>
```
