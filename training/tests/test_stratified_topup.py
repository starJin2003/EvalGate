"""Per-repo top-up sizing and the regeneration retry cap.

Deletions concentrate in repos with dense corpora and regular naming, so a single
global overshoot under-fills Grafana and over-fills Prometheus.
"""

from __future__ import annotations

from evalgate_training import config
from evalgate_training.questions.generate import topup_overshoot


def test_overshoot_is_per_repo_for_adversarial() -> None:
    assert topup_overshoot("adversarial", "grafana") > topup_overshoot("adversarial", "prometheus")


def test_each_repo_overshoot_covers_its_own_measured_leak() -> None:
    for repo, rate in config.ADVERSARIAL_LEAK_RATE.items():
        factor = topup_overshoot("adversarial", repo)
        # Requesting gap * factor must net at least the gap after `rate` is removed.
        assert factor * (1 - rate) >= 1.0, f"{repo} under-requests"


def test_non_adversarial_categories_use_the_flat_factor() -> None:
    for category in ("factual", "howto", "comparison"):
        assert topup_overshoot(category, "grafana") == config.TOPUP_OVERSHOOT


def test_unknown_repo_falls_back_to_the_flat_factor() -> None:
    assert topup_overshoot("adversarial", "not-a-repo") == config.TOPUP_OVERSHOOT


def test_overshoot_is_capped() -> None:
    assert topup_overshoot("adversarial", "grafana") <= 2.5


def test_measured_rates_are_probabilities() -> None:
    for repo, rate in config.ADVERSARIAL_LEAK_RATE.items():
        assert 0.0 <= rate < 1.0, repo


def test_grafana_leaks_more_than_prometheus() -> None:
    """Density hypothesis: Grafana has 4,210 chunks against Prometheus' 600."""
    assert config.ADVERSARIAL_LEAK_RATE["grafana"] > config.ADVERSARIAL_LEAK_RATE["prometheus"]


# --- regeneration cap ----------------------------------------------------------
def test_retry_cap_is_two() -> None:
    assert config.MAX_TEACHER_ATTEMPTS == 2


def test_cap_partitions_rows_into_retry_and_drop() -> None:
    """Mirrors regenerate_invalid()'s partition without needing a database."""
    flagged = [("a", 0), ("b", 1), ("c", 2), ("d", 3)]
    retry = [q for q, n in flagged if n < config.MAX_TEACHER_ATTEMPTS]
    dropped = [q for q, n in flagged if n >= config.MAX_TEACHER_ATTEMPTS]
    assert retry == ["a", "b"]
    assert dropped == ["c", "d"]
    assert len(retry) + len(dropped) == len(flagged)
