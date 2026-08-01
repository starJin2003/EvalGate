"""Teacher generation: retrieval attachment, dry run, batch submit, collect."""

from __future__ import annotations

import json

from openai import OpenAI

from .. import config, db, openai_batch
from ..budget import Ledger
from ..retrieval import embed_query, retrieve
from . import prompts
from . import validate as validation

JOB = "teacher"


# --- retrieval attachment -----------------------------------------------------
def retrieval_span() -> dict:
    """How many distinct repos each question's top-k spans, by category.

    The corpus is 68% Grafana, so retrieval skews toward it even though question
    sampling is balanced. A comparison question whose top-k lands entirely in one
    repo cannot be answered from both sides, and the teacher should refuse rather
    than half-answer. This measures how often that happens instead of assuming.
    """
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT q.category, q.retrieved FROM questions q
               WHERE q.retrieved IS NOT NULL"""
        ).fetchall()
        chunk_repo = dict(conn.execute("SELECT chunk_id, repo FROM chunks").fetchall())

    stats: dict[str, dict[str, int]] = {}
    for category, retrieved in rows:
        repos = {chunk_repo.get(c) for c in (retrieved or [])} - {None}
        bucket = stats.setdefault(category, {"single_repo": 0, "multi_repo": 0})
        bucket["multi_repo" if len(repos) > 1 else "single_repo"] += 1
    for bucket in stats.values():
        total = bucket["single_repo"] + bucket["multi_repo"]
        bucket["multi_repo_pct"] = round(100 * bucket["multi_repo"] / total, 1) if total else 0
    return stats


def reset_retrieval() -> dict:
    """Clear attached retrieval. Needed when k changes, since stored top-k lists are
    only valid for the k they were produced at."""
    with db.connect() as conn:
        n = conn.execute("UPDATE questions SET retrieved = NULL").rowcount
        conn.commit()
    return {"cleared": n, "k_now": config.RETRIEVAL_K}


def attach_retrieval(ledger: Ledger, limit: int | None = None) -> dict:
    """Run every question through the shared retrieval path and store the ranked
    chunk ids. This is the same code the P1.4 harness calls at eval time."""
    client = OpenAI(api_key=config.openai_api_key())
    attached = 0

    with db.connect() as conn:
        sql = "SELECT question_id, question FROM questions WHERE retrieved IS NULL"
        sql += " ORDER BY question_id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        pending = conn.execute(sql).fetchall()
        if not pending:
            return {"attached": 0}

        est_tokens = sum(len(q[1]) // 4 + 8 for q in pending)
        projected = config.cost_usd(config.EMBED_MODEL, est_tokens, 0, batch=False)
        ledger.reserve("teacher.retrieval", projected, est_tokens)
        print(f"attaching retrieval to {len(pending)} questions, projected ${projected:.4f}")

        for question_id, question in pending:
            vector = embed_query(client, question, ledger)
            hits = retrieve(conn, vector, k=config.RETRIEVAL_K)
            conn.execute(
                "UPDATE questions SET retrieved = %s WHERE question_id = %s",
                (json.dumps([h.chunk_id for h in hits]), question_id),
            )
            attached += 1
            if attached % 100 == 0:
                conn.commit()
                print(f"  {attached}/{len(pending)}  ${ledger.total_usd:.4f} spent")
        conn.commit()

    return {"attached": attached, "retrieval_span_by_category": retrieval_span()}


# --- prompt assembly ----------------------------------------------------------
def _pending_rows(conn, limit: int | None, only_ids: list[str] | None) -> list[tuple]:
    sql = """
        SELECT q.question_id, q.category, q.question, q.retrieved
        FROM questions q
        LEFT JOIN teacher_answers t ON t.question_id = q.question_id
        WHERE t.question_id IS NULL AND q.retrieved IS NOT NULL
    """
    params: list[object] = []
    if only_ids:
        sql += " AND q.question_id = ANY(%s)"
        params.append(only_ids)
    sql += " ORDER BY q.question_id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def _context_for(conn, chunk_ids: list[str]) -> tuple[str, list[str], dict[str, str], list[str]]:
    """-> (rendered context, labels, label->chunk_id, repos represented)."""
    rows = conn.execute(
        """SELECT chunk_id, repo, heading_path, source_url, content
           FROM chunks WHERE chunk_id = ANY(%s)""",
        (chunk_ids,),
    ).fetchall()
    by_id = {r[0]: r for r in rows}
    ordered = [by_id[c] for c in chunk_ids if c in by_id]

    blocks, labels, label_to_chunk = [], [], {}
    for i, r in enumerate(ordered, start=1):
        label = f"C{i}"
        labels.append(label)
        label_to_chunk[label] = r[0]
        blocks.append(f"[{label}] repo={r[1]} | {r[2]}\nsource: {r[3]}\n\n{r[4]}")
    repos = sorted({r[1] for r in ordered})
    return "\n\n---\n\n".join(blocks), labels, label_to_chunk, repos


def _store(
    conn,
    question_id,
    model,
    parsed,
    labels,
    label_to_chunk,
    category,
    ptok,
    ctok,
    bid,
    *,
    chunk_repos=None,
    question="",
):
    answer = parsed.get("answer", "")
    refused = bool(parsed.get("refused", False))
    result = validation.validate(
        answer, refused, labels, category, chunk_repos=chunk_repos, question=question
    )
    conn.execute(
        """INSERT INTO teacher_answers
           (question_id, model, answer, refused, citations, valid,
            validation_errors, prompt_tokens, completion_tokens, batch_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (question_id) DO UPDATE SET
             answer = EXCLUDED.answer, refused = EXCLUDED.refused,
             citations = EXCLUDED.citations, valid = EXCLUDED.valid,
             validation_errors = EXCLUDED.validation_errors""",
        (
            question_id,
            model,
            answer,
            refused,
            json.dumps([label_to_chunk.get(c, c) for c in result.cited]),
            result.valid,
            json.dumps(result.errors),
            ptok,
            ctok,
            bid,
        ),
    )
    # Counted on the question, not the answer, so it survives the delete-and-resubmit
    # cycle regenerate_invalid() uses. That is what makes the retry cap enforceable.
    conn.execute(
        "UPDATE questions SET teacher_attempts = teacher_attempts + 1 WHERE question_id = %s",
        (question_id,),
    )
    return result


# --- dry run ------------------------------------------------------------------
def dry_run(ledger: Ledger, n: int = config.DRY_RUN_SIZE) -> dict:
    """Synchronous, not batched, so measured tokens come back in seconds instead of
    up to 24 hours. Same model and prompts, so the numbers project onto batch rates."""
    client = OpenAI(api_key=config.openai_api_key())
    ptok = ctok = 0
    valid = 0
    errors: dict[str, int] = {}

    with db.connect() as conn:
        rows = _pending_rows(conn, n, None)
        if not rows:
            raise RuntimeError("no questions with retrieval attached. Run earlier stages first.")

        ledger.reserve("teacher.dryrun", 0.05, 0)
        for question_id, category, question, retrieved in rows:
            context, labels, label_to_chunk, chunk_repos = _context_for(conn, retrieved)
            resp = client.chat.completions.create(
                model=config.TEACHER_MODEL,
                messages=prompts.build_messages(question, context),
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": prompts.SCHEMA["name"],
                        "strict": True,
                        "schema": prompts.SCHEMA["schema"],
                    },
                },
            )
            parsed = json.loads(resp.choices[0].message.content)
            ptok += resp.usage.prompt_tokens
            ctok += resp.usage.completion_tokens
            result = _store(
                conn,
                question_id,
                config.TEACHER_MODEL,
                parsed,
                labels,
                label_to_chunk,
                category,
                resp.usage.prompt_tokens,
                resp.usage.completion_tokens,
                "dryrun",
                chunk_repos=chunk_repos,
                question=question,
            )
            valid += int(result.valid)
            for e in result.errors:
                key = e.split(":")[0]
                errors[key] = errors.get(key, 0) + 1
        conn.commit()

        remaining = conn.execute(
            """SELECT count(*) FROM questions q
               LEFT JOIN teacher_answers t ON t.question_id = q.question_id
               WHERE t.question_id IS NULL AND q.retrieved IS NOT NULL"""
        ).fetchone()[0]

    spent = ledger.record(
        "teacher.dryrun", config.TEACHER_MODEL, ptok, ctok, batch=False, note=f"{len(rows)} items"
    )

    mean_in, mean_out = ptok / len(rows), ctok / len(rows)
    projected = config.cost_usd(
        config.TEACHER_MODEL, int(mean_in * remaining), int(mean_out * remaining), batch=True
    )
    report = {
        "dry_run_items": len(rows),
        "valid": valid,
        "validation_errors": errors,
        "measured_prompt_tokens": ptok,
        "measured_completion_tokens": ctok,
        "mean_prompt_tokens": round(mean_in, 1),
        "mean_completion_tokens": round(mean_out, 1),
        "dry_run_usd": round(spent, 4),
        "remaining_questions": remaining,
        "projected_full_run_usd_batch": round(projected, 4),
        "projected_total_usd": round(ledger.total_usd + projected, 4),
        "usd_ceiling": ledger.usd_ceiling,
        "model": config.TEACHER_MODEL,
        "pricing_checked": config.PRICING_CHECKED,
    }
    config.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    config.DRY_RUN_REPORT.write_text(json.dumps(report, indent=2) + "\n")
    return report


# --- full batch ---------------------------------------------------------------
def submit(ledger: Ledger, approved: bool) -> str:
    if not approved:
        raise RuntimeError(
            "Full teacher batch requires explicit approval. Review "
            f"{config.DRY_RUN_REPORT} then rerun with --approve-full-run."
        )
    if not config.DRY_RUN_REPORT.exists():
        raise RuntimeError("No dry-run report. Run `teacher dry-run` first.")

    report = json.loads(config.DRY_RUN_REPORT.read_text())
    projected = report["projected_full_run_usd_batch"]
    ledger.reserve("teacher.batch", projected)

    requests = []
    with db.connect() as conn:
        for question_id, _category, question, retrieved in _pending_rows(conn, None, None):
            context, _labels, _map, _repos = _context_for(conn, retrieved)
            requests.append(
                openai_batch.chat_request(
                    custom_id=question_id,
                    model=config.TEACHER_MODEL,
                    messages=prompts.build_messages(question, context),
                    schema=prompts.SCHEMA,
                )
            )
    print(f"submitting {len(requests)} teacher requests, projected ${projected:.4f} at batch rates")
    return openai_batch.submit(JOB, requests, config.SCRATCH_DIR / "batch")


def collect(ledger: Ledger) -> dict:
    results = openai_batch.fetch(JOB)
    ptok = ctok = 0
    stored = failed = valid = 0
    errors: dict[str, int] = {}

    with db.connect() as conn:
        for line in results:
            question_id, parsed, p, c, err = openai_batch.parse_result(line)
            ptok += p
            ctok += c
            if err or parsed is None:
                failed += 1
                continue
            row = conn.execute(
                "SELECT category, retrieved, question FROM questions WHERE question_id = %s",
                (question_id,),
            ).fetchone()
            if not row:
                failed += 1
                continue
            category, retrieved, question_text = row
            _context, labels, label_to_chunk, chunk_repos = _context_for(conn, retrieved)
            result = _store(
                conn,
                question_id,
                config.TEACHER_MODEL,
                parsed,
                labels,
                label_to_chunk,
                category,
                p,
                c,
                openai_batch.batch_id_for(JOB),
                chunk_repos=chunk_repos,
                question=question_text,
            )
            stored += 1
            valid += int(result.valid)
            for e in result.errors:
                key = e.split(":")[0]
                errors[key] = errors.get(key, 0) + 1
        conn.commit()

    ledger.record(
        "teacher.batch", config.TEACHER_MODEL, ptok, ctok, batch=True, note=f"{stored} stored"
    )
    return {
        "stored": stored,
        "failed": failed,
        "valid": valid,
        "invalid": stored - valid,
        "validation_errors": errors,
        "poisoning_rates": poisoning_rates(),
    }


# --- poisoning audit ----------------------------------------------------------
def poisoning_rates() -> dict:
    """The two rates that describe the teacher's failure modes.

    These are the baselines the student is distilled against: how often gpt-5-mini
    fabricates the side of a comparison it was not shown, and how often it answers
    a question about an API that does not exist instead of refusing.
    """
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT q.category, t.validation_errors, t.refused
               FROM teacher_answers t JOIN questions q ON q.question_id = t.question_id"""
        ).fetchall()

    comparison = fabricated = adversarial = answered = 0
    for category, errs, _refused in rows:
        codes = [e.split(":")[0] for e in (errs or [])]
        if category == "comparison":
            comparison += 1
            fabricated += int("fabricated_absent_side" in codes)
        if category == "adversarial":
            adversarial += 1
            answered += int("answered_absent" in codes)

    return {
        "comparison_rows": comparison,
        "fabricated_absent_side": fabricated,
        "fabricated_absent_side_pct": round(100 * fabricated / comparison, 1)
        if comparison
        else None,
        "adversarial_rows": adversarial,
        "failed_to_refuse": answered,
        "failed_to_refuse_pct": round(100 * answered / adversarial, 1) if adversarial else None,
    }


