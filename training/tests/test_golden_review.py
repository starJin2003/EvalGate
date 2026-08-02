"""Selection determinism, the append-only verdict log, and the review UI."""

from __future__ import annotations

import json

import pytest

from evalgate_training import config
from evalgate_training.golden import review, review_server
from evalgate_training.golden.select import plan

REPOS = ("fastapi", "grafana", "prometheus", "pydantic")


def population(per_cell: int = 40) -> list[tuple[str, str, str]]:
    return [
        (f"{cat}-{repo}-{i:03d}", cat, repo)
        for cat in config.CATEGORIES
        for repo in REPOS
        for i in range(per_cell)
    ]


# --- selection ----------------------------------------------------------------
def test_plan_fills_every_cell_exactly():
    order, shortfalls, detail = plan(population())
    assert len(order) == 96
    assert len(set(order)) == 96
    assert shortfalls == []
    assert all(cell["selected"] == 6 for cell in detail.values())
    assert len(detail) == 16


def test_plan_is_deterministic_regardless_of_row_order():
    rows = population()
    first, _, _ = plan(rows)
    second, _, _ = plan(list(reversed(rows)))
    third, _, _ = plan(sorted(rows, key=lambda r: r[0][::-1]))
    assert first == second == third


def test_plan_backfills_a_short_cell_from_the_same_category():
    rows = [
        r
        for r in population()
        if not (r[1] == "howto" and r[2] == "pydantic" and r[0][-3:] > "001")
    ]
    order, shortfalls, detail = plan(rows)

    assert len(order) == 96
    assert len(shortfalls) == 1
    note = shortfalls[0]
    assert note["cell"] == "howto/pydantic"
    assert note["eligible"] == 2
    assert note["backfilled_from_category"] == 4
    assert note["still_short"] == 0
    # Backfill stays inside the category, never crosses into another one.
    assert all(qid.startswith("howto-") for qid in note["backfilled_ids"])
    # And it never double-books a row another cell already took.
    assert len(set(order)) == 96


def test_plan_reports_a_cell_it_cannot_fill():
    rows = [r for r in population() if r[1] != "comparison"]
    rows += [("comparison-fastapi-000", "comparison", "fastapi")]
    order, shortfalls, _ = plan(rows)
    comparison = [s for s in shortfalls if s["cell"].startswith("comparison/")]
    assert sum(s["still_short"] for s in comparison) == 23
    assert len(order) == 73


def test_plan_prefers_a_cells_own_rows_over_backfill():
    """A short cell must not steal rows a healthy cell still needs for its quota."""
    rows = [r for r in population(per_cell=6) if not (r[1] == "factual" and r[2] == "pydantic")]
    order, _, detail = plan(rows)
    for repo in ("fastapi", "grafana", "prometheus"):
        assert detail[f"factual/{repo}"]["selected"] == 6
    assert len(set(order)) == len(order)


# --- verdict log --------------------------------------------------------------
@pytest.fixture
def log(tmp_path):
    return tmp_path / "golden_review.jsonl"


def test_record_appends_and_last_write_wins(log):
    review.record("q1", "pass", path=log)
    review.record("q2", "fail", 2, "invented a flag", path=log)
    review.record("q1", "fail", 4, "cited C7, says nothing of the sort", path=log)

    assert len(log.read_text().splitlines()) == 3  # append-only, nothing overwritten

    current = review.load_judgments(log)
    assert current["q1"].verdict == "fail"
    assert current["q1"].failed_criterion == 4
    assert current["q1"].criterion_name == "citation accuracy"
    assert current["q2"].failed_criterion == 2

    # The superseded record survives in the log.
    records = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert records[0] == {**records[0], "question_id": "q1", "verdict": "pass"}


def test_pass_never_carries_a_criterion(log):
    judgment = review.record("q1", "pass", 3, path=log)
    assert judgment.failed_criterion is None


def test_fail_requires_a_criterion(log):
    with pytest.raises(ValueError, match="first failed criterion"):
        review.record("q1", "fail", None, path=log)
    with pytest.raises(ValueError, match="first failed criterion"):
        review.record("q1", "fail", 9, path=log)
    assert not log.exists()


def test_unknown_verdict_rejected(log):
    with pytest.raises(ValueError, match="verdict must be"):
        review.record("q1", "maybe", path=log)


def test_criteria_are_ordered_completeness_first():
    assert list(config.REVIEW_CRITERIA.items()) == [
        (1, "completeness"),
        (2, "groundedness"),
        (3, "refusal validity"),
        (4, "citation accuracy"),
    ]


