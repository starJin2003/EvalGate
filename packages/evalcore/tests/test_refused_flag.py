"""`CaseResult.refused` means "the model declined to answer".

It previously meant `any(refusal scorer passed) and category == "adversarial"`,
which is hard-False outside adversarial -- so counting refusals per category
returned 0 there by construction, and a recorded project conclusion ("the student
refused 0 of 24 comparison cases") was an artefact of the expression rather than a
measurement of the model.

Nothing in code read the field, which is why it survived: it is a human-read
column in run artifacts, so no score was ever wrong because of it. The numbers
derived from it by hand were.

The second test below is the important one. The obvious repair -- delete the
category conjunct -- does not fix the flag, it inverts it: for a non-adversarial
case `passed` means the model correctly *answered*, so `refused` would read True
on precisely the cases that did not refuse. Both spellings are pinned here so
neither can come back.
"""

from __future__ import annotations

from evalcore import Case, Category, ScorerKind, ScorerSpec, StubModel
from evalcore.runner import run_case
from evalcore.scorers import is_refusal

REFUSAL = "I must refuse because the provided excerpts do not include that."
ANSWER = "FastAPI generates JSON Schema from the response model [C1]."


def _case(case_id: str, category: Category, **kw) -> Case:
    return Case(
        case_id=case_id,
        category=category,
        question="q?",
        context=[],
        scorers=[ScorerSpec(kind=ScorerKind.refusal)],
        **kw,
    )


def _run(case: Case, output: str):
    return run_case(case, StubModel({case.case_id: output}))


def test_adversarial_refusal_is_flagged() -> None:
    assert _run(_case("a", Category.adversarial, absent_symbol="X"), REFUSAL).refused is True


def test_adversarial_answer_is_not_flagged() -> None:
    assert _run(_case("a", Category.adversarial, absent_symbol="X"), ANSWER).refused is False


def test_non_adversarial_refusal_is_flagged() -> None:
    """THE regression. Under the old expression this was False for every
    non-adversarial category no matter what the model said, which is what made
    the comparison refusal rate unmeasurable."""
    for category in (Category.comparison, Category.factual, Category.howto):
        result = _run(_case("c", category), REFUSAL)
        assert result.refused is True, f"{category} refusal not flagged"


def test_non_adversarial_answer_is_not_flagged_the_inverted_way() -> None:
    """Guards the *other* failure mode. Deleting the category conjunct without
    changing the source would make `refused` True here, because the refusal
    scorer passes when a non-adversarial case correctly answers."""
    for category in (Category.comparison, Category.factual, Category.howto):
        result = _run(_case("c", category), ANSWER)
        assert result.refused is False, f"{category} answer wrongly flagged as refusal"


def test_flag_is_independent_of_whether_refusing_was_correct() -> None:
    """A wrong refusal is still a refusal. The scorer judges correctness; the flag
    reports behaviour, and conflating them is what broke it."""
    wrong = _run(_case("c", Category.comparison), REFUSAL)
    assert wrong.refused is True
    refusal_score = next(s for s in wrong.scores if s.kind is ScorerKind.refusal)
    assert refusal_score.passed is False, "refusing a comparison case is a scorer failure"


def test_flag_agrees_with_the_shared_helper_on_every_case() -> None:
    for category in Category:
        for output in (REFUSAL, ANSWER, ""):
            kw = {"absent_symbol": "X"} if category is Category.adversarial else {}
            result = _run(_case("x", category, **kw), output)
            assert result.refused == is_refusal(output)


def test_a_case_that_errored_is_not_reported_as_refusing() -> None:
    """An empty output from a crashed model must not read as a refusal."""

    class Boom:
        ref = "boom"

        def generate(self, case):
            raise RuntimeError("server gone")

    result = run_case(_case("e", Category.adversarial, absent_symbol="X"), Boom())
    assert result.error is not None
    assert result.refused is False
