"""The P1.2 split rule, tested without a database.

`plan` is a pure function over `(question_id, category, repo)`, so every property
that matters -- determinism, disjointness, per-cell coverage -- is checkable here
rather than by inspecting the written artifact after the fact.
"""

from __future__ import annotations

from evalgate_training import config
from evalgate_training.dataset.build import SPLITS, plan

CATEGORIES = config.CATEGORIES
REPOS = ("fastapi", "grafana", "prometheus", "pydantic")


def _population(per_cell: int = 100) -> list[tuple[str, str, str]]:
    return [(f"{c}-{r}-{i:04d}", c, r) for c in CATEGORIES for r in REPOS for i in range(per_cell)]


def test_assigns_every_row_exactly_once() -> None:
    pop = _population()
    assignment = plan(pop)
    assert len(assignment) == len(pop)
    assert set(assignment.values()) == set(SPLITS)


def test_splits_are_disjoint() -> None:
    assignment = plan(_population())
    buckets = {s: {q for q, v in assignment.items() if v == s} for s in SPLITS}
    assert buckets["train"] & buckets["valid"] == set()
    assert buckets["train"] & buckets["test"] == set()
    assert buckets["valid"] & buckets["test"] == set()


def test_deterministic_across_input_order() -> None:
    """Postgres row order must not change the split. This is why selection hashes
    each id instead of seeding an RNG over a list."""
    pop = _population()
    assert plan(pop) == plan(list(reversed(pop)))


def test_every_cell_reaches_valid_and_test() -> None:
    """Rounding is per cell, so no (category, repo) cell can vanish from the
    held-out data -- a missing cell would hide the regression it exists to catch."""
    assignment = plan(_population())
    for c in CATEGORIES:
        for r in REPOS:
            got = {assignment[f"{c}-{r}-{i:04d}"] for i in range(100)}
            assert got == set(SPLITS), f"{c}/{r} missing a split: {got}"


def test_small_cell_still_yields_held_out_rows() -> None:
    """The smallest real cell is comparison/pydantic at 52 rows. `max(1, ...)`
    guarantees held-out coverage even if a future cell is far smaller."""
    tiny = [(f"q-{i:02d}", "comparison", "pydantic") for i in range(5)]
    assignment = plan(tiny)
    assert sorted(assignment.values()).count("test") >= 1
    assert sorted(assignment.values()).count("valid") >= 1


def test_fractions_are_respected_on_a_large_cell() -> None:
    pop = [(f"q{i:05d}", "factual", "grafana") for i in range(1000)]
    assignment = plan(pop, test_frac=0.08, valid_frac=0.08)
    counts = {s: sum(1 for v in assignment.values() if v == s) for s in SPLITS}
    assert counts["test"] == 80
    assert counts["valid"] == 80
    assert counts["train"] == 840
