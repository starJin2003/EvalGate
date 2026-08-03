"""LoRA run with a durable, structured loss log.

`python -m mlx_lm lora` prints its losses and throws them away. That is fine for a
20-step smoke test and not fine for a 600-iteration probe whose entire purpose is
to be read the next morning, by someone who was not here, to decide whether 5e-5
is the right learning rate for two 9-hour runs.

So this wraps mlx-lm rather than replacing it. `mlx_lm.lora.run()` accepts a
`training_callback` argument and then immediately overwrites it with
`get_reporting_callbacks(args.report_to, ...)`, whose only supported backends are
external services (wandb, mlflow, tensorboard) -- a paid-tier question this
project does not need to open for a number that fits in a JSONL file. So this
mirrors `run()`'s ~20 lines and passes the callback to `train_model` directly,
calling mlx-lm's own `load`, `load_dataset` and `train_model` throughout. Nothing
about the training itself is reimplemented.

Every knob comes from `mlx_lm.lora.CONFIG_DEFAULTS` and is then overridden from
`config.TRAIN_PROBE`, so what is a default and what is a deliberate choice stays
legible in one place.
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .. import config


class JsonlLossLog:
    """Appends one JSON object per report. Flushed on every write, because the
    failure this guards against is the process dying at hour three."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.started = time.time()
        self.fh = self.path.open("a")

    def _write(self, kind: str, info: dict) -> None:
        row = {"kind": kind, "elapsed_s": round(time.time() - self.started, 1), **info}
        self.fh.write(json.dumps(row, sort_keys=True) + "\n")
        self.fh.flush()

    def on_train_loss_report(self, train_info: dict) -> None:
        self._write("train", train_info)

    def on_val_loss_report(self, val_info: dict) -> None:
        self._write("val", val_info)

    def close(self) -> None:
        self.fh.close()


def build_args(overrides: dict[str, Any]) -> SimpleNamespace:
    from mlx_lm.lora import CONFIG_DEFAULTS

    merged = dict(CONFIG_DEFAULTS)
    merged.update(overrides)
    return SimpleNamespace(**merged)


