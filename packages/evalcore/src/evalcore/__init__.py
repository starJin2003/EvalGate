"""EvalGate eval harness.

Suite and case schema, scorers, judge client with cache, runner, case-level diff,
and reports. Built against a stubbed model so the whole gate is testable before
any trained model exists; P1.3 swaps in a real endpoint and nothing else changes.
"""

from .diff import Breach, CaseDelta, Diff, compare
from .judge import JudgeCache, JudgeClient, JudgeProvider, RateLimited, StubJudge
from .loader import from_golden_jsonl, load_suite, save_suite
from .report import html_report, markdown_comment, terminal_report
from .runner import EchoContextModel, ModelClient, StubModel, run_case, run_suite
from .schema import (
    Case,
    CaseResult,
    Category,
    ContextChunk,
    RunResult,
    ScoreDetail,
    ScorerKind,
    ScorerSpec,
    Suite,
    Threshold,
)

__version__ = "0.1.0"

__all__ = [
    "Breach",
    "Case",
    "CaseDelta",
    "CaseResult",
    "Category",
    "ContextChunk",
    "Diff",
    "EchoContextModel",
    "JudgeCache",
    "JudgeClient",
    "JudgeProvider",
    "ModelClient",
    "RateLimited",
    "RunResult",
    "ScoreDetail",
    "ScorerKind",
    "ScorerSpec",
    "StubJudge",
    "StubModel",
    "Suite",
    "Threshold",
    "__version__",
    "compare",
    "from_golden_jsonl",
    "html_report",
    "load_suite",
    "markdown_comment",
    "run_case",
    "run_suite",
    "save_suite",
    "terminal_report",
]
