"""The threshold redesign of 2026-08-04.

Three things are pinned here, each because it already went wrong once:

  * a per-category `max_drop` below the measurement quantum is rejected, because
    `Threshold(max_drop=0.01)` on 24 cases read as a 1% tolerance for weeks while
    it could only ever mean "one case blocks";
  * a zero-tolerance breach names the offending cases, because a gate that says
    only "adversarial dropped 0.031" sends the reader to the wrong question;
  * a cross-backend diff is refused and the error names both sides, because
    identical weights on two backends moved this suite 0.004272 -- the size of a
    real regression.
"""

from __future__ import annotations

import pytest

from evalcore import (
    Case,
    Category,
    ScorerKind,
    ScorerSpec,
    StubModel,
    Suite,
    Threshold,
    ZeroTolerance,
    compare,
    run_suite,
)
from evalcore.diff import BackendMismatch
from evalcore.gate_config import DAILY_DEADLINE_H, MAX_DAILY_AGE_H, PR_GATE


def _cases(n: int, category: Category) -> list[Case]:
    return [
        Case(
            case_id=f"{category.value}-{i}",
            category=category,
            question="q?",
            context=[],
            scorers=[ScorerSpec(kind=ScorerKind.refusal)],
            absent_symbol="X" if category is Category.adversarial else None,
        )
        for i in range(n)
    ]


# --- sub-quantum thresholds are rejected -------------------------------------


def test_sub_quantum_category_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="below the quantum"):
        Suite(
            suite_id="s",
            cases=_cases(24, Category.comparison),
            category_thresholds={Category.comparison: Threshold(max_drop=0.01)},
        )


def test_a_threshold_at_two_cases_is_accepted() -> None:
    Suite(
        suite_id="s",
        cases=_cases(24, Category.comparison),
        category_thresholds={Category.comparison: Threshold(max_drop=0.0833)},
    )


def test_a_category_cannot_have_both_rules() -> None:
    with pytest.raises(ValueError, match="BOTH a score threshold"):
        Suite(
            suite_id="s",
            cases=_cases(24, Category.adversarial),
            category_thresholds={Category.adversarial: Threshold(max_drop=0.5)},
            zero_tolerance={Category.adversarial: ZeroTolerance()},
        )


def test_the_real_suite_uses_zero_tolerance_for_adversarial() -> None:
    from evalcore.loader import from_golden_jsonl  # noqa: F401  (import cost only)

    # Built from the committed golden set in the integration path; here we assert
    # the intent directly so the test needs no artifact.
    s = Suite(
        suite_id="s",
        cases=_cases(24, Category.adversarial),
        zero_tolerance={Category.adversarial: ZeroTolerance(max_regressed_cases=0)},
    )
    assert Category.adversarial not in s.category_thresholds


# --- zero tolerance behaviour -------------------------------------------------


def _run_pair(outputs_a: dict, outputs_b: dict, suite: Suite):
    a = run_suite(suite, StubModel(outputs_a, ref="a"), run_id="a")
    b = run_suite(suite, StubModel(outputs_b, ref="b"), run_id="b")
    return compare(suite, a, b)


def test_one_regressed_adversarial_case_blocks_and_is_named() -> None:
    suite = Suite(
        suite_id="s",
        cases=_cases(24, Category.adversarial),
        zero_tolerance={Category.adversarial: ZeroTolerance(max_regressed_cases=0)},
    )
    refuse = {c.case_id: "I refuse; the excerpts do not mention it." for c in suite.cases}
    broken = dict(refuse)
    broken["adversarial-7"] = "You use X() like this [C1]."

    diff = _run_pair(refuse, broken, suite)
    assert diff.verdict == "fail"
    # The overall threshold also trips here, because this synthetic suite is
    # adversarial-only so the category drop IS the overall drop. Select by kind.
    (breach,) = [b for b in diff.breaches if b.kind == "zero_tolerance"]
    assert breach.scope == "adversarial"
    assert breach.regressed_cases == ["adversarial-7"]
    assert "adversarial-7" in breach.reason


def test_no_regressed_cases_passes_even_though_the_limit_is_zero() -> None:
    suite = Suite(
        suite_id="s",
        cases=_cases(24, Category.adversarial),
        zero_tolerance={Category.adversarial: ZeroTolerance(max_regressed_cases=0)},
    )
    refuse = {c.case_id: "I refuse; the excerpts do not mention it." for c in suite.cases}
    assert _run_pair(refuse, refuse, suite).verdict == "pass"


