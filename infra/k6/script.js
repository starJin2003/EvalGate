// P2 load test for the EvalGate API.
//
// Read paths only. Writes are excluded deliberately: API writes are one per
// eval run rather than a throughput concern, P4's high-volume ingest goes
// through Kafka rather than this endpoint, and a seven-minute write load would
// put tens of thousands of JSONB run bodies on the block volume holding the
// one dataset here that cannot be regenerated.
//
// Everything this test reads was created by seed.sh under a `k6-` id prefix,
// and teardown.sh removes exactly those rows. Nothing here can reach a row it
// did not create.

import http from "k6/http";
import { check } from "k6";

const BASE = __ENV.BASE_URL || "http://10.0.0.94";
const SUITE = "k6-smoke";

// Two plateaus rather than one, so the dashboard shows whether latency degrades
// with concurrency or holds flat. At a 15s scrape each plateau is 12 points and
// the whole run is ~30 — enough to read a curve off rather than a couple of
// dots.
export const options = {
  stages: [
    { duration: "30s", target: 20 },
    { duration: "3m", target: 20 },
    { duration: "30s", target: 50 },
    { duration: "3m", target: 50 },
    { duration: "30s", target: 0 },
  ],

  // Committed before the run. If one of these breaches, the breach is the
  // result: k6 exits non-zero and that number gets recorded. No retuning the
  // test until it passes.
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<500", "p(99)<1500"],
    // The heaviest path — loads the suite plus two runs and computes a diff —
    // gets its own, looser bar rather than being hidden in the aggregate.
    "http_req_duration{endpoint:gate}": ["p(95)<800"],
    // Bodies are correct, not merely HTTP 200. A 200 carrying the wrong verdict
    // would otherwise read as a pass.
    checks: ["rate>0.99"],
  },

  // Names the run in the metrics k6 pushes to Prometheus, so the load window is
  // identifiable on the dashboard afterwards.
  tags: { testid: __ENV.K6_TESTID || "p2-load" },

  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
};

// Weighted toward the endpoints that actually cost something. /health is
// deliberately absent: it does no work, so including it would inflate aggregate
// RPS while dragging the aggregate p95 down — both of which flatter the result
// without measuring anything.
const MIX = [
  { weight: 30, name: "gate" },
  { weight: 20, name: "diff" },
  { weight: 15, name: "runs" },
  { weight: 15, name: "suite" },
  { weight: 10, name: "suites" },
  { weight: 10, name: "ready" },
];

const TOTAL_WEIGHT = MIX.reduce((a, m) => a + m.weight, 0);

function pick() {
  let r = Math.random() * TOTAL_WEIGHT;
  for (const m of MIX) {
    r -= m.weight;
    if (r <= 0) return m.name;
  }
  return MIX[MIX.length - 1].name;
}

const JSON_HEADERS = { "Content-Type": "application/json" };

export default function () {
  const which = pick();

  switch (which) {
    case "gate": {
      // The endpoint P3's eval gate calls. Unauthenticated by design, which is
      // partly why this load test needs no credential.
      const res = http.post(
        `${BASE}/suites/${SUITE}/gate`,
        JSON.stringify({ candidate_run_id: "k6-run-bad", branch: "main" }),
        { headers: JSON_HEADERS, tags: { endpoint: "gate" } },
      );
      check(res, {
        "gate 200": (r) => r.status === 200,
        // The seeded bad run really does regress against the seeded baseline,
        // so a verdict of anything else means the comparison broke under load.
        "gate verdict is fail": (r) => r.json("verdict") === "fail",
      });
      break;
    }
    case "diff": {
      const res = http.get(
        `${BASE}/diff?suite_id=${SUITE}&baseline_run=k6-run-good&candidate_run=k6-run-bad`,
        { tags: { endpoint: "diff" } },
      );
      check(res, {
        "diff 200": (r) => r.status === 200,
        "diff has the regressed case": (r) => r.json("regressed.0.case_id") === "adv-1",
      });
      break;
    }
    case "runs": {
      const res = http.get(`${BASE}/runs?suite_id=${SUITE}`, { tags: { endpoint: "runs" } });
      check(res, {
        "runs 200": (r) => r.status === 200,
        "runs returns both seeded runs": (r) => r.json().length === 2,
      });
      break;
    }
    case "suite": {
      const res = http.get(`${BASE}/suites/${SUITE}`, { tags: { endpoint: "suite" } });
      check(res, {
        "suite 200": (r) => r.status === 200,
        "suite has 2 cases": (r) => r.json("cases").length === 2,
      });
      break;
    }
    case "suites": {
      const res = http.get(`${BASE}/suites`, { tags: { endpoint: "suites" } });
      check(res, {
        "suites 200": (r) => r.status === 200,
        "suites includes the seeded suite": (r) =>
          r.json().some((s) => s.suite_id === SUITE),
      });
      break;
    }
    case "ready": {
      // The only endpoint in the mix that queries Postgres directly, so the
      // database stays in the measurement rather than being masked by the
      // in-process work the other paths do.
      const res = http.get(`${BASE}/ready`, { tags: { endpoint: "ready" } });
      check(res, {
        "ready 200": (r) => r.status === 200,
        "ready reports ready": (r) => r.json("status") === "ready",
      });
      break;
    }
  }
}
