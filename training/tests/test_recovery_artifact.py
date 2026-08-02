"""Guards on the committed recovery artifact.

`recovery.jsonl` is the only copy of $2.89 of paid state outside a Docker volume,
and it is committed, so CI can check it directly. These tests run unconditionally
for the same reason the manifest tests do: a skip here would mean the backup is
missing, which is the failure worth seeing.
"""

from __future__ import annotations

import json
import re

from evalgate_training import config
from evalgate_training.dataset.recovery import FIELDS

# Same sweep run against the corpus. It lives here rather than in a script because
# a corpus refresh regenerates this file, and the next placeholder credential in
# upstream documentation should fail CI rather than GitHub push protection.
SECRET_PATTERNS = {
    "grafana_service_account": r"glsa_[A-Za-z0-9]{20,}_[0-9a-f]{8}",
    "grafana_cloud": r"glc_[A-Za-z0-9+/=]{30,}",
    "aws_access_key_id": r"AKIA[0-9A-Z]{16}",
    "github_pat": r"gh[pousr]_[A-Za-z0-9]{36,}",
    "openai_key": r"sk-[A-Za-z0-9]{32,}",
    "slack_token": r"xox[baprs]-[A-Za-z0-9-]{10,}",
    "private_key_block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "jwt": r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}",
}


def _rows() -> list[dict]:
    return [
        json.loads(line) for line in config.RECOVERY_FILE.read_text().splitlines() if line.strip()
    ]


def test_recovery_covers_every_question() -> None:
    rows = _rows()
    assert len(rows) == 1900
    assert len({r["question_id"] for r in rows}) == 1900
    assert sum(1 for r in rows if r["valid"]) == 1854
    assert all(r["answer"] for r in rows), "a question lost its paid teacher answer"


def test_recovery_carries_exactly_the_paid_fields() -> None:
    """Chunk text must never appear here: it rebuilds for free, it is upstream
    documentation this repo does not own, and it is what carries placeholder
    credentials into the repo."""
    for row in _rows():
        assert set(row) == set(FIELDS)
        assert "content" not in row
        assert "context" not in row


def test_retrieved_chunk_ids_are_present_and_are_ids_not_text() -> None:
    """Which chunks a question retrieved is not reproducible from anything else --
    retrieval ran against embeddings that were paid for."""
    for row in _rows():
        assert row["retrieved"], f"{row['question_id']} lost its retrieval"
        for chunk_id in row["retrieved"]:
            assert re.fullmatch(r"[0-9a-f]{16}", chunk_id), chunk_id


def test_golden_96_are_identifiable_and_match_the_committed_ids() -> None:
    """The hand review is only reproducible if its 96 cases can be picked back out.
    Without this, restoring the data would restore an unreviewed dataset."""
    golden = [r for r in _rows() if r["split"] == "golden"]
    committed = json.loads(config.GOLDEN_IDS_FILE.read_text())["ids"]
    assert len(golden) == 96
    assert sorted(r["question_id"] for r in golden) == sorted(committed)
    assert all(r["valid"] for r in golden)


def test_review_verdicts_still_join_onto_the_restored_rows() -> None:
    judged = {
        json.loads(line)["question_id"]
        for line in config.GOLDEN_REVIEW_JSONL.read_text().splitlines()
        if line.strip()
    }
    ids = {r["question_id"] for r in _rows()}
    assert judged and not (judged - ids), "hand-review verdicts would orphan on restore"


def test_recovery_artifact_carries_no_credential_shaped_strings() -> None:
    blob = config.RECOVERY_FILE.read_text()
    for name, pattern in SECRET_PATTERNS.items():
        found = set(re.findall(pattern, blob))
        assert not found, f"{name} matched in recovery.jsonl: {len(found)} distinct"
