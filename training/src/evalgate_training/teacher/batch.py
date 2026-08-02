"""Teacher generation: retrieval attachment, dry run, batch submit, collect."""

from __future__ import annotations

import json

from openai import OpenAI

from evalcore import prompt

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


def _context_for(
    conn, chunk_ids: list[str]
) -> tuple[str, list[str], dict[str, str], list[str], dict[str, set[str]]]:
    """-> (rendered context, labels, label->chunk_id, repos represented, label->projects mentioned).

    The last element is what separates a grounded cross-project claim from a
    fabricated one: a Grafana chunk that names Prometheus makes a citation to it a
    legitimate source for a statement about Prometheus.
    """
    rows = conn.execute(
        """SELECT chunk_id, repo, heading_path, source_url, content
           FROM chunks WHERE chunk_id = ANY(%s)""",
        (chunk_ids,),
    ).fetchall()
    by_id = {r[0]: r for r in rows}
    ordered = [by_id[c] for c in chunk_ids if c in by_id]

    blocks, labels, label_to_chunk = [], [], {}
    label_mentions: dict[str, set[str]] = {}
    for i, r in enumerate(ordered, start=1):
        label = f"C{i}"
        labels.append(label)
        label_to_chunk[label] = r[0]
        # A chunk always vouches for its own project, plus any it names in prose.
        label_mentions[label] = validation.repos_named(r[4]) | {r[1]}
        blocks.append(prompt.render_chunk_block(label, r[1], r[2], r[3], r[4]))
    repos = sorted({r[1] for r in ordered})
    return prompt.CONTEXT_SEPARATOR.join(blocks), labels, label_to_chunk, repos, label_mentions


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
    label_mentions=None,
):
    answer = parsed.get("answer", "")
    refused = bool(parsed.get("refused", False))
    result = validation.validate(
        answer,
        refused,
        labels,
        category,
        chunk_repos=chunk_repos,
        question=question,
        label_mentions=label_mentions,
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
def _dry_run_sample(conn, n: int) -> list[tuple]:
    """Deliberately over-sample the cases the two guards exist to catch.

    A flat first-n sample of 1,900 questions yields ~4 comparison rows and may
    contain no one-sided comparison at all, which makes it useless as a guard
    smoke test. Half the sample is one-sided comparisons (the fabrication case),
    a quarter adversarial (the refusal case), the rest a flat fill.

    This biases the token mix, so the cost projection is computed per category
    against the real category distribution rather than from a flat mean.
    """
    chunk_repo = dict(conn.execute("SELECT chunk_id, repo FROM chunks").fetchall())
    pending = _pending_rows(conn, None, None)

    one_sided, adversarial, other = [], [], []
    for row in pending:
        _qid, category, _q, retrieved = row
        repos = {chunk_repo.get(c) for c in (retrieved or [])} - {None}
        if category == "comparison" and len(repos) <= 1:
            one_sided.append(row)
        elif category == "adversarial":
            adversarial.append(row)
        else:
            other.append(row)

    want_one_sided = n // 2
    want_adversarial = n // 4
    sample = one_sided[:want_one_sided] + adversarial[:want_adversarial]
    sample += other[: n - len(sample)]
    return sample[:n]


def dry_run(
    ledger: Ledger,
    n: int = config.DRY_RUN_SIZE,
    reasoning_effort: str | None = None,
    redo: bool = False,
) -> dict:
    """Synchronous, not batched, so measured tokens come back in seconds instead of
    up to 24 hours. Same model and prompts, so the numbers project onto batch rates."""
    client = OpenAI(api_key=config.openai_api_key())
    ptok = ctok = 0
    valid = 0
    errors: dict[str, int] = {}
    per_category: dict[str, dict[str, int]] = {}

    with db.connect() as conn:
        if redo:
            # Clear prior dry-run answers so the same items are re-sampled. The
            # attempt counter is rewound too: an A/B probe is not a retry and must
            # not consume the regeneration budget.
            conn.execute(
                """UPDATE questions SET teacher_attempts = greatest(teacher_attempts - 1, 0)
                   WHERE question_id IN (
                       SELECT question_id FROM teacher_answers WHERE batch_id = 'dryrun')"""
            )
            conn.execute("DELETE FROM teacher_answers WHERE batch_id = 'dryrun'")
            conn.commit()
        rows = _dry_run_sample(conn, n)
        if not rows:
            raise RuntimeError("no questions with retrieval attached. Run earlier stages first.")

        ledger.reserve("teacher.dryrun", 0.05, 0)
        for question_id, category, question, retrieved in rows:
            context, labels, label_to_chunk, chunk_repos, mentions = _context_for(conn, retrieved)
            effort = reasoning_effort or config.TEACHER_REASONING_EFFORT
            extra = {"reasoning_effort": effort} if effort else {}
            resp = client.chat.completions.create(
                model=config.TEACHER_MODEL,
                messages=prompts.build_messages(question, context),
                **extra,
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
                label_mentions=mentions,
            )
            valid += int(result.valid)
            bucket = per_category.setdefault(category, {"n": 0, "in": 0, "out": 0})
            bucket["n"] += 1
            bucket["in"] += resp.usage.prompt_tokens
            bucket["out"] += resp.usage.completion_tokens
            for e in result.errors:
                key = e.split(":")[0]
                errors[key] = errors.get(key, 0) + 1
        conn.commit()

        remaining_by_category = dict(
            conn.execute(
                """SELECT q.category, count(*) FROM questions q
                   LEFT JOIN teacher_answers t ON t.question_id = q.question_id
                   WHERE t.question_id IS NULL AND q.retrieved IS NOT NULL
                   GROUP BY q.category"""
            ).fetchall()
        )
        remaining = sum(remaining_by_category.values())

    spent = ledger.record(
        "teacher.dryrun", config.TEACHER_MODEL, ptok, ctok, batch=False, note=f"{len(rows)} items"
    )

    # The sample is deliberately skewed toward comparison and adversarial, and
    # refusals are shorter than answers, so a flat mean would under-project output
    # tokens. Project per category against the real remaining distribution.
    mean_in, mean_out = ptok / len(rows), ctok / len(rows)
    proj_in = proj_out = 0.0
    for category, count in remaining_by_category.items():
        bucket = per_category.get(category)
        cat_in = bucket["in"] / bucket["n"] if bucket else mean_in
        cat_out = bucket["out"] / bucket["n"] if bucket else mean_out
        proj_in += cat_in * count
        proj_out += cat_out * count
    projected = config.cost_usd(config.TEACHER_MODEL, int(proj_in), int(proj_out), batch=True)
    report = {
        "dry_run_items": len(rows),
        "valid": valid,
        "validation_errors": errors,
        "measured_prompt_tokens": ptok,
        "measured_completion_tokens": ctok,
        "mean_prompt_tokens": round(mean_in, 1),
        "mean_completion_tokens": round(mean_out, 1),
        "per_category_means": {
            c: {
                "n": b["n"],
                "in": round(b["in"] / b["n"], 1),
                "out": round(b["out"] / b["n"], 1),
            }
            for c, b in sorted(per_category.items())
        },
        "sample_composition": {c: b["n"] for c, b in sorted(per_category.items())},
        "guard_rates": poisoning_rates(),
        "dry_run_usd": round(spent, 4),
        "remaining_questions": remaining,
        "remaining_by_category": remaining_by_category,
        "retrieval_k": config.RETRIEVAL_K,
        "reasoning_effort": reasoning_effort or config.TEACHER_REASONING_EFFORT or "default",
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
def next_part() -> int:
    """Parts already submitted. Pending rows shrink as each part is collected, so
    the shard boundary needs no bookkeeping beyond a distinct job name."""
    return len(openai_batch.jobs_matching(f"{JOB}_part"))


def part_job(n: int) -> str:
    return f"{JOB}_part{n}"


def submit(ledger: Ledger, approved: bool) -> dict:
    """Submit the next shard of pending questions.

    The Batch API caps enqueued tokens per model, so the run is split and each
    shard must finish before the next is sent. Whatever is still pending when this
    is called becomes the next shard, up to SHARD_TOKEN_BUDGET.
    """
    if not approved:
        raise RuntimeError(
            "Full teacher batch requires explicit approval. Review "
            f"{config.DRY_RUN_REPORT} then rerun with --approve-full-run."
        )
    if not config.DRY_RUN_REPORT.exists():
        raise RuntimeError("No dry-run report. Run `teacher dry-run` first.")

    report = json.loads(config.DRY_RUN_REPORT.read_text())
    means = report.get("per_category_means", {})
    fallback_in = report.get("mean_prompt_tokens", 2824)
    fallback_out = report.get("mean_completion_tokens", 1209)

    requests: list[dict] = []
    shard_tokens = 0.0
    shard_in = shard_out = 0.0
    total_pending = 0
    with db.connect() as conn:
        rows = _pending_rows(conn, None, None)
        total_pending = len(rows)
        for question_id, category, question, retrieved in rows:
            m = means.get(category, {})
            est_in = m.get("in", fallback_in)
            est_out = m.get("out", fallback_out)
            est = est_in + est_out
            if requests and shard_tokens + est > config.SHARD_TOKEN_BUDGET:
                break
            context, _labels, _map, _repos, _mentions = _context_for(conn, retrieved)
            requests.append(
                openai_batch.chat_request(
                    custom_id=question_id,
                    model=config.TEACHER_MODEL,
                    messages=prompts.build_messages(question, context),
                    schema=prompts.SCHEMA,
                )
            )
            shard_tokens += est
            shard_in += est_in
            shard_out += est_out

    if not requests:
        raise RuntimeError("nothing pending; every question already has a teacher answer")

    # Price THIS shard from its own estimated tokens. Scaling the stored
    # full-run figure by share-of-pending charges the last shard for the whole
    # run: with 197 of 197 pending, share is 1.0 and the projection is the
    # original total, which tripped the ceiling on work already paid for.
    projected = config.cost_usd(config.TEACHER_MODEL, int(shard_in), int(shard_out), batch=True)
    ledger.reserve("teacher.batch", projected)

    n = next_part()
    print(
        f"part {n}: {len(requests)} of {total_pending} pending, "
        f"~{shard_tokens:,} enqueued tokens (cap {config.ENQUEUED_TOKEN_LIMIT:,}), "
        f"projected ${projected:.4f}"
    )
    batch_id = openai_batch.submit(part_job(n), requests, config.SCRATCH_DIR / "batch")
    return {
        "part": n,
        "batch_id": batch_id,
        "requests": len(requests),
        "still_pending_after": total_pending - len(requests),
        "estimated_enqueued_tokens": shard_tokens,
        "projected_usd": round(projected, 4),
    }


def collect(ledger: Ledger, job: str | None = None) -> dict:
    results = openai_batch.fetch(job or part_job(max(0, next_part() - 1)))
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
            _context, labels, label_to_chunk, chunk_repos, mentions = _context_for(conn, retrieved)
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
                openai_batch.batch_id_for(job or part_job(max(0, next_part() - 1))),
                chunk_repos=chunk_repos,
                question=question_text,
                label_mentions=mentions,
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


def revalidate() -> dict:
    """Re-run validation over stored answers without regenerating anything.

    Validation logic is cheaper to fix than teacher output is to re-buy, so a
    corrected rule must be applyable to work already paid for.
    """
    before = after = 0
    changed = 0
    errors: dict[str, int] = {}
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT t.question_id, t.answer, t.refused, t.valid,
                      q.category, q.question, q.retrieved
               FROM teacher_answers t JOIN questions q ON q.question_id = t.question_id"""
        ).fetchall()
        for qid, answer, refused, was_valid, category, question, retrieved in rows:
            _ctx, labels, label_to_chunk, chunk_repos, mentions = _context_for(conn, retrieved)
            result = validation.validate(
                answer,
                refused,
                labels,
                category,
                chunk_repos=chunk_repos,
                question=question,
                label_mentions=mentions,
            )
            before += int(was_valid)
            after += int(result.valid)
            changed += int(bool(was_valid) != result.valid)
            for e in result.errors:
                errors[e.split(":")[0]] = errors.get(e.split(":")[0], 0) + 1
            conn.execute(
                """UPDATE teacher_answers
                   SET valid = %s, validation_errors = %s, citations = %s
                   WHERE question_id = %s""",
                (
                    result.valid,
                    json.dumps(result.errors),
                    json.dumps([label_to_chunk.get(c, c) for c in result.cited]),
                    qid,
                ),
            )
        conn.commit()
    return {
        "rows": len(rows),
        "valid_before": before,
        "valid_after": after,
        "verdict_changed": changed,
        "validation_errors": errors,
        "guard_rates": poisoning_rates(),
    }


POISON_CODES = ("fabricated_absent_side", "answered_absent")


def flagged_rows(codes: tuple[str, ...] = POISON_CODES, limit: int = 20) -> list[dict]:
    """Full text of poisoned answers, for eyeballing before a full batch.

    A teacher prompt problem costs 20 items to find here and 1,900 to find later,
    so the offending outputs need to be readable, not just counted.
    """
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT q.question_id, q.category, q.repo, q.question, q.absent_symbol,
                      q.retrieved, t.answer, t.refused, t.validation_errors
               FROM teacher_answers t JOIN questions q ON q.question_id = t.question_id
               WHERE t.valid = false ORDER BY q.category, q.question_id""",
        ).fetchall()
        out = []
        for r in rows:
            errs = r[8] or []
            if not any(e.split(":")[0] in codes for e in errs):
                continue
            repos = [
                x[0]
                for x in conn.execute(
                    "SELECT DISTINCT repo FROM chunks WHERE chunk_id = ANY(%s)", (r[5],)
                ).fetchall()
            ]
            out.append(
                {
                    "question_id": r[0],
                    "category": r[1],
                    "repo": r[2],
                    "question": r[3],
                    "absent_symbol": r[4],
                    "retrieved_repos": sorted(repos),
                    "answer": r[6],
                    "refused": r[7],
                    "errors": errs,
                }
            )
            if len(out) >= limit:
                break
        return out


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
