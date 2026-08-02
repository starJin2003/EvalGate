"""The paid, non-regenerable state of P1.1, and the restore path that proves it.

Everything in EvalGate's data stage is either free to rebuild or it is not:

  free      corpus markdown (`corpus fetch`, 11.3 s), chunks and chunk text
            (`corpus parse`, 3.4 s), the train/valid/test assignment (a pure
            function of the id set), the rendered splits
  paid      the 1,900 questions ($0.1389) and their 1,900 teacher answers
            ($2.7541 across batch, dry run and effort probe)

Only the paid half needs to survive, and until this file existed it lived in
exactly one place: a Docker volume. Losing it would not merely cost $2.89 -- the
Batch API would return *different* answers, invalidating the 96-case hand review,
the golden sample, and every digest in `dataset_manifest.json`.

So this module writes one row per question_id carrying the paid state and nothing
else. Chunk *text* is deliberately excluded: it regenerates for free, it is
upstream documentation this repo does not own, and it is what put a Grafana
placeholder token in front of GitHub push protection. Chunk *ids* are kept,
because which 8 chunks a question retrieved is not reproducible from anything
else -- retrieval ran against embeddings that cost money and a corpus snapshot
that has since moved on.

`restore()` is the part that makes this a backup rather than a comment. It
rebuilds all three splits from this file plus a corpus re-parse, touching no
database, and compares the digests against the committed manifest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import config, db
from ..corpus.parse import parse_repo
from ..corpus.repos import REPOS
from .build import SPLITS, file_digest, load_manifest, plan, to_example

RECOVERY_SQL = """
    SELECT q.question_id, q.category, q.repo, q.question, q.absent_symbol, q.split,
           q.retrieved, t.answer, t.refused, t.citations, t.valid, t.validation_errors
    FROM questions q
    LEFT JOIN teacher_answers t ON t.question_id = q.question_id
    ORDER BY q.question_id
