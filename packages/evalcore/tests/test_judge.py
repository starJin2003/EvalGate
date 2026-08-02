"""Judge client: cache correctness and backoff. Both are cost controls."""

from __future__ import annotations

from pathlib import Path

import pytest

from evalcore import JudgeCache, JudgeClient, JudgeProvider, RateLimited, ScorerKind, ScorerSpec
from evalcore.judge import StubJudge


class CountingJudge(JudgeProvider):
    name = "counting"
    version = "1"

    def __init__(self, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times

    def judge(self, prompt: str) -> tuple[float, str, int, int]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RateLimited("429")
        return 0.75, "ok", 10, 5


def spec() -> ScorerSpec:
    return ScorerSpec(kind=ScorerKind.judge)


def test_identical_case_and_output_hits_the_cache(suite, tmp_path: Path) -> None:
    provider = CountingJudge()
    cache = JudgeCache(tmp_path / "j.sqlite")
    client = JudgeClient(provider, cache)
    case = suite.cases[0]

    first = client.evaluate(case, "an answer [C1]", spec())
    second = client.evaluate(case, "an answer [C1]", spec())
    assert provider.calls == 1
    assert not first.cached and second.cached
    assert cache.stats()["hits"] == 1


def test_changed_output_misses_the_cache(suite, tmp_path: Path) -> None:
    provider = CountingJudge()
    client = JudgeClient(provider, JudgeCache(tmp_path / "j.sqlite"))
    case = suite.cases[0]
    client.evaluate(case, "answer one [C1]", spec())
    client.evaluate(case, "answer two [C1]", spec())
    assert provider.calls == 2


def test_changed_rubric_misses_the_cache(suite, tmp_path: Path) -> None:
    provider = CountingJudge()
    client = JudgeClient(provider, JudgeCache(tmp_path / "j.sqlite"))
    case = suite.cases[0]
    client.evaluate(case, "answer [C1]", ScorerSpec(kind=ScorerKind.judge, rubric="A"))
    client.evaluate(case, "answer [C1]", ScorerSpec(kind=ScorerKind.judge, rubric="B"))
    assert provider.calls == 2


def test_judge_version_invalidates_old_verdicts(suite, tmp_path: Path) -> None:
    """Changing the judge must not silently reuse verdicts from the old one."""
    path = tmp_path / "j.sqlite"
    case = suite.cases[0]

    v1 = CountingJudge()
    JudgeClient(v1, JudgeCache(path)).evaluate(case, "answer [C1]", spec())
    v2 = CountingJudge()
    v2.version = "2"
    JudgeClient(v2, JudgeCache(path)).evaluate(case, "answer [C1]", spec())
    assert v1.calls == 1 and v2.calls == 1


def test_cache_survives_a_new_process(suite, tmp_path: Path) -> None:
    path = tmp_path / "j.sqlite"
    case = suite.cases[0]
    first = CountingJudge()
    JudgeClient(first, JudgeCache(path)).evaluate(case, "answer [C1]", spec())
    second = CountingJudge()
    verdict = JudgeClient(second, JudgeCache(path)).evaluate(case, "answer [C1]", spec())
    assert second.calls == 0
    assert verdict.cached


def test_backoff_retries_then_succeeds(suite) -> None:
    provider = CountingJudge(fail_times=3)
    slept: list[float] = []
    client = JudgeClient(provider, None, base_delay=0.01, sleep=slept.append)
    verdict = client.evaluate(suite.cases[0], "answer [C1]", spec())
    assert verdict.score == 0.75
    assert provider.calls == 4
    assert len(slept) == 3


def test_backoff_gives_up_after_max_retries(suite) -> None:
    provider = CountingJudge(fail_times=99)
    client = JudgeClient(provider, None, max_retries=3, base_delay=0.01, sleep=lambda _: None)
    with pytest.raises(RateLimited):
        client.evaluate(suite.cases[0], "answer [C1]", spec())
    assert provider.calls == 3


def test_delays_grow_and_are_jittered(suite) -> None:
    provider = CountingJudge(fail_times=4)
    slept: list[float] = []
    client = JudgeClient(provider, None, base_delay=1.0, sleep=slept.append)
    client.evaluate(suite.cases[0], "answer [C1]", spec())
    # Full jitter, so each delay sits inside its own doubling window.
    for i, d in enumerate(slept):
        assert 0 <= d <= 1.0 * (2**i)
    assert max(slept) > min(slept)


def test_scores_are_clamped(suite) -> None:
    class Wild(JudgeProvider):
        name, version = "wild", "1"

        def judge(self, prompt):
            return 4.2, "over", 0, 0

    verdict = JudgeClient(Wild()).evaluate(suite.cases[0], "a", spec())
    assert verdict.score == 1.0


def test_stub_judge_rewards_citation_density(suite) -> None:
    client = JudgeClient(StubJudge())
    dense = client.evaluate(suite.cases[0], "a [C1] b [C2] c [C3]", spec())
    sparse = client.evaluate(suite.cases[0], "no citations at all", spec())
    assert dense.score > sparse.score
