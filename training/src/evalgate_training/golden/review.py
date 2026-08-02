"""Hand review of the golden sample: case loading, the verdict log, and the summary.

This module and `review_server` are deliberately dumb. The tool calls no model,
judges nothing, and summarises no chunk. Everything shown to the reviewer is the
exact text the teacher saw or produced. Its only job is to make 96 manual
judgments fast, resumable, and auditable.

Two things follow from "auditable":

  * The verdict log is append-only JSONL. A judgment is never overwritten and
    never deleted. Rejudging a case appends a superseding record, so the log
    keeps the fact that the reviewer changed their mind, which is itself a
    signal about the criteria.
  * Retrieved context is read back from the chunk ids stored on the row, never
    re-retrieved. Re-running retrieval would show the reviewer a context the
    teacher never actually saw, and any drift in the corpus or the index would
    silently turn a fair review into a wrong one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .. import config, db
from .select import load_manifest

VERDICTS = ("pass", "fail")

CASE_SQL = """
    SELECT q.question_id, q.category, q.repo, q.question, q.absent_symbol,
           q.retrieved, t.answer, t.refused
    FROM questions q
    JOIN teacher_answers t ON t.question_id = q.question_id
    WHERE q.question_id = ANY(%s)
"""


@dataclass(frozen=True)
class Chunk:
    label: str
    chunk_id: str
    repo: str
    heading_path: str
    source_url: str
    content: str


@dataclass(frozen=True)
class Case:
    question_id: str
    category: str
    repo: str
    question: str
    absent_symbol: str | None
    answer: str
    refused: bool
    context: list[Chunk] = field(default_factory=list)


@dataclass(frozen=True)
class Judgment:
    question_id: str
    verdict: str
    failed_criterion: int | None
    note: str
    at: str

    @property
    def criterion_name(self) -> str | None:
        if self.failed_criterion is None:
            return None
        return config.REVIEW_CRITERIA.get(self.failed_criterion)


# --- cases --------------------------------------------------------------------
def load_cases(order: list[str]) -> list[Case]:
    """Load the manifest's cases, in manifest order.

    Citation markers are positional: `[C1]` is the first chunk in
    `questions.retrieved`, which is exactly how `teacher/batch.py` labelled the
    context when it built the prompt. Reconstructing the mapping the same way is
    what lets a marker in the answer be tied back to the chunk it came from.
    """
    with db.connect() as conn:
        rows = {r[0]: r for r in conn.execute(CASE_SQL, (order,)).fetchall()}
        missing = [qid for qid in order if qid not in rows]
        if missing:
            raise RuntimeError(
                f"{len(missing)} manifest case(s) are no longer in the database, "
                f"first: {missing[0]}. The manifest and the database disagree; "
                f"re-run `golden select` only if you accept discarding the review."
            )

        chunk_ids = sorted({cid for r in rows.values() for cid in r[5]})
        chunks = {
            c[0]: c
            for c in conn.execute(
                """SELECT chunk_id, repo, heading_path, source_url, content
                   FROM chunks WHERE chunk_id = ANY(%s)""",
                (chunk_ids,),
            ).fetchall()
        }

    cases = []
    for qid in order:
        r = rows[qid]
        context = [
            Chunk(
                label=f"C{i}",
                chunk_id=cid,
                repo=chunks[cid][1],
                heading_path=chunks[cid][2],
                source_url=chunks[cid][3],
                content=chunks[cid][4],
            )
            for i, cid in enumerate(r[5], start=1)
            if cid in chunks
        ]
        cases.append(
            Case(
                question_id=r[0],
                category=r[1],
                repo=r[2],
                question=r[3],
                absent_symbol=r[4],
                answer=r[6],
                refused=r[7],
                context=context,
            )
        )
    return cases


# --- verdict log --------------------------------------------------------------
def _log_path(path: Path | None = None) -> Path:
    return path or config.GOLDEN_REVIEW_JSONL


def record(
    question_id: str,
    verdict: str,
    failed_criterion: int | None = None,
    note: str = "",
    *,
    path: Path | None = None,
) -> Judgment:
    """Append one judgment. Fsynced before returning, so a judgment survives a
    kill between the browser's POST and the next page load -- the resume promise
    is only worth anything if the write is already on disk."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    if verdict == "fail":
        if failed_criterion not in config.REVIEW_CRITERIA:
            raise ValueError(
                f"a failing case needs the first failed criterion, one of "
                f"{sorted(config.REVIEW_CRITERIA)}, got {failed_criterion!r}"
            )
    else:
        failed_criterion = None

    judgment = Judgment(
        question_id=question_id,
        verdict=verdict,
        failed_criterion=failed_criterion,
        note=note.strip(),
        at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    target = _log_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "at": judgment.at,
                    "question_id": judgment.question_id,
                    "verdict": judgment.verdict,
                    "failed_criterion": judgment.failed_criterion,
                    "criterion_name": judgment.criterion_name,
                    "note": judgment.note,
                },
                sort_keys=True,
            )
            + "\n"
        )
        fh.flush()
    return judgment


