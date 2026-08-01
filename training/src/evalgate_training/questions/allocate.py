"""Repo quota allocation with floors and caps.

The corpus is 68% Grafana and 6% Pydantic. Sampling questions proportionally would
produce a Grafana model that has barely seen Pydantic, so shares are clamped into
[min_share, max_share] and the surplus from capped repos is redistributed to the
others in proportion to their corpus size.

Worked example, 4 repos with corpus shares 16.0 / 68.1 / 9.7 / 6.2 and bounds
[0.15, 0.35]: Grafana caps at 35, Prometheus and Pydantic float up off their
floors, FastAPI absorbs part of Grafana's surplus. Result 25.0 / 35.0 / 21.1 / 18.9.
"""

from __future__ import annotations

import math


def clamped_shares(
    weights: dict[str, float], min_share: float, max_share: float
) -> dict[str, float]:
    """Normalized shares, each inside [min_share, max_share], summing to 1.

    Water-filling: allocate the free budget proportionally, clamp the single worst
    violator, repeat. Clamping one at a time avoids the oscillation you get from
    clamping both directions simultaneously.
    """
    names = sorted(weights)
    n = len(names)
    if n == 0:
        return {}
    if min_share * n > 1.0 + 1e-9:
        raise ValueError(f"min_share {min_share} impossible for {n} repos")
    if max_share * n < 1.0 - 1e-9:
        raise ValueError(f"max_share {max_share} impossible for {n} repos")

    total = sum(weights.values())
    base = {k: (weights[k] / total if total else 1.0 / n) for k in names}

    fixed: dict[str, float] = {}
    free = set(names)
    while free:
        budget = 1.0 - sum(fixed.values())
        pool = sum(base[k] for k in free)
        tentative = {k: (budget * base[k] / pool if pool > 0 else budget / len(free)) for k in free}
        worst_key, worst_gap, worst_val = None, 1e-9, 0.0
        for k, v in tentative.items():
            if v - max_share > worst_gap:
                worst_key, worst_gap, worst_val = k, v - max_share, max_share
            if min_share - v > worst_gap:
                worst_key, worst_gap, worst_val = k, min_share - v, min_share
        if worst_key is None:
            fixed.update(tentative)
            break
        fixed[worst_key] = worst_val
        free.discard(worst_key)
    return fixed


def allocate(
    weights: dict[str, float], total: int, min_share: float, max_share: float
) -> dict[str, int]:
    """Integer counts summing to exactly `total`, using largest-remainder rounding."""
    shares = clamped_shares(weights, min_share, max_share)
    exact = {k: shares[k] * total for k in shares}
    counts = {k: int(math.floor(v)) for k, v in exact.items()}
    short = total - sum(counts.values())
    # Hand the leftovers to the largest fractional parts, ties broken by name.
    order = sorted(exact, key=lambda k: (-(exact[k] - counts[k]), k))
    for k in order[:short]:
        counts[k] += 1
    return counts
