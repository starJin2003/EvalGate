"""Scorer behaviour. These are the rules the gate's verdict rests on."""

from __future__ import annotations

import pytest

from evalcore import Case, Category, ScorerKind, ScorerSpec
from evalcore.scorers import score_citation, score_exact, score_refusal, score_regex, sentences


def make_case(category: Category, context, **kw) -> Case:
    return Case(
        case_id="c",
        category=category,
        question="q",
        context=context,
        scorers=[ScorerSpec(kind=ScorerKind.citation)],
        **kw,
    )


# --- citation -----------------------------------------------------------------
def test_fully_cited_answer_scores_one(context) -> None:
    case = make_case(Category.factual, context)
    d = score_citation(
        ScorerSpec(kind=ScorerKind.citation), case, "A fact worth stating here [C1]."
    )
    assert d.score == 1.0 and d.passed


def test_partial_citation_gets_partial_credit(context) -> None:
    case = make_case(Category.factual, context)
    answer = (
        "FastAPI generates JSON Schema from the response model [C1]. "
        "This is generally considered a sensible default for most applications."
    )
    d = score_citation(ScorerSpec(kind=ScorerKind.citation), case, answer)
    assert d.score == pytest.approx(0.5)
    assert not d.passed


def test_citing_a_chunk_that_was_never_supplied_scores_zero(context) -> None:
    case = make_case(Category.factual, context)
    d = score_citation(ScorerSpec(kind=ScorerKind.citation), case, "A claim about things [C9].")
    assert d.score == 0.0
    assert "C9" in d.rationale


def test_markers_after_the_full_stop_still_count(context) -> None:
    case = make_case(Category.factual, context)
    answer = "FastAPI generates JSON Schema from the response model. [C1]"
    assert score_citation(ScorerSpec(kind=ScorerKind.citation), case, answer).score == 1.0


def test_refusal_has_nothing_to_cite(context) -> None:
    case = make_case(Category.adversarial, context)
    d = score_citation(ScorerSpec(kind=ScorerKind.citation), case, "No.")
    assert d.score == 1.0


def test_code_blocks_do_not_count_as_uncited_sentences(context) -> None:
    case = make_case(Category.howto, context)
    answer = "Install it with pip [C1].\n\n```bash\npip install fastapi\n```"
    assert score_citation(ScorerSpec(kind=ScorerKind.citation), case, answer).score == 1.0


# --- refusal ------------------------------------------------------------------
def test_adversarial_must_refuse(context) -> None:
    case = make_case(Category.adversarial, context, absent_symbol="add_response_hook")
    spec = ScorerSpec(kind=ScorerKind.refusal)
    assert score_refusal(spec, case, "That does not appear in the documentation.").score == 1.0
    bad = score_refusal(spec, case, "Call add_response_hook() [C1].")
    assert bad.score == 0.0
    assert "add_response_hook" in bad.rationale


def test_non_adversarial_must_not_refuse(context) -> None:
    case = make_case(Category.factual, context)
    spec = ScorerSpec(kind=ScorerKind.refusal)
    assert score_refusal(spec, case, "FastAPI generates JSON Schema [C1].").score == 1.0
    assert score_refusal(spec, case, "That is not covered in the excerpts.").score == 0.0


# --- exact and regex ----------------------------------------------------------
def test_exact_is_case_insensitive_by_default(context) -> None:
    case = make_case(Category.factual, context)
    spec = ScorerSpec(kind=ScorerKind.exact, expected="JSON Schema")
    assert score_exact(spec, case, "json schema").score == 1.0
    strict = ScorerSpec(kind=ScorerKind.exact, expected="JSON Schema", case_sensitive=True)
    assert score_exact(strict, case, "json schema").score == 0.0


def test_regex_can_require_or_forbid(context) -> None:
    case = make_case(Category.factual, context)
    require = ScorerSpec(kind=ScorerKind.regex, pattern=r"JSON Schema")
    assert score_regex(require, case, "returns JSON Schema").score == 1.0
    forbid = ScorerSpec(kind=ScorerKind.regex, pattern=r"TODO", must_match=False)
    assert score_regex(forbid, case, "a clean answer").score == 1.0
    assert score_regex(forbid, case, "TODO finish this").score == 0.0


# --- schema guards ------------------------------------------------------------
def test_scorer_spec_rejects_missing_configuration() -> None:
    with pytest.raises(ValueError, match="expected"):
        ScorerSpec(kind=ScorerKind.exact)
    with pytest.raises(ValueError, match="pattern"):
        ScorerSpec(kind=ScorerKind.regex)
    with pytest.raises(ValueError, match="weight"):
        ScorerSpec(kind=ScorerKind.citation, weight=0)


def test_sentence_splitter_does_not_break_on_abbreviations() -> None:
    assert len(sentences("Use v1.2 of the library [C1]. Then restart it [C2].")) == 2
