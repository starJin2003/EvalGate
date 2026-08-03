"""Build v2's data mix: v1's split with most adversarial rows removed from train.

BUILD_PLAN P1.2 wants v2 trained on "a mix that raises practical question weight
and cuts refusal examples", expecting a higher overall score with the refusal
category collapsing -- a real data-mix experiment whose cause is documented.

Two properties matter more than anything else in this file.

**valid.jsonl and test.jsonl must be byte-identical to v1's.** The entire v1-to-v2
comparison rests on both models being scored on the same rows. If a rebuild
regenerated them -- even to identical content by a different route -- the claim
"the mix caused the regression" would rest on an assumption instead of a digest.
So they are COPIED, and the copies are checked against the digests committed in
`dataset_manifest.json`. A mismatch raises; it is not reported as a warning.

**v2's train is a strict line-subset of v1's train.** Not a re-render from
Postgres. The rows are copied as raw bytes, so the prompt text, the chunk
ordering, the system prompt and the JSON key order are the same objects v1 trained
on rather than a reconstruction that agrees today. That also means this module
needs no database and no tokenizer, and cannot drift from `evalcore.prompt`.

Selection of the surviving adversarial rows is a sha256 ordering over question
ids, the same no-RNG rule used by `golden select` and `dataset.build.plan`.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from .. import config
from .build import SPLITS, file_digest


def _mix_order_key(question_id: str) -> str:
    return hashlib.sha256(f"{config.DATASET_V2_MIX_SEED}|{question_id}".encode()).hexdigest()


def _read_lines(path: Path) -> list[tuple[str, dict]]:
    """-> [(raw_line_without_newline, parsed)]. The raw text is what gets written
    back out, so nothing is re-serialised."""
    out = []
    with path.open() as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line:
                out.append((line, json.loads(line)))
    return out


def select_kept_adversarial(rows: list[dict], keep: int | None = None) -> set[str]:
    """Which adversarial question ids survive into v2's train.

    Pure over the id list so it is testable without any files. Deterministic:
    sha256(seed | question_id) ascending, take the first `keep`.
    """
    keep = config.DATASET_V2_ADVERSARIAL_KEEP if keep is None else keep
    adversarial = [r["question_id"] for r in rows if r["category"] == "adversarial"]
    if keep > len(adversarial):
        raise ValueError(f"asked to keep {keep} adversarial rows but only {len(adversarial)} exist")
    return set(sorted(adversarial, key=_mix_order_key)[:keep])


def _counts(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r["category"]] = out.get(r["category"], 0) + 1
    return dict(sorted(out.items()))


def _refused(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        if r.get("refused"):
            out[r["category"]] = out.get(r["category"], 0) + 1
    return dict(sorted(out.items()))


def derive_iters(train_rows: int, epochs: int = config.TRAIN_EPOCHS) -> int:
    """iters = epochs x rows, because batch_size is 1 so one iteration consumes
    exactly one example.

    The matched variable across v1 and v2 is the RECIPE, not the iteration count.
    Holding iters at v1's 2,956 over a smaller split would give v2 more passes
    over less data, which confounds the data mix with overfitting -- the two
    explanations the experiment exists to tell apart.
    """
    return epochs * train_rows


def build_v2() -> dict[str, Any]:
    v1_dir = config.DATASET_DIR
    v2_dir = config.DATASET_V2_DIR

    v1_manifest = json.loads(config.DATASET_MANIFEST_FILE.read_text())

    for split in SPLITS:
        path = v1_dir / f"{split}.jsonl"
        if not path.exists():
            raise RuntimeError(
                f"{path} is missing. v2 is derived from v1's split, so rebuild it first:\n"
                "  uv run evalgate-training dataset restore --target training/artifacts/dataset\n"
                "then confirm with `dataset verify`."
            )

    # v1's own files must match the committed manifest before anything is derived
    # from them. Deriving v2 from a drifted v1 would produce a v2 that is
    # self-consistent and wrong.
    for split in SPLITS:
        actual = file_digest(v1_dir / f"{split}.jsonl")
        expected = v1_manifest["sha256"][split]
        if actual != expected:
            raise RuntimeError(
                f"v1 {split}.jsonl does not match the committed manifest "
                f"({actual[:16]} != {expected[:16]}). Refusing to derive v2 from it."
            )

    train = _read_lines(v1_dir / "train.jsonl")
    kept_adversarial = select_kept_adversarial([r for _, r in train])

    kept: list[tuple[str, dict]] = []
    dropped: list[dict] = []
    for raw, row in train:
        if row["category"] != "adversarial" or row["question_id"] in kept_adversarial:
            kept.append((raw, row))
        else:
            dropped.append(row)

    v2_dir.mkdir(parents=True, exist_ok=True)

    # Train: the surviving raw lines, in the same question_id order v1 wrote. A
    # subset of a sorted sequence, kept sorted.
    with (v2_dir / "train.jsonl").open("w") as fh:
        for raw, _ in sorted(kept, key=lambda t: t[1]["question_id"]):
            fh.write(raw + "\n")

    # valid and test: byte copies, then proved byte-identical below.
    for split in ("valid", "test"):
        shutil.copyfile(v1_dir / f"{split}.jsonl", v2_dir / f"{split}.jsonl")

    digests = {s: file_digest(v2_dir / f"{s}.jsonl") for s in SPLITS}

    # THE assertion this module exists for. Not a warning, not a report line.
    for split in ("valid", "test"):
        if digests[split] != v1_manifest["sha256"][split]:
            raise RuntimeError(
                f"v2 {split}.jsonl is not byte-identical to v1's. "
                "Only train may differ between versions; both models must be scored "
                "on the same rows or the comparison means nothing."
            )

    kept_rows = [r for _, r in kept]
    v1_train_ids = {r["question_id"] for _, r in train}
    v2_train_ids = {r["question_id"] for r in kept_rows}
    if not v2_train_ids < v1_train_ids:
        raise RuntimeError("v2 train is not a strict subset of v1 train")

    golden = set(json.loads(config.GOLDEN_IDS_FILE.read_text())["ids"])
    if v2_train_ids & golden:
        raise RuntimeError(f"{len(v2_train_ids & golden)} golden ids leaked into v2 train")

    v1_counts = _counts([r for _, r in train])
    v2_counts = _counts(kept_rows)
    iters = derive_iters(len(kept_rows))

    manifest = {
        "variant": "v2",
        "derived_from": {
            "dir": str(v1_dir),
            "manifest": str(config.DATASET_MANIFEST_FILE),
            "sha256": v1_manifest["sha256"],
        },
        "mix": {
            "lever": "adversarial rows removed from train; nothing else changed",
            "adversarial_keep": config.DATASET_V2_ADVERSARIAL_KEEP,
            "adversarial_dropped": len(dropped),
            "mix_seed": config.DATASET_V2_MIX_SEED,
        },
        "totals": {
            "train": len(kept_rows),
            "valid": len(_read_lines(v2_dir / "valid.jsonl")),
            "test": len(_read_lines(v2_dir / "test.jsonl")),
        },
        "sha256": digests,
        "valid_test_identical_to_v1": True,
        "by_category": {
            "v1_train": v1_counts,
            "v2_train": v2_counts,
            "delta": {c: v2_counts.get(c, 0) - v1_counts.get(c, 0) for c in sorted(v1_counts)},
        },
        "shares": {
            "v1_train": {c: round(100 * n / len(train), 1) for c, n in v1_counts.items()},
            "v2_train": {c: round(100 * n / len(kept_rows), 1) for c, n in v2_counts.items()},
        },
        "refusal_rows": {
            "v1_train": {
                "by_category": _refused([r for _, r in train]),
                "total": sum(1 for _, r in train if r.get("refused")),
                "of": len(train),
            },
            "v2_train": {
                "by_category": _refused(kept_rows),
                "total": sum(1 for r in kept_rows if r.get("refused")),
                "of": len(kept_rows),
            },
        },
        "schedule": {
            "epochs": config.TRAIN_EPOCHS,
            "batch_size": config.TRAIN_PROBE["batch_size"],
            "grad_accumulation_steps": config.TRAIN_PROBE["grad_accumulation_steps"],
            "iters": iters,
            "optimizer_steps": iters // config.TRAIN_PROBE["grad_accumulation_steps"],
            "v1_iters": config.TRAIN_V1_ITERS,
            "derivation": (
                "iters = epochs x train_rows at batch_size 1. The matched variable is "
                "the recipe, not the iteration count: holding v1's 2,956 over a smaller "
                "split would give v2 more passes over less data and confound the mix "
                "with overfitting."
            ),
        },
        "kept_adversarial_ids": sorted(kept_adversarial),
        "dropped_adversarial_ids": sorted(r["question_id"] for r in dropped),
    }
    config.DATASET_V2_MANIFEST_FILE.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def verify_v2() -> dict[str, Any]:
    """Recompute v2's digests and re-check the v1 identity of valid and test.

    Separate from build so the claim survives a rebuild by someone else, and so CI
    can assert it from the two committed manifests without the gitignored files.
    """
    manifest = json.loads(config.DATASET_V2_MANIFEST_FILE.read_text())
    v1_manifest = json.loads(config.DATASET_MANIFEST_FILE.read_text())
    checks = []
    ok = True
    for split in SPLITS:
        path = config.DATASET_V2_DIR / f"{split}.jsonl"
        if not path.exists():
            checks.append({"split": split, "status": "missing", "ok": False})
            ok = False
            continue
        actual = file_digest(path)
        matches_v2 = actual == manifest["sha256"][split]
        row = {
            "split": split,
            "sha256": actual,
            "matches_v2_manifest": matches_v2,
            "ok": matches_v2,
        }
        if split in ("valid", "test"):
            row["matches_v1"] = actual == v1_manifest["sha256"][split]
            row["ok"] = matches_v2 and row["matches_v1"]
        ok = ok and row["ok"]
        checks.append(row)
    return {"ok": ok, "checks": checks, "manifest": manifest}


def format_v2(m: dict[str, Any]) -> str:
    lines = []
    v1c = m["by_category"]["v1_train"]
    v2c = m["by_category"]["v2_train"]
    v1s = m["shares"]["v1_train"]
    v2s = m["shares"]["v2_train"]
    lines.append(f"{'category':<13}{'v1':>7}{'share':>8}{'v2':>7}{'share':>8}{'delta':>8}")
    for c in config.CATEGORIES:
        lines.append(
            f"{c:<13}{v1c.get(c, 0):>7}{v1s.get(c, 0):>7.1f}%"
            f"{v2c.get(c, 0):>7}{v2s.get(c, 0):>7.1f}%"
            f"{m['by_category']['delta'].get(c, 0):>8}"
        )
    t1 = sum(v1c.values())
    t2 = sum(v2c.values())
    lines.append(f"{'TOTAL':<13}{t1:>7}{'':>8}{t2:>7}{'':>8}{t2 - t1:>8}")
    r1 = m["refusal_rows"]["v1_train"]
    r2 = m["refusal_rows"]["v2_train"]
    lines.append("")
    lines.append(
        f"refusal rows   v1 {r1['total']}/{r1['of']} ({100 * r1['total'] / r1['of']:.1f}%)"
        f"   ->   v2 {r2['total']}/{r2['of']} ({100 * r2['total'] / r2['of']:.1f}%)"
    )
    s = m["schedule"]
    lines.append("")
    lines.append(
        f"schedule       {s['epochs']} epochs x {t2} rows = {s['iters']} iters "
        f"({s['optimizer_steps']} optimizer steps at accum {s['grad_accumulation_steps']})"
    )
    lines.append(f"               v1 was {s['v1_iters']} iters over {t1} rows")
    lines.append("")
    for split in SPLITS:
        tag = "  <- byte-identical to v1" if split in ("valid", "test") else ""
        lines.append(f"  {split:<6} sha256 {m['sha256'][split][:16]}...{tag}")
    return "\n".join(lines)
