"""Citation and refusal validation."""

from __future__ import annotations

from evalgate_training.teacher import validate as v

LABELS = ["C1", "C2", "C3", "C4"]


def test_every_sentence_cited_is_valid() -> None:
    answer = "FastAPI reads the default from the parameter signature [C1]. It is optional [C2]."
    assert v.validate(answer, False, LABELS, "factual").valid


def test_uncited_sentence_is_rejected() -> None:
    answer = "FastAPI reads the default from the signature [C1]. This is generally a good idea."
    result = v.validate(answer, False, LABELS, "factual")
    assert not result.valid
    assert any(e.startswith("uncited_sentence") for e in result.errors)


def test_citation_to_a_chunk_that_was_never_supplied_is_rejected() -> None:
    result = v.validate("The default is None [C9].", False, LABELS, "factual")
    assert not result.valid
    assert "cite_unknown:C9" in result.errors


def test_answer_with_no_citations_is_rejected() -> None:
    result = v.validate("The default is None.", False, LABELS, "factual")
    assert not result.valid
    assert "no_citations" in result.errors


def test_adversarial_must_refuse() -> None:
    answered = v.validate("You call add_response_hook() [C1].", False, LABELS, "adversarial")
    assert not answered.valid
    assert "answered_absent" in answered.errors

    refused = v.validate(
        "The documentation provided does not mention add_response_hook.",
        True,
        LABELS,
        "adversarial",
    )
    assert refused.valid


def test_refusal_on_an_answerable_case_is_flagged_but_still_usable() -> None:
    result = v.validate("The excerpts do not cover this.", True, LABELS, "factual")
    assert result.valid  # honest refusal on a retrieval miss is correct behaviour
    assert "refused_present" in result.errors


def test_code_blocks_do_not_count_as_uncited_sentences() -> None:
    answer = "Install it with pip [C1].\n\n```bash\npip install fastapi\nuvicorn main:app\n```"
    assert v.validate(answer, False, LABELS, "howto").valid


def test_cited_labels_are_deduped_and_sorted() -> None:
    result = v.validate("A [C2][C1]. B [C1].", False, LABELS, "factual")
    assert result.cited == ["C1", "C2"]