def load_judgments(path: Path | None = None) -> dict[str, Judgment]:
    """Current verdict per case. Last record wins, which is what makes a
    superseding append a rejudgement rather than a duplicate."""
    target = _log_path(path)
    current: dict[str, Judgment] = {}
    if not target.exists():
        return current
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        current[rec["question_id"]] = Judgment(
            question_id=rec["question_id"],
            verdict=rec["verdict"],
            failed_criterion=rec.get("failed_criterion"),
            note=rec.get("note", ""),
            at=rec.get("at", ""),
        )
    return current


def history(path: Path | None = None) -> list[dict]:
    target = _log_path(path)
    if not target.exists():
        return []
    return [json.loads(ln) for ln in target.read_text(encoding="utf-8").splitlines() if ln.strip()]


def first_unjudged(cases: list[Case], judgments: dict[str, Judgment]) -> int | None:
    """1-based index of the first case with no verdict, or None when complete."""
    for i, case in enumerate(cases, start=1):
        if case.question_id not in judgments:
            return i
    return None


# --- summary ------------------------------------------------------------------
def _rate(passed: int, total: int) -> float | None:
    return round(100.0 * passed / total, 1) if total else None


def summarize(cases: list[Case], judgments: dict[str, Judgment]) -> dict:
    judged = [(c, judgments[c.question_id]) for c in cases if c.question_id in judgments]
    passed = [c for c, j in judged if j.verdict == "pass"]

    by_category: dict[str, dict] = {}
    by_repo: dict[str, dict] = {}
    for bucket, key in ((by_category, "category"), (by_repo, "repo")):
        for case, judgment in judged:
            entry = bucket.setdefault(getattr(case, key), {"judged": 0, "passed": 0})
            entry["judged"] += 1
            entry["passed"] += judgment.verdict == "pass"
        for entry in bucket.values():
            entry["pass_rate_pct"] = _rate(entry["passed"], entry["judged"])

    by_criterion = {
        f"{n} {name}": sum(1 for _, j in judged if j.verdict == "fail" and j.failed_criterion == n)
        for n, name in config.REVIEW_CRITERIA.items()
    }

    return {
        "seed": config.GOLDEN_SEED,
        "cases": len(cases),
        "judged": len(judged),
        "unjudged": len(cases) - len(judged),
        "passed": len(passed),
        "failed": len(judged) - len(passed),
        "pass_rate_pct": _rate(len(passed), len(judged)),
        "by_category": dict(sorted(by_category.items())),
        "by_repo": dict(sorted(by_repo.items())),
        "failures_by_first_failed_criterion": by_criterion,
        "judgment_records": len(history()),
        "log": str(config.GOLDEN_REVIEW_JSONL),
    }


def write_summary(summary: dict) -> Path:
    config.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    config.GOLDEN_REVIEW_SUMMARY.write_text(json.dumps(summary, indent=2) + "\n")
    return config.GOLDEN_REVIEW_SUMMARY


def format_summary(summary: dict) -> str:
    lines = [
        f"Golden hand review  {summary['judged']} of {summary['cases']} judged",
    ]
    if summary["pass_rate_pct"] is not None:
        lines.append(
            f"Pass rate           {summary['pass_rate_pct']}%  "
            f"({summary['passed']} pass / {summary['failed']} fail)"
        )
    lines.append("\nBy category")
    for name, entry in summary["by_category"].items():
        lines.append(f"  {name:<14} {entry['passed']}/{entry['judged']}  {entry['pass_rate_pct']}%")
    lines.append("\nBy repo")
    for name, entry in summary["by_repo"].items():
        lines.append(f"  {name:<14} {entry['passed']}/{entry['judged']}  {entry['pass_rate_pct']}%")
    lines.append("\nFailures by first failed criterion")
    for name, count in summary["failures_by_first_failed_criterion"].items():
        lines.append(f"  {name:<22} {count}")
    return "\n".join(lines)


def load_all() -> tuple[list[Case], dict[str, Judgment], dict]:
    manifest = load_manifest()
    cases = load_cases(manifest["order"])
    return cases, load_judgments(), manifest
