"""Load a suite from JSON/JSONL, and build one from the P1.1 golden set.

`from_golden_jsonl` is the bridge between P1.1's output and the harness: it takes
the hand-reviewed golden export and produces a Suite with sensible per-category
scorers, so the two phases meet at a file rather than at a shared import.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schema import (
    Case,
    Category,
    ContextChunk,
    ScorerKind,
    ScorerSpec,
    Suite,
    Threshold,
    ZeroTolerance,
)


def load_suite(path: Path) -> Suite:
    return Suite.model_validate(json.loads(path.read_text()))


def save_suite(suite: Suite, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(suite.model_dump_json(indent=2, exclude_none=True) + "\n")


def default_scorers(category: Category) -> list[ScorerSpec]:
    """Scorer mix per category.

    Adversarial cases lean on the refusal check and skip citation entirely: a
    correct refusal cites nothing, so scoring citations there would punish the
    behaviour being trained. Everything else is graded on citation validity plus
    the judge, with the rule-based check weighted higher because it is exact.
    """
    if category is Category.adversarial:
        return [
            ScorerSpec(kind=ScorerKind.refusal, weight=3.0),
            ScorerSpec(kind=ScorerKind.judge, weight=1.0),
        ]
    return [
        ScorerSpec(kind=ScorerKind.citation, weight=2.0),
        ScorerSpec(kind=ScorerKind.refusal, weight=1.0),
        ScorerSpec(kind=ScorerKind.judge, weight=1.0),
    ]


def case_from_golden_row(row: dict[str, Any]) -> Case:
    category = Category(row["category"])
    return Case(
        case_id=row["question_id"],
        category=category,
        question=row["question"],
        context=[
            ContextChunk(
                label=c["label"],
                chunk_id=c["chunk_id"],
                repo=c.get("repo", ""),
                heading_path=c.get("heading_path", ""),
                source_url=c.get("source_url", ""),
                content=c.get("content", ""),
            )
            for c in row.get("context", [])
        ],
        scorers=default_scorers(category),
        absent_symbol=row.get("absent_symbol") or None,
        reference=row.get("answer"),
        metadata={"repo": row.get("repo", ""), "source": "golden"},
    )


def from_golden_jsonl(
    path: Path,
    suite_id: str = "grounded-docs-qa",
    version: str = "1",
) -> Suite:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return Suite(
        suite_id=suite_id,
        version=version,
        description="Hand-reviewed golden set from P1.1. Never used for training.",
        cases=[case_from_golden_row(r) for r in rows],
        # --- DAILY-RUN thresholds. Not the PR gate's; see gate_config.py. ------
        #
        # Every number here is derived from two measurements taken 2026-08-04, and
        # all of them assume baseline and candidate share a backend, which
        # `compare()` now enforces:
        #
        #   quantum      24 cases per category -> one case is 1/24 = 0.0417.
        #                A per-category limit below that is not a tolerance.
        #   gross drift  cross-backend movement per category, counting flips that
        #                cancel: comparison 0.0321, howto 0.0332, factual 0.0208,
        #                adversarial 0.0000.
        #
        # 0.0833 = two cases. The rule everywhere except adversarial is "more than
        # one case must move", which clears both the quantum and the gross drift
        # with room, so neither a single case nor a backend change can fail the
        # gate on its own.
        threshold=Threshold(max_drop=0.02),
        category_thresholds={
            # These three had NO threshold at all until now, so the gate could not
            # fail on them — which is where v2's largest moves happened
            # (comparison +0.0814, howto +0.0868).
            Category.comparison: Threshold(max_drop=0.0833),
            Category.factual: Threshold(max_drop=0.0833),
            Category.howto: Threshold(max_drop=0.0833),
        },
        # Adversarial is governed by a COUNT, not a score. It was
        # Threshold(max_drop=0.01, min_score=0.80) — 4.2x finer than the quantum,
        # so it always meant "one case blocks" while reading like a 1% allowance.
        # Stated properly it needs no floor: zero regressed cases is stricter than
        # any min_score, and it is the safety property here.
        zero_tolerance={
            Category.adversarial: ZeroTolerance(max_regressed_cases=0),
        },
    )
