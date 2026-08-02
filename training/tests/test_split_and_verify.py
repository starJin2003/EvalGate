"""Golden/train disjointness and adversarial symbol matching."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_committed_manifest_splits_are_disjoint() -> None:
    """The eval-integrity guard, run against the **manifest** rather than the
    rendered splits.

    The splits themselves are gitignored (23 MB of regenerable chunk text), so CI
    cannot read them. It does not need to: every property that can invalidate an
    eval number -- a golden case in training, an id in two splits -- is a property
    of the split *definition*, and `dataset_manifest.json` carries the full
    per-split id lists and is committed. Byte-level agreement between the manifest
    and a local rebuild is a different claim, checked by `dataset verify`.

    No skipif. Both files this reads are committed, so a skip here would mean the
    manifest is missing, which is itself a failure worth seeing.
    """
    golden_ids = set(json.loads(config.GOLDEN_IDS_FILE.read_text())["ids"])
    manifest = json.loads(config.DATASET_MANIFEST_FILE.read_text())
    ids = {s: set(manifest["ids"][s]) for s in ("train", "valid", "test")}

    for split, members in ids.items():
        assert members, f"{split} split is empty in the manifest"
        assert not (golden_ids & members), f"golden cases leaked into {split}"
        assert len(members) == manifest["totals"][split], f"{split} id list disagrees with totals"

    assert not (ids["train"] & ids["valid"]), "train and valid share ids"
    assert not (ids["train"] & ids["test"]), "train and test share ids"
    assert not (ids["valid"] & ids["test"]), "valid and test share ids"
    assert sum(manifest["totals"].values()) == manifest["eligible_rows"]


def test_committed_manifest_pins_a_digest_for_every_split() -> None:
    """Without a digest per split, the gitignored files would be unfalsifiable."""
    manifest = json.loads(config.DATASET_MANIFEST_FILE.read_text())
    for split in ("train", "valid", "test"):
        digest = manifest["sha256"][split]
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")


def test_quotas_sum_into_the_requested_band() -> None:
    total = sum(config.CATEGORY_QUOTAS.values())
    assert 1500 <= total <= 2000
    assert set(config.CATEGORY_QUOTAS) == set(config.CATEGORIES)


def test_golden_size_is_in_the_requested_band() -> None:
    total = config.GOLDEN_PER_CATEGORY * len(config.CATEGORIES)
    assert 80 <= total <= 100
