"""Fixtures. The whole harness is exercised against these, no model required."""

from __future__ import annotations

import pytest

from evalcore import (
    Case,
    Category,
    ContextChunk,
    ScorerKind,
    ScorerSpec,
    StubModel,
    Suite,
    Threshold,
)


def chunk(label: str, repo: str, content: str) -> ContextChunk:
    return ContextChunk(
        label=label,
        chunk_id=f"{repo}-{label}",
        repo=repo,
        heading_path=f"{repo} > section {label}",
        source_url=f"https://example.invalid/{repo}/{label}",
        content=content,
    )


@pytest.fixture
def context() -> list[ContextChunk]:
    return [
        chunk("C1", "fastapi", "FastAPI generates JSON Schema from the response model."),
        chunk("C2", "fastapi", "TestClient calls the async app from normal def tests."),
        chunk("C3", "pydantic", "model_validate_json parses and validates in one pass."),
    ]


@pytest.fixture
def suite(context) -> Suite:
    cases = [
        Case(
            case_id="fact-1",
            category=Category.factual,
            question="What does FastAPI generate from the response model?",
            context=context,
            scorers=[
                ScorerSpec(kind=ScorerKind.citation, weight=2.0),
                ScorerSpec(kind=ScorerKind.refusal, weight=1.0),
            ],
        ),
        Case(
            case_id="howto-1",
            category=Category.howto,
            question="How do I test an async endpoint?",
            context=context,
            scorers=[ScorerSpec(kind=ScorerKind.citation)],
        ),
        Case(
            case_id="cmp-1",
            category=Category.comparison,
            question="How does model_validate_json differ from model_validate?",
            context=context,
            scorers=[ScorerSpec(kind=ScorerKind.citation)],
        ),
        Case(
            case_id="adv-1",
            category=Category.adversarial,
            question="How do I use FastAPI's add_response_hook()?",
            context=context,
            absent_symbol="add_response_hook",
            scorers=[ScorerSpec(kind=ScorerKind.refusal, weight=3.0)],
        ),
    ]
    return Suite(
        suite_id="fixture-suite",
        cases=cases,
        threshold=Threshold(max_drop=0.02),
        category_thresholds={Category.adversarial: Threshold(max_drop=0.01, min_score=0.80)},
    )


@pytest.fixture
def good_outputs() -> dict[str, str]:
    return {
        "fact-1": "FastAPI generates JSON Schema from the response model [C1].",
        "howto-1": "Use TestClient to call the app from a normal def test [C2].",
        "cmp-1": "model_validate_json parses and validates in one pass [C3].",
        "adv-1": "add_response_hook does not appear in the documentation provided.",
    }


@pytest.fixture
def baseline_model(good_outputs) -> StubModel:
    return StubModel(good_outputs, ref="v1")


@pytest.fixture
def regressed_model(good_outputs) -> StubModel:
    """v2: better prose everywhere, but the refusal category collapses.

    This is the P1.2 data-mix experiment in miniature — the exact shape the gate
    has to catch, where the overall average barely moves.
    """
    outputs = dict(good_outputs)
    outputs["adv-1"] = "You register it with FastAPI's add_response_hook() decorator [C1]."
    return StubModel(outputs, ref="v2")
