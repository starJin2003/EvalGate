"""Suite runner.

The model is behind a `ModelClient` protocol, so the harness runs identically
against a stub, a local llama-server, or anything else with an OpenAI-compatible
endpoint. P1.3 supplies the real client; nothing else changes.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from .judge import JudgeClient
from .schema import Case, CaseResult, RunResult, ScorerKind, Suite
from .scorers import is_refusal, is_rule_based, score_rule_based


@runtime_checkable
class ModelClient(Protocol):
    """What the harness needs from a model. Deliberately one method."""

    ref: str

    def generate(self, case: Case) -> str: ...


class StubModel:
    """Replays canned outputs by case id. Lets the whole harness, diff report, and
    gate be built and tested before any model exists."""

    def __init__(self, outputs: dict[str, str], ref: str = "stub") -> None:
        self.outputs = outputs
        self.ref = ref

    def generate(self, case: Case) -> str:
        return self.outputs.get(case.case_id, "")


class EchoContextModel:
    """A crude but non-trivial baseline: quotes the first chunk and cites it.
    Useful as a floor when checking that a suite can distinguish anything."""

    ref = "echo-context"

    def generate(self, case: Case) -> str:
        if not case.context:
            return "The documentation provided does not cover this."
        first = case.context[0]
        return f"{first.content.strip()[:200]} [{first.label}]"


def run_case(
    case: Case,
    model: ModelClient,
    judge: JudgeClient | None = None,
) -> CaseResult:
    started = time.perf_counter()
    try:
        output = model.generate(case)
    except Exception as exc:  # a model crash is a case failure, not a run failure
        return CaseResult(
            case_id=case.case_id,
            category=case.category,
            output="",
            error=f"{type(exc).__name__}: {exc}",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    scores = []
    error = None
    for spec in case.scorers:
        try:
            if is_rule_based(spec.kind):
                scores.append(score_rule_based(spec, case, output))
            elif spec.kind is ScorerKind.judge:
                if judge is None:
                    continue  # no judge configured: rule-based scores still count
                scores.append(judge.score(case, output, spec))
        except Exception as exc:
            error = f"{spec.kind}: {type(exc).__name__}: {exc}"
            break

    return CaseResult(
        case_id=case.case_id,
        category=case.category,
        output=output,
        # What the model DID, not whether doing it was correct.
        #
        # This was `any(refusal scorer passed) and category == "adversarial"`,
        # which hard-forced False outside adversarial — so counting refusals by
        # category could only ever return 0 there, and a recorded conclusion
        # ("0 of 24 comparison") was an artefact rather than a measurement.
        #
        # Note that simply DROPPING the category conjunct does not fix it, it
        # inverts it: `passed` for a non-adversarial case means the model
        # correctly *answered*, so `refused` would read True on exactly the cases
        # that did not refuse. The flag has to come from the text, which is what
        # `score_refusal` itself does — hence one shared `is_refusal`.
        refused=is_refusal(output),
        scores=scores,
        error=error,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def run_suite(
    suite: Suite,
    model: ModelClient,
    judge: JudgeClient | None = None,
    run_id: str | None = None,
    on_result: Callable[[CaseResult], None] | None = None,
) -> RunResult:
    """Score every case.

    `on_result` fires after each case, before the next one starts. It exists so a
    caller can persist progress incrementally: on CPU-only hardware a suite is
    hours long, and the whole run previously reached disk in a single write at the
    very end -- so a crash at case 90, or a model server that dies mid-suite,
    discarded every completed case. A callback that raises will abort the run,
    which is deliberate: a progress sink that has started failing is worth
    knowing about immediately rather than at the end.
    """
    results = []
    for case in suite.cases:
        result = run_case(case, model, judge)
        results.append(result)
        if on_result is not None:
            on_result(result)
    return RunResult(
        run_id=run_id or uuid.uuid4().hex[:12],
        suite_id=suite.suite_id,
        suite_version=suite.version,
        model_ref=model.ref,
        results=results,
        metadata={"judge": judge.provider.name if judge else None},
    )
