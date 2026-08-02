"""Dump the 96 selected cases as data an external reviewer can read.

Same 96 cases, same order, and the same text as the `golden review` UI shows,
in JSONL instead of HTML so the review can happen somewhere this repo is not.

Three constraints shape the record, and each of them is a way the review could
be silently invalidated:

  * **No machine judgments.** `valid`, `validation_errors`, `refused`, and the
    parsed citation list are all this pipeline's own opinion of the answer. A
    reviewer who sees them before reading is anchored by them, and the review
    stops being an independent check on the validator. So the record is built
    from `review.Case`, which has no field for them, rather than from a fresh
    query that would have to remember to leave them out.
  * **No re-retrieval.** Chunks come from the ids stored on the question row,
    exactly as `review.load_cases` reads them. Retrieving again would show a
    context the teacher never saw, and any corpus or index drift since the
    teacher ran would turn a fair review into a wrong one.
  * **No truncation.** The reviewer is checking groundedness against the chunk,
    which cannot be done against an elided chunk.

Markers are positional: `[C1]` is the first id in `questions.retrieved`, which
is how `teacher/batch.py` labelled the context when it built the prompt. That is
what lets a marker in the answer be traced back to the chunk it claims.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import config
from .review import Case, load_cases
from .select import load_manifest

# Everything a record is allowed to carry. Asserted on write, so adding a field
# upstream cannot quietly leak a machine judgment into the reviewer's copy.
RECORD_FIELDS = (
    "case_index",
    "question_id",
    "category",
    "repo",
    "question",
    "teacher_answer",
    "chunks",
)
CHUNK_FIELDS = ("marker", "title", "text")


def build_records(cases: list[Case]) -> list[dict]:
    """One record per case, in manifest order, `case_index` starting at 1."""
    records = []
    for index, case in enumerate(cases, start=1):
        if len(case.context) != config.RETRIEVAL_K:
            raise RuntimeError(
                f"case {index} ({case.question_id}) has {len(case.context)} chunks, "
                f"expected {config.RETRIEVAL_K}. A chunk id on the question row is no "
                f"longer in the chunks table, so the reviewer would see less context "
                f"than the teacher did."
            )
        records.append(
            {
                "case_index": index,
                "question_id": case.question_id,
                "category": case.category,
                "repo": case.repo,
                "question": case.question,
                "teacher_answer": case.answer,
                "chunks": [
                    {"marker": c.label, "title": c.heading_path, "text": c.content}
                    for c in case.context
                ],
            }
        )
    return records


def _line(record: dict) -> str:
    """One JSONL line. `ensure_ascii=False` so the reviewer reads the text the
    teacher read, and so the character counts are counts of that text."""
    if tuple(record) != RECORD_FIELDS:
        raise RuntimeError(f"record fields {tuple(record)} != {RECORD_FIELDS}")
    for chunk in record["chunks"]:
        if tuple(chunk) != CHUNK_FIELDS:
            raise RuntimeError(f"chunk fields {tuple(chunk)} != {CHUNK_FIELDS}")
    return json.dumps(record, ensure_ascii=False) + "\n"


def _write(path: Path, records: list[dict]) -> int:
    """-> characters written."""
    text = "".join(_line(r) for r in records)
    path.write_text(text, encoding="utf-8")
    return len(text)


def write_export(records: list[dict], batch_size: int = config.REVIEW_BATCH_SIZE) -> dict:
    config.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    config.REVIEW_BATCH_DIR.mkdir(parents=True, exist_ok=True)

    characters = _write(config.REVIEW_EXPORT_JSONL, records)

    batches = []
    for n, start in enumerate(range(0, len(records), batch_size), start=1):
        slice_ = records[start : start + batch_size]
        path = config.REVIEW_BATCH_DIR / f"batch_{n:02d}.jsonl"
        batches.append(
            {
                "file": path.name,
                "cases": f"{slice_[0]['case_index']}-{slice_[-1]['case_index']}",
                "records": len(slice_),
                "characters": _write(path, slice_),
            }
        )

    # Same records and same serialisation on both sides, so a mismatch means a
    # batch dropped or duplicated a case.
    batched = sum(b["characters"] for b in batches)
    if batched != characters or sum(b["records"] for b in batches) != len(records):
        raise RuntimeError(f"batches hold {batched} chars against {characters} in the export")

    return {
        "records": len(records),
        "characters": characters,
        "jsonl": str(config.REVIEW_EXPORT_JSONL),
        "batch_dir": str(config.REVIEW_BATCH_DIR),
        "batch_size": batch_size,
        "batches": batches,
    }


def export_review(batch_size: int = config.REVIEW_BATCH_SIZE) -> dict:
    cases = load_cases(load_manifest()["order"])
    return write_export(build_records(cases), batch_size=batch_size)


def format_report(result: dict) -> str:
    lines = [
        f"Records            {result['records']}",
        f"Characters         {result['characters']:,}",
        f"Export             {result['jsonl']}",
        f"Batches            {len(result['batches'])} x {result['batch_size']} "
        f"-> {result['batch_dir']}",
        "",
        f"  {'file':<16} {'cases':>7} {'records':>8} {'characters':>12}",
    ]
    for batch in result["batches"]:
        lines.append(
            f"  {batch['file']:<16} {batch['cases']:>7} {batch['records']:>8} "
            f"{batch['characters']:>12,}"
        )
    return "\n".join(lines)
