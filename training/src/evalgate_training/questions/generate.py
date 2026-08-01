"""Build and consume the question-generation batch.

Seed chunks are drawn deterministically (ordered by chunk_id) and spread evenly
across the four repos so Grafana's larger file count does not dominate the dataset.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .. import config, db, openai_batch
from ..budget import Ledger
from ..corpus.repos import REPOS
from . import prompts
from .allocate import allocate

JOB = "questions"
CHUNKS_PER_CALL = 3
# In a cross-repo call, this many chunks come from the primary repo and the
# remainder from the partner.
PRIMARY_CHUNKS = 2


@dataclass(frozen=True)
class CallSpec:
    category: str
    repo: str  # primary; the call's questions count against this repo's quota
    partner: str | None
    chunks: list[tuple[str, str, str, str]] = field(default_factory=list)
    alerting: bool = False

    @property
    def repos_used(self) -> list[str]:
        return sorted({c[1] for c in self.chunks})


def _question_id(category: str, repo: str, question: str) -> str:
    return hashlib.sha256(f"{category}|{repo}|{question.strip()}".encode()).hexdigest()[:16]


def _is_alerting(file_path: str, heading_path: str) -> bool:
    blob = f"{file_path} {heading_path}".lower()
    return any(hint in blob for hint in config.ALERTING_HINTS)


def _load_pools() -> tuple[dict[str, list], dict[str, list]]:
    """-> (all chunks by repo, alerting chunks by repo). Ordered by chunk_id so
    the plan is deterministic across runs."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT chunk_id, repo, heading_path, content, file_path
               FROM chunks ORDER BY chunk_id"""
        ).fetchall()
    by_repo: dict[str, list] = {}
    alerting: dict[str, list] = {}
    for chunk_id, repo, heading_path, content, file_path in rows:
        entry = (chunk_id, repo, heading_path, content)
        by_repo.setdefault(repo, []).append(entry)
        if _is_alerting(file_path, heading_path):
            alerting.setdefault(repo, []).append(entry)
    return by_repo, alerting


def _take(pool: list, start: int, count: int, stride: int) -> list:
    """Strided sample so one call spans the docs instead of three adjacent sections."""
    if not pool:
        return []
    return [pool[(start + j * stride) % len(pool)] for j in range(count)]


def _partner_for(primary: str, others: list[str], index: int) -> str | None:
    preferred = config.PREFERRED_PARTNERS.get(primary)
    if preferred and preferred in others:
        return preferred
    return others[index % len(others)] if others else None


def _calls_for_repo(
    category: str,
    primary: str,
    n_calls: int,
    by_repo: dict[str, list],
    alerting: dict[str, list],
    present: list[str],
    offset: int,
) -> list[CallSpec]:
    pool = by_repo[primary]
    others = [r for r in present if r != primary]
    stride = max(1, len(pool) // max(1, n_calls))
    cross = category in config.CROSS_REPO_CATEGORIES
    calls: list[CallSpec] = []

    for call_no in range(n_calls):
        i = call_no + offset
        use_cross = cross and (i % config.CROSS_REPO_DENOMINATOR < config.CROSS_REPO_NUMERATOR)
        if not use_cross:
            picked = _take(pool, i, CHUNKS_PER_CALL, stride)
            calls.append(CallSpec(category, primary, None, _dedupe(picked)))
            continue

        partner = _partner_for(primary, others, i)
        is_pair = config.PREFERRED_PARTNERS.get(primary) == partner
        # Inside the Prometheus/Grafana pairing, spend part of the calls on
        # alerting docs on both sides: that is the near-miss this corpus was
        # assembled to produce.
        # Modulo 10, not 100: per-repo call counts are ~20-30, so a modulo-100
        # window would never wrap and every call would qualify.
        want_alerting = (
            is_pair
            and alerting.get(primary)
            and alerting.get(partner)
            and (i % 10) < round(config.ALERTING_CALL_FRACTION * 10)
        )
        src_primary = alerting[primary] if want_alerting else pool
        src_partner = alerting[partner] if want_alerting else by_repo[partner]

        picked = _take(src_primary, i, PRIMARY_CHUNKS, max(1, len(src_primary) // 8))
        picked += _take(
            src_partner, i, CHUNKS_PER_CALL - PRIMARY_CHUNKS, max(1, len(src_partner) // 8)
        )
        calls.append(CallSpec(category, primary, partner, _dedupe(picked), bool(want_alerting)))
    return calls


def category_repo_quota(category: str) -> dict[str, int]:
    """The per-repo question target for one category.

    Derived from the corpus, so it is the same number the original allocation
    produced and a top-up can backfill against it rather than against an aggregate.
    """
    by_repo, _ = _load_pools()
    present = [spec.name for spec in REPOS if by_repo.get(spec.name)]
    weights = {name: float(len(by_repo[name])) for name in present}
    calls_needed = -(-config.CATEGORY_QUOTAS[category] // config.QUESTIONS_PER_CALL)
    per_repo = allocate(weights, calls_needed, config.REPO_MIN_SHARE, config.REPO_MAX_SHARE)
    return {repo: n * config.QUESTIONS_PER_CALL for repo, n in per_repo.items()}


def plan_calls(quotas: dict[str, int] | None = None, offset: int = 0) -> list[CallSpec]:
    """One CallSpec per batch request. Repo shares are clamped into
    [REPO_MIN_SHARE, REPO_MAX_SHARE] rather than following corpus proportions.
    """
    by_repo, alerting = _load_pools()
    present = [spec.name for spec in REPOS if by_repo.get(spec.name)]
    weights = {name: float(len(by_repo[name])) for name in present}

    calls: list[CallSpec] = []
    for category, quota in (quotas or config.CATEGORY_QUOTAS).items():
        if quota <= 0:
            continue
        calls_needed = -(-quota // config.QUESTIONS_PER_CALL)  # ceil
        per_repo = allocate(weights, calls_needed, config.REPO_MIN_SHARE, config.REPO_MAX_SHARE)
        for primary in present:
            calls.extend(
                _calls_for_repo(
                    category, primary, per_repo[primary], by_repo, alerting, present, offset
                )
            )
    return calls


def plan_topup_calls(gaps: dict[tuple[str, str], int], offset: int) -> list[CallSpec]:
    """Calls for specific (category, repo) deficits.

    Deletions are not uniform across repos -- collision probability scales with
    corpus density -- so backfilling in aggregate and re-applying the global split
    lets the category drift off the intended per-repo shares.
    """
    by_repo, alerting = _load_pools()
    present = [spec.name for spec in REPOS if by_repo.get(spec.name)]
    calls: list[CallSpec] = []
    for (category, repo), missing in sorted(gaps.items()):
        if missing <= 0 or repo not in by_repo:
            continue
        n_calls = -(-missing // config.QUESTIONS_PER_CALL)
        calls.extend(_calls_for_repo(category, repo, n_calls, by_repo, alerting, present, offset))
    return calls


def _dedupe(picked: list) -> list:
    seen: set[str] = set()
    return [c for c in picked if not (c[0] in seen or seen.add(c[0]))]


def plan_summary(calls: list[CallSpec]) -> dict:
    per_repo: dict[str, int] = {}
    per_category: dict[str, int] = {}
    cross = alerting_calls = 0
    pairs: dict[str, int] = {}
    for c in calls:
        q = config.QUESTIONS_PER_CALL
        per_repo[c.repo] = per_repo.get(c.repo, 0) + q
        per_category[c.category] = per_category.get(c.category, 0) + q
        if c.partner:
            cross += 1
            key = " + ".join(sorted({c.repo, c.partner}))
            pairs[key] = pairs.get(key, 0) + 1
        if c.alerting:
            alerting_calls += 1
    total = sum(per_repo.values())
    return {
        "calls": len(calls),
        "projected_questions": total,
        "questions_per_repo": dict(sorted(per_repo.items())),
        "repo_share_pct": {k: round(100 * v / total, 1) for k, v in sorted(per_repo.items())},
        "questions_per_category": dict(sorted(per_category.items())),
        "cross_repo_calls": cross,
        "cross_repo_pairs": dict(sorted(pairs.items(), key=lambda kv: -kv[1])),
        "prometheus_grafana_alerting_calls": alerting_calls,
        "bounds": {"min": config.REPO_MIN_SHARE, "max": config.REPO_MAX_SHARE},
    }


def build_requests(calls: list[CallSpec] | None = None) -> list[dict]:
    calls = plan_calls() if calls is None else calls
    requests = []
    for idx, call in enumerate(calls):
        context = "\n\n---\n\n".join(
            f"### [{repo}] {heading}\n\n{content}" for _, repo, heading, content in call.chunks
        )
        label = call.repo if not call.partner else f"{call.repo} and {call.partner}"
        messages = prompts.build_messages(call.category, label, context, config.QUESTIONS_PER_CALL)
        requests.append(
            openai_batch.chat_request(
                custom_id=f"q-{idx:05d}-{call.category}-{call.repo}",
                model=config.QUESTION_MODEL,
                messages=messages,
                schema=prompts.SCHEMA,
            )
        )
    return requests


def stored_counts() -> dict:
    """Actual rows in Postgres, per repo and per category. This is the number that
    matters after generation and the verify deletion pass, not the plan."""
    with db.connect() as conn:
        by_repo = dict(
            conn.execute("SELECT repo, count(*) FROM questions GROUP BY repo").fetchall()
        )
        by_cat = dict(
            conn.execute("SELECT category, count(*) FROM questions GROUP BY category").fetchall()
        )
        grid = conn.execute(
            "SELECT category, repo, count(*) FROM questions GROUP BY category, repo"
        ).fetchall()
    total = sum(by_repo.values())
    return {
        "total": total,
        "by_repo": dict(sorted(by_repo.items())),
        "repo_share_pct": {k: round(100 * v / total, 1) for k, v in sorted(by_repo.items())}
        if total
        else {},
        "by_category": dict(sorted(by_cat.items())),
        "quota_gap": {k: config.CATEGORY_QUOTAS[k] - by_cat.get(k, 0) for k in config.CATEGORIES},
        "by_category_repo": {f"{c}/{r}": n for c, r, n in sorted(grid)},
    }


def topup_gaps() -> dict[str, int]:
    """Shortfall per (category, repo) after generation and the verify deletion pass.

    Per-repo, not aggregate: deletions concentrate in dense repos, so an aggregate
    backfill re-applying the global split would leave the category off its intended
    shares. Over-requested by TOPUP_OVERSHOOT, since a top-up is itself subject to
    dedupe and the symbol-absence check.
    """
    grid = stored_counts()["by_category_repo"]
    gaps: dict[tuple[str, str], int] = {}
    for category in config.CATEGORIES:
        for repo, target in category_repo_quota(category).items():
            have = grid.get(f"{category}/{repo}", 0)
            if have < target:
                gaps[(category, repo)] = int((target - have) * topup_overshoot(category, repo))
    return gaps


def topup_overshoot(category: str, repo: str) -> float:
    """How far to over-request so a round nets the gap after deletions.

    Only adversarial rows are deleted by the symbol-absence check, and the rate
    varies by repo, so a global factor under-fills Grafana and over-fills
    Prometheus. Other categories only lose rows to dedupe, which is near zero.
    """
    if category != "adversarial":
        return config.TOPUP_OVERSHOOT
    rate = config.ADVERSARIAL_LEAK_RATE.get(repo)
    if rate is None or not 0.0 <= rate < 1.0:
        return config.TOPUP_OVERSHOOT
    return min(2.5, config.LEAK_SAFETY_MARGIN / (1.0 - rate))


def trim_to_targets(apply: bool = True) -> dict:
    """Drop the surplus from cells that overshot their per-repo target.

    A top-up that backfills in aggregate can leave a cell above target while
    another sits below, which is drift off the intended split even when the
    category total is right. Selection is deterministic by question_id, and rows
    that already carry a teacher answer are kept in preference to fresh ones.
    """
    grid = stored_counts()["by_category_repo"]
    dropped: dict[str, int] = {}
    doomed: list[str] = []

    with db.connect() as conn:
        for category in config.CATEGORIES:
            for repo, target in category_repo_quota(category).items():
                have = grid.get(f"{category}/{repo}", 0)
                if have <= target:
                    continue
                surplus = have - target
                rows = conn.execute(
                    """SELECT q.question_id FROM questions q
                       LEFT JOIN teacher_answers t ON t.question_id = q.question_id
                       WHERE q.category = %s AND q.repo = %s
                       ORDER BY (t.question_id IS NOT NULL) ASC, q.question_id DESC
                       LIMIT %s""",
                    (category, repo, surplus),
                ).fetchall()
                ids = [r[0] for r in rows]
                doomed.extend(ids)
                dropped[f"{category}/{repo}"] = len(ids)
        if apply and doomed:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM questions WHERE question_id = ANY(%s)", (doomed,))
            conn.commit()

    return {"trimmed": dropped, "total_dropped": len(doomed), "applied": apply}


def submit_topup(ledger: Ledger, round_no: int) -> tuple[str, dict[str, int]]:
    gaps = topup_gaps()
    if not gaps:
        raise RuntimeError("no (category, repo) cell is short; nothing to top up")
    # Offset sampling by a prime multiple of the round so the top-up draws
    # different chunks than the round it is repairing.
    calls = plan_topup_calls(gaps, offset=17 * round_no)
    requests = build_requests(calls)
    est_in = sum(len(json.dumps(r["body"]["messages"])) // 4 for r in requests)
    est_out = len(requests) * config.QUESTIONS_PER_CALL * 140
    projected = config.cost_usd(config.QUESTION_MODEL, est_in, est_out, batch=True)
    ledger.reserve(f"questions.topup{round_no}", projected, est_in + est_out)
    readable = {f"{c}/{r}": n for (c, r), n in sorted(gaps.items())}
    print(f"top-up round {round_no}: gaps {readable}, {len(requests)} calls, ${projected:.4f}")
    job = f"{JOB}_topup{round_no}"
    return openai_batch.submit(job, requests, config.SCRATCH_DIR / "batch"), readable


def submit(ledger: Ledger) -> str:
    requests = build_requests()
    # Rough projection from the actual prompt sizes; output assumed ~140 tokens/question.
    est_in = sum(len(json.dumps(r["body"]["messages"])) // 4 for r in requests)
    est_out = len(requests) * config.QUESTIONS_PER_CALL * 140
    projected = config.cost_usd(config.QUESTION_MODEL, est_in, est_out, batch=True)
    ledger.reserve("questions.generate", projected, est_in + est_out)
    print(
        f"{len(requests)} calls, ~{est_in:,} in / ~{est_out:,} out tokens, "
        f"projected ${projected:.4f} at batch rates"
    )
    return openai_batch.submit(JOB, requests, config.SCRATCH_DIR / "batch")


def collect(ledger: Ledger, job: str = JOB) -> dict[str, int]:
    results = openai_batch.fetch(job)
    rows: list[tuple] = []
    errors = 0
    prompt_tokens = completion_tokens = 0

    for line in results:
        custom_id, parsed, ptok, ctok, err = openai_batch.parse_result(line)
        prompt_tokens += ptok
        completion_tokens += ctok
        if err or parsed is None:
            errors += 1
            continue
        _, _, category, repo = custom_id.split("-", 3)
        for item in parsed.get("questions", []):
            question = item.get("question", "").strip()
            if not question:
                continue
            symbol = (item.get("absent_symbol") or "").strip()
            rows.append(
                (
                    _question_id(category, repo, question),
                    category,
                    repo,
                    question,
                    json.dumps([]),
                    symbol if category == "adversarial" else None,
                )
            )

    ledger.record(
        f"questions.{job}",
        config.QUESTION_MODEL,
        prompt_tokens,
        completion_tokens,
        batch=True,
        note=f"{len(rows)} questions, {errors} failed calls",
    )

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO questions
                   (question_id, category, repo, question, seed_chunk_ids, absent_symbol)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (question_id) DO NOTHING""",
                rows,
            )
        conn.commit()

    return {"parsed": len(rows), "failed_calls": errors}
