"""Validate teacher answers before they can become training data.

Checks, in order of how badly they corrupt the dataset:
  cite_unknown           a marker points at an excerpt that was never supplied
  uncited_sentence       a factual sentence carries no citation
  answered_absent        an adversarial case produced an answer instead of a refusal
  fabricated_absent_side a one-sided comparison described the side that was not
                         retrieved, i.e. the teacher filled the gap from pretraining
  refused_present        a normal case refused (usually a retrieval miss, not a bug)

The last two are the dataset-poisoning cases. A model distilled on a fabricated
comparison learns to invent the missing half; a model distilled on an adversarial
row that was confidently answered learns not to refuse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..corpus.repos import REPO_ALIASES

MARKER_RE = re.compile(r"\[(C\d+)\]")
# Sentence split that does not fire on "e.g." / "v1.2" / decimals.
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z`\"'(\[])")
CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
# A sentence with no letters, or only a list bullet, carries no factual claim.
TRIVIAL_RE = re.compile(r"^[\s\W\d]*$")

# A sentence asserting that something is missing is allowed to name the absent
# project; that is exactly the behaviour rule 5 of the system prompt asks for.
DISCLAIMER_RE = re.compile(
    r"\b("
    r"do(es)?\s+not\s+(appear|cover|mention|include|contain|say|describe|discuss)"
    r"|not\s+(covered|mentioned|included|present|provided|available|documented)"
    r"|no\s+(information|excerpt|documentation|details?|mention)"
    r"|nothing\s+(about|on)"
    r"|isn't|aren't|don't|doesn't|cannot|can't"
    r"|only\s+(covers?|includes?|describes?|contains?)"
    r"|without\s+(information|details)"
    r"|outside\s+the\s+(scope|excerpts|documentation)"
    r"|unable\s+to"
    r")\b",
    re.IGNORECASE,
)

_ALIAS_RE = {
    repo: re.compile(r"\b(" + "|".join(re.escape(a) for a in aliases) + r")\b", re.IGNORECASE)
    for repo, aliases in REPO_ALIASES.items()
}


@dataclass
class Validation:
    valid: bool
    errors: list[str] = field(default_factory=list)
    cited: list[str] = field(default_factory=list)


# "…retrieve all alert rules. [C1][C5]" puts the markers after the full stop, so a
# naive split orphans them onto the next sentence and the cited one reads as uncited.
# Pull trailing markers back inside the sentence they belong to.
TRAILING_MARKER_RE = re.compile(r"([.!?])(\s*)((?:\[C\d+\])+)")


def sentences(answer: str) -> list[str]:
    prose = TRAILING_MARKER_RE.sub(r"\3\1", answer)
    prose = CODE_BLOCK_RE.sub(" ", prose)
    out: list[str] = []
    for line in prose.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("|", ">")):
            continue
        out.extend(s.strip() for s in SENTENCE_SPLIT_RE.split(stripped) if s.strip())
    return out


def repos_named(text: str) -> set[str]:
    return {repo for repo, pattern in _ALIAS_RE.items() if pattern.search(text)}


def is_disclaimer(sentence: str) -> bool:
    return bool(DISCLAIMER_RE.search(sentence))


def fabricated_sides(
    answer: str,
    absent_repos: set[str],
    label_mentions: dict[str, set[str]] | None = None,
) -> set[str]:
    """Absent projects asserted **without grounding**.

    "Named in the question but missing from retrieval" is not sufficient: FastAPI's
    docs discuss Pydantic constantly, and a Grafana chunk can state that Grafana
    Alerting is built on the Prometheus model. Those claims are cited to real
    retrieved text and are not fabrication.

    A sentence is only fabrication when it names an absent project and *no chunk it
    cites mentions that project*. That is the case where the assertion can only have
    come from pretraining.
    """
    mentions = label_mentions or {}
    found: set[str] = set()
    for sentence in sentences(answer):
        if TRIVIAL_RE.match(sentence) or is_disclaimer(sentence):
            continue
        named = repos_named(sentence) & absent_repos
        if not named:
            continue
        grounded: set[str] = set()
        for marker in MARKER_RE.findall(sentence):
            grounded |= mentions.get(marker, set())
        found |= named - grounded
    return found


def validate(
    answer: str,
    refused: bool,
    available_labels: list[str],
    category: str,
    *,
    chunk_repos: list[str] | None = None,
    question: str = "",
    label_mentions: dict[str, set[str]] | None = None,
) -> Validation:
    errors: list[str] = []
    used = MARKER_RE.findall(answer)
    available = set(available_labels)

    unknown = sorted({m for m in used if m not in available})
    if unknown:
        errors.append(f"cite_unknown:{','.join(unknown)}")

    if category == "adversarial" and not refused:
        errors.append("answered_absent")
    if category != "adversarial" and refused:
        errors.append("refused_present")

    if not refused:
        if not used:
            errors.append("no_citations")
        # A statement of absence is not a claim drawn from the excerpts, so it
        # carries no citation by design. Without this exemption the citation rule
        # would reject exactly the one-sided-comparison behaviour rule 5 requires.
        uncited = [
            s
            for s in sentences(answer)
            if not MARKER_RE.search(s)
            and not TRIVIAL_RE.match(s)
            and not is_disclaimer(s)
            and len(s) > 25
        ]
        if uncited:
            errors.append(f"uncited_sentence:{len(uncited)}")

    # One-sided comparison guard. Only meaningful when the question actually names
    # projects the retrieval did not surface.
    if category == "comparison" and chunk_repos is not None:
        present = set(chunk_repos)
        absent = repos_named(question) - present
        if absent and len(present) <= 1:
            fabricated = fabricated_sides(answer, absent, label_mentions)
            if fabricated:
                errors.append(f"fabricated_absent_side:{','.join(sorted(fabricated))}")

    # refused_present is recorded but does not invalidate: an honest refusal on a
    # retrieval miss is correct behaviour, and those rows are still worth training on.
    fatal = [e for e in errors if not e.startswith("refused_present")]
    return Validation(valid=not fatal, errors=errors, cited=sorted(set(used)))
