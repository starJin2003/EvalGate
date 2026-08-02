"""P1.1 pipeline CLI.

Ordered run:

    evalgate-training corpus fetch            # free
    evalgate-training corpus parse            # free
    evalgate-training db init                 # free
    evalgate-training corpus load             # free
    evalgate-training corpus embed            # paid, ~$0.03
    evalgate-training questions submit        # paid, batch
    evalgate-training questions collect
    evalgate-training questions verify
    evalgate-training teacher retrieval       # paid, ~$0.01
    evalgate-training teacher dry-run         # paid, ~$0.02   <-- STOP, review report
    evalgate-training teacher submit --approve-full-run
    evalgate-training teacher collect
    evalgate-training golden select           # writes the 96-case manifest
    evalgate-training golden review           # hand review, localhost, resumable
    evalgate-training golden review-export    # the same 96 cases for an outside reviewer
    evalgate-training golden export
    evalgate-training dataset export
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser

from . import config, db, openai_batch
from .budget import BudgetExceeded, Ledger
from .corpus import embed, fetch, parse
from .corpus.repos import REPOS
from .golden import export as golden_export
from .golden import review as golden_review
from .golden import review_export as golden_review_export
from .golden import review_server
from .golden import select as golden_select
from .questions import generate, verify
from .teacher import batch as teacher_batch


def _print(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


# --- corpus ------------------------------------------------------------------
def cmd_corpus_fetch(args: argparse.Namespace) -> None:
    _print(fetch.fetch_all(method=args.method, only=args.repo))


def cmd_corpus_parse(args: argparse.Namespace) -> None:
    all_chunks = []
    summary = {}
    for spec in REPOS:
        if args.repo and spec.name != args.repo:
            continue
        chunks = parse.parse_repo(spec, fetch.target_dir(spec))
        all_chunks.extend(chunks)
        files = len({c.file_path for c in chunks})
        summary[spec.name] = {
            "chunks": len(chunks),
            "files_with_chunks": files,
            "tokens": sum(c.token_count for c in chunks),
            "mean_tokens": round(sum(c.token_count for c in chunks) / max(1, len(chunks)), 1),
        }
    embed.write_manifest(all_chunks)
    summary["total"] = {
        "chunks": len(all_chunks),
        "tokens": sum(c.token_count for c in all_chunks),
        "manifest": str(config.CHUNK_MANIFEST),
    }
    _print(summary)


def cmd_corpus_load(args: argparse.Namespace) -> None:
    all_chunks = []
    for spec in REPOS:
        if args.repo and spec.name != args.repo:
            continue
        all_chunks.extend(parse.parse_repo(spec, fetch.target_dir(spec)))
    embed.load_chunks(all_chunks)
    _print({"loaded": len(all_chunks), **db.counts()})


def cmd_corpus_embed(args: argparse.Namespace) -> None:
    ledger = Ledger()
    result = embed.embed_pending(ledger, limit=args.limit)
    db.create_vector_index()
    _print(result)
    print(ledger.summary())


# --- db ----------------------------------------------------------------------
def cmd_db_init(_: argparse.Namespace) -> None:
    db.init_schema()
    _print({"schema": "ready", **db.counts()})


def cmd_db_status(_: argparse.Namespace) -> None:
    _print(db.counts())


# --- questions ---------------------------------------------------------------
def cmd_questions_plan(_: argparse.Namespace) -> None:
    _print(generate.plan_summary(generate.plan_calls()))


def cmd_questions_counts(_: argparse.Namespace) -> None:
    counts = generate.stored_counts()
    counts["per_repo_targets"] = {
        f"{c}/{r}": n for c in config.CATEGORIES for r, n in generate.category_repo_quota(c).items()
    }
    _print(counts)


def cmd_questions_leak_report(_: argparse.Namespace) -> None:
    _print(verify.leak_report())


def cmd_questions_trim(args: argparse.Namespace) -> None:
    _print(generate.trim_to_targets(apply=not args.dry_run))


def cmd_questions_submit(_: argparse.Namespace) -> None:
    _print({"batch_id": generate.submit(Ledger())})


def _questions_job(round_no: int) -> str:
    return generate.JOB if round_no == 0 else f"{generate.JOB}_topup{round_no}"


def cmd_questions_poll(args: argparse.Namespace) -> None:
    _print({"status": openai_batch.poll(_questions_job(args.round), wait=args.wait)})


def cmd_questions_collect(args: argparse.Namespace) -> None:
    ledger = Ledger()
    _print(generate.collect(ledger, job=_questions_job(args.round)))
    print(ledger.summary())


def cmd_questions_topup(args: argparse.Namespace) -> None:
    ledger = Ledger()
    batch_id, gaps = generate.submit_topup(ledger, args.round)
    _print({"batch_id": batch_id, "gaps_requested": gaps, "round": args.round})
    print(
        f"\nPoll with:    questions poll --round {args.round} --wait"
        f"\nThen collect: questions collect --round {args.round}"
        f"\nThen re-run:  questions verify"
    )


def cmd_questions_verify(args: argparse.Namespace) -> None:
    result = verify.verify_adversarial(delete=not args.keep)
    result["deduped"] = verify.dedupe()
    _print({**result, **db.counts()})


# --- teacher -----------------------------------------------------------------
def cmd_teacher_retrieval(args: argparse.Namespace) -> None:
    ledger = Ledger()
    result = teacher_batch.attach_retrieval(ledger, limit=args.limit)
    _print(result)
    span = result.get("retrieval_span_by_category", {}).get("comparison", {})
    k, reason = config.decide_k(span.get("multi_repo_pct"))
    print(f"\nPre-committed k rule: {reason}")
    if k != config.RETRIEVAL_K:
        print(
            f"ACTION REQUIRED: set RETRIEVAL_K = {k} in config.py, clear retrieval with\n"
            f"  evalgate-training teacher reset-retrieval\n"
            f"then re-run `teacher retrieval` once and re-measure."
        )
    print(ledger.summary())


def cmd_teacher_reset_retrieval(_: argparse.Namespace) -> None:
    _print(teacher_batch.reset_retrieval())


def cmd_teacher_revalidate(_: argparse.Namespace) -> None:
    _print(teacher_batch.revalidate())


def cmd_teacher_flagged(args: argparse.Namespace) -> None:
    _print(teacher_batch.flagged_rows(limit=args.limit))


def cmd_teacher_audit(args: argparse.Namespace) -> None:
    _print(teacher_batch.poisoning_rates())
    if args.regenerate:
        result = teacher_batch.regenerate_invalid()
        _print(result)
        if result["queued_for_retry"]:
            print(
                f"{result['queued_for_retry']} rows cleared for retry. "
                "Re-run `teacher submit --approve-full-run` to regenerate them."
            )
        if result["dropped_after_exhausting_retries"]:
            print(
                f"{result['dropped_after_exhausting_retries']} rows dropped after "
                f"{result['max_attempts']} failed attempts each."
            )
    if args.drop:
        _print(teacher_batch.drop_invalid_questions(teacher_batch.POISON_CODES))


def cmd_teacher_dry_run(args: argparse.Namespace) -> None:
    ledger = Ledger()
    report = teacher_batch.dry_run(
        ledger, n=args.n, reasoning_effort=args.reasoning_effort, redo=args.redo
    )
    _print(report)
    print(ledger.summary())
    print(
        f"\nSTOP. Full run projected at ${report['projected_full_run_usd_batch']:.4f}, "
        f"total would reach ${report['projected_total_usd']:.4f} of "
        f"${report['usd_ceiling']:.2f}.\n"
        "Review the report, then rerun with: teacher submit --approve-full-run"
    )


def cmd_teacher_submit(args: argparse.Namespace) -> None:
    result = teacher_batch.submit(Ledger(), approved=args.approve_full_run)
    _print(result)
    if result["still_pending_after"]:
        print(
            f"\n{result['still_pending_after']} questions remain. The enqueued-token cap\n"
            f"is on in-flight work, so wait for part {result['part']} to complete:\n"
            f"  teacher poll --wait && teacher collect\n"
            f"then rerun submit for the next shard."
        )


def cmd_teacher_poll(args: argparse.Namespace) -> None:
    part = args.part if args.part is not None else max(0, teacher_batch.next_part() - 1)
    _print(
        {"part": part, "status": openai_batch.poll(teacher_batch.part_job(part), wait=args.wait)}
    )


def cmd_teacher_collect(args: argparse.Namespace) -> None:
    ledger = Ledger()
    part = args.part if args.part is not None else max(0, teacher_batch.next_part() - 1)
    _print(teacher_batch.collect(ledger, job=teacher_batch.part_job(part)))
    print(ledger.summary())


# --- golden and dataset -------------------------------------------------------
def cmd_golden_select(args: argparse.Namespace) -> None:
    _print(golden_select.select(per_cell=args.per_cell))


def cmd_golden_export(_: argparse.Namespace) -> None:
    counts = golden_export.export_golden()
    _print({"by_category": counts, "jsonl": str(config.GOLDEN_JSONL)})
    print("\nHand review with:  evalgate-training golden review")


def cmd_golden_review(args: argparse.Namespace) -> None:
    cases, judgments, manifest = golden_review.load_all()
    server = review_server.serve(cases, args.host, args.port)
    url = f"http://{args.host}:{server.server_address[1]}"

    if args.case:
        start = f"{url}/case/{args.case}"
    else:
        nxt = golden_review.first_unjudged(cases, judgments)
        start = f"{url}/case/{nxt}" if nxt else f"{url}/summary"

    print(f"{len(cases)} cases from {config.GOLDEN_MANIFEST_FILE.name}, {len(judgments)} judged.")
    if manifest.get("shortfalls"):
        print(f"{len(manifest['shortfalls'])} cell(s) were backfilled; see the manifest.")
    print(f"Serving {start}\nAppending to {config.GOLDEN_REVIEW_JSONL}\nCtrl-C to stop.")
    if not args.no_browser:
        webbrowser.open(start)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.shutdown()
        server.server_close()
    cmd_golden_summary(args)


def cmd_golden_review_export(args: argparse.Namespace) -> None:
    print(golden_review_export.format_report(golden_review_export.export_review(args.batch_size)))


def cmd_golden_summary(_: argparse.Namespace) -> None:
    cases, judgments, _manifest = golden_review.load_all()
    summary = golden_review.summarize(cases, judgments)
    print(golden_review.format_summary(summary))
    if summary["unjudged"] == 0:
        print(f"\nWritten to {golden_review.write_summary(summary)}")
    else:
        print(f"\n{summary['unjudged']} unjudged; resume with `golden review`.")


def cmd_dataset_export(_: argparse.Namespace) -> None:
    _print(golden_export.export_training())


def cmd_budget(_: argparse.Namespace) -> None:
    print(Ledger().summary())


# --- wiring -------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="evalgate-training", description=__doc__)
    sub = p.add_subparsers(dest="group", required=True)

    corpus = sub.add_parser("corpus").add_subparsers(dest="cmd", required=True)
    f = corpus.add_parser("fetch", help="clone docs repos into the gitignored scratch dir")
    f.add_argument("--method", choices=("git", "tarball"), default="git")
    f.add_argument("--repo")
    f.set_defaults(func=cmd_corpus_fetch)
    pa = corpus.add_parser("parse", help="parse markdown to chunks and write the manifest")
    pa.add_argument("--repo")
    pa.set_defaults(func=cmd_corpus_parse)
    lo = corpus.add_parser("load", help="upsert chunks into Postgres")
    lo.add_argument("--repo")
    lo.set_defaults(func=cmd_corpus_load)
    em = corpus.add_parser("embed", help="embed pending chunks (paid)")
    em.add_argument("--limit", type=int)
    em.set_defaults(func=cmd_corpus_embed)

    dbp = sub.add_parser("db").add_subparsers(dest="cmd", required=True)
    dbp.add_parser("init").set_defaults(func=cmd_db_init)
    dbp.add_parser("status").set_defaults(func=cmd_db_status)

    q = sub.add_parser("questions").add_subparsers(dest="cmd", required=True)
    q.add_parser("plan", help="show the call plan without spending").set_defaults(
        func=cmd_questions_plan
    )
    q.add_parser("counts", help="stored questions per repo and category").set_defaults(
        func=cmd_questions_counts
    )
    q.add_parser(
        "leak-report", help="per-repo adversarial leak rate, replayed from batch outputs"
    ).set_defaults(func=cmd_questions_leak_report)
    qtr = q.add_parser("trim", help="drop surplus from cells above their per-repo target")
    qtr.add_argument("--dry-run", action="store_true")
    qtr.set_defaults(func=cmd_questions_trim)
    q.add_parser("submit", help="submit the generation batch (paid)").set_defaults(
        func=cmd_questions_submit
    )
    qp = q.add_parser("poll")
    qp.add_argument("--wait", action="store_true")
    qp.add_argument("--round", type=int, default=0, help="0 = main batch, N = top-up round N")
    qp.set_defaults(func=cmd_questions_poll)
    qc = q.add_parser("collect")
    qc.add_argument("--round", type=int, default=0)
    qc.set_defaults(func=cmd_questions_collect)
    qt = q.add_parser("topup", help="generate more questions for short categories (paid)")
    qt.add_argument("--round", type=int, default=1)
    qt.set_defaults(func=cmd_questions_topup)
    qv = q.add_parser("verify", help="prove adversarial symbols are absent from the corpus")
    qv.add_argument("--keep", action="store_true", help="report without deleting")
    qv.set_defaults(func=cmd_questions_verify)

    t = sub.add_parser("teacher").add_subparsers(dest="cmd", required=True)
    tr = t.add_parser("retrieval", help="attach top-k context to every question (paid)")
    tr.add_argument("--limit", type=int)
    tr.set_defaults(func=cmd_teacher_retrieval)
    td = t.add_parser("dry-run", help="measure real tokens on a small sample (paid)")
    td.add_argument("-n", type=int, default=config.DRY_RUN_SIZE)
    td.add_argument("--reasoning-effort", choices=("low", "medium", "high"))
    td.add_argument("--redo", action="store_true", help="re-run over the same sample")
    td.set_defaults(func=cmd_teacher_dry_run)
    ts = t.add_parser("submit", help="submit the full teacher batch (paid)")
    ts.add_argument("--approve-full-run", action="store_true")
    ts.set_defaults(func=cmd_teacher_submit)
    tp = t.add_parser("poll")
    tp.add_argument("--wait", action="store_true")
    tp.add_argument("--part", type=int, help="shard index; defaults to the latest")
    tp.set_defaults(func=cmd_teacher_poll)
    tc = t.add_parser("collect")
    tc.add_argument("--part", type=int, help="shard index; defaults to the latest")
    tc.set_defaults(func=cmd_teacher_collect)
    t.add_parser(
        "reset-retrieval", help="clear attached retrieval so it can be redone at a new k"
    ).set_defaults(func=cmd_teacher_reset_retrieval)
    t.add_parser(
        "revalidate", help="re-apply validation to stored answers, no regeneration"
    ).set_defaults(func=cmd_teacher_revalidate)
    tf = t.add_parser("flagged", help="full text of poisoned answers, for review")
    tf.add_argument("--limit", type=int, default=20)
    tf.set_defaults(func=cmd_teacher_flagged)
    ta = t.add_parser("audit", help="fabrication and refusal-failure rates")
    ta.add_argument("--regenerate", action="store_true", help="clear poisoned answers for reruns")
    ta.add_argument("--drop", action="store_true", help="delete the questions outright")
    ta.set_defaults(func=cmd_teacher_audit)

    g = sub.add_parser("golden").add_subparsers(dest="cmd", required=True)
    gs = g.add_parser("select", help="pick the 96-case sample and write the manifest")
    gs.add_argument("--per-cell", type=int, default=config.GOLDEN_PER_CELL)
    gs.set_defaults(func=cmd_golden_select)
    g.add_parser("export").set_defaults(func=cmd_golden_export)
    gr = g.add_parser("review", help="hand review the sample in a localhost UI")
    gr.add_argument("--port", type=int, default=config.REVIEW_PORT)
    gr.add_argument("--host", default=config.REVIEW_HOST)
    gr.add_argument("--case", help="open a specific case: 1-based index or question id")
    gr.add_argument("--no-browser", action="store_true")
    gr.set_defaults(func=cmd_golden_review)
    gre = g.add_parser(
        "review-export", help="dump the 96 cases for an external reviewer, no machine judgments"
    )
    gre.add_argument("--batch-size", type=int, default=config.REVIEW_BATCH_SIZE)
    gre.set_defaults(func=cmd_golden_review_export)
    g.add_parser("summary", help="pass rates from the review log").set_defaults(
        func=cmd_golden_summary
    )

    d = sub.add_parser("dataset").add_subparsers(dest="cmd", required=True)
    d.add_parser("export").set_defaults(func=cmd_dataset_export)

    sub.add_parser("budget", help="show cumulative spend").set_defaults(func=cmd_budget)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except BudgetExceeded as exc:
        print(f"BUDGET HALT: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
