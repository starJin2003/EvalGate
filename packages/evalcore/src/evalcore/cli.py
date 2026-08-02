"""Gate CLI. This is what `eval-gate.yml` shells out to.

The exit code is the merge decision: 0 passes, 1 blocks. Everything else the
workflow does — posting the comment, uploading the report — is presentation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .diff import compare
from .judge import JudgeCache, JudgeClient, StubJudge
from .loader import from_golden_jsonl, load_suite, save_suite
from .report import html_report, markdown_comment, terminal_report
from .runner import EchoContextModel, StubModel, run_suite
from .schema import RunResult


def _load_run(path: Path) -> RunResult:
    return RunResult.model_validate(json.loads(path.read_text()))


def cmd_build_suite(args: argparse.Namespace) -> int:
    suite = from_golden_jsonl(Path(args.golden), suite_id=args.suite_id)
    save_suite(suite, Path(args.out))
    print(f"{len(suite.cases)} cases -> {args.out}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    suite = load_suite(Path(args.suite))
    if args.outputs:
        model = StubModel(json.loads(Path(args.outputs).read_text()), ref=args.model_ref)
    else:
        model = EchoContextModel()
    judge = None
    if args.judge:
        cache = JudgeCache(Path(args.judge_cache)) if args.judge_cache else None
        judge = JudgeClient(StubJudge(), cache)
    run = run_suite(suite, model, judge, run_id=args.run_id)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(run.model_dump_json(indent=2) + "\n")
    print(f"{run.run_id}  score {run.score:.3f}  -> {args.out}")
    for category, score in run.category_scores().items():
        print(f"  {category:<14} {score:.3f}")
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    suite = load_suite(Path(args.suite))
    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        # First run on a new suite has nothing to regress against.
        print("no baseline artifact; passing")
        if args.comment:
            Path(args.comment).write_text(
                f"## ✅ eval-gate — `{suite.suite_id}`\n\nNo baseline yet.\n"
            )
        return 0

    diff = compare(suite, _load_run(baseline_path), _load_run(Path(args.candidate)))
    print(terminal_report(diff, color=sys.stdout.isatty()))
    if args.comment:
        Path(args.comment).parent.mkdir(parents=True, exist_ok=True)
        Path(args.comment).write_text(markdown_comment(diff) + "\n")
    if args.html:
        Path(args.html).parent.mkdir(parents=True, exist_ok=True)
        Path(args.html).write_text(html_report(diff))
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "verdict": diff.verdict,
                    "delta": diff.delta,
                    "categories": diff.by_category(),
                    "breaches": [b.__dict__ for b in diff.breaches],
                },
                indent=2,
            )
            + "\n"
        )
    return 0 if diff.verdict == "pass" else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="evalgate-eval", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build-suite", help="build a suite from the P1.1 golden export")
    b.add_argument("--golden", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--suite-id", default="grounded-docs-qa")
    b.set_defaults(func=cmd_build_suite)

    r = sub.add_parser("run", help="run a suite and write a run artifact")
    r.add_argument("--suite", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--outputs", help="JSON map of case_id -> output; omit to use the echo model")
    r.add_argument("--model-ref", default="stub")
    r.add_argument("--run-id")
    r.add_argument("--judge", action="store_true")
    r.add_argument("--judge-cache")
    r.set_defaults(func=cmd_run)

    g = sub.add_parser("gate", help="compare two runs; exit 1 blocks the merge")
    g.add_argument("--suite", required=True)
    g.add_argument("--baseline", required=True)
    g.add_argument("--candidate", required=True)
    g.add_argument("--comment", help="write the PR comment markdown here")
    g.add_argument("--html", help="write the full HTML report here")
    g.add_argument("--json", help="write a machine-readable verdict here")
    g.set_defaults(func=cmd_gate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
