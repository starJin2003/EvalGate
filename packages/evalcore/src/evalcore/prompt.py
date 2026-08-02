"""The grounded-QA prompt. One definition, used everywhere.

This string is a contract, not a formatting detail. The teacher was asked in this
exact shape, the student was trained on this exact shape, and the harness must
evaluate in this exact shape. A stray separator or a renamed field silently moves
every eval score with nothing raising anywhere -- the model simply sees a prompt
it was not trained on and does slightly worse, and the number looks like a real
regression.

It used to live in three places: `teacher.prompts` for the API calls,
`dataset.build.to_example` for the training rows, and it would have appeared a
third time in the P1.4 model client. All three agreed by hand and by luck. They
now all call in here, so agreement is structural rather than maintained.

`evalcore` owns it because `evalcore` owns `Case` and `ContextChunk`, and the
prompt is a pure function of those. `training` depends on `evalcore`; nothing
depends on `training`.

Two guards, because one is not enough:
  - `test_prompt.py` pins the rendered bytes against a committed fixture, so CI
    fails on any change to this file.
  - `dataset_manifest.json` pins a sha256 per split, so `dataset verify` fails if
    a change here would alter the committed training data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SYSTEM = """You answer questions using ONLY the numbered documentation excerpts provided.

Rules:
1. Every sentence that states a fact must end with one or more citation markers
   naming the excerpts it came from, like [C1] or [C2][C3]. A sentence with no
   citation is only allowed if it is a pure transition with no factual content.
2. Never use knowledge from outside the excerpts. If you know something the
   excerpts do not say, do not say it.
3. If the excerpts do not contain the answer, set "refused" to true and write a
   short refusal that names specifically what is missing and what the excerpts do
   cover instead. Do not guess, do not offer a plausible-sounding answer, and do
   not suggest the thing might exist under another name unless an excerpt says so.
4. If the question asks about an API, function, parameter, or version that does
   not appear anywhere in the excerpts, that is a refusal case. Say plainly that
   it does not appear in the documentation provided.
5. COMPARISONS. If the question compares two projects or two approaches but the
   excerpts only cover one side, you must say so explicitly and supply NOTHING
   about the absent side. Do not describe it, do not characterise it, do not say
   how it differs, and do not add a general remark about it from your own
   knowledge. Name the side the excerpts do cover, cite it, and state plainly
   that the other side does not appear in the documentation provided. Mentioning
   the absent project is only acceptable inside that statement of absence.
6. Keep answers under 150 words. Be direct. No preamble.

Set "citations" to the list of excerpt labels you actually cited, e.g. ["C1","C3"].
For a refusal, "citations" may be empty."""

# Between rendered chunks, and between the excerpt block and the question.
CONTEXT_SEPARATOR = "\n\n---\n\n"


def _field(chunk: Any, name: str) -> str:
    """Chunks arrive as `ContextChunk`, as a plain dict from the training export,
    or as a database row already mapped to a dict. Reading either shape here keeps
    the callers from each growing their own adapter."""
    if isinstance(chunk, Mapping):
        return str(chunk.get(name, ""))
    return str(getattr(chunk, name, ""))


def render_chunk_block(
    label: str, repo: str, heading_path: str, source_url: str, content: str
) -> str:
    """One excerpt as the model sees it. `label` is what a `[C1]` citation refers to."""
    return f"[{label}] repo={repo} | {heading_path}\nsource: {source_url}\n\n{content}"


def render_context(chunks: Sequence[Any]) -> str:
    return CONTEXT_SEPARATOR.join(
        render_chunk_block(
            _field(c, "label"),
            _field(c, "repo"),
            _field(c, "heading_path"),
            _field(c, "source_url"),
            _field(c, "content"),
        )
        for c in chunks
    )


def build_user_message(question: str, context: str) -> str:
    return f"Documentation excerpts:\n\n{context}{CONTEXT_SEPARATOR}Question: {question}"


def build_messages(question: str, chunks: Sequence[Any]) -> list[dict[str, str]]:
    """The full chat prompt: system turn plus the rendered user turn."""
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": build_user_message(question, render_context(chunks))},
    ]
