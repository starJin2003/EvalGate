"""Two instruments, two configs. Deliberately not one.

The daily run and the PR gate answer different questions on different hardware at
different cadences, and giving them one config is how a threshold ends up sized
for neither.

    DAILY RUN      "did model quality change?"      96 cases, 2.90 h on the node
    PR GATE        "did this diff break the         no inference by default,
                    instrument, and is the           seconds
                    last measurement still good?"

WHY THE PR GATE DOES NOT RUN THE MODEL

A PR cannot change model weights. It can change the prompt renderer, the scorers,
the suite builder and the harness -- all deterministic, and all already pinned by
digests and unit tests. Re-running 96 cases per PR would spend 2.90 h measuring
something the PR almost never affects.

The exception is real and narrow: a change to `evalcore/prompt.py`, `scorers.py`
or the suite builder DOES move every score, and no digest can tell you by how
much. Those PRs escalate to a full run, as an async required check rather than a
blocking-in-five-minutes one.

WHY A SUBSET WAS REJECTED FOR THE PR GATE

Measured 2026-08-04, node at 108.89 s/case:

    cases   per cat   node time   quantum/cat
       96        24      2.90 h        0.0417
       48        12      1.45 h        0.0833
       24         6      44 min        0.1667
       12         3      22 min        0.3333

A gate anyone waits for needs <10 min, i.e. ~5 cases, where one case is a quarter
of a category. A subset does not trade accuracy for speed -- it destroys the
resolution the thresholds are written in. A fast subset can only detect binary
invariants (server down, empty outputs, no citation markers at all), which is
what the PR gate checks instead, without the model.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- schedule and staleness, chosen together ---------------------------------
#
# These three numbers are one decision. N is not a free parameter: a PR opened
# just before tonight's run must not be blocked for a result that is merely old
# rather than missing.
#
#     DAILY_SCHEDULE_UTC   08:00, a quiet hour on a node whose binding
#                          constraint is CPU and which also serves a public API.
#                          Moved from 03:00 UTC on 2026-08-05: that was 22:00 in
#                          the operator's America/Chicago, so a "nightly" batch
#                          ran every evening. 08:00 UTC is 03:00 Chicago. ONLY
#                          the instant moved -- the interval is still 24 h, so
#                          the two numbers below keep their derivations.
#     DAILY_DEADLINE_H     6 h. Measured full run is 2.90 h; the soak-derived
#                          projection under sustained load was 3.80 h. Sized on
#                          3.80, NOT on the 2.75 h a short burst suggested --
#                          the soak taught that lesson by dying at 11 of 12 cases
#                          against a deadline set from burst numbers.
#     MAX_DAILY_AGE_H      36 h.
#
# The relationship, which is the whole point:
#
#     floor(N) = interval (24 h) + worst-case duration (3.8 h) = 27.8 h
#
# Anything below 27.8 h marks a perfectly current result stale purely because
# tonight's run has not finished yet. 36 h leaves 8.2 h of slack over that floor,
# which buys exactly one property worth naming:
#
#     ONE missed or late nightly run does not block merges.
#     TWO consecutive missed runs do -- age passes 48 h and the gate fails.
#
# That is the intended trade. A single flaky night should not stop the team; a
# silently dead DAG should.
DAILY_SCHEDULE_UTC = "0 8 * * *"
DAILY_DEADLINE_H = 6
MAX_DAILY_AGE_H = 36

# Paths in the repo whose change moves every score, so a PR touching them cannot
# be gated on a stale daily run and must escalate to a full run.
SCORE_AFFECTING_PATHS = (
    "packages/evalcore/src/evalcore/prompt.py",
    "packages/evalcore/src/evalcore/scorers.py",
    "packages/evalcore/src/evalcore/loader.py",
)


@dataclass(frozen=True)
class PrGateConfig:
    """The PR gate has NO score thresholds. That is not an omission.

    Scores come from the daily run; this gate checks that the instrument is
    intact and that the last measurement is both passing and recent. Adding score
    thresholds here would require inference on every PR, at the resolution cost
    documented at the top of this module.
    """

    max_daily_age_h: int = MAX_DAILY_AGE_H
    require_daily_pass: bool = True
    score_affecting_paths: tuple[str, ...] = SCORE_AFFECTING_PATHS

    def stale(self, age_h: float) -> bool:
        return age_h > self.max_daily_age_h

    def escalates(self, changed_paths: list[str]) -> list[str]:
        """Which changed files force a full run. Returns them, so the gate comment
        can name the file rather than saying 'escalated'."""
        return sorted(p for p in changed_paths if p in self.score_affecting_paths)


PR_GATE = PrGateConfig()
