"""The gate verdict. This is what blocks a merge, so it gets the most tests.

The headline case is a candidate that improves on average while one category
collapses. An overall-only threshold passes it; that is the regression EvalGate
exists to catch.
"""

from __future__ import annotations

import pytest

from evalcore import (
    Category,
    JudgeClient,
    StubJudge,
    StubModel,
    Threshold,
    compare,
    markdown_comment,
    run_suite,
    terminal_report,
)
from evalcore.report import html_report


def test_clean_baseline_scores_well(suite, baseline_model) -> None:
    run = run_suite(suite, baseline_model)
    assert run.score > 0.9
    assert all(r.error is None for r in run.results)


def test_identical_runs_pass(suite, baseline_model) -> None:
    a = run_suite(suite, baseline_model, run_id="a")
    b = run_suite(suite, baseline_model, run_id="b")
    diff = compare(suite, a, b)
    assert diff.verdict == "pass"
    assert diff.delta == pytest.approx(0.0)
    assert not diff.breaches


def test_collapsing_one_category_fails_even_when_overall_barely_moves(
    suite, baseline_model, regressed_model
) -> None:
    base = run_suite(suite, baseline_model, run_id="v1")
    cand = run_suite(suite, regressed_model, run_id="v2")
    diff = compare(suite, base, cand)

    assert diff.verdict == "fail"
    scopes = {b.scope for b in diff.breaches}
    assert "adversarial" in scopes
    cats = diff.by_category()
    assert cats["adversarial"]["delta"] < 0
    # Every other category is untouched, which is what makes this the hard case.
    for name in ("factual", "howto", "comparison"):
        assert cats[name]["delta"] == pytest.approx(0.0)


def test_the_category_check_catches_what_the_overall_delta_hides(
    suite, baseline_model, regressed_model
) -> None:
    """Pin why the gate checks categories at all.

    With a slack overall threshold the run-level delta is comfortably inside
    tolerance, so a gate that only looked at the average would green-light a
    candidate whose refusal behaviour went to zero.
    """
    suite.category_thresholds = {}
    suite.threshold = Threshold(max_drop=0.30)
    base = run_suite(suite, baseline_model, run_id="v1")
    cand = run_suite(suite, regressed_model, run_id="v2")
    diff = compare(suite, base, cand)

    overall_drop = diff.baseline_score - diff.candidate_score
    assert overall_drop <= suite.threshold.max_drop, "overall alone stays in tolerance"
    assert diff.verdict == "fail"
    # The breach is the category, never the overall figure.
    assert {b.scope for b in diff.breaches} == {"adversarial"}


def test_default_threshold_applies_to_every_category_without_an_override(
    suite, baseline_model, regressed_model
) -> None:
    """There is no overall-only mode: an absent override means the default
    threshold still guards each category individually."""
    suite.category_thresholds = {}
    suite.threshold = Threshold(max_drop=0.02)
    diff = compare(
        suite,
        run_suite(suite, baseline_model, run_id="v1"),
        run_suite(suite, regressed_model, run_id="v2"),
    )
    assert any(b.scope == "adversarial" for b in diff.breaches)


def test_a_category_override_can_be_tighter_than_the_default(
    suite, baseline_model, good_outputs
) -> None:
    """A partial dip the default tolerates still trips a tightened category.

    Uses `factual`, which scores citations with partial credit; the adversarial
    case has a single refusal scorer and is therefore all-or-nothing.
    """
    base = run_suite(suite, baseline_model, run_id="v1")
    outputs = dict(good_outputs)
    outputs["fact-1"] = (
        "FastAPI generates JSON Schema from the response model [C1]. "
        "This is generally the behaviour most applications want by default."
    )
    cand = run_suite(suite, StubModel(outputs, ref="v2"), run_id="v2")

    suite.threshold = Threshold(max_drop=0.40)
    suite.category_thresholds = {}
    assert compare(suite, base, cand).verdict == "pass"

    suite.category_thresholds = {Category.factual: Threshold(max_drop=0.01)}
    diff = compare(suite, base, cand)
    assert diff.verdict == "fail"
    assert {b.scope for b in diff.breaches} == {"factual"}


def test_min_score_floor_fires_independently_of_drop(suite, baseline_model) -> None:
    base = run_suite(suite, baseline_model, run_id="v1")
    suite.threshold = Threshold(max_drop=1.0, min_score=0.99)
    weak = StubModel({c.case_id: "Unsupported claim with no citation." for c in suite.cases})
    diff = compare(suite, base, run_suite(suite, weak, run_id="v2"))
    assert diff.verdict == "fail"
    assert any("floor" in b.reason for b in diff.breaches)


def test_a_crashing_model_scores_zero_rather_than_being_skipped(suite, baseline_model) -> None:
    class Broken:
        ref = "broken"

        def generate(self, case):
            raise RuntimeError("endpoint down")

    base = run_suite(suite, baseline_model, run_id="v1")
    cand = run_suite(suite, Broken(), run_id="v2")
    assert cand.score == 0.0
    assert all(r.error for r in cand.results)
    assert compare(suite, base, cand).verdict == "fail"


def test_added_and_removed_cases_are_labelled(suite, baseline_model) -> None:
    base = run_suite(suite, baseline_model, run_id="v1")
    cand = run_suite(suite, baseline_model, run_id="v2")
    cand.results = [r for r in cand.results if r.case_id != "adv-1"]
    diff = compare(suite, base, cand)
    statuses = {c.case_id: c.status for c in diff.cases}
    assert statuses["adv-1"] == "removed"


# --- reports ------------------------------------------------------------------
def test_reports_render_and_agree_on_the_verdict(suite, baseline_model, regressed_model) -> None:
    base = run_suite(suite, baseline_model, run_id="v1")
    cand = run_suite(suite, regressed_model, run_id="v2")
    diff = compare(suite, base, cand)

    term = terminal_report(diff, color=False)
    md = markdown_comment(diff)
    page = html_report(diff)

    assert "FAIL" in term
    assert "❌" in md
    assert "fail" in page
    for text in (term, md, page):
        assert "adversarial" in text
        assert "adv-1" in text


def test_html_report_is_self_contained(suite, baseline_model, regressed_model) -> None:
    diff = compare(
        suite,
        run_suite(suite, baseline_model, run_id="v1"),
        run_suite(suite, regressed_model, run_id="v2"),
    )
    page = html_report(diff)
    assert "http://" not in page.replace("https://example.invalid", "")
    assert "<script" not in page
    assert page.startswith("<!doctype html>")


def test_report_escapes_model_output(suite, baseline_model) -> None:
    evil = StubModel({c.case_id: "<script>alert(1)</script>" for c in suite.cases})
    diff = compare(
        suite,
        run_suite(suite, baseline_model, run_id="v1"),
        run_suite(suite, evil, run_id="v2"),
    )
    assert "<script>alert(1)</script>" not in html_report(diff)
    assert "&lt;script&gt;" in html_report(diff)


def test_judge_participates_in_scoring(suite, baseline_model) -> None:
    from evalcore import ScorerKind, ScorerSpec

    for case in suite.cases:
        case.scorers.append(ScorerSpec(kind=ScorerKind.judge))
    run = run_suite(suite, baseline_model, judge=JudgeClient(StubJudge()))
    assert any(s.kind is ScorerKind.judge for r in run.results for s in r.scores)
    assert run.metadata["judge"] == "stub"