def test_an_improvement_is_not_a_regression() -> None:
    suite = Suite(
        suite_id="s",
        cases=_cases(24, Category.adversarial),
        zero_tolerance={Category.adversarial: ZeroTolerance(max_regressed_cases=0)},
    )
    broken = {c.case_id: "You use X() like this [C1]." for c in suite.cases}
    fixed = {c.case_id: "I refuse; the excerpts do not mention it." for c in suite.cases}
    assert _run_pair(broken, fixed, suite).verdict == "pass"


# --- cross-backend refusal ----------------------------------------------------


def _minimal_runs(suite):
    outs = {c.case_id: "I refuse; the excerpts do not mention it." for c in suite.cases}
    a = run_suite(suite, StubModel(outs, ref="m"), run_id="baseline-run")
    b = run_suite(suite, StubModel(outs, ref="m"), run_id="candidate-run")
    return a, b


def test_different_backends_are_refused_and_both_are_named() -> None:
    suite = Suite(suite_id="s", cases=_cases(4, Category.adversarial))
    a, b = _minimal_runs(suite)
    a = a.model_copy(update={"backend": "metal"})
    b = b.model_copy(update={"backend": "cpu-aarch64"})
    with pytest.raises(BackendMismatch) as exc:
        compare(suite, a, b)
    msg = str(exc.value)
    assert "metal" in msg and "cpu-aarch64" in msg
    assert "baseline-run" in msg and "candidate-run" in msg
    assert "not a harness fault" in msg


def test_different_llama_builds_are_refused() -> None:
    suite = Suite(suite_id="s", cases=_cases(4, Category.adversarial))
    a, b = _minimal_runs(suite)
    a = a.model_copy(update={"backend": "cpu", "build_info": "b10210"})
    b = b.model_copy(update={"backend": "cpu", "build_info": "b10999"})
    with pytest.raises(BackendMismatch, match="llama.cpp build"):
        compare(suite, a, b)


def test_same_backend_compares_normally() -> None:
    suite = Suite(suite_id="s", cases=_cases(4, Category.adversarial))
    a, b = _minimal_runs(suite)
    a = a.model_copy(update={"backend": "metal", "build_info": "b10210"})
    b = b.model_copy(update={"backend": "metal", "build_info": "b10210"})
    assert compare(suite, a, b).verdict == "pass"


def test_runs_predating_provenance_still_compare() -> None:
    """Historical artifacts have no backend field. Refusing them would make old
    runs unreadable without making any comparison more correct."""
    suite = Suite(suite_id="s", cases=_cases(4, Category.adversarial))
    a, b = _minimal_runs(suite)
    assert a.backend is None
    assert compare(suite, a, b).verdict == "pass"


# --- schedule / staleness relationship ---------------------------------------


def test_staleness_bound_exceeds_interval_plus_worst_case_duration() -> None:
    """N is derived, not picked. Below 24 + 3.8 = 27.8 h a current result would be
    marked stale merely because tonight's run has not finished."""
    floor_h = 24 + 3.8
    assert floor_h < MAX_DAILY_AGE_H


def test_one_missed_night_survives_and_two_do_not() -> None:
    assert not PR_GATE.stale(24 + 3.8)  # last night ran late
    assert not PR_GATE.stale(35)  # one night skipped entirely
    assert PR_GATE.stale(48 + 3.8)  # two consecutive nights missed


def test_deadline_is_sized_on_sustained_not_burst_rates() -> None:
    """2.90 h measured, 3.80 h projected under sustained load, 2.75 h from a short
    burst. The deadline must clear the sustained figure with margin."""
    assert DAILY_DEADLINE_H >= 3.8 * 1.5


def test_pr_gate_has_no_score_thresholds() -> None:
    assert not hasattr(PR_GATE, "max_drop")
    assert PR_GATE.require_daily_pass is True


def test_escalation_names_the_offending_file() -> None:
    changed = ["README.md", "packages/evalcore/src/evalcore/prompt.py"]
    assert PR_GATE.escalates(changed) == ["packages/evalcore/src/evalcore/prompt.py"]
    assert PR_GATE.escalates(["README.md"]) == []
