"""The ceiling must actually halt. A budget guard that only warns is decoration."""

from __future__ import annotations

from pathlib import Path

import pytest

from evalgate_training import config
from evalgate_training.budget import BudgetExceeded, Ledger


def make_ledger(tmp_path: Path, ceiling: float = 1.0) -> Ledger:
    return Ledger(path=tmp_path / "ledger.json", usd_ceiling=ceiling, token_ceiling=10_000_000)


def test_reserve_blocks_a_call_that_would_cross(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path, ceiling=0.10)
    ledger.record("stage", "gpt-5-mini", 100_000, 10_000, batch=True)
    with pytest.raises(BudgetExceeded, match="ceiling"):
        ledger.reserve("stage", projected_usd=0.50)


def test_record_raises_once_the_ceiling_is_crossed(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path, ceiling=0.01)
    with pytest.raises(BudgetExceeded, match="halted"):
        ledger.record("stage", "gpt-5-mini", 1_000_000, 1_000_000, batch=False)


def test_token_ceiling_is_independent_of_usd(tmp_path: Path) -> None:
    ledger = Ledger(path=tmp_path / "l.json", usd_ceiling=1000.0, token_ceiling=1_000)
    with pytest.raises(BudgetExceeded, match="tokens"):
        ledger.reserve("stage", projected_usd=0.0001, projected_tokens=2_000)


def test_ledger_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    first = Ledger(path=path, usd_ceiling=5.0, token_ceiling=10_000_000)
    first.record("stage", "gpt-5-mini", 1_000, 1_000, batch=True)
    second = Ledger(path=path, usd_ceiling=5.0, token_ceiling=10_000_000)
    assert second.total_usd == pytest.approx(first.total_usd)
    assert second.total_tokens == 2_000


def test_batch_is_exactly_half_of_sync() -> None:
    sync = config.cost_usd("gpt-5-mini", 1_000_000, 1_000_000, batch=False)
    batch = config.cost_usd("gpt-5-mini", 1_000_000, 1_000_000, batch=True)
    assert batch == pytest.approx(sync * config.BATCH_DISCOUNT)


def test_unknown_model_raises_rather_than_costing_zero() -> None:
    with pytest.raises(KeyError, match="No price"):
        config.cost_usd("gpt-does-not-exist", 1000, 1000, batch=False)


def test_teacher_and_judge_candidates_are_disjoint() -> None:
    assert config.TEACHER_MODEL not in config.JUDGE_CANDIDATES
