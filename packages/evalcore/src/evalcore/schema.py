"""Suite and case schema.

A suite is a versioned set of cases plus the thresholds that decide whether a run
passes. Everything the gate needs to make a merge decision is declared here, so
the decision is reviewable in a diff rather than buried in CI config.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Category(StrEnum):
    factual = "factual"
    howto = "howto"
    comparison = "comparison"
    adversarial = "adversarial"


class ScorerKind(StrEnum):
    exact = "exact"
    regex = "regex"
    citation = "citation"
    refusal = "refusal"
    judge = "judge"


class ScorerSpec(BaseModel):
    """One scorer applied to a case. `weight` is relative within the case."""

    kind: ScorerKind
    weight: float = 1.0
    # exact
    expected: str | None = None
    case_sensitive: bool = False
    # regex
    pattern: str | None = None
    must_match: bool = True
    # judge
    rubric: str | None = None
    # citation / refusal take their configuration from the case itself
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check_required(self) -> ScorerSpec:
        if self.kind is ScorerKind.exact and self.expected is None:
            raise ValueError("exact scorer requires `expected`")
        if self.kind is ScorerKind.regex and self.pattern is None:
            raise ValueError("regex scorer requires `pattern`")
        if self.weight <= 0:
            raise ValueError("weight must be positive")
        return self


class ContextChunk(BaseModel):
    """A retrieved chunk as the model sees it. `label` is what citations refer to."""

    label: str
    chunk_id: str
    repo: str
    heading_path: str = ""
    source_url: str = ""
    content: str = ""


class Case(BaseModel):
    case_id: str
    category: Category
    question: str
    context: list[ContextChunk] = Field(default_factory=list)
    scorers: list[ScorerSpec] = Field(default_factory=list)
    # Adversarial cases carry the symbol that must not be answered about.
    absent_symbol: str | None = None
    # Hand-reviewed reference answer. Optional: rule-based scorers do not need it.
    reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}

    @property
    def labels(self) -> list[str]:
        return [c.label for c in self.context]

    @model_validator(mode="after")
    def _check(self) -> Case:
        if not self.scorers:
            raise ValueError(f"case {self.case_id} has no scorers")
        seen = {c.label for c in self.context}
        if len(seen) != len(self.context):
            raise ValueError(f"case {self.case_id} has duplicate context labels")
        return self


class Threshold(BaseModel):
    """How much a score may drop before the gate fails.

    Absolute, not relative: a 5% relative drop means something different at 0.9
    than at 0.4, and the gate comment has to be explainable in a PR review.
    """

    max_drop: float = 0.02
    min_score: float | None = None
    model_config = {"extra": "forbid"}


class Suite(BaseModel):
    suite_id: str
    version: str = "1"
    description: str = ""
    cases: list[Case]
    # Overall threshold plus optional per-category overrides. A category that
    # collapses while the overall average holds is the exact regression this
    # project exists to catch, so per-category thresholds are first class.
    threshold: Threshold = Field(default_factory=Threshold)
    category_thresholds: dict[Category, Threshold] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check(self) -> Suite:
        ids = [c.case_id for c in self.cases]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate case ids in {self.suite_id}: {sorted(dupes)}")
        return self

    def threshold_for(self, category: Category | None) -> Threshold:
        if category is not None and category in self.category_thresholds:
            return self.category_thresholds[category]
        return self.threshold

    def by_category(self) -> dict[Category, list[Case]]:
        out: dict[Category, list[Case]] = {}
        for case in self.cases:
            out.setdefault(case.category, []).append(case)
        return out


# --- results ------------------------------------------------------------------
class ScoreDetail(BaseModel):
    kind: ScorerKind
    score: float
    weight: float
    passed: bool
    rationale: str = ""
    model_config = {"extra": "forbid"}


class CaseResult(BaseModel):
    case_id: str
    category: Category
    output: str
    refused: bool = False
    scores: list[ScoreDetail] = Field(default_factory=list)
    error: str | None = None
    latency_ms: int = 0
    model_config = {"extra": "forbid"}

    @property
    def score(self) -> float:
        """Weighted mean across scorers. An errored case scores 0 rather than
        being skipped, so a crashing model cannot improve a suite average."""
        if self.error:
            return 0.0
        total = sum(s.weight for s in self.scores)
        if not total:
            return 0.0
        return sum(s.score * s.weight for s in self.scores) / total


class RunResult(BaseModel):
    run_id: str
    suite_id: str
    suite_version: str
    model_ref: str
    results: list[CaseResult]
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}

    @property
    def score(self) -> float:
        return sum(r.score for r in self.results) / len(self.results) if self.results else 0.0

    def category_scores(self) -> dict[str, float]:
        buckets: dict[str, list[float]] = {}
        for r in self.results:
            buckets.setdefault(str(r.category), []).append(r.score)
        return {c: sum(v) / len(v) for c, v in sorted(buckets.items())}

    def by_case(self) -> dict[str, CaseResult]:
        return {r.case_id: r for r in self.results}


Verdict = Literal["pass", "fail"]
