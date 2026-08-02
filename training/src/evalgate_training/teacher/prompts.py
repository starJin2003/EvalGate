"""Teacher system prompt and response schema.

Two behaviours are being distilled: cite every factual sentence, and refuse when
the context does not contain the answer. Both are enforced downstream by
validate.py, so a teacher that ignores the instruction produces a rejected row
rather than a silently bad training example.
"""

from __future__ import annotations

from evalcore.prompt import SYSTEM, build_user_message

__all__ = ["SYSTEM", "SCHEMA", "build_messages"]


def build_messages(question: str, context: str) -> list[dict[str, str]]:
    """Teacher-side prompt. Takes an already-rendered context string because
    `teacher.batch` needs the per-label metadata it computes while rendering."""
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": build_user_message(question, context)},
    ]


SCHEMA = {
    "name": "grounded_answer",
    "schema": {
        "type": "object",
        "properties": {
            "refused": {
                "type": "boolean",
                "description": "True when the excerpts do not contain the answer.",
            },
            "answer": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["refused", "answer", "citations"],
        "additionalProperties": False,
    },
}