# --- resume -------------------------------------------------------------------
def fake_case(qid: str, category: str = "factual", repo: str = "fastapi") -> review.Case:
    return review.Case(
        question_id=qid,
        category=category,
        repo=repo,
        question=f"question {qid}?",
        absent_symbol=None,
        answer=f"An answer [C1] for {qid}.",
        refused=False,
        context=[
            review.Chunk("C1", "chunk-a", repo, "Guide > Section", "https://example/a", "text a"),
            review.Chunk("C2", "chunk-b", repo, "Guide > Other", "https://example/b", "text b"),
        ],
    )


def test_first_unjudged_advances(log):
    cases = [fake_case(f"q{i}") for i in range(1, 5)]
    assert review.first_unjudged(cases, {}) == 1
    review.record("q1", "pass", path=log)
    assert review.first_unjudged(cases, review.load_judgments(log)) == 2
    review.record("q2", "fail", 1, path=log)
    review.record("q3", "pass", path=log)
    review.record("q4", "pass", path=log)
    assert review.first_unjudged(cases, review.load_judgments(log)) is None


def test_summary_counts(log):
    cases = [
        fake_case("q1", "factual", "fastapi"),
        fake_case("q2", "factual", "grafana"),
        fake_case("q3", "comparison", "grafana"),
        fake_case("q4", "adversarial", "pydantic"),
    ]
    review.record("q1", "pass", path=log)
    review.record("q2", "fail", 2, path=log)
    review.record("q3", "fail", 2, path=log)

    summary = review.summarize(cases, review.load_judgments(log))
    assert summary["judged"] == 3
    assert summary["unjudged"] == 1
    assert summary["pass_rate_pct"] == 33.3
    assert summary["by_category"]["factual"] == {
        "judged": 2,
        "passed": 1,
        "pass_rate_pct": 50.0,
    }
    assert summary["failures_by_first_failed_criterion"]["2 groundedness"] == 2
    assert summary["failures_by_first_failed_criterion"]["4 citation accuracy"] == 0
    assert "Pass rate" in review.format_summary(summary)


# --- rendering ----------------------------------------------------------------
def test_citation_markers_link_to_their_chunk():
    page = review_server.case_page(fake_case("q1"), 1, 96, None, 0)
    assert 'href="#chunk-C1"' in page
    assert 'id="chunk-C1"' in page
    assert "Case 1 of 96" in page
    # Both chunks are rendered whether or not the answer cited them.
    assert 'id="chunk-C2"' in page


def test_unknown_marker_is_shown_not_swallowed():
    case = review.Case(
        question_id="q1",
        category="factual",
        repo="fastapi",
        question="q?",
        absent_symbol=None,
        answer="Claim [C9].",
        refused=False,
        context=[review.Chunk("C1", "c", "fastapi", "h", "https://e/a", "body")],
    )
    page = review_server.case_page(case, 1, 96, None, 0)
    assert "cite unknown" in page
    assert "C9?" in page


def test_answer_text_is_escaped_not_interpreted():
    case = review.Case(
        question_id="q1",
        category="factual",
        repo="fastapi",
        question="<script>alert(1)</script>",
        absent_symbol=None,
        answer="a < b & c <img src=x> [C1]",
        refused=True,
        context=[review.Chunk("C1", "c", "fastapi", "h", "https://e/a", "<b>raw</b>")],
    )
    page = review_server.case_page(case, 3, 96, None, 2)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;img src=x&gt;" in page
    assert "<b>raw</b>" not in page
    assert "refused" in page


def test_chunks_are_never_truncated():
    long_body = "x" * 9000
    case = review.Case(
        question_id="q1",
        category="howto",
        repo="grafana",
        question="q?",
        absent_symbol=None,
        answer="ans [C1]",
        refused=False,
        context=[review.Chunk("C1", "c", "grafana", "h", "https://e/a", long_body)],
    )
    assert long_body in review_server.case_page(case, 1, 96, None, 0)


def test_existing_judgment_is_prefilled():
    judgment = review.Judgment("q1", "fail", 3, "refusal was wrong", "2026-08-01T00:00:00+00:00")
    page = review_server.case_page(fake_case("q1"), 2, 96, judgment, 1)
    assert 'value="3" checked' in page
    assert 'value="fail" checked' in page
    assert "refusal was wrong" in page
    assert "superseding record" in page


def test_criteria_render_in_order():
    page = review_server.case_page(fake_case("q1"), 1, 96, None, 0)
    positions = [page.index(name) for name in config.REVIEW_CRITERIA.values()]
    assert positions == sorted(positions)


def test_page_is_self_contained():
    page = review_server.case_page(fake_case("q1"), 1, 96, None, 0)
    for forbidden in ("http://cdn", "https://cdn", "<link", "unpkg", "googleapis"):
        assert forbidden not in page
