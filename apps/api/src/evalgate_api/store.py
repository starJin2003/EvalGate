"""Persistence for suites, runs, and the baseline table.

The baseline table is what the gate compares against: one row per (suite,
branch), pointing at the run that currently defines "good". Promotion is
explicit, so a bad run cannot become the baseline just by being newest.

The store is a protocol with an in-memory implementation, so the API and the
gate are testable without Postgres. The Postgres implementation lands with P2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from evalcore import RunResult, Suite


@dataclass(frozen=True)
class Baseline:
    suite_id: str
    branch: str
    run_id: str
    score: float
    promoted_at: str


class Store(Protocol):
    def put_suite(self, suite: Suite) -> None: ...
    def get_suite(self, suite_id: str) -> Suite | None: ...
    def list_suites(self) -> list[Suite]: ...
    def put_run(self, run: RunResult) -> None: ...
    def get_run(self, run_id: str) -> RunResult | None: ...
    def list_runs(self, suite_id: str | None = None, limit: int = 50) -> list[RunResult]: ...
    def promote(self, suite_id: str, branch: str, run_id: str) -> Baseline: ...
    def get_baseline(self, suite_id: str, branch: str) -> Baseline | None: ...


@dataclass
class MemoryStore:
    suites: dict[str, Suite] = field(default_factory=dict)
    runs: dict[str, RunResult] = field(default_factory=dict)
    baselines: dict[tuple[str, str], Baseline] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)

    def put_suite(self, suite: Suite) -> None:
        self.suites[suite.suite_id] = suite

    def get_suite(self, suite_id: str) -> Suite | None:
        return self.suites.get(suite_id)

    def list_suites(self) -> list[Suite]:
        return [self.suites[k] for k in sorted(self.suites)]

    def put_run(self, run: RunResult) -> None:
        if run.run_id not in self.runs:
            self._order.append(run.run_id)
        self.runs[run.run_id] = run

    def get_run(self, run_id: str) -> RunResult | None:
        return self.runs.get(run_id)

    def list_runs(self, suite_id: str | None = None, limit: int = 50) -> list[RunResult]:
        out = [self.runs[r] for r in reversed(self._order)]
        if suite_id:
            out = [r for r in out if r.suite_id == suite_id]
        return out[:limit]

    def promote(self, suite_id: str, branch: str, run_id: str) -> Baseline:
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(f"unknown run {run_id}")
        if run.suite_id != suite_id:
            raise ValueError(f"run {run_id} belongs to suite {run.suite_id}, not {suite_id}")
        baseline = Baseline(
            suite_id=suite_id,
            branch=branch,
            run_id=run_id,
            score=run.score,
            promoted_at=datetime.now(UTC).isoformat(),
        )
        self.baselines[(suite_id, branch)] = baseline
        return baseline

    def get_baseline(self, suite_id: str, branch: str) -> Baseline | None:
        return self.baselines.get((suite_id, branch))


# --- Postgres schema, applied in P2 -------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS suites (
    suite_id    TEXT PRIMARY KEY,
    version     TEXT NOT NULL,
    body        JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    suite_id    TEXT NOT NULL REFERENCES suites(suite_id) ON DELETE CASCADE,
    model_ref   TEXT NOT NULL,
    score       DOUBLE PRECISION NOT NULL,
    body        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS runs_suite_created_idx ON runs (suite_id, created_at DESC);

-- One baseline per suite per branch. Promotion is explicit: the newest run does
-- not become the baseline by default, or a regression would rebase the target
-- it was supposed to be measured against.
CREATE TABLE IF NOT EXISTS baselines (
    suite_id    TEXT NOT NULL REFERENCES suites(suite_id) ON DELETE CASCADE,
    branch      TEXT NOT NULL,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    score       DOUBLE PRECISION NOT NULL,
    promoted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (suite_id, branch)
);
"""


def dump_run(run: RunResult) -> str:
    return run.model_dump_json()


def load_run(blob: str) -> RunResult:
    return RunResult.model_validate(json.loads(blob))
