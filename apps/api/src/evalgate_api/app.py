"""apps/api v0.

Register a suite, submit a run, list runs, promote a baseline, fetch a diff
between any two runs, and ask the gate for a verdict. The gate endpoint is what
`eval-gate.yml` calls, so the merge decision lives in one place rather than being
reimplemented in CI shell.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from evalcore import RunResult, Suite, compare, markdown_comment

from .store import MemoryStore, Store


class PromoteRequest(BaseModel):
    run_id: str
    branch: str = "main"


class GateRequest(BaseModel):
    candidate_run_id: str
    branch: str = "main"


def create_app(store: Store | None = None) -> FastAPI:
    app = FastAPI(title="EvalGate API", version="0.1.0")
    app.state.store = store or MemoryStore()

    def db() -> Store:
        return app.state.store

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # --- suites ---------------------------------------------------------------
    @app.put("/suites/{suite_id}", status_code=201)
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
    @app.post("/runs", status_code=201)
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
    @app.post("/suites/{suite_id}/baseline", status_code=201)
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
