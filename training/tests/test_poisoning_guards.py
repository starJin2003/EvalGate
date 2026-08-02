"""The two dataset-poisoning guards.

Both failures produce training rows that teach the student the wrong lesson: fill
in what you were not shown, and answer things that do not exist.
"""

from __future__ import annotations

import pytest

from evalgate_training import config
from evalgate_training.teacher import validate as v

LABELS = ["C1", "C2", "C3", "C4"]
Q = "What is the difference between Prometheus alerting rules and Grafana alerting?"


# --- guard 1: one-sided comparison -------------------------------------------
def test_describing_the_absent_side_is_fatal() -> None:
    answer = (
        "Grafana alerting evaluates rules in the Grafana backend [C1]. "
        "Prometheus alerting rules are evaluated by the Prometheus server itself [C2]."
    )
    result = v.validate(answer, False, LABELS, "comparison", chunk_repos=["grafana"], question=Q)
    assert not result.valid
    assert "fabricated_absent_side:prometheus" in result.errors


def test_stating_the_absence_is_allowed() -> None:
    answer = (
        "Grafana alerting evaluates rules in the Grafana backend [C1]. "
        "The excerpts do not cover Prometheus alerting rules, so I cannot compare them."
    )
    result = v.validate(answer, False, LABELS, "comparison", chunk_repos=["grafana"], question=Q)
    assert result.valid, result.errors


def test_two_sided_retrieval_is_not_flagged() -> None:
    answer = (
        "Prometheus evaluates alerting rules server-side [C1]. "
        "Grafana evaluates them in its own backend [C2]."
    )
    result = v.validate(
        answer,
        False,
        LABELS,
        "comparison",
        chunk_repos=["prometheus", "grafana"],
        question=Q,
    )
    assert result.valid, result.errors


def test_guard_only_applies_to_the_comparison_category() -> None:
    answer = "Grafana does this [C1]. Prometheus does that [C2]."
    result = v.validate(answer, False, LABELS, "factual", chunk_repos=["grafana"], question=Q)
    assert "fabricated_absent_side" not in " ".join(result.errors)


def test_project_not_named_in_the_question_is_not_an_absent_side() -> None:
    answer = "FastAPI validates with Pydantic models [C1]."
    result = v.validate(
        answer,
        False,
        LABELS,
        "comparison",
        chunk_repos=["fastapi"],
        question="How does FastAPI validate request bodies?",
    )
    assert result.valid, result.errors


def test_disclaimer_detection() -> None:
    assert v.is_disclaimer("The excerpts do not mention Prometheus.")
    assert v.is_disclaimer("No information about Grafana is provided.")
    assert v.is_disclaimer("This is not covered in the documentation.")
    assert not v.is_disclaimer("Prometheus evaluates rules server-side.")


def test_repos_named_uses_word_boundaries() -> None:
    assert v.repos_named("Use PromQL to query") == {"prometheus"}
    assert v.repos_named("grafana dashboards") == {"grafana"}
    assert v.repos_named("no projects here") == set()


# --- guard 2: adversarial refusal ---------------------------------------------
def test_adversarial_answered_confidently_is_fatal() -> None:
    result = v.validate(
        "You configure it with add_response_hook() [C1].", False, LABELS, "adversarial"
    )
    assert not result.valid
    assert "answered_absent" in result.errors


def test_adversarial_refusal_passes() -> None:
    result = v.validate(
        "add_response_hook does not appear in the documentation provided.",
        True,
        LABELS,
        "adversarial",
    )
    assert result.valid, result.errors


# --- the pre-committed k rule --------------------------------------------------
def test_k_rises_globally_below_the_threshold() -> None:
    k, reason = config.decide_k(31.2)
    assert k == config.RETRIEVAL_K_RAISED
    assert "below" in reason


def test_k_holds_at_or_above_the_threshold() -> None:
    assert config.decide_k(50.0)[0] == config.RETRIEVAL_K
    assert config.decide_k(78.4)[0] == config.RETRIEVAL_K


def test_k_unchanged_when_nothing_measured() -> None:
    assert config.decide_k(None)[0] == config.RETRIEVAL_K


def test_rule_is_idempotent_once_applied() -> None:
    """The rule re-measures once, not forever. With k already at the raised value,
    a still-low span must not demand another raise, or the pipeline would loop."""
    assert config.RETRIEVAL_K_RAISED >= config.RETRIEVAL_K
    if config.RETRIEVAL_K == config.RETRIEVAL_K_RAISED:
        assert config.decide_k(24.0)[0] == config.RETRIEVAL_K


@pytest.mark.parametrize("pct", [0.0, 49.9, 50.0, 50.1, 100.0])
def test_k_rule_is_total(pct: float) -> None:
    k, reason = config.decide_k(pct)
    assert k in (config.RETRIEVAL_K, config.RETRIEVAL_K_RAISED)
    assert reason


# --- guard 1 refinement: grounded cross-project claims are not fabrication ------
def test_claim_cited_to_a_chunk_that_mentions_the_absent_project_is_grounded() -> None:
    """A Grafana chunk stating that Grafana Alerting is built on the Prometheus
    model makes a citation to it a legitimate source for naming Prometheus.
    Flagging this was a false positive found in the 20-item dry run."""
    answer = "Grafana Alerting is built on the Prometheus alerting model [C8]."
    result = v.validate(
        answer,
        False,
        ["C8"],
        "comparison",
        chunk_repos=["grafana"],
        question=Q,
        label_mentions={"C8": {"grafana", "prometheus"}},
    )
    assert result.valid, result.errors


def test_claim_cited_to_a_chunk_that_does_not_mention_it_is_still_fabrication() -> None:
    answer = "Prometheus evaluates alerting rules in the server itself [C1]."
    result = v.validate(
        answer,
        False,
        ["C1"],
        "comparison",
        chunk_repos=["grafana"],
        question=Q,
        label_mentions={"C1": {"grafana"}},
    )
    assert not result.valid
    assert "fabricated_absent_side:prometheus" in result.errors


def test_dependency_projects_named_in_the_question_are_not_absent_sides() -> None:
    """FastAPI's docs discuss Pydantic throughout. A cited FastAPI chunk that names
    Pydantic grounds the claim. Also a dry-run false positive."""
    answer = "FastAPI uses Pydantic to serialize the response to JSON [C3]."
    result = v.validate(
        answer,
        False,
        ["C3"],
        "comparison",
        chunk_repos=["fastapi"],
        question="Difference between a Pydantic response model and JWT auth in FastAPI?",
        label_mentions={"C3": {"fastapi", "pydantic"}},
    )
    assert result.valid, result.errors


def test_uncited_claim_about_an_absent_project_is_fabrication() -> None:
    answer = "Grafana does this [C1]. Prometheus stores samples in a TSDB."
    result = v.validate(
        answer,
        False,
        ["C1"],
        "comparison",
        chunk_repos=["grafana"],
        question=Q,
        label_mentions={"C1": {"grafana"}},
    )
    assert not result.valid
    assert any(e.startswith("fabricated_absent_side") for e in result.errors)
