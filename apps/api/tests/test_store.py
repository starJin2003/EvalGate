"""One set of assertions, run against both Store implementations.

`app.py` maps `KeyError` to 404 and `ValueError` to 400, so the two stores have
to agree on failure as well as on success. A `PostgresStore` that raised the
other one would turn a missing run into the wrong status code, and no test that
only exercises `MemoryStore` would ever notice.

The Postgres half needs a live database and skips without one. Set
`EVALGATE_TEST_DATABASE_URL` to run it; that is how it runs in the container on
the k3s node, which is the only place a real Postgres exists for this project.

That URL must name a database whose name ends in `_test`, and the fixture aborts
the run if it does not. This is not decoration. Teardown runs `TRUNCATE ... CASCADE`,
so a URL pointing at the application database silently destroys everything in it
— which is exactly what happened the first time these tests ran on the node, on
2026-08-03, against `evalgate` rather than `evalgate_test`.
"""

from __future__ import annotations

import os

import pytest

from evalcore import (
    Case,
    Category,
    ContextChunk,
    ScorerKind,
    ScorerSpec,
    StubModel,
    Suite,
    run_suite,
)
from evalgate_api import MemoryStore, PostgresStore
from evalgate_api.app import build_store

TEST_DB_URL = os.environ.get("EVALGATE_TEST_DATABASE_URL", "").strip()


def make_suite(suite_id: str = "s1") -> Suite:
    ctx = [ContextChunk(label="C1", chunk_id="k1", repo="fastapi", content="Response models.")]
    return Suite(
        suite_id=suite_id,
        cases=[
            Case(
                case_id="fact-1",
                category=Category.factual,
                question="q?",
                context=ctx,
                scorers=[ScorerSpec(kind=ScorerKind.citation)],
            )
        ],
    )


def make_run(suite: Suite, run_id: str, ref: str = "v1"):
    return run_suite(
        suite,
        StubModel({"fact-1": "Response models are used [C1]."}, ref=ref),
        run_id=run_id,
    )


@pytest.fixture(params=["memory", "postgres"])
def store(request):
    if request.param == "memory":
        yield MemoryStore()
        return

    if not TEST_DB_URL:
        pytest.skip("EVALGATE_TEST_DATABASE_URL not set; no live Postgres")
    pytest.importorskip("psycopg_pool")

    pg = PostgresStore(TEST_DB_URL)
    _refuse_non_test_database(pg)
    pg.init_schema()
    _truncate(pg)
    try:
        yield pg
    finally:
        _truncate(pg)
        pg.close()


def _refuse_non_test_database(pg: PostgresStore) -> None:
    """Abort the whole run unless the target is a throwaway database.

    Checked against `current_database()` rather than against the URL string,
    because the URL can name a host and port that resolve somewhere other than
    the operator expects, and what teardown truncates is whatever the connection
    actually landed on.

    pytest.exit, not skip and not fail: if the URL is pointed at something real,
    every subsequent test would truncate it again on its way past.
    """
    with pg._pool.connection() as conn:  # noqa: SLF001 - test setup
        dbname = conn.execute("SELECT current_database()").fetchone()[0]
    if not dbname.endswith("_test"):
        pg.close()
        pytest.exit(
            f"EVALGATE_TEST_DATABASE_URL points at database {dbname!r}, whose name does "
            "not end in '_test'. These tests TRUNCATE every table on teardown. "
            "Point them at a throwaway database (e.g. evalgate_test).",
            returncode=2,
        )


def _truncate(pg: PostgresStore) -> None:
    with pg._pool.connection() as conn:  # noqa: SLF001 - test teardown
        conn.execute("TRUNCATE baselines, runs, suites CASCADE")


# --- behaviour both implementations must share --------------------------------
def test_ping(store) -> None:
    store.ping()


