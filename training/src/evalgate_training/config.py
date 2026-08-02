"""Paths, model choices, and pricing. Single source of truth for the P1.1 pipeline."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]
TRAINING_ROOT = REPO_ROOT / "training"

# Vendored docs land here. Gitignored; never committed.
SCRATCH_DIR = TRAINING_ROOT / ".scratch"
# Committed outputs: chunk manifest, dataset, golden set, spend ledger.
ARTIFACTS_DIR = TRAINING_ROOT / "artifacts"

CHUNK_MANIFEST = ARTIFACTS_DIR / "chunk_manifest.jsonl"
QUESTIONS_FILE = ARTIFACTS_DIR / "questions.jsonl"
DATASET_FILE = ARTIFACTS_DIR / "dataset.jsonl"
GOLDEN_IDS_FILE = ARTIFACTS_DIR / "golden_ids.json"
GOLDEN_JSONL = ARTIFACTS_DIR / "golden_set.jsonl"
# Hand-review artifacts. The manifest is written by `golden select` before any
# review happens, so the sample is fixed in advance and cannot be reshaped after
# the verdicts start landing. The JSONL is append-only; the summary is derived.
GOLDEN_MANIFEST_FILE = ARTIFACTS_DIR / "golden_manifest.json"
GOLDEN_REVIEW_JSONL = ARTIFACTS_DIR / "golden_review.jsonl"
GOLDEN_REVIEW_SUMMARY = ARTIFACTS_DIR / "golden_review_summary.json"
LEDGER_FILE = ARTIFACTS_DIR / "ledger.json"
DRY_RUN_REPORT = ARTIFACTS_DIR / "dry_run_report.json"
BATCH_STATE_FILE = ARTIFACTS_DIR / "batch_state.json"

# --- Models -----------------------------------------------------------------
# Teacher and judge must differ so the judge never scores its own writing style.
# The judge is selected in P1.4 from JUDGE_CANDIDATES.
TEACHER_MODEL = "gpt-5-mini"
QUESTION_MODEL = "gpt-4.1-mini"
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536
JUDGE_CANDIDATES = ("gpt-5.4-mini", "gpt-4.1")

# --- Pricing ----------------------------------------------------------------
# USD per 1M tokens as (input, output). Verified against PRICING_SOURCE on
# PRICING_CHECKED. Printed on every priced run so a stale table is never silent.
PRICING_CHECKED = "2026-07-31"
PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
PRICES: dict[str, tuple[float, float]] = {
    "gpt-5-mini": (0.25, 2.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-4.1": (2.00, 8.00),
    "text-embedding-3-small": (0.02, 0.00),
}
BATCH_DISCOUNT = 0.5

# --- Corpus and dataset shape ------------------------------------------------
# Raised from 4 to 8 on 2026-08-01 by the pre-committed rule below: comparison
# questions spanned 2+ repos in only 24.0% of cases at k=4, against a 50% threshold.
# Applies to training and eval alike; P1.4 must retrieve at this same k.
RETRIEVAL_K = 8
MIN_CHUNK_PROSE_CHARS = 200
MAX_CHUNK_TOKENS = 900

# Pre-committed decision rule, agreed 2026-07-31 before the numbers were known, so
# the choice is not made after seeing the result.
#
#   If fewer than COMPARISON_MULTI_REPO_THRESHOLD of comparison questions retrieve
#   chunks spanning 2+ repos, raise k globally to RETRIEVAL_K_RAISED -- the same
#   value for every category and for eval -- and re-measure exactly once. At or
#   above the threshold, keep k=4 and rely on the one-sided comparison guard in
#   teacher/validate.py. k is never raised for a single category: training and eval
#   must see identical retrieval.
COMPARISON_MULTI_REPO_THRESHOLD = 50.0  # percent
RETRIEVAL_K_RAISED = 8


def decide_k(comparison_multi_repo_pct: float | None) -> tuple[int, str]:
    """-> (k, reason). Applies the pre-committed rule above."""
    if comparison_multi_repo_pct is None:
        return RETRIEVAL_K, "no comparison rows measured yet; k unchanged"
    if comparison_multi_repo_pct < COMPARISON_MULTI_REPO_THRESHOLD:
        return (
            RETRIEVAL_K_RAISED,
            f"comparison multi-repo span {comparison_multi_repo_pct}% is below "
            f"{COMPARISON_MULTI_REPO_THRESHOLD}%, so k rises to {RETRIEVAL_K_RAISED} "
            f"globally and is re-measured once",
        )
    return (
        RETRIEVAL_K,
        f"comparison multi-repo span {comparison_multi_repo_pct}% is at or above "
        f"{COMPARISON_MULTI_REPO_THRESHOLD}%, so k stays {RETRIEVAL_K}",
    )


CATEGORIES = ("factual", "howto", "comparison", "adversarial")
CATEGORY_QUOTAS = {"factual": 600, "howto": 500, "comparison": 400, "adversarial": 400}
QUESTIONS_PER_CALL = 5

# Corpus is 68% Grafana and 6% Pydantic. Proportional sampling would train a
# Grafana model, so per-repo question share is clamped into this band.
REPO_MIN_SHARE = 0.15
REPO_MAX_SHARE = 0.35

# Categories that benefit from context spanning two projects.
CROSS_REPO_CATEGORIES = ("comparison", "adversarial")
# 3 of every 5 calls in those categories draw a cross-repo pair; the rest stay
# within one repo so single-project comparisons still exist.
CROSS_REPO_NUMERATOR = 3
CROSS_REPO_DENOMINATOR = 5

# Prometheus and Grafana alerting is the confusable pair Grafana was chosen for.
# These repos are partnered with each other whenever either is the primary.
PREFERRED_PARTNERS = {"prometheus": "grafana", "grafana": "prometheus"}
# Within that pairing, this fraction of calls draws from alerting docs on both
# sides, which is where the near-miss questions come from.
ALERTING_CALL_FRACTION = 0.5
ALERTING_HINTS = ("alert",)

# Top-up rounds over-request, because a top-up is itself subject to dedupe and the
# adversarial symbol-absence deletion. Measured 2026-07-31: 104 of 400 adversarial
# questions named a symbol that actually exists, a 26% loss, so netting N needs
# N/0.74 = 1.35N. 1.40 leaves headroom; overshoot is trimmed by the quota, undershoot
# costs another round trip through a 24-hour batch window.
TOPUP_OVERSHOOT = 1.40

# Per-repo adversarial symbol-collision rates, measured 2026-07-31 via
# `questions leak-report` over 545 generated questions. A single global overshoot
# under-fills dense repos and over-fills sparse ones, so the top-up uses these.
#
# Grafana leaks most, matching its 4,210-chunk corpus. Pydantic is second despite
# the smallest corpus (382 chunks) because its API naming is extremely regular --
# model_*, field_*, *_validator -- so a plausible invented name lands on a real
# symbol far more often than corpus density alone predicts. Density is one driver;
# naming-convention regularity is another.
ADVERSARIAL_LEAK_RATE = {
    "grafana": 0.342,
    "pydantic": 0.275,
    "fastapi": 0.206,
    "prometheus": 0.190,
}
# Safety margin on top of 1/(1-leak) so a round rarely lands short twice.
LEAK_SAFETY_MARGIN = 1.10

# Hard cap on teacher regeneration per row. A systematically hard case class -- a
# comparison the corpus genuinely cannot support, an adversarial symbol the model
# keeps answering -- would otherwise loop and burn budget forever. After this many
# attempts the row is dropped rather than retried, and the drop count is reported.
MAX_TEACHER_ATTEMPTS = 2

# Golden sample shape. 6 per (category, repo) cell x 16 cells = 96 cases, inside
# the 80-100 band BUILD_PLAN asks for. Balanced per cell rather than per category:
# a category-level quota can silently skew toward the repos with the most eligible
# rows, and repo is exactly the axis the corpus is most lopsided on.
GOLDEN_PER_CELL = 6
GOLDEN_PER_CATEGORY = GOLDEN_PER_CELL * 4  # 24

# Fixed seed for golden selection. Selection uses sha256(seed|question_id) as a
# sort key, not an RNG, so the sample is reproducible from this string alone and
# is stable no matter what order the database returns rows in. Changing it
# reshuffles the entire sample and invalidates any review already recorded.
GOLDEN_SEED = "evalgate-golden-v1"

# Hand-review criteria, in the order they are presented and recorded. Order is
# part of the protocol: completeness is judged first because a half-answer that
# is perfectly grounded and perfectly cited still fails, and reviewing citations
# first anchors the reviewer on the half that was written.
REVIEW_CRITERIA: dict[int, str] = {
    1: "completeness",
    2: "groundedness",
    3: "refusal validity",
    4: "citation accuracy",
}
REVIEW_HOST = "127.0.0.1"  # never 0.0.0.0; this UI has no auth
REVIEW_PORT = 8765

DRY_RUN_SIZE = 20

# The Batch API caps *enqueued* tokens per model per org. Measured 2026-08-01:
# gpt-5-mini is 5,000,000, and the full 1,880-request teacher run is ~5.3M input
# alone, so it failed outright with token_limit_exceeded and processed nothing.
# The cap counts in-flight work, so shards must complete before the next is sent.
# Budget covers input plus expected output, with margin, since it is unclear
# whether the cap counts reserved completions.
ENQUEUED_TOKEN_LIMIT = 5_000_000
SHARD_TOKEN_BUDGET = 3_500_000

# Reasoning effort for the teacher. Left at the model default after a 20-item A/B
# on identical inputs, 2026-08-01.
#
# "low" costs 454 output tokens against 1,209 and would have halved the full run to
# $1.44, and on aggregate metrics it looked equal or better: 20/20 valid vs 19/20,
# identical refusal counts, both guards clean. Reading the prose reversed that. On
# comparison questions low truncates one side: asked for the trade-offs between
# async and sync FastAPI tests it answered only the async half in 30 words against
# 150, never mentioning synchronous testing, while citing the very chunk that
# carried that side. Decline language appeared in 3 of 10 comparison rows against 1.
#
# The model is being distilled to do grounded comparison. Training it on answers
# that address half a two-sided question teaches the failure directly.
TEACHER_REASONING_EFFORT: str | None = None


def load_env() -> None:
    """Load .env from the repo root. Secrets never come from anywhere else."""
    load_dotenv(REPO_ROOT / ".env")


def openai_api_key() -> str:
    load_env()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in. "
            "See .env.example for the full list."
        )
    return key


def database_url() -> str:
    load_env()
    return os.environ.get("DATABASE_URL", "postgresql://evalgate:evalgate@localhost:5432/evalgate")


def usd_ceiling() -> float:
    load_env()
    return float(os.environ.get("OPENAI_USD_CEILING", "5.00"))


def token_ceiling() -> int:
    load_env()
    return int(os.environ.get("OPENAI_TOKEN_CEILING", "30000000"))


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int, batch: bool) -> float:
    """Cost for one call. Raises on an unknown model rather than guessing zero."""
    if model not in PRICES:
        raise KeyError(f"No price for {model!r}. Add it to PRICES after checking {PRICING_SOURCE}.")
    in_rate, out_rate = PRICES[model]
    multiplier = BATCH_DISCOUNT if batch else 1.0
    return multiplier * (
        prompt_tokens * in_rate / 1_000_000 + completion_tokens * out_rate / 1_000_000
    )
