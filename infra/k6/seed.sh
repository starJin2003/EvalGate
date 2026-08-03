#!/usr/bin/env bash
# Seed the fixed dataset the load test reads, through the API.
#
#   KUBECONFIG=~/.kube/evalgate.yaml ./infra/k6/seed.sh
#
# Every id carries the `k6-` prefix, which is what makes teardown.sh able to
# delete exactly what this created and nothing else.
#
# Seeding through the API rather than straight into Postgres is deliberate: it
# exercises the authenticated write path once, which is why the load mix itself
# does not need to.

set -euo pipefail

NS=evalgate
API="${EVALGATE_API_URL:-http://64.181.195.241}"
SUITE=k6-smoke

log() { printf '[k6-seed] %s\n' "$*"; }

TOKEN="$(kubectl -n "$NS" get secret evalgate-api-token -o jsonpath='{.data.EVALGATE_API_TOKEN}' | base64 -d)"
[ -n "$TOKEN" ] || { log "FATAL: could not read the API write token."; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

log "Building the suite and two runs with evalcore."
uv run --no-sync python - "$tmp" "$SUITE" <<'PY'
import pathlib, sys
from evalcore import (Case, Category, ContextChunk, ScorerKind, ScorerSpec,
                      StubModel, Suite, Threshold, run_suite)

out, suite_id = pathlib.Path(sys.argv[1]), sys.argv[2]
ctx = [ContextChunk(label="C1", chunk_id="k1", repo="fastapi",
                    content="Response models shape the output schema.")]
suite = Suite(
    suite_id=suite_id,
    description="k6 P2 load-test fixture. Deleted by infra/k6/teardown.sh.",
    cases=[
        Case(case_id="fact-1", category=Category.factual, question="What shapes output?",
             context=ctx, scorers=[ScorerSpec(kind=ScorerKind.citation)]),
        Case(case_id="adv-1", category=Category.adversarial,
             question="How do I use nope_hook()?", context=ctx,
             absent_symbol="nope_hook", scorers=[ScorerSpec(kind=ScorerKind.refusal)]),
    ],
    threshold=Threshold(max_drop=0.02),
)
good = run_suite(suite, StubModel({
    "fact-1": "Response models shape the output schema [C1].",
    "adv-1": "nope_hook does not appear in the documentation provided."}, ref="v1"),
    run_id="k6-run-good")
# Regresses on adv-1 on purpose: the load test asserts the gate still returns
# `fail` under concurrency, so a 200 carrying the wrong verdict cannot pass.
bad = run_suite(suite, StubModel({
    "fact-1": "Response models shape the output schema [C1].",
    "adv-1": "You call nope_hook() to register it [C1]."}, ref="v2"),
    run_id="k6-run-bad")

(out / "suite.json").write_text(suite.model_dump_json())
(out / "run_good.json").write_text(good.model_dump_json())
(out / "run_bad.json").write_text(bad.model_dump_json())
print(f"  suite {suite_id}: {len(suite.cases)} cases | good {good.score} | bad {bad.score}")
PY

post() {
  local method="$1" path="$2" body="$3" expect="$4"
  local code
  code=$(curl -sS -o /dev/null -w '%{http_code}' -X "$method" "${API}${path}" \
    -H 'Content-Type: application/json' -H "Authorization: Bearer ${TOKEN}" --data "$body")
  printf '  %-6s %-34s -> HTTP %s\n' "$method" "$path" "$code"
  [ "$code" = "$expect" ] || { log "FATAL: expected $expect."; exit 1; }
}

log "Seeding through ${API}."
post PUT  "/suites/${SUITE}"          "@${tmp}/suite.json"    201
post POST "/runs"                     "@${tmp}/run_good.json" 201
post POST "/runs"                     "@${tmp}/run_bad.json"  201
post POST "/suites/${SUITE}/baseline" '{"run_id":"k6-run-good","branch":"main"}' 201

log "Verifying the gate returns the verdict the load test asserts on."
verdict=$(curl -sS -X POST "${API}/suites/${SUITE}/gate" -H 'Content-Type: application/json' \
  -d '{"candidate_run_id":"k6-run-bad","branch":"main"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["verdict"])')
log "  verdict = ${verdict}"
[ "$verdict" = "fail" ] || { log "FATAL: expected 'fail'; the load test's checks would all fail."; exit 1; }

log "Done."
