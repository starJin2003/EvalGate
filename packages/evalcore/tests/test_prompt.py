"""The prompt is a contract. These tests pin its bytes.

`evalcore.prompt` is the single renderer for the teacher API calls, the training
rows, and the eval client. That makes drift impossible by construction, but it
also means a careless edit here changes all three at once -- and the committed
training splits would no longer be reproducible from the committed corpus.

So the rendering is pinned twice over:
  - here, against literal expected strings, which fails in CI;
  - by `dataset_manifest.json`, whose per-split sha256 `dataset verify` recomputes
    from the real 1,758 rows, which fails locally.

If you are here because a test failed after editing `prompt.py`: that is the test
working. Changing the prompt means regenerating the training data and retraining.
"""

from __future__ import annotations

from evalcore.prompt import (
    CONTEXT_SEPARATOR,
    SYSTEM,
    build_messages,
    build_user_message,
    render_chunk_block,
    render_context,
)
from evalcore.schema import ContextChunk

CHUNKS = [
    ContextChunk(
        label="C1",
        chunk_id="aaaa000000000001",
        repo="fastapi",
        heading_path="Deploy > Docker",
        source_url="https://example.test/docker",
        content="Build an image.",
    ),
    ContextChunk(
        label="C2",
        chunk_id="aaaa000000000002",
        repo="grafana",
        heading_path="Alerting > Mute timings",
        source_url="https://example.test/mute",
        content="Mute timings suppress notifications.",
    ),
]

EXPECTED_CONTEXT = (
    "[C1] repo=fastapi | Deploy > Docker\n"
    "source: https://example.test/docker\n"
    "\n"
    "Build an image."
    "\n\n---\n\n"
    "[C2] repo=grafana | Alerting > Mute timings\n"
    "source: https://example.test/mute\n"
    "\n"
    "Mute timings suppress notifications."
)

EXPECTED_USER = (
    "Documentation excerpts:\n\n" + EXPECTED_CONTEXT + "\n\n---\n\nQuestion: How do I deploy?"
)


def test_separator_is_exactly_what_the_training_data_used() -> None:
    assert CONTEXT_SEPARATOR == "\n\n---\n\n"


def test_chunk_block_format_is_pinned() -> None:
    assert render_chunk_block("C1", "fastapi", "Deploy > Docker", "https://x", "body") == (
        "[C1] repo=fastapi | Deploy > Docker\nsource: https://x\n\nbody"
    )


def test_rendered_context_is_pinned() -> None:
    assert render_context(CHUNKS) == EXPECTED_CONTEXT


def test_user_message_is_pinned() -> None:
    assert build_user_message("How do I deploy?", EXPECTED_CONTEXT) == EXPECTED_USER


def test_build_messages_shape_and_bytes() -> None:
    messages = build_messages("How do I deploy?", CHUNKS)
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == SYSTEM
    assert messages[1]["content"] == EXPECTED_USER


def test_dicts_and_models_render_identically() -> None:
    """The training export passes plain dicts; the harness passes `ContextChunk`.
    If these ever diverge, training and eval prompts diverge with them."""
    as_dicts = [c.model_dump() for c in CHUNKS]
    assert render_context(as_dicts) == render_context(CHUNKS)


def test_system_prompt_still_states_the_rules_being_distilled() -> None:
    """Not a style check. Rules 1, 3 and 5 are what the two poisoning guards and
    criterion 3 of the hand review actually measure; deleting one silently
    invalidates the 96-case review as evidence about this prompt."""
    assert "citation markers" in SYSTEM
    assert '"refused" to true' in SYSTEM
    assert "COMPARISONS" in SYSTEM
    assert SYSTEM.count("\n1. ") == 1
