"""Select the held-out golden set.

Deterministic: questions are ordered by sha256 of their id and taken round-robin
across repos so each category's 24 cases spread over all four projects. No RNG, so
the same corpus always yields the same split and the selection is reproducible from
the committed golden_ids.json.
"""

from __future__ import annotations

import hashlib
import json

from .. import config, db


def _order_key(question_id: str) -> str:
    return hashlib.sha256(f"golden|{question_id}".encode()).hexdigest()


def select(per_category: int = config.GOLDEN_PER_CATEGORY) -> dict[str, int]:
    chosen: list[str] = []
    with db.connect() as conn:
        for category in config.CATEGORIES:
            rows = conn.execute(
                """SELECT q.question_id, q.repo FROM questions q
                   JOIN teacher_answers t ON t.question_id = q.question_id
                   WHERE q.category = %s AND t.valid = true""",
                (category,),
            ).fetchall()
            by_repo: dict[str, list[str]] = {}
            for question_id, repo in rows:
                by_repo.setdefault(repo, []).append(question_id)
            for ids in by_repo.values():
                ids.sort(key=_order_key)

            picked: list[str] = []
            repos = sorted(by_repo)
            i = 0
            while len(picked) < per_category and any(by_repo[r] for r in repos):
                repo = repos[i % len(repos)]
                if by_repo[repo]:
                    picked.append(by_repo[repo].pop(0))
                i += 1
            chosen.extend(picked)

        with conn.cursor() as cur:
            cur.execute("UPDATE questions SET split = 'train'")
            cur.execute(
                "UPDATE questions SET split = 'golden' WHERE question_id = ANY(%s)", (chosen,)
            )
        conn.commit()

        counts = dict(
            conn.execute(
                "SELECT category, count(*) FROM questions WHERE split='golden' GROUP BY category"
            ).fetchall()
        )

    config.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    config.GOLDEN_IDS_FILE.write_text(
        json.dumps({"per_category": per_category, "ids": sorted(chosen)}, indent=2) + "\n"
    )
    return counts
