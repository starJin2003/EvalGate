"""apps/api v0.

Register a suite, submit a run, list runs, promote a baseline, fetch a diff
between any two runs, and ask the gate for a verdict. The gate endpoint is what
`eval-gate.yml` calls, so the merge decision lives in one place rather than being
reimplemented in CI shell.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from evalcore import RunResult, Suite, compare, markdown_comment

from .store import MemoryStore, PostgresStore, Store


class PromoteRequest(BaseModel):
    run_id: str
    branch: str = "main"


class GateRequest(BaseModel):
    candidate_run_id: str
    branch: str = "main"


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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init = getattr(app.state.store, "init_schema", None)
        if init is not None:
            init()
        yield
        close = getattr(app.state.store, "close", None)
        if close is not None:
            close()

    app = FastAPI(title="EvalGate API", version="0.1.0", lifespan=lifespan)
    app.state.store = resolved_store
    app.state.write_token = token

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
