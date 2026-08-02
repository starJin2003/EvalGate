"""Select the held-out golden sample and write its manifest.

96 cases, 6 per (category, repo) cell across the 16 cells. Balanced per cell, not
per category: the corpus is 68% Grafana and the question set is deliberately not,
so a category-level quota would let the sample drift back toward whichever repos
happen to have the most eligible rows.

Deterministic with no RNG. Rows are ordered by sha256 of `config.GOLDEN_SEED` plus
the question id, so the same eligible population always yields the same sample no
matter what order Postgres returns rows in, and the sample can be reproduced from
the seed string alone.

Eligibility is `teacher_answers.valid`. The point of the review is to estimate the
quality of what P1.2 will actually train on, and `dataset export` skips invalid
rows, so reviewing them would measure a population that never reaches the model.

The manifest is written before any review happens. That ordering is the whole
point: the sample is fixed in advance, so it cannot be reshaped once the verdicts
start coming in and looking bad.
"""

from __future__ import annotations

import hashlib
import json

from .. import config, db

ELIGIBLE_SQL = """
    SELECT q.question_id, q.category, q.repo
    FROM questions q
    JOIN teacher_answers t ON t.question_id = q.question_id
    WHERE t.valid = true
"""


def _order_key(question_id: str) -> str:
    return hashlib.sha256(f"{config.GOLDEN_SEED}|{question_id}".encode()).hexdigest()


def _sorted_ids(ids: list[str]) -> list[str]:
    return sorted(ids, key=_order_key)


def plan(
    eligible: list[tuple[str, str, str]], per_cell: int = config.GOLDEN_PER_CELL
) -> tuple[list[str], list[dict], dict[str, dict]]:
    """-> (ordered case ids, shortfall notes, per-cell detail).

    Pure function over `(question_id, category, repo)` rows so the selection rule
    is testable without a database.

    Two passes. Every cell takes its own quota first; only then does a short cell
    borrow from the rest of its category. A single pass would let an early cell's
    backfill eat rows a later cell still needed for its own quota.
    """
    by_cell: dict[tuple[str, str], list[str]] = {}
    for question_id, category, repo in eligible:
        by_cell.setdefault((category, repo), []).append(question_id)

    categories = list(config.CATEGORIES)
    repos = sorted({repo for _, _, repo in eligible})
    cells = [(c, r) for c in categories for r in repos]

    picked: dict[tuple[str, str], list[str]] = {}
    for cell in cells:
        picked[cell] = _sorted_ids(by_cell.get(cell, []))[:per_cell]

    taken = {qid for ids in picked.values() for qid in ids}
    shortfalls: list[dict] = []
    for cell in cells:
        missing = per_cell - len(picked[cell])
        if missing <= 0:
            continue
        category, repo = cell
        pool = [
            qid
            for qid, cat, _ in eligible
            if cat == category and qid not in taken  # same category, any repo
        ]
        fill = _sorted_ids(pool)[:missing]
        picked[cell].extend(fill)
        taken.update(fill)
        shortfalls.append(
            {
                "cell": f"{category}/{repo}",
                "eligible": len(by_cell.get(cell, [])),
                "quota": per_cell,
                "backfilled_from_category": len(fill),
                "still_short": missing - len(fill),
                "backfilled_ids": fill,
            }
        )

    detail = {
        f"{c}/{r}": {
            "eligible": len(by_cell.get((c, r), [])),
            "selected": len(picked[(c, r)]),
        }
        for c, r in cells
    }
    order = [qid for cell in cells for qid in picked[cell]]
    return order, shortfalls, detail


def select(per_cell: int = config.GOLDEN_PER_CELL) -> dict:
    with db.connect() as conn:
        eligible = [tuple(row) for row in conn.execute(ELIGIBLE_SQL).fetchall()]

    order, shortfalls, detail = plan(eligible, per_cell=per_cell)

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE questions SET split = 'train'")
            cur.execute(
                "UPDATE questions SET split = 'golden' WHERE question_id = ANY(%s)", (order,)
            )
        conn.commit()
        by_category = dict(
            conn.execute(
                "SELECT category, count(*) FROM questions WHERE split='golden' GROUP BY category"
            ).fetchall()
        )

    manifest = {
        "seed": config.GOLDEN_SEED,
        "per_cell": per_cell,
        "total": len(order),
        "eligibility": "teacher_answers.valid = true",
        "cells": detail,
        "shortfalls": shortfalls,
        "order": order,
    }
    config.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    # No timestamp anywhere in here on purpose: rerunning `golden select` on the
    # same population must produce a byte-identical file, so a diff means the
    # sample really moved.
    config.GOLDEN_MANIFEST_FILE.write_text(json.dumps(manifest, indent=2) + "\n")
    config.GOLDEN_IDS_FILE.write_text(
        json.dumps({"per_category": per_cell * 4, "ids": sorted(order)}, indent=2) + "\n"
    )
    return {
        "total": len(order),
        "by_category": by_category,
        "shortfall_cells": len(shortfalls),
        "manifest": str(config.GOLDEN_MANIFEST_FILE),
    }


def load_manifest() -> dict:
    if not config.GOLDEN_MANIFEST_FILE.exists():
        raise RuntimeError(
            f"No golden manifest at {config.GOLDEN_MANIFEST_FILE}. "
            f"Run `evalgate-training golden select` first."
        )
    return json.loads(config.GOLDEN_MANIFEST_FILE.read_text())
