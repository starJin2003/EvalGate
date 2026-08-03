"""Persistence for suites, runs, and the baseline table.

The baseline table is what the gate compares against: one row per (suite,
branch), pointing at the run that currently defines "good". Promotion is
explicit, so a bad run cannot become the baseline just by being newest.

The store is a protocol with two implementations. `MemoryStore` keeps the API
and the gate testable without a database, and CI runs against it. `PostgresStore`
is what runs on k3s from P2 onward.

Both must behave identically, including on failure: `app.py` maps `KeyError` to
404 and `ValueError` to 400, so an implementation that raises the wrong one
turns a missing run into a client error or vice versa. `tests/test_store.py`
runs one set of assertions against both for exactly that reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from evalcore import RunResult, Suite

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class Baseline:
    suite_id: str
    branch: str
    run_id: str
    score: float
    promoted_at: str


# Arbitrary but fixed. Any 64-bit int works; it only has to be the same value in
# every replica so they contend on the same lock.
_SCHEMA_LOCK_KEY = 0x4556_414C_4741_5445  # "EVALGATE" in ASCII


class Store(Protocol):
    def ping(self) -> None: ...
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

    def ping(self) -> None:
        return None

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


class PostgresStore:
    """The Store protocol over Postgres 16.

    `body` is the source of truth for both suites and runs: the full pydantic
    model, round-tripped through JSONB. The scalar columns beside it
    (`version`, `model_ref`, `score`) are a denormalised cache, written from the
    model at insert time so they can be indexed and ordered on. `RunResult.score`
    is a computed property over `results`, not a stored field, so it is derived
    on write rather than read off the payload.
    """

    def __init__(self, conninfo: str, *, min_size: int = 1, max_size: int = 8) -> None:
        # Imported here rather than at module scope so that importing this
        # module — which CI and every MemoryStore test does — never requires
        # psycopg to be installed.
        from psycopg_pool import ConnectionPool

        self._pool: ConnectionPool = ConnectionPool(
            conninfo, min_size=min_size, max_size=max_size, open=True
        )

    def init_schema(self) -> None:
        """Create the tables if they are not already there.

        Deliberately not a migration tool. SCHEMA is idempotent by construction
        and this is its first and only version. The limitation is real and worth
        naming: `CREATE TABLE IF NOT EXISTS` silently does nothing against an
        existing table, so the first schema *change* after P4 puts real trace
        data here needs Alembic, not another line in this string.
        """
        with self._pool.connection() as conn, conn.transaction():
            # The deployment runs 2 replicas, so both call this at startup and
            # race. `CREATE TABLE IF NOT EXISTS` is not atomic against a
            # concurrent creator — it checks, then creates, and the loser gets a
            # duplicate-key violation on pg_type rather than a clean no-op. The
            # advisory lock serialises the whole script; it is transaction
            # scoped, so it releases on commit even if the DDL raises.
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_KEY,))
            # No bind parameters on the DDL itself, on purpose. Passing any would
            # put psycopg on the extended query protocol, which is
            # single-statement only, and SCHEMA is a multi-statement script.
            # Same lesson as `db init` in P1.1 (DECISIONS, 2026-07-31).
            conn.execute(SCHEMA)

    def ping(self) -> None:
        """Raises if the database is unreachable. Backs the readiness probe."""
        with self._pool.connection() as conn:
            conn.execute("SELECT 1")

    def close(self) -> None:
        self._pool.close()

    # --- suites ---------------------------------------------------------------
    def put_suite(self, suite: Suite) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO suites (suite_id, version, body, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (suite_id) DO UPDATE
                   SET version = EXCLUDED.version,
                       body = EXCLUDED.body,
                       updated_at = now()
                """,
                (suite.suite_id, suite.version, _jsonb(suite.model_dump(mode="json"))),
            )

    def get_suite(self, suite_id: str) -> Suite | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT body FROM suites WHERE suite_id = %s", (suite_id,)
            ).fetchone()
        return Suite.model_validate(row[0]) if row else None

    def list_suites(self) -> list[Suite]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT body FROM suites ORDER BY suite_id").fetchall()
        return [Suite.model_validate(r[0]) for r in rows]

    # --- runs -----------------------------------------------------------------
    def put_run(self, run: RunResult) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, suite_id, model_ref, score, body)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE
                   SET suite_id = EXCLUDED.suite_id,
                       model_ref = EXCLUDED.model_ref,
                       score = EXCLUDED.score,
                       body = EXCLUDED.body
                """,
                (
                    run.run_id,
                    run.suite_id,
                    run.model_ref,
                    run.score,
                    _jsonb(run.model_dump(mode="json")),
                ),
            )

    def get_run(self, run_id: str) -> RunResult | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT body FROM runs WHERE run_id = %s", (run_id,)).fetchone()
        return RunResult.model_validate(row[0]) if row else None

    def list_runs(self, suite_id: str | None = None, limit: int = 50) -> list[RunResult]:
        # run_id breaks ties on created_at. Two runs submitted inside the same
        # microsecond would otherwise come back in an arbitrary order, which
        # makes a passing test a coin flip rather than a guarantee.
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT body FROM runs
                WHERE %s::text IS NULL OR suite_id = %s
                ORDER BY created_at DESC, run_id DESC
                LIMIT %s
                """,
                (suite_id, suite_id, limit),
            ).fetchall()
        return [RunResult.model_validate(r[0]) for r in rows]

    # --- baselines ------------------------------------------------------------
    def promote(self, suite_id: str, branch: str, run_id: str) -> Baseline:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT suite_id, score FROM runs WHERE run_id = %s", (run_id,)
            ).fetchone()
            # These two exception types are the contract app.py maps to 404 and
            # 400. MemoryStore raises the same pair for the same conditions.
            if row is None:
                raise KeyError(f"unknown run {run_id}")
            if row[0] != suite_id:
                raise ValueError(f"run {run_id} belongs to suite {row[0]}, not {suite_id}")
            promoted = conn.execute(
                """
                INSERT INTO baselines (suite_id, branch, run_id, score, promoted_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (suite_id, branch) DO UPDATE
                   SET run_id = EXCLUDED.run_id,
                       score = EXCLUDED.score,
                       promoted_at = now()
                RETURNING score, promoted_at
                """,
                (suite_id, branch, run_id, row[1]),
            ).fetchone()
        return Baseline(
            suite_id=suite_id,
            branch=branch,
            run_id=run_id,
            score=promoted[0],
            promoted_at=promoted[1].isoformat(),
        )

    def get_baseline(self, suite_id: str, branch: str) -> Baseline | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT run_id, score, promoted_at FROM baselines
                WHERE suite_id = %s AND branch = %s
                """,
                (suite_id, branch),
            ).fetchone()
        if row is None:
            return None
        return Baseline(
            suite_id=suite_id,
            branch=branch,
            run_id=row[0],
            score=row[1],
            promoted_at=row[2].isoformat(),
        )


def _jsonb(value: Any) -> Any:
    """Wrap a dict so psycopg sends it as JSONB rather than as a text literal."""
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def dump_run(run: RunResult) -> str:
    return run.model_dump_json()


def load_run(blob: str) -> RunResult:
    return RunResult.model_validate(json.loads(blob))
