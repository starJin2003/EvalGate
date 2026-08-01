"""Golden/train disjointness and adversarial symbol matching."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evalgate_training import config
from evalgate_training.questions import verify


def test_distinctive_parts_keeps_specific_identifiers() -> None:
    parts = verify.distinctive_parts("add_response_hook")
    assert "add_response_hook" in parts


def test_generic_words_are_not_expanded_into_standalone_probes() -> None:
    """A dotted symbol expands into components, but generic ones are dropped so a
    corpus-wide ILIKE '%config%' never nukes a legitimate adversarial question."""
    parts = verify.distinctive_parts("FastAPI.config")
    assert "config" not in parts
    assert "FastAPI.config" in parts  # the full symbol is still probed


def test_dotted_symbol_yields_its_components() -> None:
    parts = verify.distinctive_parts("FastAPI.register_teardown")
    assert "register_teardown" in parts


def test_golden_ids_file_never_overlaps_the_dataset(tmp_path: Path) -> None:
    """Guards the one invariant that would silently invalidate every eval number:
    a golden case appearing in the training split."""
    golden = tmp_path / "golden_ids.json"
    dataset = tmp_path / "dataset.jsonl"
    golden.write_text(json.dumps({"per_category": 2, "ids": ["aaa", "bbb"]}))
    dataset.write_text(
        json.dumps({"question_id": "ccc"}) + "\n" + json.dumps({"question_id": "ddd"}) + "\n"
    )
    golden_ids = set(json.loads(golden.read_text())["ids"])
    train_ids = {json.loads(line)["question_id"] for line in dataset.read_text().splitlines()}
    assert not (golden_ids & train_ids)


@pytest.mark.skipif(
    not config.GOLDEN_IDS_FILE.exists() or not config.DATASET_FILE.exists(),
    reason="artifacts not generated yet",
)
def test_committed_artifacts_are_disjoint() -> None:
    golden_ids = set(json.loads(config.GOLDEN_IDS_FILE.read_text())["ids"])
    train_ids = {
        json.loads(line)["question_id"]
        for line in config.DATASET_FILE.read_text().splitlines()
        if line.strip()
    }
    assert not (golden_ids & train_ids), "golden cases leaked into the training split"


def test_quotas_sum_into_the_requested_band() -> None:
    total = sum(config.CATEGORY_QUOTAS.values())
    assert 1500 <= total <= 2000
    assert set(config.CATEGORY_QUOTAS) == set(config.CATEGORIES)


def test_golden_size_is_in_the_requested_band() -> None:
    total = config.GOLDEN_PER_CATEGORY * len(config.CATEGORIES)
    assert 80 <= total <= 100
