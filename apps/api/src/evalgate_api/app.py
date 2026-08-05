"""apps/api v0.

Register a suite, submit a run, list runs, promote a baseline, fetch a diff
between any two runs, and ask the gate for a verdict. The gate endpoint is what
`eval-gate.yml` calls, so the merge decision lives in one place rather than being
reimplemented in CI shell.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from prometheus_client import CollectorRegistry, Gauge, start_http_server
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

from evalcore import RunResult, Suite, compare, markdown_comment

from .store import MemoryStore, PostgresStore, Store


class PromoteRequest(BaseModel):
    run_id: str
    branch: str = "main"


class GateRequest(BaseModel):
    candidate_run_id: str
    branch: str = "main"


class InProgressMiddleware:
    """Counts HTTP requests currently in flight.

    Hand-rolled, because prometheus-fastapi-instrumentator's own in-progress
    gauge is constructed without `registry=`, unlike every other metric it
    creates — so with a per-app registry it silently lands in the global default
    instead, where our metrics server never exports it and a second app in the
    same process collides with it.

    Unlabelled on purpose. Total concurrency is what a saturation panel wants,
    and dropping the labels also drops the need to resolve the route template
    before the request has been routed.
    """

    def __init__(self, app: Any, gauge: Gauge) -> None:
        self.app = app
        self.gauge = gauge

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self.gauge.inc()
        try:
            await self.app(scope, receive, send)
        finally:
            # finally, not after the await: a client that disconnects mid-request
            # raises, and a gauge that only decrements on success climbs forever.
            self.gauge.dec()


def build_store() -> Store:
    """Postgres when DATABASE_URL is set, memory otherwise.

    Presence of the variable is the whole switch. CI and every unit test get
    MemoryStore without configuring anything; the k8s Deployment sets
    DATABASE_URL and gets Postgres. Nothing has to remember to flip a mode flag.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    return PostgresStore(url) if url else MemoryStore()