POISON_CODES = ("fabricated_absent_side", "answered_absent")


def regenerate_invalid(codes: tuple[str, ...] = POISON_CODES) -> dict:
    """Clear poisoned answers so a later `teacher submit` regenerates them.

    Capped at MAX_TEACHER_ATTEMPTS per row. A row past its retry budget is dropped
    instead: a case class the teacher systematically fails will never converge, and
    without a cap the audit/regenerate loop burns budget indefinitely.
    """
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT t.question_id, t.validation_errors, q.teacher_attempts
               FROM teacher_answers t JOIN questions q ON q.question_id = t.question_id
               WHERE t.valid = false"""
        ).fetchall()
        flagged = [
            (qid, attempts)
            for qid, errs, attempts in rows
            if any(e.split(":")[0] in codes for e in (errs or []))
        ]
        retry = [qid for qid, attempts in flagged if attempts < config.MAX_TEACHER_ATTEMPTS]
        exhausted = [qid for qid, attempts in flagged if attempts >= config.MAX_TEACHER_ATTEMPTS]

        with conn.cursor() as cur:
            if retry:
                cur.execute("DELETE FROM teacher_answers WHERE question_id = ANY(%s)", (retry,))
            if exhausted:
                # Drop the question too: leaving it would create a permanent hole
                # that every later audit re-flags.
                cur.execute("DELETE FROM questions WHERE question_id = ANY(%s)", (exhausted,))
        conn.commit()

    return {
        "flagged": len(flagged),
        "queued_for_retry": len(retry),
        "dropped_after_exhausting_retries": len(exhausted),
        "max_attempts": config.MAX_TEACHER_ATTEMPTS,
        "codes": list(codes),
    }


def drop_invalid_questions(codes: tuple[str, ...]) -> dict:
    """Delete the questions themselves, for rows not worth regenerating."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT question_id, validation_errors FROM teacher_answers WHERE valid = false"
        ).fetchall()
        doomed = [qid for qid, errs in rows if any(e.split(":")[0] in codes for e in (errs or []))]
        if doomed:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM questions WHERE question_id = ANY(%s)", (doomed,))
            conn.commit()
    return {"deleted_questions": len(doomed), "codes": list(codes)}