"""

FIELDS = (
    "question_id",
    "category",
    "repo",
    "question",
    "absent_symbol",
    "split",
    "retrieved",
    "answer",
    "refused",
    "citations",
    "valid",
    "validation_errors",
)


def export_recovery() -> dict[str, Any]:
    """Write every question_id in the database, not just the trainable ones.

    All 1,900, including the 46 invalid rows and the 96 golden rows. The invalid
    rows were paid for and their validation_errors are the evidence behind the
    97.6% figure; the golden rows are the hand review's subject. A backup that
    restores only what P1.2 happens to train on cannot reproduce either.
    """
    with db.connect() as conn:
        rows = conn.execute(RECOVERY_SQL).fetchall()

    config.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    counts = {"total": 0, "valid": 0, "golden": 0, "missing_answer": 0}
    with config.RECOVERY_FILE.open("w") as fh:
        for r in rows:
            record = dict(zip(FIELDS, r, strict=True))
            if record["answer"] is None:
                counts["missing_answer"] += 1
            counts["total"] += 1
            counts["valid"] += bool(record["valid"])
            counts["golden"] += record["split"] == "golden"
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    counts["sha256"] = file_digest(config.RECOVERY_FILE)
    counts["bytes"] = config.RECOVERY_FILE.stat().st_size
    counts["path"] = str(config.RECOVERY_FILE)
    return counts


def load_recovery() -> list[dict]:
    if not config.RECOVERY_FILE.exists():
        raise RuntimeError(
            f"No recovery artifact at {config.RECOVERY_FILE}. "
            f"Run `evalgate-training dataset recovery-export` with Postgres up."
        )
    return [
        json.loads(line) for line in config.RECOVERY_FILE.read_text().splitlines() if line.strip()
    ]


def corpus_chunk_map() -> dict[str, Any]:
    """chunk_id -> Chunk, rebuilt from the markdown on disk. No database.

    This is the half of the restore that is supposed to be free. If it drifts,
    `restore()` catches it by cross-checking `content_sha256` against the
    committed `chunk_manifest.jsonl` rather than trusting the re-parse.
    """
    out: dict[str, Any] = {}
    for spec in REPOS:
        root = config.SCRATCH_DIR / spec.name
        if not root.exists():
            raise RuntimeError(f"{root} missing. Run `corpus fetch` before restoring.")
        for chunk in parse_repo(spec, root):
            out[chunk.chunk_id] = chunk
    return out


def restore(target: Path | None = None) -> dict[str, Any]:
    """Rebuild the three splits from the recovery artifact plus a corpus re-parse,
    and compare digests against the committed manifest. Postgres is never opened.
    """
    target = target or config.RESTORE_DIR
    manifest = load_manifest()
    records = load_recovery()
    chunks = corpus_chunk_map()

    committed_hashes = {}
    for line in config.CHUNK_MANIFEST.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            committed_hashes[row["chunk_id"]] = row["content_sha256"]

    result: dict[str, Any] = {
        "recovery_rows": len(records),
        "corpus_chunks": len(chunks),
        "chunk_sha_mismatches": [],
        "missing_chunks": [],
        "splits": {},
        "ok": True,
    }

    # The training population is exactly what build() selects: non-golden and valid.
    pool = [r for r in records if r["split"] == "train" and r["valid"]]
    assignment = plan([(r["question_id"], r["category"], r["repo"]) for r in pool])

    examples: dict[str, list[dict]] = {s: [] for s in SPLITS}
    for r in pool:
        context = []
        for i, cid in enumerate(r["retrieved"], start=1):
            chunk = chunks.get(cid)
            if chunk is None:
                result["missing_chunks"].append(cid)
                continue
            expected = committed_hashes.get(cid)
            if expected and expected != chunk.content_sha256:
                result["chunk_sha_mismatches"].append(cid)
            context.append(
                {
                    "label": f"C{i}",
                    "chunk_id": cid,
                    "repo": chunk.repo,
                    "heading_path": chunk.heading_path,
                    "source_url": chunk.source_url,
                    "content": chunk.content,
                }
            )
        row = dict(r)
        row["context"] = context
        examples[assignment[r["question_id"]]].append(to_example(row))

    target.mkdir(parents=True, exist_ok=True)
    for split in SPLITS:
        path = target / f"{split}.jsonl"
        with path.open("w") as fh:
            for ex in sorted(examples[split], key=lambda e: e["question_id"]):
                fh.write(json.dumps(ex, sort_keys=True) + "\n")
        actual = file_digest(path)
        expected = manifest["sha256"][split]
        entry = {
            "rows": len(examples[split]),
            "expected_rows": manifest["totals"][split],
            "sha256": actual,
            "expected_sha256": expected,
            "matches": actual == expected,
        }
        if not entry["matches"]:
            result["ok"] = False
        result["splits"][split] = entry

    if result["missing_chunks"] or result["chunk_sha_mismatches"]:
        result["ok"] = False
    result["target"] = str(target)
    return result


def format_restore(r: dict) -> str:
    out = [
        f"recovery rows {r['recovery_rows']}   corpus chunks re-parsed {r['corpus_chunks']}",
        f"chunk sha256 mismatches vs committed chunk_manifest: "
        f"{len(r['chunk_sha_mismatches'])}   missing chunks: {len(r['missing_chunks'])}",
        "",
    ]
    for split, e in r["splits"].items():
        mark = "MATCH" if e["matches"] else "MISMATCH"
        out.append(f"  {split:<6} {mark:<9} {e['rows']:>5} rows   sha256 {e['sha256'][:16]}...")
        if not e["matches"]:
            out.append(f"         expected {e['expected_sha256'][:16]}...")
    out.append("")
    out.append(
        "RESTORE PROVEN: rebuilt splits are byte-identical to dataset_manifest.json"
        if r["ok"]
        else "RESTORE FAILED: rebuilt splits differ from dataset_manifest.json"
    )
    return "\n".join(out)
