"""v2's data mix, and the invariant the whole v1-to-v2 comparison rests on.

Two kinds of test here.

Pure-function tests over the selection and schedule rules, which need no files.

Manifest tests that run in CI against the two COMMITTED manifests. The rendered
splits are gitignored, so CI cannot hash them -- but both manifests carry the
digests, and asserting `v2.sha256.valid == v1.sha256.valid` from committed data is
exactly the claim that matters: only train may differ between versions. If a
future edit to the v2 builder ever regenerates valid or test, this fails in CI
rather than silently invalidating a 9-hour training comparison.
"""

from __future__ import annotations

import json

import pytest

from evalgate_training import config
from evalgate_training.dataset.mix_v2 import derive_iters, select_kept_adversarial


def _rows(n_adversarial: int = 314, n_other: int = 1164) -> list[dict]:
    rows = [
        {"question_id": f"adv-{i:04d}", "category": "adversarial"} for i in range(n_adversarial)
    ]
    rows += [{"question_id": f"fact-{i:04d}", "category": "factual"} for i in range(n_other)]
    return rows


# --- selection rule -----------------------------------------------------------


def test_keeps_exactly_the_configured_count() -> None:
    kept = select_kept_adversarial(_rows(), keep=64)
    assert len(kept) == 64


def test_selection_is_deterministic_across_calls() -> None:
    rows = _rows()
    assert select_kept_adversarial(rows, keep=64) == select_kept_adversarial(rows, keep=64)


def test_selection_is_independent_of_input_order() -> None:
    """A re-query returning rows in a different order must not change the mix."""
    rows = _rows()
    shuffled = list(reversed(rows))
    assert select_kept_adversarial(rows, keep=64) == select_kept_adversarial(shuffled, keep=64)


def test_selection_is_nested_as_keep_grows() -> None:
    """A sha256 ordering means a larger keep is a superset of a smaller one, so
    changing the fraction later moves one boundary rather than reshuffling."""
    rows = _rows()
    assert select_kept_adversarial(rows, keep=32) < select_kept_adversarial(rows, keep=64)


def test_only_adversarial_ids_are_ever_returned() -> None:
    kept = select_kept_adversarial(_rows(), keep=64)
    assert all(qid.startswith("adv-") for qid in kept)


def test_refuses_to_keep_more_than_exist() -> None:
    with pytest.raises(ValueError, match="only 10"):
        select_kept_adversarial(_rows(n_adversarial=10), keep=64)


# --- schedule rule ------------------------------------------------------------


def test_iters_is_two_epochs_of_the_actual_row_count() -> None:
    assert derive_iters(1228) == 2456
    assert derive_iters(1478) == config.TRAIN_V1_ITERS


def test_iters_scales_with_the_split_rather_than_being_pinned() -> None:
    """The matched variable is the recipe. Pinning iters across differently sized
    splits would give the smaller one more passes over less data, confounding the
    data mix with overfitting."""
    assert derive_iters(1228) < derive_iters(1478)


def test_configured_keep_yields_whole_optimizer_steps() -> None:
    """1,228 rows -> 2,456 iters -> 614 steps at accum 4, with no trailing partial
    accumulation. This is why the keep is 64 and not a rounder 63."""
    v1 = json.loads(config.DATASET_MANIFEST_FILE.read_text())
    rows = v1["totals"]["train"] - (
        v1["by_category"]["adversarial"]["train"] - config.DATASET_V2_ADVERSARIAL_KEEP
    )
    iters = derive_iters(rows)
    assert iters % config.TRAIN_PROBE["grad_accumulation_steps"] == 0


# --- the committed manifests --------------------------------------------------


@pytest.fixture
def v1() -> dict:
    return json.loads(config.DATASET_MANIFEST_FILE.read_text())


@pytest.fixture
def v2() -> dict:
    if not config.DATASET_V2_MANIFEST_FILE.exists():
        pytest.skip("v2 manifest not built yet")
    return json.loads(config.DATASET_V2_MANIFEST_FILE.read_text())


@pytest.mark.parametrize("split", ["valid", "test"])
def test_valid_and_test_digests_are_identical_to_v1(v1: dict, v2: dict, split: str) -> None:
    """THE invariant. Both models must be scored on the same rows, or a score
    delta cannot be attributed to the data mix."""
    assert v2["sha256"][split] == v1["sha256"][split]


def test_train_digest_differs_from_v1(v1: dict, v2: dict) -> None:
    """The one thing that is supposed to change."""
    assert v2["sha256"]["train"] != v1["sha256"]["train"]


def test_v2_train_is_smaller_by_exactly_the_dropped_rows(v1: dict, v2: dict) -> None:
    assert v2["totals"]["train"] == v1["totals"]["train"] - v2["mix"]["adversarial_dropped"]


def test_dropped_plus_kept_accounts_for_every_v1_adversarial_row(v1: dict, v2: dict) -> None:
    assert (
        v2["mix"]["adversarial_keep"] + v2["mix"]["adversarial_dropped"]
        == v1["by_category"]["adversarial"]["train"]
    )


def test_no_category_other_than_adversarial_moved(v1: dict, v2: dict) -> None:
    """One lever. If a future edit starts trimming comparison too, the eval can no
    longer attribute the regression to a single cause."""
    for category, delta in v2["by_category"]["delta"].items():
        if category != "adversarial":
            assert delta == 0, f"{category} changed by {delta}"


def test_kept_and_dropped_ids_are_disjoint_and_complete(v2: dict) -> None:
    kept = set(v2["kept_adversarial_ids"])
    dropped = set(v2["dropped_adversarial_ids"])
    assert not kept & dropped
    assert len(kept) == v2["mix"]["adversarial_keep"]
    assert len(dropped) == v2["mix"]["adversarial_dropped"]


def test_every_v2_adversarial_id_came_from_v1s_train_split(v1: dict, v2: dict) -> None:
    v1_train = set(v1["ids"]["train"])
    assert set(v2["kept_adversarial_ids"]) <= v1_train
    assert set(v2["dropped_adversarial_ids"]) <= v1_train


def test_manifest_schedule_matches_the_derivation(v2: dict) -> None:
    s = v2["schedule"]
    assert s["iters"] == derive_iters(v2["totals"]["train"], s["epochs"])
    assert s["optimizer_steps"] == s["iters"] // s["grad_accumulation_steps"]


def test_practical_weight_rose_and_refusals_fell(v2: dict) -> None:
    """The two things BUILD_PLAN asks the v2 mix to do, asserted rather than
    described."""
    assert v2["shares"]["v2_train"]["howto"] > v2["shares"]["v1_train"]["howto"]
    r1 = v2["refusal_rows"]["v1_train"]
    r2 = v2["refusal_rows"]["v2_train"]
    assert r2["total"] / r2["of"] < r1["total"] / r1["of"]