def create_app(store: Store | None = None, write_token: str | None = None) -> FastAPI:
    resolved_store = store if store is not None else build_store()
    token = write_token if write_token is not None else os.environ.get("EVALGATE_API_TOKEN", "")

    # A registry per app, not the global default. Tests build several apps in
    # one process, and re-registering the same collectors into the default
    # REGISTRY raises "Duplicated timeseries in CollectorRegistry".
    registry = CollectorRegistry()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init = getattr(app.state.store, "init_schema", None)
        if init is not None:
            init()
        # Metrics are served on their own port, never on the public router. The
        # Ingress maps :80 to the API port only, so /metrics — which publishes
        # route names, request counts, and error rates — is not reachable from
        # the internet. Started here rather than at import so that tests, which
        # never set METRICS_PORT, do not try to bind a socket.
        metrics_port = os.environ.get("METRICS_PORT", "").strip()
        if metrics_port:
            start_http_server(int(metrics_port), registry=app.state.metrics_registry)
        yield
        close = getattr(app.state.store, "close", None)
        if close is not None:
            close()

    app = FastAPI(title="EvalGate API", version="0.1.0", lifespan=lifespan)
    app.state.store = resolved_store
    app.state.write_token = token
    app.state.metrics_registry = registry

    # Labels by route template (/suites/{suite_id}), not raw path. That is the
    # whole reason this is a dependency: labelling by path would make every
    # distinct suite id its own series.
    Instrumentator(registry=registry).instrument(app)
    app.add_middleware(
        InProgressMiddleware,
        gauge=Gauge(
            "http_requests_inprogress",
            "Number of HTTP requests currently in flight.",
            registry=registry,
        ),
    )

    def db() -> Store:
        return app.state.store

    def require_write(authorization: str = Header(default="")) -> None:
        """Guards every endpoint that mutates state.

        Fails closed. With no token configured the writes are refused outright
        rather than left open, because the alternative — treating "unset" as
        "unauthenticated access is fine" — turns one forgotten environment
        variable into a public write endpoint on an internet-facing service.
        """
        expected = app.state.write_token
        if not expected:
            raise HTTPException(503, "write token is not configured; writes are disabled")
        scheme, _, presented = authorization.partition(" ")
        # Constant time, so a wrong token cannot be recovered a byte at a time
        # from response latency.
        if scheme.lower() != "bearer" or not secrets.compare_digest(presented, expected):
            raise HTTPException(401, "missing or invalid bearer token")

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness. Deliberately does not touch the database — a Postgres
        outage should take pods out of the Service, not restart them in a loop."""
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        """Readiness. Does touch the database, because a pod that cannot reach
        Postgres has nothing useful to serve and should leave the endpoints."""
        try:
            db().ping()
        except Exception as exc:  # noqa: BLE001 - probe reports any failure the same way
            raise HTTPException(503, f"database unreachable: {type(exc).__name__}") from exc
        return {"status": "ready"}

    # --- suites ---------------------------------------------------------------
    @app.put("/suites/{suite_id}", status_code=201, dependencies=[Depends(require_write)])
    def register_suite(suite_id: str, suite: Suite) -> dict[str, Any]:
        if suite.suite_id != suite_id:
            raise HTTPException(400, f"body declares {suite.suite_id}, path says {suite_id}")
        db().put_suite(suite)
        return {"suite_id": suite.suite_id, "version": suite.version, "cases": len(suite.cases)}

    @app.get("/suites")
    def list_suites() -> list[dict[str, Any]]:
        return [
            {"suite_id": s.suite_id, "version": s.version, "cases": len(s.cases)}
            for s in db().list_suites()
        ]

    @app.get("/suites/{suite_id}")
    def get_suite(suite_id: str) -> Suite:
        suite = db().get_suite(suite_id)
        if suite is None:
            raise HTTPException(404, f"unknown suite {suite_id}")
        return suite

    # --- runs -----------------------------------------------------------------
    @app.post("/runs", status_code=201, dependencies=[Depends(require_write)])
    def submit_run(run: RunResult) -> dict[str, Any]:
        if db().get_suite(run.suite_id) is None:
            raise HTTPException(404, f"unknown suite {run.suite_id}")
        db().put_run(run)
        return {
            "run_id": run.run_id,
            "suite_id": run.suite_id,
            "score": run.score,
            "categories": run.category_scores(),
        }

    @app.get("/runs")
    def list_runs(suite_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return [
            {
                "run_id": r.run_id,
                "suite_id": r.suite_id,
                "model_ref": r.model_ref,
                "score": r.score,
            }
            for r in db().list_runs(suite_id, limit)
        ]

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> RunResult:
        run = db().get_run(run_id)
        if run is None:
            raise HTTPException(404, f"unknown run {run_id}")
        return run

    # --- baselines ------------------------------------------------------------
    @app.post("/suites/{suite_id}/baseline", status_code=201, dependencies=[Depends(require_write)])
    def promote(suite_id: str, req: PromoteRequest) -> dict[str, Any]:
        try:
            baseline = db().promote(suite_id, req.branch, req.run_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return baseline.__dict__

    @app.get("/suites/{suite_id}/baseline")
    def get_baseline(suite_id: str, branch: str = "main") -> dict[str, Any]:
        baseline = db().get_baseline(suite_id, branch)
        if baseline is None:
            raise HTTPException(404, f"no baseline for {suite_id} on {branch}")
        return baseline.__dict__

    # --- diff and gate --------------------------------------------------------
    @app.get("/diff")
    def diff(suite_id: str, baseline_run: str, candidate_run: str) -> dict[str, Any]:
        suite, base, cand = _resolve(db(), suite_id, baseline_run, candidate_run)
        d = compare(suite, base, cand)
        return _diff_payload(d)

    # Not behind require_write. It is a POST for the request body, but it reads
    # and returns a verdict without mutating anything, and leaving it open is
    # what lets k6 load-test the realistic path in P2 without a credential.
    @app.post("/suites/{suite_id}/gate")
    def gate(suite_id: str, req: GateRequest) -> dict[str, Any]:
        """The merge decision. A missing baseline passes: the first run on a new
        suite has nothing to regress against, and failing it would block the PR
        that introduces the suite."""
        store = db()
        baseline = store.get_baseline(suite_id, req.branch)
        if baseline is None:
            return {
                "verdict": "pass",
                "reason": "no baseline yet; nothing to compare against",
                "comment": f"## ✅ eval-gate — `{suite_id}`\n\nNo baseline on `{req.branch}` yet.",
            }
        suite, base, cand = _resolve(store, suite_id, baseline.run_id, req.candidate_run_id)
        d = compare(suite, base, cand)
        return {**_diff_payload(d), "comment": markdown_comment(d)}

    # --- the daily verdict ----------------------------------------------------
    @app.get("/daily/latest")
    def daily_latest() -> dict[str, Any]:
        """Serve the daily DAG's `latest.json` verbatim, for the PR gate to age.

        The file is written by the DAG's `publish_latest` task onto the results
        volume, which this pod mounts readOnly. It is served, not re-derived: the
        PR gate must age the same bytes the DAG published, and a second copy in
        Postgres could disagree with the first without anything noticing.

        OPEN, not bearer-guarded, and that is deliberate on both counts.
        `require_write` guards writes; every read here is already open, and this
        is a read. The payload is a verdict, a delta, four category means and a
        timestamp -- no prompts, no answers, no weights. The API is HTTP-only on a
        bare IP (Let's Encrypt will not issue for 64.181.195.241), so requiring a
        bearer would push the token across the wire in cleartext on every PR check
        of every branch, spending a real credential to protect a pass/fail. It
        would also add a second place to rotate it, and that token is already
        pending rotation.

        EVERY FAILURE IS A 503 THAT NAMES ITS CAUSE. It must never be an empty
        pass: `check-daily` blocks on a stale or failing verdict, so a missing
        file that returned `{}` or a 200 with no `verdict` would convert a dead
        DAG into a green check -- which is the exact confusion the whole staleness
        design exists to prevent. 503 rather than 404 because the route exists and
        the dependency behind it does not, and because `curl -f` fails on both so
        CI blocks either way.
        """
        path = Path(os.environ.get("EVALGATE_DAILY_LATEST", "/out/daily/latest.json"))
        try:
            raw = path.read_text()
        except FileNotFoundError as exc:
            raise HTTPException(
                503,
                f"no daily verdict at {path}: the DAG has not published one, or the "
                "results volume is not mounted",
            ) from exc
        except OSError as exc:
            raise HTTPException(
                503, f"cannot read {path}: {type(exc).__name__}: {exc.strerror}"
            ) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(503, f"daily verdict at {path} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict) or "completed_at" not in payload:
            # A body without completed_at cannot be aged, and an un-ageable verdict
            # is indistinguishable from a fresh one to a caller that only reads
            # `verdict`. Refuse rather than serve something that looks answerable.
            raise HTTPException(
                503, f"daily verdict at {path} has no completed_at; it cannot be aged"
            )
        return payload

    return app


def _resolve(store: Store, suite_id: str, baseline_run: str, candidate_run: str):
    suite = store.get_suite(suite_id)
    if suite is None:
        raise HTTPException(404, f"unknown suite {suite_id}")
    base = store.get_run(baseline_run)
    if base is None:
        raise HTTPException(404, f"unknown run {baseline_run}")
    cand = store.get_run(candidate_run)
    if cand is None:
        raise HTTPException(404, f"unknown run {candidate_run}")
    return suite, base, cand


def _diff_payload(d) -> dict[str, Any]:
    return {
        "verdict": d.verdict,
        "suite_id": d.suite_id,
        "baseline_run": d.baseline_run,
        "candidate_run": d.candidate_run,
        "baseline_score": d.baseline_score,
        "candidate_score": d.candidate_score,
        "delta": d.delta,
        "categories": d.by_category(),
        "breaches": [b.__dict__ for b in d.breaches],
        "regressed": [
            {
                "case_id": c.case_id,
                "category": c.category,
                "baseline": c.baseline_score,
                "candidate": c.candidate_score,
            }
            for c in d.regressions()
        ],
    }


app = create_app()
