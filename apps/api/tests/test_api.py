"""apps/api v0. The gate endpoint is the merge decision, so it carries the weight."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from evalcore import (
    Case,
    Category,
    ContextChunk,
    ScorerKind,
    ScorerSpec,
    StubModel,
    Suite,
    Threshold,
    run_suite,
)
from evalgate_api import MemoryStore, create_app


@pytest.fixture
def suite() -> Suite:
    ctx = [
        ContextChunk(label="C1", chunk_id="k1", repo="fastapi", content="Response models."),
    ]
    return Suite(
        suite_id="s1",
        cases=[
            Case(
                case_id="fact-1",
                category=Category.factual,
                question="q?",
                context=ctx,
                scorers=[ScorerSpec(kind=ScorerKind.citation)],
            ),
            Case(
                case_id="adv-1",
                category=Category.adversarial,
                question="How do I use nope_hook()?",
                context=ctx,
                absent_symbol="nope_hook",
                scorers=[ScorerSpec(kind=ScorerKind.refusal)],
            ),
        ],
        threshold=Threshold(max_drop=0.02),
    )


TOKEN = "test-write-token"


@pytest.fixture
def client(suite) -> TestClient:
    c = TestClient(create_app(MemoryStore(), write_token=TOKEN))
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    c.put(f"/suites/{suite.suite_id}", json=suite.model_dump(mode="json")).raise_for_status()
    return c


def good(suite):
    return run_suite(
        suite,
        StubModel(
            {
                "fact-1": "Response models are used [C1].",
                "adv-1": "nope_hook does not appear in the documentation provided.",
            },
            ref="v1",
        ),
        run_id="run-good",
    )


def bad(suite):
    return run_suite(
        suite,
        StubModel(
            {
                "fact-1": "Response models are used [C1].",
                "adv-1": "You call nope_hook() to register it [C1].",
            },
            ref="v2",
        ),
        run_id="run-bad",
    )


def test_health(client) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_ready_reports_the_store_is_reachable(client) -> None:
    assert client.get("/ready").json() == {"status": "ready"}


def test_ready_is_503_when_the_store_cannot_be_reached(suite) -> None:
    class DeadStore(MemoryStore):
        def ping(self) -> None:
            raise ConnectionError("no route to database")

    c = TestClient(create_app(DeadStore(), write_token=TOKEN))
    assert c.get("/ready").status_code == 503
    # Liveness must stay green, or a database outage becomes a restart loop.
    assert c.get("/health").status_code == 200


# --- write authentication -----------------------------------------------------
def test_writes_require_a_bearer_token(suite) -> None:
    c = TestClient(create_app(MemoryStore(), write_token=TOKEN))
    payload = suite.model_dump(mode="json")
    assert c.put("/suites/s1", json=payload).status_code == 401
    assert (
        c.put("/suites/s1", json=payload, headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    assert c.put("/suites/s1", json=payload, headers={"Authorization": TOKEN}).status_code == 401
    assert (
        c.put("/suites/s1", json=payload, headers={"Authorization": f"Bearer {TOKEN}"}).status_code
        == 201
    )


def test_every_mutating_endpoint_is_guarded(client, suite) -> None:
    """The guard is opt-in per route, so a new write endpoint can silently ship
    unprotected. This asserts the current set rather than trusting the decorator."""
    unauth = TestClient(client.app)
    assert unauth.put("/suites/s1", json=suite.model_dump(mode="json")).status_code == 401
    assert unauth.post("/runs", json=good(suite).model_dump(mode="json")).status_code == 401
    assert unauth.post("/suites/s1/baseline", json={"run_id": "run-good"}).status_code == 401


def test_reads_and_the_gate_stay_open(client, suite) -> None:
    """k6 and the P3 demo hit these without a credential."""
    client.post("/runs", json=good(suite).model_dump(mode="json"))
    unauth = TestClient(client.app)
    assert unauth.get("/suites").status_code == 200
    assert unauth.get("/suites/s1").status_code == 200
    assert unauth.get("/runs").status_code == 200
    assert unauth.post("/suites/s1/gate", json={"candidate_run_id": "run-good"}).status_code == 200


def test_writes_fail_closed_when_no_token_is_configured(suite) -> None:
    """An unset token disables writes rather than opening them. The opposite
    default turns one missing env var into a public write endpoint."""
    c = TestClient(create_app(MemoryStore(), write_token=""))
    r = c.put("/suites/s1", json=suite.model_dump(mode="json"))
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"]


def test_register_and_fetch_suite(client, suite) -> None:
    body = client.get("/suites/s1").json()
    assert body["suite_id"] == "s1"
    assert len(body["cases"]) == 2
    assert client.get("/suites").json()[0]["cases"] == 2


def test_suite_id_mismatch_is_rejected(client, suite) -> None:
    payload = suite.model_dump(mode="json")
    r = client.put("/suites/other", json=payload)
    assert r.status_code == 400


def test_run_against_unknown_suite_is_rejected(client, suite) -> None:
    run = good(suite)
    run.suite_id = "ghost"
    assert client.post("/runs", json=run.model_dump(mode="json")).status_code == 404


def test_submit_and_list_runs(client, suite) -> None:
    r = client.post("/runs", json=good(suite).model_dump(mode="json"))
    assert r.status_code == 201
    assert r.json()["score"] == pytest.approx(1.0)
    assert [x["run_id"] for x in client.get("/runs?suite_id=s1").json()] == ["run-good"]


def test_gate_passes_when_there_is_no_baseline(client, suite) -> None:
    client.post("/runs", json=good(suite).model_dump(mode="json"))
    r = client.post("/suites/s1/gate", json={"candidate_run_id": "run-good"}).json()
    assert r["verdict"] == "pass"
    assert "No baseline" in r["comment"]


def test_promotion_is_explicit_and_gate_uses_it(client, suite) -> None:
    client.post("/runs", json=good(suite).model_dump(mode="json"))
    client.post("/runs", json=bad(suite).model_dump(mode="json"))

    # A newer run must not become the baseline on its own.
    assert client.get("/suites/s1/baseline").status_code == 404
    client.post("/suites/s1/baseline", json={"run_id": "run-good"}).raise_for_status()
    assert client.get("/suites/s1/baseline").json()["run_id"] == "run-good"

    verdict = client.post("/suites/s1/gate", json={"candidate_run_id": "run-bad"}).json()
    assert verdict["verdict"] == "fail"
    assert any(b["scope"] == "adversarial" for b in verdict["breaches"])
    assert "❌" in verdict["comment"]
    assert "adv-1" in verdict["comment"]


def test_gate_passes_an_unchanged_candidate(client, suite) -> None:
    client.post("/runs", json=good(suite).model_dump(mode="json"))
    same = good(suite)
    same.run_id = "run-same"
    client.post("/runs", json=same.model_dump(mode="json"))
    client.post("/suites/s1/baseline", json={"run_id": "run-good"})
    r = client.post("/suites/s1/gate", json={"candidate_run_id": "run-same"}).json()
    assert r["verdict"] == "pass"
    assert not r["breaches"]


def test_promoting_a_run_from_another_suite_is_rejected(client, suite) -> None:
    run = good(suite)
    run.suite_id = "s1"
    client.post("/runs", json=run.model_dump(mode="json"))
    other = suite.model_copy(update={"suite_id": "s2"})
    client.put("/suites/s2", json=other.model_dump(mode="json"))
    r = client.post("/suites/s2/baseline", json={"run_id": "run-good"})
    assert r.status_code == 400


def test_promoting_an_unknown_run_is_rejected(client) -> None:
    assert client.post("/suites/s1/baseline", json={"run_id": "ghost"}).status_code == 404


def test_diff_endpoint_reports_case_level_movement(client, suite) -> None:
    client.post("/runs", json=good(suite).model_dump(mode="json"))
    client.post("/runs", json=bad(suite).model_dump(mode="json"))
    d = client.get("/diff?suite_id=s1&baseline_run=run-good&candidate_run=run-bad").json()
    assert d["verdict"] == "fail"
    assert d["regressed"][0]["case_id"] == "adv-1"
    assert d["categories"]["adversarial"]["delta"] < 0
    assert d["categories"]["factual"]["delta"] == pytest.approx(0.0)


def test_unknown_run_in_diff_is_404(client) -> None:
    r = client.get("/diff?suite_id=s1&baseline_run=ghost&candidate_run=ghost2")
    assert r.status_code == 404
