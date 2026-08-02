"""LLM-as-judge client.

Provider-abstracted so the harness is testable without a network, and cached on
(case, output, rubric, judge version) so a rerun of an unchanged case costs zero
quota. The cache key deliberately includes the judge version: changing the judge
must invalidate old verdicts rather than silently mixing them.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import Case, ScoreDetail, ScorerKind, ScorerSpec

DEFAULT_RUBRIC = (
    "Score how well the answer is supported by the supplied excerpts. "
    "Full marks require every factual claim to be traceable to an excerpt. "
    "Penalise claims that go beyond the excerpts even if they are true in general."
)


@dataclass(frozen=True)
class JudgeVerdict:
    score: float
    rationale: str
    cached: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0


class JudgeProvider(ABC):
    """Implemented per vendor. The harness never imports a vendor SDK directly."""

    name: str = "abstract"
    version: str = "0"

    @abstractmethod
    def judge(self, prompt: str) -> tuple[float, str, int, int]:
        """-> (score in [0,1], rationale, prompt_tokens, completion_tokens)."""


class StubJudge(JudgeProvider):
    """Deterministic judge for tests and for running the harness with no key.

    Scores by citation density, which correlates with what the real rubric asks
    about, so fixture-driven tests exercise realistic score movement.
    """

    name = "stub"
    version = "1"

    def judge(self, prompt: str) -> tuple[float, str, int, int]:
        markers = prompt.count("[C")
        score = min(1.0, markers / 6.0)
        return score, f"stub: {markers} citation markers in prompt", 0, 0


class JudgeCache:
    """SQLite because it needs no service and survives across runs and CI jobs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS verdicts (
                   key TEXT PRIMARY KEY,
                   score REAL NOT NULL,
                   rationale TEXT NOT NULL,
                   created_at REAL NOT NULL
               )"""
        )
        self._conn.commit()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(case_id: str, output: str, rubric: str, judge_name: str, judge_version: str) -> str:
        blob = json.dumps([case_id, output, rubric, judge_name, judge_version], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, key: str) -> tuple[float, str] | None:
        row = self._conn.execute(
            "SELECT score, rationale FROM verdicts WHERE key = ?", (key,)
        ).fetchone()
        if row:
            self.hits += 1
            return float(row[0]), str(row[1])
        self.misses += 1
        return None

    def put(self, key: str, score: float, rationale: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO verdicts VALUES (?,?,?,?)",
            (key, score, rationale, time.time()),
        )
        self._conn.commit()

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else None,
        }

    def close(self) -> None:
        self._conn.close()


class RateLimited(RuntimeError):
    """Raised by a provider so the client can back off rather than fail the run."""


class JudgeClient:
    def __init__(
        self,
        provider: JudgeProvider,
        cache: JudgeCache | None = None,
        max_retries: int = 5,
        base_delay: float = 1.0,
        sleep=time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._sleep = sleep
        self._rng = rng or random.Random(0)

    def build_prompt(self, case: Case, output: str, rubric: str) -> str:
        context = "\n\n".join(f"[{c.label}] {c.heading_path}\n{c.content}" for c in case.context)
        return (
            f"{rubric}\n\nExcerpts:\n{context}\n\n"
            f"Question: {case.question}\n\nAnswer under review:\n{output}\n\n"
            "Reply with a score from 0 to 1 and a one-sentence rationale."
        )

    def evaluate(self, case: Case, output: str, spec: ScorerSpec) -> JudgeVerdict:
        rubric = spec.rubric or DEFAULT_RUBRIC
        key = None
        if self.cache is not None:
            key = JudgeCache.key(
                case.case_id, output, rubric, self.provider.name, self.provider.version
            )
            cached = self.cache.get(key)
            if cached is not None:
                return JudgeVerdict(cached[0], cached[1], cached=True)

        prompt = self.build_prompt(case, output, rubric)
        score, rationale, ptok, ctok = self._call_with_backoff(prompt)
        score = max(0.0, min(1.0, score))
        if self.cache is not None and key is not None:
            self.cache.put(key, score, rationale)
        return JudgeVerdict(score, rationale, False, ptok, ctok)

    def _call_with_backoff(self, prompt: str) -> tuple[float, str, int, int]:
        for attempt in range(self.max_retries):
            try:
                return self.provider.judge(prompt)
            except RateLimited:
                if attempt == self.max_retries - 1:
                    raise
                # Full jitter: synchronised retries across a batch of cases are
                # what turn one 429 into a sustained one.
                delay = self._rng.uniform(0, self.base_delay * (2**attempt))
                self._sleep(delay)
        raise RateLimited("exhausted retries")

    def score(self, case: Case, output: str, spec: ScorerSpec) -> ScoreDetail:
        verdict = self.evaluate(case, output, spec)
        return ScoreDetail(
            kind=ScorerKind.judge,
            score=verdict.score,
            weight=spec.weight,
            passed=verdict.score >= 0.7,
            rationale=verdict.rationale + (" (cached)" if verdict.cached else ""),
        )
