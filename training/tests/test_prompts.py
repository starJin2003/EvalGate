"""Prompt assembly, especially the cross-repo variants."""

from __future__ import annotations

from evalgate_training import config
from evalgate_training.questions import prompts


def _user(messages: list[dict[str, str]]) -> str:
    return messages[1]["content"]


def test_single_repo_prompt_has_no_cross_repo_note() -> None:
    msgs = prompts.build_messages("factual", "pydantic", "ctx", 5)
    assert "span two projects" not in _user(msgs)
    assert "Project: pydantic" in _user(msgs)


def test_cross_repo_label_adds_the_note() -> None:
    msgs = prompts.build_messages("comparison", "prometheus and grafana", "ctx", 5)
    body = _user(msgs)
    assert "span two projects" in body
    assert "never assume a feature of one project exists in the other" in body


def test_adversarial_cross_repo_targets_the_primary_repo() -> None:
    msgs = prompts.build_messages("adversarial", "prometheus and grafana", "ctx", 5)
    body = _user(msgs)
    # The invented symbol must belong to the primary, not the partner.
    assert "about prometheus" in body
    assert "attributes a real capability of the OTHER project to prometheus" in body


def test_adversarial_single_repo_still_names_the_project() -> None:
    body = _user(prompts.build_messages("adversarial", "fastapi", "ctx", 5))
    assert "DOES NOT EXIST in fastapi" in body
    assert "span two projects" not in body


def test_every_category_renders_without_a_missing_placeholder() -> None:
    for category in config.CATEGORIES:
        body = _user(prompts.build_messages(category, "grafana and prometheus", "ctx", 5))
        assert "{" not in body.split("Documentation excerpts")[0]
        assert "Return exactly 5 questions." in body


def test_explicit_primary_overrides_the_label_split() -> None:
    body = _user(prompts.build_messages("adversarial", "a and b", "ctx", 5, primary="grafana"))
    assert "about grafana" in body