def test_suite_round_trip(store) -> None:
    suite = make_suite()
    store.put_suite(suite)
    fetched = store.get_suite("s1")
    assert fetched is not None
    assert fetched.suite_id == "s1"
    assert [c.case_id for c in fetched.cases] == ["fact-1"]
    assert store.get_suite("ghost") is None


def test_put_suite_is_an_upsert(store) -> None:
    store.put_suite(make_suite())
    store.put_suite(make_suite().model_copy(update={"version": "2"}))
    assert len(store.list_suites()) == 1
    assert store.get_suite("s1").version == "2"


def test_run_round_trip_preserves_the_computed_score(store) -> None:
    suite = make_suite()
    store.put_suite(suite)
    run = make_run(suite, "run-1")
    store.put_run(run)
    fetched = store.get_run("run-1")
    assert fetched is not None
    # score is a property over `results`, not a stored field. If the JSONB round
    # trip dropped results, this is where it shows.
    assert fetched.score == pytest.approx(run.score)
    assert store.get_run("ghost") is None


def test_list_runs_is_newest_first_and_filters_by_suite(store) -> None:
    s1, s2 = make_suite("s1"), make_suite("s2")
    store.put_suite(s1)
    store.put_suite(s2)
    store.put_run(make_run(s1, "run-a"))
    store.put_run(make_run(s2, "run-b"))
    store.put_run(make_run(s1, "run-c"))

    assert [r.run_id for r in store.list_runs("s1")] == ["run-c", "run-a"]
    assert [r.run_id for r in store.list_runs("s2")] == ["run-b"]
    assert len(store.list_runs()) == 3
    assert len(store.list_runs(limit=2)) == 2


def test_promotion_is_explicit(store) -> None:
    suite = make_suite()
    store.put_suite(suite)
    store.put_run(make_run(suite, "run-1"))

    assert store.get_baseline("s1", "main") is None
    baseline = store.promote("s1", "main", "run-1")
    assert baseline.run_id == "run-1"
    assert baseline.score == pytest.approx(store.get_run("run-1").score)
    assert store.get_baseline("s1", "main").run_id == "run-1"


def test_promotion_is_per_branch(store) -> None:
    suite = make_suite()
    store.put_suite(suite)
    store.put_run(make_run(suite, "run-1"))
    store.put_run(make_run(suite, "run-2", ref="v2"))

    store.promote("s1", "main", "run-1")
    store.promote("s1", "feature", "run-2")
    assert store.get_baseline("s1", "main").run_id == "run-1"
    assert store.get_baseline("s1", "feature").run_id == "run-2"


def test_repromotion_replaces_the_baseline(store) -> None:
    suite = make_suite()
    store.put_suite(suite)
    store.put_run(make_run(suite, "run-1"))
    store.put_run(make_run(suite, "run-2", ref="v2"))
    store.promote("s1", "main", "run-1")
    store.promote("s1", "main", "run-2")
    assert store.get_baseline("s1", "main").run_id == "run-2"


# --- the failure contract app.py depends on -----------------------------------
def test_promoting_an_unknown_run_raises_keyerror(store) -> None:
    """app.py turns this into a 404."""
    store.put_suite(make_suite())
    with pytest.raises(KeyError):
        store.promote("s1", "main", "ghost")


def test_promoting_across_suites_raises_valueerror(store) -> None:
    """app.py turns this into a 400, which is a different status to the above."""
    s1, s2 = make_suite("s1"), make_suite("s2")
    store.put_suite(s1)
    store.put_suite(s2)
    store.put_run(make_run(s1, "run-1"))
    with pytest.raises(ValueError):
        store.promote("s2", "main", "run-1")


# --- store selection ----------------------------------------------------------
def test_build_store_picks_memory_without_a_database_url(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert isinstance(build_store(), MemoryStore)


def test_build_store_ignores_a_blank_database_url(monkeypatch) -> None:
    """An empty or whitespace value means unset, not "connect to nothing"."""
    monkeypatch.setenv("DATABASE_URL", "   ")
    assert isinstance(build_store(), MemoryStore)
