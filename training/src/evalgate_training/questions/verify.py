"""Verify adversarial questions really are unanswerable.

A generator asked to invent a non-existent API will sometimes name a real one.
Every adversarial question is checked against the full corpus text; any whose
invented symbol actually appears is deleted, because a question with a real answer
in the refusal category would train the model to refuse things it should answer.
"""

from __future__ import annotations

import re

from .. import db, openai_batch

# Split an invented symbol into its distinctive parts, e.g.
# "FastAPI.add_response_hook" -> ["add_response_hook"], "model_validate_strict"
# stays whole. Short or generic fragments are ignored to avoid false positives.
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{4,}")
GENERIC = {
    "class",
    "config",
    "field",
    "model",
    "param",
    "value",
    "query",
    "response",
    "request",
    "server",
    "client",
    "metric",
    "target",
    "python",
    "import",
    "return",
}


def distinctive_parts(symbol: str) -> list[str]:
    parts = [p for p in IDENT_RE.findall(symbol) if p.lower() not in GENERIC]
    # The full dotted/underscored symbol first, then its components.
    ordered = [symbol.strip()] if len(symbol.strip()) >= 5 else []
    ordered += [p for p in parts if p != symbol.strip()]
    return ordered


def verify_adversarial(delete: bool = True) -> dict[str, int]:
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT question_id, question, absent_symbol FROM questions
               WHERE category = 'adversarial' ORDER BY question_id"""
        ).fetchall()

        leaked: list[str] = []
        no_symbol: list[str] = []

        for question_id, _question, symbol in rows:
            if not symbol:
                no_symbol.append(question_id)
                continue
            hit = False
            for part in distinctive_parts(symbol):
                found = conn.execute(
                    "SELECT 1 FROM chunks WHERE content ILIKE %s LIMIT 1",
                    (f"%{part}%",),
                ).fetchone()
                if found:
                    hit = True
                    break
            if hit:
                leaked.append(question_id)

        doomed = leaked + no_symbol
        if delete and doomed:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM questions WHERE question_id = ANY(%s)", (doomed,))
            conn.commit()

    return {
        "checked": len(rows),
        "symbol_found_in_corpus": len(leaked),
        "missing_symbol": len(no_symbol),
        "deleted": len(doomed) if delete else 0,
    }


def leak_report() -> dict:
    """Per-repo adversarial leak rate, replayed from the stored batch outputs.

    Collision probability scales with corpus density, so a repo with 4,210 chunks
    should leak more than one with 382. Deleted rows are gone from Postgres, but the
    batch outputs still hold every question the generator produced, and custom_id
    carries the repo. Replaying costs nothing: no generation, just a file download.
    """
    by_repo: dict[str, dict[str, int]] = {}
    with db.connect() as conn:
        for job in openai_batch.jobs_matching("questions"):
            try:
                results = openai_batch.fetch(job)
            except RuntimeError:
                continue
            for line in results:
                custom_id, parsed, _p, _c, err = openai_batch.parse_result(line)
                if err or parsed is None:
                    continue
                _, _, category, repo = custom_id.split("-", 3)
                if category != "adversarial":
                    continue
                bucket = by_repo.setdefault(repo, {"generated": 0, "leaked": 0})
                for item in parsed.get("questions", []):
                    symbol = (item.get("absent_symbol") or "").strip()
                    if not item.get("question", "").strip():
                        continue
                    bucket["generated"] += 1
                    if not symbol:
                        bucket["leaked"] += 1
                        continue
                    for part in distinctive_parts(symbol):
                        if conn.execute(
                            "SELECT 1 FROM chunks WHERE content ILIKE %s LIMIT 1", (f"%{part}%",)
                        ).fetchone():
                            bucket["leaked"] += 1
                            break

    chunk_counts = {}
    with db.connect() as conn:
        chunk_counts = dict(
            conn.execute("SELECT repo, count(*) FROM chunks GROUP BY repo").fetchall()
        )

    for repo, bucket in by_repo.items():
        bucket["leak_pct"] = (
            round(100 * bucket["leaked"] / bucket["generated"], 1) if bucket["generated"] else None
        )
        bucket["corpus_chunks"] = chunk_counts.get(repo, 0)
    total_gen = sum(b["generated"] for b in by_repo.values())
    total_leak = sum(b["leaked"] for b in by_repo.values())
    return {
        "by_repo": dict(sorted(by_repo.items())),
        "overall_generated": total_gen,
        "overall_leaked": total_leak,
        "overall_leak_pct": round(100 * total_leak / total_gen, 1) if total_gen else None,
    }


def dedupe() -> int:
    """Drop questions whose text repeats. IDs hash the text, so exact dupes collapse
    on insert; this catches case and whitespace variants."""
    with db.connect() as conn:
        removed = conn.execute(
            """DELETE FROM questions WHERE question_id IN (
                   SELECT question_id FROM (
                       SELECT question_id, row_number() OVER (
                           PARTITION BY lower(regexp_replace(question, '\\s+', ' ', 'g'))
                           ORDER BY question_id
                       ) AS rn FROM questions
                   ) t WHERE t.rn > 1
               )"""
        ).rowcount
        conn.commit()
    return removed
