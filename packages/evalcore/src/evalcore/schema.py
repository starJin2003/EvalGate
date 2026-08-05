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

    A threshold is only meaningful ABOVE the measurement quantum. This suite has
    24 cases per category, so the smallest possible category move is 1/24 =
    0.0417: any `max_drop` below that is not a tolerance at all, it is "one case
    blocks", written in a form that hides the fact. `ZeroTolerance` exists so that
    intent can be stated instead of encoded as a small float -- see its docstring.
    """

    max_drop: float = 0.02
    min_score: float | None = None
    model_config = {"extra": "forbid"}


class ZeroTolerance(BaseModel):
    """Any regressed case in this category blocks. A COUNT, not a score.

    Why this is not `Threshold(max_drop=0.01)`, which is what it replaced:
    adversarial has 24 cases, so one case is 0.0417. A 0.01 limit is 4.2x FINER
    than the smallest move the metric can make -- so it could never mean "tolerate
    a small drop", it could only ever mean "tolerate nothing". It was read as the
    former for weeks, including by the person who set it, and the v1->v2 result
    turned on exactly that: one adversarial case breaching a limit that looked
    like a 1% allowance.

    Stated as a case count, the rule is unambiguous and cannot silently become
    sub-quantum if the suite size changes: 24 cases or 240, `max_regressed_cases:
    0` means the same thing.

    Used for the refusal category because refusal discipline is the safety
    property here -- a model that answers about an API it was told does not exist
    is wrong in a way no aggregate should be allowed to average away.
    """

    max_regressed_cases: int = 0
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
    # Categories governed by a case-count rule instead of a score threshold. A
    # category must not appear in both; the validator below enforces that, because
    # two rules over one category is exactly how an intent gets lost.
    zero_tolerance: dict[Category, ZeroTolerance] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check(self) -> Suite:
        ids = [c.case_id for c in self.cases]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate case ids in {self.suite_id}: {sorted(dupes)}")
        both = set(self.category_thresholds) & set(self.zero_tolerance)
        if both:
            raise ValueError(
                f"{sorted(c.value for c in both)} have BOTH a score threshold and a "
                "zero-tolerance rule. Pick one: a category governed by two rules is how "
                "the intent behind either stops being readable."
            )
        # A per-category threshold below the quantum is the defect ZeroTolerance
        # exists to prevent, so it is rejected rather than silently honoured.
        per_cat = {}
        for case in self.cases:
            per_cat[case.category] = per_cat.get(case.category, 0) + 1
        for category, t in self.category_thresholds.items():
            n = per_cat.get(category, 0)
            if n and 0 < t.max_drop < 1 / n:
                raise ValueError(
                    f"{category.value}: max_drop {t.max_drop} is below the quantum "
                    f"1/{n} = {1 / n:.4f}, so it cannot mean 'tolerate a small drop' — "
                    "it can only mean 'tolerate nothing'. Use ZeroTolerance and say so."
                )
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
    # --- provenance -----------------------------------------------------------
    # Measured 2026-08-04: identical weights scored on Metal and on ggml CPU give
    # a 0.004272 suite delta and 79 of 96 different answers, because greedy
    # decoding forks on near-tied logits. That is the same order as several real
    # v1->v2 category deltas, so a diff assembled across backends is not a
    # measurement of the models. `compare()` refuses it.
    #
    # `backend` is DECLARED by the caller, because nothing the server exposes
    # names it reliably. `build_info` is OBSERVED from llama-server's /props, so a
    # llama.cpp version change is caught even if someone declares the backend
    # wrongly. Both default to None so runs recorded before this existed still
    # load; comparisons involving them are allowed and warned about, not blocked,
    # since refusing to read historical artifacts helps nobody.
    backend: str | None = None
    quantization: str | None = None
    build_info: str | None = None
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
