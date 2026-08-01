"""Top-up round sizing.

A top-up is itself subject to dedupe and the adversarial symbol-absence check, so
requesting exactly the gap reliably lands short again.
"""

from __future__ import annotations

from evalgate_training import config
from evalgate_training.questions import generate


def _gaps(counts: dict[str, int]) -> dict[str, int]:
    return {
        category: int(
            (config.CATEGORY_QUOTAS[category] - counts.get(category, 0)) * config.TOPUP_OVERSHOOT
        )
        for category in config.CATEGORIES
        if counts.get(category, 0) < config.CATEGORY_QUOTAS[category]
    }


def test_no_gap_when_every_category_is_at_quota() -> None:
    assert _gaps(dict(config.CATEGORY_QUOTAS)) == {}


def test_only_short_categories_appear() -> None:
    counts = dict(config.CATEGORY_QUOTAS)
    counts["adversarial"] -= 120
    gaps = _gaps(counts)
    assert set(gaps) == {"adversarial"}


def test_gap_is_over_requested_to_survive_the_deletion_pass() -> None:
    counts = dict(config.CATEGORY_QUOTAS)
    counts["adversarial"] -= 100
    assert _gaps(counts)["adversarial"] == int(100 * config.TOPUP_OVERSHOOT)


def test_overshoot_covers_the_measured_leak_rate() -> None:
    """104 of 400 adversarial questions were deleted on 2026-07-31, a 26% loss.
    Netting the gap needs at least 1/0.74 = 1.35x."""
    assert config.TOPUP_OVERSHOOT >= 1 / (1 - 0.26)


def test_overshooting_a_category_is_not_treated_as_a_gap() -> None:
    counts = dict(config.CATEGORY_QUOTAS)
    counts["factual"] += 50
    assert "factual" not in _gaps(counts)


def test_topup_job_names_are_distinct_per_round() -> None:
    names = {generate.JOB} | {f"{generate.JOB}_topup{n}" for n in (1, 2, 3)}
    assert len(names) == 4
