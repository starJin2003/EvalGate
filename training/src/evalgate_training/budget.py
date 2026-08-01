"""Cumulative spend ledger with a hard halt.

Every priced call goes through `Ledger.reserve()` before it is sent and
`Ledger.record()` after. Crossing either the USD or the token ceiling raises
`BudgetExceeded`, which no caller catches. The ledger is committed so actual
spend is evidence for DECISIONS.md rather than a claim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config


class BudgetExceeded(RuntimeError):
    """Raised when a call would push cumulative spend past a ceiling."""


@dataclass
class Ledger:
    path: Path = field(default_factory=lambda: config.LEDGER_FILE)
    usd_ceiling: float = field(default_factory=config.usd_ceiling)
    token_ceiling: int = field(default_factory=config.token_ceiling)
    entries: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.entries = data.get("entries", [])

    # --- totals --------------------------------------------------------------
    @property
    def total_usd(self) -> float:
        return sum(e["usd"] for e in self.entries)

    @property
    def total_tokens(self) -> int:
        return sum(e["prompt_tokens"] + e["completion_tokens"] for e in self.entries)

    def remaining_usd(self) -> float:
        return max(0.0, self.usd_ceiling - self.total_usd)

    # --- gates ---------------------------------------------------------------
    def reserve(self, stage: str, projected_usd: float, projected_tokens: int = 0) -> None:
        """Halt before sending if the projected call would cross a ceiling."""
        if self.total_usd + projected_usd > self.usd_ceiling:
            raise BudgetExceeded(
                f"{stage}: projected ${projected_usd:.4f} on top of spent "
                f"${self.total_usd:.4f} would cross the ${self.usd_ceiling:.2f} ceiling. "
                f"Raise OPENAI_USD_CEILING in .env only if you mean to."
            )
        if self.total_tokens + projected_tokens > self.token_ceiling:
            raise BudgetExceeded(
                f"{stage}: projected {projected_tokens:,} tokens on top of "
                f"{self.total_tokens:,} would cross the {self.token_ceiling:,} ceiling."
            )

    def record(
        self,
        stage: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        *,
        batch: bool,
        note: str = "",
    ) -> float:
        usd = config.cost_usd(model, prompt_tokens, completion_tokens, batch)
        self.entries.append(
            {
                "stage": stage,
                "model": model,
                "batch": batch,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "usd": usd,
                "note": note,
            }
        )
        self.flush()
        if self.total_usd > self.usd_ceiling:
            raise BudgetExceeded(
                f"Ceiling crossed after {stage}: spent ${self.total_usd:.4f} of "
                f"${self.usd_ceiling:.2f}. Execution halted."
            )
        return usd

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "usd_ceiling": self.usd_ceiling,
            "token_ceiling": self.token_ceiling,
            "total_usd": self.total_usd,
            "total_tokens": self.total_tokens,
            "pricing_checked": config.PRICING_CHECKED,
            "pricing_source": config.PRICING_SOURCE,
            "entries": self.entries,
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n")

    def summary(self) -> str:
        by_stage: dict[str, float] = {}
        for e in self.entries:
            by_stage[e["stage"]] = by_stage.get(e["stage"], 0.0) + e["usd"]
        lines = [
            f"Pricing checked {config.PRICING_CHECKED} against {config.PRICING_SOURCE}",
            f"Spent ${self.total_usd:.4f} of ${self.usd_ceiling:.2f} "
            f"({self.total_tokens:,} tokens)",
        ]
        lines.extend(f"  {stage:<22} ${usd:.4f}" for stage, usd in sorted(by_stage.items()))
        return "\n".join(lines)
