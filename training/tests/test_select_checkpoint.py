"""The pre-committed checkpoint-selection rule.

The load-bearing test is `test_reproduces_v1s_recorded_selection`: it runs the
implementation against v1's committed sweep and asserts it lands on iter 2000, the
checkpoint that was already selected, published in DECISIONS.md and shipped as a
GGUF. That validates the code against a decision made before the code existed,
rather than only against its own logic -- and it means v2 is selected by something
demonstrably identical to what selected v1, which is the matched-variable
requirement the whole comparison rests on.
"""

from __future__ import annotations

import json

import pytest

from evalgate_training import config
from evalgate_training.train.select import select_checkpoint


def test_reproduces_v1s_recorded_selection() -> None:
    path = config.ARTIFACTS_DIR / "v1_eval_adapters.json"
    if not path.exists():
        pytest.skip("v1 sweep artifact absent")
    losses = json.loads(path.read_text())["losses"]
    s = select_checkpoint(losses)

    assert s["selected"] == "2000"
    assert s["selected_loss"] == pytest.approx(1.088358, abs=1e-6)
    # The raw minimum was the final weights, and the tie-break is what moved it.
    assert s["raw_minimum"] == "final"
    assert s["tie_break_applied"] is True
    # Two different margins. The selected arm scores 0.009770 above the raw
    # minimum, and clears the 0.01 band by 0.000230 -- the latter is the number
    # DECISIONS records and calls knife-edge.
    assert s["gap_to_raw_minimum"] == pytest.approx(0.009770, abs=1e-6)
    assert s["qualifies_by"] == pytest.approx(0.000230, abs=1e-6)
    assert s["within_tolerance"] == ["2000", "2600", "2800", "final"]


def test_baseline_is_never_a_candidate() -> None:
    """The untrained model has the worst loss by far, so this only matters if the
    sign is ever flipped -- but it is also simply not a checkpoint."""
    s = select_checkpoint({"baseline": 0.1, "200": 1.0, "400": 2.0})
    assert s["selected"] == "200"
    assert s["n_candidates"] == 2


def test_final_sorts_after_every_numbered_checkpoint() -> None:
    """`final` has no iteration number but is by construction the last one."""
    s = select_checkpoint({"200": 1.005, "final": 1.000})
    assert s["selected"] == "200", "the earlier arm wins inside the band"
    assert s["raw_minimum"] == "final"


def test_no_tie_break_when_the_earliest_is_also_the_minimum() -> None:
    s = select_checkpoint({"200": 1.0, "400": 1.5, "final": 1.4})
    assert s["selected"] == "200"
    assert s["tie_break_applied"] is False
    assert s["gap_to_raw_minimum"] == pytest.approx(0.0)
    assert s["qualifies_by"] == pytest.approx(0.01)


def test_band_is_measured_from_the_minimum_not_from_the_earliest() -> None:
    """400 is within 0.01 of the 1.000 minimum; 200 is not, despite being earlier."""
    s = select_checkpoint({"200": 1.02, "400": 1.005, "final": 1.000})
    assert s["within_tolerance"] == ["400", "final"]
    assert s["selected"] == "400"


def test_tolerance_is_inclusive_at_the_boundary() -> None:
    s = select_checkpoint({"200": 1.01, "final": 1.00})
    assert s["selected"] == "200"


def test_selection_cannot_see_another_run(  # noqa: D103
) -> None:
    """Structural guard on the barred behaviour: the signature takes one run's
    losses and nothing else, so 'pick the checkpoint that maximises the v1-v2 gap'
    is not expressible through this function."""
    import inspect

    params = set(inspect.signature(select_checkpoint).parameters)
    assert params == {"losses", "tolerance"}
