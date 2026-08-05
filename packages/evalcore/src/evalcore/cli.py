"""Gate CLI. This is what `eval-gate.yml` shells out to.

The exit code is the merge decision: 0 passes, 1 blocks. Everything else the
workflow does — posting the comment, uploading the report — is presentation.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .diff import BackendMismatch, compare
from .gate_config import MAX_DAILY_AGE_H
from .judge import JudgeCache, JudgeClient, StubJudge
from .loader import from_golden_jsonl, load_suite, save_suite
from .model import LlamaServerModel
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
    if args.server_url and not args.backend:
        print(
            "--backend is required with --server-url. A run without it cannot be "
            "safely compared against another backend's run, and the gate would have "
            "to either guess or allow a contaminated diff.",
            file=sys.stderr,
        )
        return 2
    if args.server_url:
        # The live path. Until P1.3 produced a real GGUF there was no way to
        # reach it from here at all: `LlamaServerModel` existed and was tested
        # against a fake server, but nothing in this CLI could construct one, so
        # the client, the prompt renderer and a real llama-server had never been
        # in the same process.
        model = LlamaServerModel(
            version=args.model_version,
            quantization=args.quantization,
            base_url=args.server_url,
            timeout_s=args.timeout_s,
        )
    elif args.outputs:
        model = StubModel(json.loads(Path(args.outputs).read_text()), ref=args.model_ref)
    else:
        model = EchoContextModel()
    judge = None
    if args.judge:
        cache = JudgeCache(Path(args.judge_cache)) if args.judge_cache else None
        judge = JudgeClient(StubJudge(), cache)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Prove the destination is writable BEFORE spending hours on inference. A
    # 2.5-hour CPU-only run previously reached disk in a single write at the very
    # end and died there on a permission error, discarding the whole run. This
    # costs one file create.
    probe = out.parent / f".{out.name}.writable"
    try:
        probe.touch()
        probe.unlink()
    except OSError as exc:
        print(f"cannot write to {out.parent}: {exc}", file=sys.stderr)
        return 2

    on_result = None
    progress = None
    if args.progress_jsonl:
        progress_path = Path(args.progress_jsonl)
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        progress = progress_path.open("w")

        def on_result(result) -> None:  # noqa: F811
            # Flushed per case, so a killed pod leaves every completed case on
            # disk and the run is resumable evidence rather than a total loss.
            progress.write(result.model_dump_json() + "\n")
            progress.flush()

    try:
        run = run_suite(suite, model, judge, run_id=args.run_id, on_result=on_result)
    finally:
        if progress is not None:
            progress.close()

    if args.server_url:
        # Provenance, recorded at write time rather than threaded through
        # run_suite: the runner has no business knowing about backends.
        # build_info is deliberately NOT part of the ModelClient protocol — it is
        # a llama-server detail, and the protocol is one method on purpose. Asked
        # for optionally so any other client stays usable here.
        probe_build = getattr(model, "build_info", None)
        run = run.model_copy(
            update={
                "backend": args.backend,
                "quantization": args.quantization,
                "build_info": probe_build() if callable(probe_build) else None,
            }
        )
    out.write_text(run.model_dump_json(indent=2) + "\n")
    errors = sum(1 for r in run.results if r.error)
    if errors:
        # Loud, because `run_case` turns a dead endpoint into a per-case error:
        # a run artifact can look complete while most of it is failures.
        print(f"WARNING: {errors} of {len(run.results)} cases errored", file=sys.stderr)
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

    try:
        diff = compare(suite, _load_run(baseline_path), _load_run(Path(args.candidate)))
    except BackendMismatch as exc:
        # Exit 2, not 1. A merge-blocking regression (1) and a comparison that
        # should never have been attempted (2) are different outcomes, and a CI
        # log that conflates them sends the reader to the wrong problem.
        print(f"\nGATE REFUSED\n{exc}", file=sys.stderr)
        if args.comment:
            Path(args.comment).parent.mkdir(parents=True, exist_ok=True)
            Path(args.comment).write_text(
                f"## ⛔ eval-gate refused — `{suite.suite_id}`\n\n```\n{exc}\n```\n"
            )
        return 2
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


def cmd_publish_latest(args: argparse.Namespace) -> int:
    """Stamp the daily verdict with a completion time the PR gate can age.

    Freshness is measured from when the run FINISHED, not from the DAG's logical
    date: a run that starts at 03:00 and ends at 06:48 is 3.8 hours less stale
    than its `ds` suggests, and the staleness bound is only 8.2 hours above its
    floor.
    """
    verdict_path = Path(args.verdict)
    payload = {
        "verdict": "unknown",
        "completed_at": datetime.now(UTC).isoformat(),
        "source": str(verdict_path),
    }
    if verdict_path.exists():
        payload.update(json.loads(verdict_path.read_text()))
        payload["completed_at"] = datetime.now(UTC).isoformat()
    else:
        # The gate task failed before writing a verdict. Recording "unknown"
        # rather than nothing keeps the two failure modes distinguishable: a
        # regression is a `fail`, a broken DAG is an `unknown`, and a dead DAG is
        # an absent/old timestamp.
        payload["error"] = f"{verdict_path} not written"
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"{payload['verdict']}  {payload['completed_at']}  -> {args.out}")
    return 0


def cmd_check_daily(args: argparse.Namespace) -> int:
    """The PR gate's only model-dependent check, and it runs no model.

    Asserts the last daily run passed and is recent. Exit 1 blocks the merge.
    """
    latest = Path(args.latest)
    if not latest.exists():
        print(f"BLOCK: no daily verdict at {latest}", file=sys.stderr)
        return 1
    payload = json.loads(latest.read_text())
    completed = datetime.fromisoformat(payload["completed_at"])
    age_h = (datetime.now(UTC) - completed).total_seconds() / 3600
    verdict = payload.get("verdict", "unknown")

    print(f"daily verdict {verdict}, {age_h:.1f} h old (limit {args.max_age_h} h)")
    ok = True
    if age_h > args.max_age_h:
        print(
            f"BLOCK: the last daily eval is {age_h:.1f} h old, over the "
            f"{args.max_age_h} h bound. One missed night is tolerated; this is more "
            "than one, or the DAG is not running.",
            file=sys.stderr,
        )
        ok = False
    if verdict != "pass":
        print(
            f"BLOCK: the last daily eval verdict is {verdict!r}, not 'pass'. "
            "Merges are blocked until the measured regression is resolved or a new "
            "baseline is promoted deliberately.",
            file=sys.stderr,
        )
        ok = False
    return 0 if ok else 1


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
    r.add_argument(
        "--server-url",
        help="live llama-server URL, e.g. http://127.0.0.1:8080. Takes precedence over --outputs",
    )
    r.add_argument(
        "--model-version",
        default="v1",
        help="weights id baked into model_ref; use something that names the checkpoint",
    )
    r.add_argument("--quantization", default="Q4_K_M")
    r.add_argument(
        "--timeout-s",
        type=float,
        default=180.0,
        help=(
            "per-case HTTP timeout against --server-url. The 180s default is sized for "
            "GPU-backed serving; CPU-only inference on the OCI node needs more, because a "
            "timeout there records a per-case error that is indistinguishable from a model "
            "failure in the run artifact"
        ),
    )
    r.add_argument(
        "--backend",
        help=(
            "REQUIRED with --server-url. Declares which backend served the run, e.g. "
            "'metal' or 'cpu-aarch64'. The gate refuses to diff runs from different "
            "backends: identical weights across Metal and ggml CPU moved this suite "
            "0.004272, the size of a real category regression. Declared rather than "
            "detected because nothing the server exposes names it reliably"
        ),
    )
    r.add_argument(
        "--progress-jsonl",
        help=(
            "append one CaseResult per line as each case completes, flushed. On "
            "hours-long CPU runs this is what survives a killed pod; without it the "
            "entire run reaches disk only in the final write"
        ),
    )
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

    pl = sub.add_parser("publish-latest", help="stamp the daily verdict with a completion time")
    pl.add_argument("--verdict", required=True)
    pl.add_argument("--out", required=True)
    pl.set_defaults(func=cmd_publish_latest)

    cd = sub.add_parser(
        "check-daily", help="PR gate: assert the last daily eval passed and is recent"
    )
    cd.add_argument("--latest", required=True)
    cd.add_argument("--max-age-h", type=float, default=MAX_DAILY_AGE_H)
    cd.set_defaults(func=cmd_check_daily)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
