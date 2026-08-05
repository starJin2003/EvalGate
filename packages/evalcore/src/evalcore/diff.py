"""Case-level diff and the gate verdict.

The gate fails on the worst of overall and per-category. A model that gains on
three categories while the refusal category collapses shows a flat average and is
exactly the regression this project exists to catch, so a category breach fails
the run on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schema import CaseResult, RunResult, Suite, Threshold, Verdict


@dataclass(frozen=True)
class CaseDelta:
    case_id: str
    category: str
    baseline_score: float
    candidate_score: float
    baseline: CaseResult | None
    candidate: CaseResult | None

    @property
    def delta(self) -> float:
        return self.candidate_score - self.baseline_score

    @property
    def status(self) -> str:
        if self.baseline is None:
            return "added"
        if self.candidate is None:
            return "removed"
        if self.delta < -1e-9:
            return "regressed"
        if self.delta > 1e-9:
            return "improved"
        return "unchanged"


@dataclass(frozen=True)
class Breach:
    scope: str  # "overall" or a category name
    baseline: float
    candidate: float
    drop: float
    limit: float
    reason: str
    # "threshold" for a score drop, "zero_tolerance" for a case-count rule. The
    # two read very differently in a PR comment and conflating them is what let a
    # sub-quantum limit pass as a tolerance.
    kind: str = "threshold"
    regressed_cases: list[str] = field(default_factory=list)


class BackendMismatch(RuntimeError):
    """Raised when a diff is asked to compare runs from different backends.

    Loud and specific on purpose. A generic error here gets diagnosed as a broken
    gate; this one names both sides so the reader immediately sees the real
    problem is the comparison, not the harness.
    """


@dataclass
class Diff:
    suite_id: str
    baseline_run: str
    candidate_run: str
    baseline_score: float
    candidate_score: float
    cases: list[CaseDelta] = field(default_factory=list)
    breaches: list[Breach] = field(default_factory=list)

    @property
    def delta(self) -> float:
        return self.candidate_score - self.baseline_score

    @property
    def verdict(self) -> Verdict:
        return "fail" if self.breaches else "pass"

    def regressions(self) -> list[CaseDelta]:
        return sorted((c for c in self.cases if c.status == "regressed"), key=lambda c: c.delta)

    def improvements(self) -> list[CaseDelta]:
        return sorted((c for c in self.cases if c.status == "improved"), key=lambda c: -c.delta)

    def by_category(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for c in self.cases:
            b = out.setdefault(c.category, {"baseline": 0.0, "candidate": 0.0, "n": 0})
            b["baseline"] += c.baseline_score
            b["candidate"] += c.candidate_score
            b["n"] += 1
        for b in out.values():
            n = b.pop("n") or 1
            b["baseline"] /= n
            b["candidate"] /= n
            b["delta"] = b["candidate"] - b["baseline"]
        return dict(sorted(out.items()))


def _check(scope: str, baseline: float, candidate: float, t: Threshold) -> Breach | None:
    drop = baseline - candidate
    if drop > t.max_drop + 1e-9:
        return Breach(
            scope=scope,
            baseline=baseline,
            candidate=candidate,
            drop=drop,
            limit=t.max_drop,
            reason=f"dropped {drop:.3f}, limit {t.max_drop:.3f}",
        )
    if t.min_score is not None and candidate < t.min_score - 1e-9:
        return Breach(
            scope=scope,
            baseline=baseline,
            candidate=candidate,
            drop=drop,
            limit=t.min_score,
            reason=f"score {candidate:.3f} below floor {t.min_score:.3f}",
        )
    return None


def _assert_comparable(baseline: RunResult, candidate: RunResult) -> None:
    """Refuse a cross-backend or cross-build diff, naming both sides.

    Only fires when BOTH runs declare the field. A run recorded before provenance
    existed compares as before -- blocking those would make historical artifacts
    unreadable without making any comparison more correct.
    """
    for field_name, label in (("backend", "backend"), ("build_info", "llama.cpp build")):
        a, b = getattr(baseline, field_name), getattr(candidate, field_name)
        if a is not None and b is not None and a != b:
            raise BackendMismatch(
                f"refusing to compare runs from different {label}s:\n"
                f"  baseline  {baseline.run_id!r}\n"
                f"      {label}={a!r}  model_ref={baseline.model_ref!r}\n"
                f"  candidate {candidate.run_id!r}\n"
                f"      {label}={b!r}  model_ref={candidate.model_ref!r}\n"
                f"Measured 2026-08-04: identical weights across Metal and ggml CPU "
                f"move the suite 0.004272 and change 79 of 96 answers, which is the "
                f"same size as a real category regression. This is not a harness "
                f"fault — re-run one side on the other's {label}."
            )


def compare(suite: Suite, baseline: RunResult, candidate: RunResult) -> Diff:
    _assert_comparable(baseline, candidate)
    base_cases, cand_cases = baseline.by_case(), candidate.by_case()
    categories = {c.case_id: str(c.category) for c in suite.cases}

    deltas: list[CaseDelta] = []
    for case_id in sorted(set(base_cases) | set(cand_cases)):
        b = base_cases.get(case_id)
        c = cand_cases.get(case_id)
        deltas.append(
            CaseDelta(
                case_id=case_id,
                category=categories.get(case_id, str((b or c).category) if (b or c) else "unknown"),
                baseline_score=b.score if b else 0.0,
                candidate_score=c.score if c else 0.0,
                baseline=b,
                candidate=c,
            )
        )

    diff = Diff(
        suite_id=suite.suite_id,
        baseline_run=baseline.run_id,
        candidate_run=candidate.run_id,
        baseline_score=baseline.score,
        candidate_score=candidate.score,
        cases=deltas,
    )

    overall = _check("overall", diff.baseline_score, diff.candidate_score, suite.threshold)
    if overall:
        diff.breaches.append(overall)
    zero_tol = {
        c.value if hasattr(c, "value") else str(c): r for c, r in suite.zero_tolerance.items()
    }
    for category, scores in diff.by_category().items():
        if category in zero_tol:
            rule = zero_tol[category]
            regressed = sorted(
                c.case_id
                for c in diff.cases
                if c.category == category and c.candidate_score < c.baseline_score - 1e-9
            )
            if len(regressed) > rule.max_regressed_cases:
                diff.breaches.append(
                    Breach(
                        scope=category,
                        baseline=scores["baseline"],
                        candidate=scores["candidate"],
                        drop=scores["baseline"] - scores["candidate"],
                        limit=float(rule.max_regressed_cases),
                        kind="zero_tolerance",
                        regressed_cases=regressed,
                        reason=(
                            f"{len(regressed)} regressed case(s), "
                            f"limit {rule.max_regressed_cases} — "
                            f"{', '.join(regressed)}"
                        ),
                    )
                )
            continue
        t = suite.threshold_for(category)  # type: ignore[arg-type]
        breach = _check(category, scores["baseline"], scores["candidate"], t)
        if breach:
            diff.breaches.append(breach)
    return diff
