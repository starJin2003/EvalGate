"""Repo quota allocation.

The real corpus is 68% Grafana and 6% Pydantic. Without clamping, question
sampling would inherit that skew and produce a Grafana model.
"""

from __future__ import annotations

import pytest

from evalgate_training import config
from evalgate_training.questions.allocate import allocate, clamped_shares

# Measured chunk counts, 2026-07-31.
REAL_CORPUS = {"fastapi": 986, "grafana": 4210, "prometheus": 600, "pydantic": 382}


def test_shares_respect_both_bounds_on_the_real_corpus() -> None:
    shares = clamped_shares(REAL_CORPUS, 0.15, 0.35)
    assert sum(shares.values()) == pytest.approx(1.0)
    for name, share in shares.items():
        assert 0.15 - 1e-9 <= share <= 0.35 + 1e-9, f"{name} at {share}"


def test_dominant_repo_is_capped_and_smallest_is_floored() -> None:
    shares = clamped_shares(REAL_CORPUS, 0.15, 0.35)
    assert shares["grafana"] == pytest.approx(0.35)
    assert shares["pydantic"] >= 0.15


def test_ordering_by_corpus_size_survives_clamping() -> None:
    shares = clamped_shares(REAL_CORPUS, 0.15, 0.35)
    assert shares["grafana"] >= shares["fastapi"] >= shares["prometheus"] >= shares["pydantic"]


def test_counts_sum_to_exactly_the_total() -> None:
    for total in (17, 80, 100, 120, 380, 1900):
        counts = allocate(REAL_CORPUS, total, 0.15, 0.35)
        assert sum(counts.values()) == total


def test_uniform_corpus_is_left_alone() -> None:
    shares = clamped_shares({"a": 10, "b": 10, "c": 10, "d": 10}, 0.15, 0.35)
    assert all(s == pytest.approx(0.25) for s in shares.values())


def test_extreme_skew_still_honours_the_floor() -> None:
    # Bounds must be satisfiable for the repo count: 2 repos need max >= 0.5.
    counts = allocate({"big": 100_000, "tiny": 1}, 100, 0.15, 0.85)
    assert counts["tiny"] >= 15
    assert counts["big"] <= 85


def test_impossible_bounds_raise() -> None:
    with pytest.raises(ValueError, match="min_share"):
        clamped_shares(REAL_CORPUS, 0.30, 0.35)  # 4 x 0.30 > 1
    with pytest.raises(ValueError, match="max_share"):
        clamped_shares(REAL_CORPUS, 0.05, 0.20)  # 4 x 0.20 < 1


def test_configured_bounds_are_satisfiable_for_four_repos() -> None:
    n = 4
    assert config.REPO_MIN_SHARE * n <= 1.0
    assert config.REPO_MAX_SHARE * n >= 1.0


def test_no_repo_exceeds_35_percent_of_the_full_question_budget() -> None:
    total = sum(config.CATEGORY_QUOTAS.values())
    counts = allocate(REAL_CORPUS, total, config.REPO_MIN_SHARE, config.REPO_MAX_SHARE)
    for name, n in counts.items():
        share = n / total
        assert share <= config.REPO_MAX_SHARE + 0.01, f"{name} at {share:.3f}"
        assert share >= config.REPO_MIN_SHARE - 0.01, f"{name} at {share:.3f}"
