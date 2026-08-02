"""Export the golden sample as data, and the training split for P1.2.

This used to also render a self-contained HTML page with a localStorage verdict
tracker. That page is gone: `golden review` is now the review path, and two
verdict stores that can disagree with each other is worse than one.
`golden_set.jsonl` stays, as the flat record of what the sample contained.
"""

from __future__ import annotations

import json

from .. import config, db
from ..teacher.prompts import SYSTEM as TEACHER_SYSTEM

ROW_SQL = """
    SELECT q.question_id, q.category, q.repo, q.question, q.absent_symbol,
           q.retrieved, t.answer, t.refused, t.citations, t.valid, t.validation_errors
    FROM questions q
    JOIN teacher_answers t ON t.question_id = q.question_id
    WHERE q.split = %s
    ORDER BY q.category, q.repo, q.question_id
"""


def _rows(split: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(ROW_SQL, (split,)).fetchall()
        out = []
        for r in rows:
            chunk_rows = conn.execute(
                """SELECT chunk_id, repo, heading_path, source_url, content
                   FROM chunks WHERE chunk_id = ANY(%s)""",
                (r[5],),
            ).fetchall()
            by_id = {c[0]: c for c in chunk_rows}
            context = [
                {
                    "label": f"C{i}",
                    "chunk_id": cid,
                    "repo": by_id[cid][1],
                    "heading_path": by_id[cid][2],
                    "source_url": by_id[cid][3],
                    "content": by_id[cid][4],
                }
                for i, cid in enumerate(r[5], start=1)
                if cid in by_id
            ]
            out.append(
                {
                    "question_id": r[0],
                    "category": r[1],
                    "repo": r[2],
                    "question": r[3],
                    "absent_symbol": r[4],
                    "context": context,
                    "answer": r[6],
                    "refused": r[7],
                    "citations": r[8],
                    "valid": r[9],
                    "validation_errors": r[10],
                }
            )
        return out


def export_golden() -> dict[str, int]:
    rows = _rows("golden")
    config.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with config.GOLDEN_JSONL.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    return counts


def export_training() -> dict[str, int]:
    """Chat-format rows for P1.2. Golden ids are excluded here and the exclusion is
    asserted, so an accidental leak fails loudly instead of contaminating eval."""
    rows = _rows("train")
    golden = set(json.loads(config.GOLDEN_IDS_FILE.read_text())["ids"])
    leaked = {r["question_id"] for r in rows} & golden
    if leaked:
        raise RuntimeError(f"{len(leaked)} golden ids leaked into the training split: {leaked}")

    kept = 0
    with config.DATASET_FILE.open("w") as fh:
        for row in rows:
            if not row["valid"]:
                continue
            context = "\n\n---\n\n".join(
                f"[{c['label']}] repo={c['repo']} | {c['heading_path']}\n"
                f"source: {c['source_url']}\n\n{c['content']}"
                for c in row["context"]
            )
            fh.write(
                json.dumps(
                    {
                        "question_id": row["question_id"],
                        "category": row["category"],
                        "repo": row["repo"],
                        "refused": row["refused"],
                        "messages": [
                            {"role": "system", "content": TEACHER_SYSTEM},
                            {
                                "role": "user",
                                "content": (
                                    f"Documentation excerpts:\n\n{context}\n\n"
                                    f"---\n\nQuestion: {row['question']}"
                                ),
                            },
                            {"role": "assistant", "content": row["answer"]},
                        ],
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            kept += 1
    return {"train_rows": kept, "skipped_invalid": len(rows) - kept, "golden_excluded": len(golden)}