def run_probe(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    import numpy as np
    from mlx_lm import load
    from mlx_lm.lora import load_dataset, train_model

    settings = dict(config.TRAIN_PROBE)
    settings.update(overrides or {})
    log_path = Path(settings.pop("loss_log"))
    args = build_args(settings)

    np.random.seed(args.seed)
    logger = JsonlLossLog(log_path)
    logger._write(
        "config",
        {
            "model": args.model,
            "data": args.data,
            "iters": args.iters,
            "batch_size": args.batch_size,
            "grad_accumulation_steps": args.grad_accumulation_steps,
            "learning_rate": args.learning_rate,
            "lr_schedule": args.lr_schedule,
            "num_layers": args.num_layers,
            "lora_parameters": args.lora_parameters,
            "max_seq_length": args.max_seq_length,
            "grad_checkpoint": args.grad_checkpoint,
            "mask_prompt": args.mask_prompt,
            "save_every": args.save_every,
            "steps_per_eval": args.steps_per_eval,
            "val_batches": args.val_batches,
            "adapter_path": args.adapter_path,
        },
    )

    print(f"Structured loss log -> {log_path}")
    print("Loading pretrained model")
    model, tokenizer = load(args.model, tokenizer_config={"trust_remote_code": True})
    print("Loading datasets")
    train_set, valid_set, _test_set = load_dataset(args, tokenizer)
    print("Training")
    try:
        train_model(args, model, train_set, valid_set, logger)
    finally:
        logger.close()
    return {"loss_log": str(log_path), "adapter_path": args.adapter_path}


def eval_adapters(
    checkpoints: list[str] | None = None,
    adapter_path: str | None = None,
    progress_log: str | None = None,
) -> dict[str, Any]:
    """Loss for the untrained baseline and each saved checkpoint over the FULL
    valid split.

    Why this is not just a bigger version of the in-training eval: mlx-lm's
    `evaluate` calls `iterate_batches` with no `seed`, so batch *order* comes from
    the global NumPy RNG, which advances through every training and validation
    permutation. Consecutive in-training evals therefore score **different** 25-batch
    subsets, and the 200-vs-400 comparison was never paired. With `num_batches=-1`
    the whole split is consumed in one pass, and a token-weighted mean over every
    row is order-independent -- so these numbers are deterministic and comparable.

    The baseline applies LoRA and loads no adapter. LoRA's B matrix is zero-init, so
    that is exactly the base model, and it keeps every arm on one code path.
    """
    from mlx_lm import load
    from mlx_lm.lora import load_dataset
    from mlx_lm.tuner import linear_to_lora_layers
    from mlx_lm.tuner.datasets import CacheDataset
    from mlx_lm.tuner.trainer import default_loss, evaluate

    settings = dict(config.TRAIN_PROBE)
    settings.pop("loss_log")
    if adapter_path:
        settings["adapter_path"] = adapter_path
    args = build_args(settings)
    adapter_dir = Path(args.adapter_path)

    if checkpoints:
        names = checkpoints
    else:
        # Sorted numerically, not lexically: "0001000" before "0000200" is what a
        # plain string sort gives once the run is long enough to have five digits.
        numbered = sorted(
            adapter_dir.glob("[0-9]*_adapters.safetensors"),
            key=lambda p: int(p.name.split("_")[0]),
        )
        names = [p.name for p in numbered]
        # The final weights are a candidate the pre-committed rule names
        # explicitly, and the numbered glob does not match them — mlx-lm writes
        # them as a bare `adapters.safetensors`. Discovering only the numbered
        # files would silently drop one arm of the comparison, and the arm most
        # likely to be assumed the default at that.
        final = adapter_dir / "adapters.safetensors"
        if final.exists():
            names.append(final.name)

    model, tokenizer = load(args.model, tokenizer_config={"trust_remote_code": True})
    _train, valid_set, _test = load_dataset(args, tokenizer)
    valid = CacheDataset(valid_set)
    # Only the tokenizer was needed to build the dataset. Freed before scoring
    # rather than at the end, because `score()` holds its own copy of the model
    # and this one would otherwise sit resident for the whole multi-hour sweep.
    del model

    def score(adapter: str | None) -> float:
        m, _ = load(args.model, tokenizer_config={"trust_remote_code": True})
        m.freeze()
        linear_to_lora_layers(m, args.num_layers, args.lora_parameters)
        if adapter:
            m.load_weights(str(adapter_dir / adapter), strict=False)
        m.eval()
        return evaluate(
            model=m,
            dataset=valid,
            batch_size=args.batch_size,
            num_batches=-1,  # -1 consumes the whole split
            max_seq_length=args.max_seq_length,
            loss=default_loss,
        )

    # Scoring 16 arms over the full split is a multi-hour job, and holding every
    # result in memory until the last finishes means a crash at hour three loses
    # all of it. Each arm is flushed the moment it is computed.
    if progress_log:
        Path(progress_log).parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    results: dict[str, Any] = {
        "rows": len(valid_set),
        "adapter_path": str(adapter_dir),
        "losses": {},
    }

    with ExitStack() as stack:
        log_fh = stack.enter_context(Path(progress_log).open("a")) if progress_log else None

        def record(label: str, value: float) -> None:
            if log_fh:
                log_fh.write(
                    json.dumps(
                        {
                            "label": label,
                            "val_loss": value,
                            "elapsed_s": round(time.time() - t0, 1),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
                log_fh.flush()
            print(f"  {label:<10} {value:.4f}", flush=True)

        base = score(None)
        results["losses"]["baseline"] = base
        record("baseline", base)
        for name in names:
            label = (
                "final"
                if name == "adapters.safetensors"
                else (name.split("_")[0].lstrip("0") or "0")
            )
            value = score(name)
            results["losses"][label] = value
            record(label, value)

    results["wall_clock_s"] = round(time.time() - t0, 1)
    return results


def format_eval(r: dict[str, Any]) -> str:
    losses = r["losses"]
    base = losses["baseline"]
    out = [f"full valid split, {r['rows']} rows, num_batches=-1 (deterministic)", ""]
    out.append(f"{'checkpoint':<12}{'val_loss':>10}{'vs prev':>10}{'vs baseline':>13}")
    prev = None
    for name, v in losses.items():
        delta = "" if prev is None else f"{v - prev:+.4f}"
        drop = "" if name == "baseline" else f"{100 * (base - v) / base:.1f}%"
        out.append(f"{name:<12}{v:>10.4f}{delta:>10}{drop:>13}")
        prev = v
    return "\n".join(out)


def read_trajectory(path: Path | None = None) -> dict[str, Any]:
    """Read the probe log back without rerunning anything."""
    path = Path(path or config.TRAIN_PROBE["loss_log"])
    if not path.exists():
        raise RuntimeError(f"No loss log at {path}.")
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    val = [r for r in rows if r["kind"] == "val"]
    train = [r for r in rows if r["kind"] == "train"]
    # mlx-lm reports validation at `it - 1` and training at `it`, so the two series
    # never share an index. They are printed side by side rather than joined; a join
    # would silently drop every train row.
    return {
        "config": next((r for r in rows if r["kind"] == "config"), {}),
        "val": [(r["iteration"] + 1, round(r["val_loss"], 4)) for r in val],
        "train": [(r["iteration"], round(r["train_loss"], 4)) for r in train],
        "peak_memory_gb": max((r.get("peak_memory", 0) for r in train), default=0),
        "seconds_per_step": (
            round(1.0 / train[-1]["iterations_per_second"], 2)
            if train and train[-1].get("iterations_per_second")
            else None
        ),
        "wall_clock_s": round(rows[-1]["elapsed_s"], 1) if rows else 0,
    }


def format_trajectory(t: dict[str, Any]) -> str:
    out = ["validation (the number the pre-committed rule reads)", "  iter   val_loss"]
    for it, v in t["val"]:
        out.append(f"  {it:>4}   {v:>8.4f}")
    out.append("")
    out.append("training (noisier; batch 1 means each point is a handful of examples)")
    out.append("  iter   train_loss")
    for it, v in t["train"]:
        out.append(f"  {it:>4}   {v:>8.4f}")
    out.append("")
    out.append(
        f"peak memory {t['peak_memory_gb']:.3f} GB   "
        f"~{t['seconds_per_step']} s/step (excludes validation)   "
        f"wall clock {t['wall_clock_s'] / 60:.1f} min"
    )
    return "\n".join(out)
