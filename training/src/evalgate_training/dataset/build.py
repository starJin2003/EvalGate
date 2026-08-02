"""Build the P1.2 training splits from the valid teacher rows.

Three files under `config.DATASET_DIR`, named train/valid/test.jsonl because that
is what `mlx_lm.lora --data <dir>` looks for. `valid.jsonl` is not optional: MLX
evaluates against it during training and errors without it.

Format is mlx-lm's chat format, `{"messages": [...]}`. Two alternatives were
rejected. The `prompt`/`completion` format is read first by
`mlx_lm.tuner.datasets.create_dataset`, and `CompletionsDataset.process` rebuilds
the conversation as user+assistant only -- it would silently drop the teacher
system prompt, which is where every rule being distilled lives. The `text` format
keeps control of the rendering but cannot mask the prompt, so loss would be
computed over ~3k tokens of retrieved documentation.

Selection is stratified per (category, repo) cell and deterministic: rows are
ordered by sha256(DATASET_SPLIT_SEED | question_id), exactly as `golden select`
orders its sample, so the split is a pure function of the id set and survives a
re-query or a row-order change. No RNG anywhere.

The split is disjoint by construction -- each id lands in exactly one slice of one
cell -- and `build()` asserts that, plus disjointness from the golden 96, rather
than trusting it.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from typing import Any

from evalcore import prompt

from .. import config
from ..golden.export import _rows

SPLITS = ("train", "valid", "test")


def _order_key(question_id: str) -> str:
    return hashlib.sha256(f"{config.DATASET_SPLIT_SEED}|{question_id}".encode()).hexdigest()


def file_digest(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def to_example(row: dict) -> dict:
    """One chat-format record. The user turn is byte-identical to what the teacher
    saw, so the student trains on the same prompt shape P1.4 will evaluate on."""
    return {
        "question_id": row["question_id"],
        "category": row["category"],
        "repo": row["repo"],
        "refused": row["refused"],
        "messages": [
            *prompt.build_messages(row["question"], row["context"]),
            {"role": "assistant", "content": row["answer"]},
        ],
    }


def plan(
    eligible: list[tuple[str, str, str]],
    test_frac: float = config.DATASET_TEST_FRAC,
    valid_frac: float = config.DATASET_VALID_FRAC,
) -> dict[str, str]:
    """-> {question_id: split}. Pure function over `(question_id, category, repo)`
    so the stratification rule is testable without a database.

    Held-out counts are rounded per cell, never globally, so every one of the 16
    cells appears in valid and test. The smallest cell has 52 rows, so an 8% slice
    still yields 4 -- thin, but a cell missing entirely from the held-out data
    would hide exactly the per-cell regression the split exists to catch.
    """
    by_cell: dict[tuple[str, str], list[str]] = {}
    for question_id, category, repo in eligible:
        by_cell.setdefault((category, repo), []).append(question_id)

    assignment: dict[str, str] = {}
    for _cell, ids in by_cell.items():
        ordered = sorted(ids, key=_order_key)
        n_test = max(1, round(len(ordered) * test_frac))
        n_valid = max(1, round(len(ordered) * valid_frac))
        for qid in ordered[:n_test]:
            assignment[qid] = "test"
        for qid in ordered[n_test : n_test + n_valid]:
            assignment[qid] = "valid"
        for qid in ordered[n_test + n_valid :]:
            assignment[qid] = "train"
    return assignment


def _token_stats(lengths: list[int]) -> dict[str, Any]:
    if not lengths:
        return {}
    ordered = sorted(lengths)

    def pct(p: float) -> int:
        # Nearest-rank. On n=1758 the difference from an interpolating percentile is
        # under a token, and an integer that is a real example length is easier to
        # reason about when it is about to become a truncation boundary.
        idx = min(len(ordered) - 1, max(0, round(p / 100 * len(ordered) + 0.5) - 1))
        return ordered[idx]

    return {
        "n": len(ordered),
        "min": ordered[0],
        "p50": pct(50),
        "p90": pct(90),
        "p95": pct(95),
        "p99": pct(99),
        "max": ordered[-1],
        "mean": round(statistics.fmean(ordered), 1),
    }


def _tokenizer():
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(config.BASE_MODEL)


def measure_tokens(examples: list[dict]) -> dict[str, Any]:
    """Token lengths of the exact strings MLX will train on.

    `apply_chat_template` over the full conversation is what `ChatDataset.process`
    calls, so these are the sequence lengths max_seq_len is compared against --
    not an estimate over the raw text. Prompt-only lengths use
    `add_generation_prompt=True`, which is the boundary `--mask-prompt` computes.
    """
    tk = _tokenizer()
    full: list[int] = []
    prompt: list[int] = []
    per_category: dict[str, list[int]] = {}
    over: dict[int, int] = {}
    for ex in examples:
        # return_dict=False is load-bearing: transformers 5 defaults it to True and
        # returns a BatchEncoding, so len() silently counts *keys* (2) instead of
        # tokens. mlx-lm's ChatDataset passes the same flag, so these are its counts.
        f = len(tk.apply_chat_template(ex["messages"], tokenize=True, return_dict=False))
        p = len(
            tk.apply_chat_template(
                ex["messages"][:-1],
                tokenize=True,
                add_generation_prompt=True,
                return_dict=False,
            )
        )
        full.append(f)
        prompt.append(p)
        per_category.setdefault(ex["category"], []).append(f)
    for limit in (2048, 3072, 4096, 6144, 8192):
        over[limit] = sum(1 for n in full if n > limit)
    return {
        "full_sequence": _token_stats(full),
        "prompt_only": _token_stats(prompt),
        "answer_only": _token_stats([f - p for f, p in zip(full, prompt, strict=True)]),
        "full_sequence_by_category": {c: _token_stats(v) for c, v in sorted(per_category.items())},
        "examples_over_limit": over,
    }


def build() -> dict[str, Any]:
    rows = [r for r in _rows("train") if r["valid"]]
    golden = set(json.loads(config.GOLDEN_IDS_FILE.read_text())["ids"])
    leaked = {r["question_id"] for r in rows} & golden
    if leaked:
        raise RuntimeError(f"{len(leaked)} golden ids leaked into the training pool: {leaked}")

    assignment = plan([(r["question_id"], r["category"], r["repo"]) for r in rows])
    examples = {s: [] for s in SPLITS}
    for row in rows:
        examples[assignment[row["question_id"]]].append(to_example(row))

    ids = {s: {e["question_id"] for e in examples[s]} for s in SPLITS}
    for a in SPLITS:
        for b in SPLITS:
            if a < b and ids[a] & ids[b]:
                raise RuntimeError(f"{len(ids[a] & ids[b])} ids shared between {a} and {b}")
        if ids[a] & golden:
            raise RuntimeError(f"{len(ids[a] & golden)} golden ids leaked into {a}")

    config.DATASET_DIR.mkdir(parents=True, exist_ok=True)
    for split in SPLITS:
        with (config.DATASET_DIR / f"{split}.jsonl").open("w") as fh:
            for ex in sorted(examples[split], key=lambda e: e["question_id"]):
                fh.write(json.dumps(ex, sort_keys=True) + "\n")

    digests = {s: file_digest(config.DATASET_DIR / f"{s}.jsonl") for s in SPLITS}

    tokens = {s: measure_tokens(examples[s]) for s in SPLITS}
    tokens["all"] = measure_tokens([e for s in SPLITS for e in examples[s]])

    def counts(key: str) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for split in SPLITS:
            for ex in examples[split]:
                out.setdefault(ex[key], dict.fromkeys(SPLITS, 0))[split] += 1
        return dict(sorted(out.items()))

    manifest = {
        "seed": config.DATASET_SPLIT_SEED,
        "base_model": config.BASE_MODEL,
        "source": "questions.split = 'train' AND teacher_answers.valid = true",
        "eligible_rows": len(rows),
        "golden_excluded": len(golden),
        "test_frac": config.DATASET_TEST_FRAC,
        "valid_frac": config.DATASET_VALID_FRAC,
        "totals": {s: len(examples[s]) for s in SPLITS},
        # The rendered splits are gitignored -- they are ~23 MB of chunk text that
        # `corpus fetch` + `corpus parse` rebuild for free and deterministically, and
        # they carry upstream documentation verbatim, which is what tripped GitHub
        # push protection on a Grafana placeholder token. These digests are how a
        # rebuild is *proved* identical rather than assumed: `dataset verify`
        # recomputes them. The id lists below make the split itself reconstructible.
        "sha256": digests,
        "by_category": counts("category"),
        "by_repo": counts("repo"),
        "by_cell": {
            f"{c}/{r}": {
                s: sum(1 for e in examples[s] if e["category"] == c and e["repo"] == r)
                for s in SPLITS
            }
            for c in config.CATEGORIES
            for r in sorted({e["repo"] for s in SPLITS for e in examples[s]})
        },
        "refusal_rows": {
            s: {
                "refused": sum(1 for e in examples[s] if e["refused"]),
                "total": len(examples[s]),
            }
            for s in SPLITS
        },
        "tokens": tokens,
        "ids": {s: sorted(ids[s]) for s in SPLITS},
    }
    # No timestamp, same reason as the golden manifest: a rerun on the same
    # population must be byte-identical, so a diff means the split really moved.
    config.DATASET_MANIFEST_FILE.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def load_manifest() -> dict:
    if not config.DATASET_MANIFEST_FILE.exists():
        raise RuntimeError(
            f"No dataset manifest at {config.DATASET_MANIFEST_FILE}. "
            f"Run `evalgate-training dataset export` first."
        )
    return json.loads(config.DATASET_MANIFEST_FILE.read_text())


def verify() -> dict[str, Any]:
    """Prove the on-disk splits are the ones the committed manifest describes.

    This is the half of the guarantee CI cannot give. CI checks the *split
    definition* -- ids, disjointness, counts -- straight from the committed
    manifest, which needs no rendered text. Only a machine that has actually
    rebuilt the splits can check that the rendered bytes match, so that check
    lives here and runs locally after `dataset export`.
    """
    m = load_manifest()
    results: dict[str, Any] = {"splits": {}, "ok": True}
    for split in SPLITS:
        path = config.DATASET_DIR / f"{split}.jsonl"
        entry: dict[str, Any] = {"path": str(path), "present": path.exists()}
        if path.exists():
            actual = file_digest(path)
            expected = m["sha256"][split]
            entry["sha256_matches"] = actual == expected
            entry["expected"] = expected
            entry["actual"] = actual
            on_disk = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            entry["rows"] = len(on_disk)
            entry["ids_match"] = sorted(r["question_id"] for r in on_disk) == m["ids"][split]
            if not (entry["sha256_matches"] and entry["ids_match"]):
                results["ok"] = False
        else:
            results["ok"] = False
        results["splits"][split] = entry
    return results


def format_verify(r: dict) -> str:
    out = []
    for split, e in r["splits"].items():
        if not e["present"]:
            out.append(f"  {split:<6} MISSING  {e['path']}  -- run `dataset export`")
            continue
        mark = "ok" if e["sha256_matches"] and e["ids_match"] else "MISMATCH"
        out.append(
            f"  {split:<6} {mark:<9} {e['rows']:>5} rows  sha256 {e['actual'][:16]}..."
            + ("" if e["sha256_matches"] else f"  expected {e['expected'][:16]}...")
        )
    out.append("")
    out.append(
        "byte-identical to the committed manifest"
        if r["ok"]
        else "DOES NOT match the committed manifest"
    )
    return "\n".join(out)


def format_report(m: dict) -> str:
    out: list[str] = []
    t = m["totals"]
    out.append(
        f"{m['eligible_rows']} valid rows -> train {t['train']}  valid {t['valid']}  "
        f"test {t['test']}   (golden {m['golden_excluded']} excluded, disjointness asserted)"
    )
    out.append("")
    out.append(f"{'':<14}{'train':>8}{'valid':>8}{'test':>8}{'total':>8}")
    for label, table in (("category", m["by_category"]), ("repo", m["by_repo"])):
        out.append(f"-- by {label}")
        for name, c in table.items():
            tot = sum(c.values())
            out.append(f"  {name:<12}{c['train']:>8}{c['valid']:>8}{c['test']:>8}{tot:>8}")
    out.append("-- refusal share")
    for s in SPLITS:
        r = m["refusal_rows"][s]
        pct = 100 * r["refused"] / r["total"] if r["total"] else 0
        out.append(f"  {s:<12}{r['refused']:>8}{'':>8}{'':>8}{pct:>7.1f}%")
    out.append("")
    out.append("-- token lengths, full rendered sequence (system+user+assistant)")
    out.append(f"{'':<14}{'n':>8}{'p50':>8}{'p95':>8}{'p99':>8}{'max':>8}{'mean':>9}")
    for name, key in (("all", "all"), ("train", "train"), ("valid", "valid"), ("test", "test")):
        s = m["tokens"][key]["full_sequence"]
        out.append(
            f"  {name:<12}{s['n']:>8}{s['p50']:>8}{s['p95']:>8}{s['p99']:>8}"
            f"{s['max']:>8}{s['mean']:>9}"
        )
    out.append("-- token lengths by category (full sequence, all splits)")
    for name, s in m["tokens"]["all"]["full_sequence_by_category"].items():
        out.append(
            f"  {name:<12}{s['n']:>8}{s['p50']:>8}{s['p95']:>8}{s['p99']:>8}"
            f"{s['max']:>8}{s['mean']:>9}"
        )
    for name, key in (("prompt only", "prompt_only"), ("answer only", "answer_only")):
        s = m["tokens"]["all"][key]
        out.append(
            f"  {name:<12}{s['n']:>8}{s['p50']:>8}{s['p95']:>8}{s['p99']:>8}"
            f"{s['max']:>8}{s['mean']:>9}"
        )
    out.append("")
    out.append(
        "-- examples that a max_seq_len would truncate (of "
        f"{m['tokens']['all']['full_sequence']['n']})"
    )
    for limit, n in sorted(m["tokens"]["all"]["examples_over_limit"].items()):
        pct = 100 * n / m["tokens"]["all"]["full_sequence"]["n"]
        out.append(f"  {limit:<12}{n:>8}{pct:>7.2f}%")
    return "\n".join(out)
