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


# --- metrics ------------------------------------------------------------------
def _metric_names(app) -> set[str]:
    from prometheus_client import generate_latest

    text = generate_latest(app.state.metrics_registry).decode()
    return {ln.split("{")[0].split(" ")[0] for ln in text.splitlines() if not ln.startswith("#")}


def test_metrics_land_in_the_app_registry_not_the_global_one(client, suite) -> None:
    client.get("/health")
    names = _metric_names(client.app)
    assert any(n.startswith("http_requests_total") for n in names)
    assert any(n.startswith("http_request_duration_seconds") for n in names)
    assert any(n.startswith("http_requests_inprogress") for n in names)


def test_two_apps_do_not_collide_in_the_registry(suite) -> None:
    """The library's own in-progress gauge ignores the custom registry and lands
    in the global default, so a second app raises DuplicateTimeseries. Ours does
    not, and this is the test that catches a regression back to theirs."""
    a = TestClient(create_app(MemoryStore(), write_token=TOKEN))
    b = TestClient(create_app(MemoryStore(), write_token=TOKEN))
    a.get("/health")
    b.get("/health")
    assert a.app.state.metrics_registry is not b.app.state.metrics_registry


def test_requests_are_labelled_by_route_template_not_raw_path(client, suite) -> None:
    """Cardinality guard. Labelling by path would make every suite id a series."""
    from prometheus_client import generate_latest

    client.get("/suites/s1")
    client.get("/suites/ghost")
    text = generate_latest(client.app.state.metrics_registry).decode()
    assert "/suites/{suite_id}" in text
    assert "/suites/ghost" not in text


def test_metrics_are_not_served_on_the_public_app(client) -> None:
    """/metrics lives on its own port, so the Ingress cannot reach it."""
    assert client.get("/metrics").status_code == 404


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


# --- /daily/latest ------------------------------------------------------------
#
# Every one of these asserts the SAME property from a different angle: the
# endpoint must never turn a missing or unusable verdict into something a caller
# reads as passing. `check-daily` blocks on verdict != pass and on age, so a 200
# with an empty body would convert a dead DAG into a green PR check.


def _daily_client(monkeypatch, path) -> TestClient:
    monkeypatch.setenv("EVALGATE_DAILY_LATEST", str(path))
    return TestClient(create_app(MemoryStore(), write_token=TOKEN))


def test_daily_latest_serves_the_published_verdict(monkeypatch, tmp_path) -> None:
    p = tmp_path / "latest.json"
    p.write_text(
        '{"verdict": "pass", "delta": 0.0, "completed_at": "2026-08-05T05:55:27.403857+00:00"}'
    )
    r = _daily_client(monkeypatch, p).get("/daily/latest")
    assert r.status_code == 200
    assert r.json()["verdict"] == "pass"
    assert r.json()["completed_at"] == "2026-08-05T05:55:27.403857+00:00"


def test_daily_latest_is_open_and_needs_no_token(monkeypatch, tmp_path) -> None:
    p = tmp_path / "latest.json"
    p.write_text('{"verdict": "pass", "completed_at": "2026-08-05T05:55:27+00:00"}')
    c = _daily_client(monkeypatch, p)
    # No Authorization header at all, unlike every write path.
    assert c.get("/daily/latest", headers={}).status_code == 200


def test_missing_verdict_is_503_naming_the_path(monkeypatch, tmp_path) -> None:
    r = _daily_client(monkeypatch, tmp_path / "absent.json").get("/daily/latest")
    assert r.status_code == 503
    assert "absent.json" in r.json()["detail"]
    assert "verdict" not in r.json()


def test_unparseable_verdict_is_503_not_a_pass(monkeypatch, tmp_path) -> None:
    p = tmp_path / "latest.json"
    p.write_text("{not json")
    r = _daily_client(monkeypatch, p).get("/daily/latest")
    assert r.status_code == 503
    assert "not valid JSON" in r.json()["detail"]


def test_verdict_without_completed_at_is_refused(monkeypatch, tmp_path) -> None:
    # An un-ageable verdict is indistinguishable from a fresh one to a caller
    # that only reads `verdict`, so serving it would defeat the staleness bound.
    p = tmp_path / "latest.json"
    p.write_text('{"verdict": "pass"}')
    r = _daily_client(monkeypatch, p).get("/daily/latest")
    assert r.status_code == 503
    assert "completed_at" in r.json()["detail"]


def test_a_failing_verdict_is_served_as_is(monkeypatch, tmp_path) -> None:
    # The endpoint reports; it does not judge. check-daily decides.
    p = tmp_path / "latest.json"
    p.write_text('{"verdict": "fail", "completed_at": "2026-08-05T05:55:27+00:00"}')
    r = _daily_client(monkeypatch, p).get("/daily/latest")
    assert r.status_code == 200
    assert r.json()["verdict"] == "fail"
