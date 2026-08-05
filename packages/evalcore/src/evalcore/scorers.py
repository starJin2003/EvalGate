"""Scorers.

Four are rule-based and deterministic; the fifth delegates to a judge model. The
rule-based ones carry the load: citation validity and refusal correctness are the
two behaviours being trained, and both are checkable without an LLM. A judge that
disagrees with a rule-based check is a judge bug, not a model finding.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .schema import Case, ScoreDetail, ScorerKind, ScorerSpec

MARKER_RE = re.compile(r"\[(C\d+)\]")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z`\"'(\[])")
CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
TRIVIAL_RE = re.compile(r"^[\s\W\d]*$")
# Citation markers written after the full stop belong to the sentence before it.
TRAILING_MARKER_RE = re.compile(r"([.!?])(\s*)((?:\[C\d+\])+)")

REFUSAL_RE = re.compile(
    r"\b("
    r"do(es)?\s+not\s+(appear|cover|mention|include|contain|say|describe|discuss)"
    r"|not\s+(covered|mentioned|included|present|provided|available|documented)"
    r"|no\s+(information|excerpt|documentation|details?|mention)"
    r"|cannot\s+(find|answer|confirm|verify)"
    r"|unable\s+to\s+(find|answer|confirm)"
    r"|i\s+refuse"
    r"|is\s+not\s+(a\s+)?(real|valid|documented)"
    r")\b",
    re.IGNORECASE,
)


def is_refusal(output: str) -> bool:
    """Did the model decline to answer?

    The single source for that question. `score_refusal` judges whether refusing
    was *correct*; this says only what the model did, which is a different thing
    and the one that can be counted per category.
    """
    return bool(REFUSAL_RE.search(output))


def sentences(text: str) -> list[str]:
    prose = TRAILING_MARKER_RE.sub(r"\3\1", text)
    prose = CODE_BLOCK_RE.sub(" ", prose)
    out: list[str] = []
    for line in prose.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("|", ">")):
            continue
        out.extend(s.strip() for s in SENTENCE_SPLIT_RE.split(stripped) if s.strip())
    return out


# --- individual scorers -------------------------------------------------------
def score_exact(spec: ScorerSpec, case: Case, output: str) -> ScoreDetail:
    expected = spec.expected or ""
    a, b = (output.strip(), expected.strip())
    if not spec.case_sensitive:
        a, b = a.lower(), b.lower()
    hit = a == b
    return ScoreDetail(
        kind=ScorerKind.exact,
        score=1.0 if hit else 0.0,
        weight=spec.weight,
        passed=hit,
        rationale="exact match" if hit else f"expected {expected!r}",
    )


def score_regex(spec: ScorerSpec, case: Case, output: str) -> ScoreDetail:
    found = bool(re.search(spec.pattern or "", output, re.IGNORECASE | re.MULTILINE))
    hit = found is spec.must_match
    return ScoreDetail(
        kind=ScorerKind.regex,
        score=1.0 if hit else 0.0,
        weight=spec.weight,
        passed=hit,
        rationale=(
            f"pattern {'found' if found else 'absent'}, "
            f"{'required' if spec.must_match else 'forbidden'}"
        ),
    )


def score_citation(spec: ScorerSpec, case: Case, output: str) -> ScoreDetail:
    """Fraction of factual sentences that carry a resolvable citation.

    Partial credit on purpose: "cited 7 of 9 sentences" is a more useful
    regression signal than a boolean, because it moves before it breaks.
    """
    available = set(case.labels)
    used = MARKER_RE.findall(output)
    unknown = sorted({m for m in used if m not in available})

    factual = [s for s in sentences(output) if not TRIVIAL_RE.match(s) and len(s) > 25]
    if not factual:
        # A refusal has nothing to cite; that is the refusal scorer's business.
        return ScoreDetail(
            kind=ScorerKind.citation,
            score=1.0,
            weight=spec.weight,
            passed=True,
            rationale="no factual sentences to cite",
        )

    cited = sum(1 for s in factual if MARKER_RE.search(s))
    score = cited / len(factual)
    if unknown:
        # Citing a chunk that was never supplied is worse than not citing at all.
        score = 0.0
    return ScoreDetail(
        kind=ScorerKind.citation,
        score=score,
        weight=spec.weight,
        passed=score == 1.0,
        rationale=(
            f"cited {cited}/{len(factual)} factual sentences"
            + (f"; unresolvable markers {','.join(unknown)}" if unknown else "")
        ),
    )


def score_refusal(spec: ScorerSpec, case: Case, output: str) -> ScoreDetail:
    """Adversarial cases must refuse. Everything else must not.

    Scored from the text rather than a structured flag, because at eval time the
    served model returns prose and the gate must work on what a user would see.
    """
    refused = is_refusal(output)
    should_refuse = case.category == "adversarial"
    hit = refused is should_refuse

    detail = "refused" if refused else "answered"
    want = "refusal required" if should_refuse else "answer required"
    rationale = f"{detail}, {want}"
    if should_refuse and not refused and case.absent_symbol:
        rationale += f"; answered about absent symbol {case.absent_symbol!r}"
    return ScoreDetail(
        kind=ScorerKind.refusal,
        score=1.0 if hit else 0.0,
        weight=spec.weight,
        passed=hit,
        rationale=rationale,
    )


RULE_SCORERS: dict[ScorerKind, Callable[[ScorerSpec, Case, str], ScoreDetail]] = {
    ScorerKind.exact: score_exact,
    ScorerKind.regex: score_regex,
    ScorerKind.citation: score_citation,
    ScorerKind.refusal: score_refusal,
}


def is_rule_based(kind: ScorerKind) -> bool:
    return kind in RULE_SCORERS


def score_rule_based(spec: ScorerSpec, case: Case, output: str) -> ScoreDetail:
    return RULE_SCORERS[spec.kind](spec, case, output)
