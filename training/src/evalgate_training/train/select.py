"""Apply the pre-committed checkpoint-selection rule to an `eval-adapters` result.

The rule, recorded in DECISIONS.md on 2026-08-02 **before either run started**:

    Candidates are every numbered checkpoint plus the final weights. Score each on
    the full 140-row valid split. Select the lowest full-split valid loss; if two
    are within 0.01, take the earlier one.

Why this is code rather than arithmetic in a terminal: v1's winner qualified by
**0.000230**. That is exactly the margin at which reading a column by eye goes
wrong, and it is also exactly the margin at which a person who wants a particular
answer can find one. The rule was fixed in advance precisely so the outcome is not
a judgement call, and a fixed rule that is applied by hand is only half a
commitment.

The same function runs for v1 and v2. `test_select_checkpoint.py` asserts it
reproduces v1's recorded selection (iter 2000) from v1's committed result file, so
the implementation is validated against a decision that was already made and
published rather than only against its own logic.

Explicitly barred by the same DECISIONS entry: choosing the checkpoint that
maximises the v1-v2 gap. Nothing here can see the other version's numbers -- the
function takes one run's losses and no comparison input at all.
"""

from __future__ import annotations

from typing import Any

TOLERANCE = 0.01
FINAL = "final"


def _iter_of(label: str) -> int:
    """Ordering key. `final` is the last checkpoint of the run by construction, so
    it sorts after every numbered one without needing to know the iteration count.
    """
    return 10**9 if label == FINAL else int(label)


def select_checkpoint(losses: dict[str, float], tolerance: float = TOLERANCE) -> dict[str, Any]:
    """-> the selection, plus everything needed to audit it.

    `losses` is the `losses` map from an `eval-adapters` result: label -> full-split
    valid loss. The `baseline` entry is dropped; it is the untrained model and was
    never a candidate.
    """
    candidates = {k: v for k, v in losses.items() if k != "baseline"}
    if not candidates:
        raise ValueError("no candidate checkpoints in losses")

    ordered = sorted(candidates.items(), key=lambda kv: _iter_of(kv[0]))
    best_label, best_loss = min(ordered, key=lambda kv: kv[1])

    # Everything the metric cannot distinguish from the minimum.
    #
    # The epsilon is for float representation, not for slack in the rule: a gap of
    # exactly 0.01 in decimal comes out as 0.010000000000000009 in binary, which
    # would fall outside a bare `<=` and silently exclude an arm the rule as
    # written includes. No real arm has landed on the boundary -- v1's closest was
    # 0.000230 inside it -- but a rule that changes answer on a representation
    # artefact is not a rule.
    band = [(k, v) for k, v in ordered if v - best_loss <= tolerance + 1e-12]
    winner_label, winner_loss = band[0]  # `ordered` is ascending, so [0] is earliest

    # TWO different margins, and conflating them is easy:
    #
    #   gap_to_raw_minimum  how much WORSE the selected arm scores than the best
    #                       arm. For v1: 1.088358 - 1.078588 = 0.009770.
    #   qualifies_by        how much room the selected arm had before falling OUT
    #                       of the band: tolerance - gap. For v1: 0.000230.
    #
    # DECISIONS records v1 as qualifying "by 0.000230" and calls it knife-edge --
    # that is the second number. A small `qualifies_by` means the selection would
    # have flipped to the raw minimum under a slightly tighter tolerance, i.e. the
    # tie-break is resolving what the metric cannot distinguish rather than
    # expressing a real preference.
    gap = winner_loss - best_loss
    return {
        "rule": (
            "lowest full-split valid loss; if two are within "
            f"{tolerance}, take the earlier one (DECISIONS 2026-08-02)"
        ),
        "tolerance": tolerance,
        "n_candidates": len(candidates),
        "selected": winner_label,
        "selected_loss": winner_loss,
        "raw_minimum": best_label,
        "raw_minimum_loss": best_loss,
        "tie_break_applied": winner_label != best_label,
        "gap_to_raw_minimum": gap,
        "qualifies_by": tolerance - gap,
        "within_tolerance": [k for k, _ in band],
    }


def format_selection(s: dict[str, Any], losses: dict[str, float]) -> str:
    lines = []
    ordered = sorted(
        ((k, v) for k, v in losses.items() if k != "baseline"),
        key=lambda kv: _iter_of(kv[0]),
    )
    base = losses.get("baseline")
    lines.append(f"{'arm':>9}{'val loss':>12}{'vs prev':>10}{'':>4}")
    if base is not None:
        lines.append(f"{'baseline':>9}{base:>12.6f}")
    prev = base
    for label, value in ordered:
        delta = "" if prev is None else f"{value - prev:+.4f}"
        marks = []
        if label in s["within_tolerance"]:
            marks.append("~")
        if label == s["raw_minimum"]:
            marks.append("min")
        if label == s["selected"]:
            marks.append("SELECTED")
        lines.append(f"{label:>9}{value:>12.6f}{delta:>10}  {' '.join(marks)}")
        prev = value
    lines.append("")
    lines.append(f"rule            {s['rule']}")
    lines.append(f"candidates      {s['n_candidates']}")
    lines.append(
        f"within {s['tolerance']}     {', '.join(s['within_tolerance'])}"
        f"  ({len(s['within_tolerance'])} arms)"
    )
    lines.append(f"raw minimum     {s['raw_minimum']} at {s['raw_minimum_loss']:.6f}")
    lines.append(f"SELECTED        {s['selected']} at {s['selected_loss']:.6f}")
    if s["tie_break_applied"]:
        lines.append(
            f"tie-break       applied. The winner scores {s['gap_to_raw_minimum']:.6f} "
            f"above the raw minimum, and clears the {s['tolerance']} band by "
            f"{s['qualifies_by']:.6f}"
        )
        if s["qualifies_by"] < 0.001:
            lines.append(
                "                that is knife-edge: a slightly tighter tolerance "
                f"would have selected {s['raw_minimum']} instead"
            )
    else:
        lines.append("tie-break       not needed; the earliest arm is also the minimum")
    return "\n".join(lines)
