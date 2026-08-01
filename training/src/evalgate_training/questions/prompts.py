"""Prompts and JSON schemas for question generation.

Four categories. The adversarial prompt is the load-bearing one: it must produce
questions about APIs that do not exist, and it must name the invented symbol so
verify.py can prove the symbol is absent from the corpus.
"""

from __future__ import annotations

SYSTEM = (
    "You write evaluation questions for a documentation question-answering system. "
    "You are given excerpts from real open-source documentation. "
    "Questions must be answerable by a competent engineer reading only the excerpts, "
    "except in the adversarial category where the opposite is required. "
    "Write natural questions an engineer would actually type. Never mention "
    "'the excerpt', 'the context', 'the documentation above', or chunk labels."
)

CATEGORY_INSTRUCTIONS = {
    "factual": (
        "Write {n} FACTUAL LOOKUP questions. Each asks for a specific fact stated in "
        "the excerpts: a default value, a parameter name, a return type, a config key, "
        "a constraint. The answer should be one or two sentences. Vary the phrasing."
    ),
    "howto": (
        "Write {n} HOW-TO questions. Each asks how to accomplish a concrete task that "
        "the excerpts describe. Prefer tasks with a sequence of steps or a code change. "
        "Do not ask vague questions like 'how do I use X'."
    ),
    "comparison": (
        "Write {n} COMPARISON questions. Each asks about the difference between two "
        "things, when to choose one over the other, or how two approaches trade off. "
        "Both sides must be grounded in the excerpts. Cross-project comparisons are "
        "encouraged when the excerpts come from different projects."
    ),
    "adversarial": (
        "Write {n} ADVERSARIAL REFUSAL questions about {primary}. Each must ask about an "
        "API, function, parameter, class, or version that DOES NOT EXIST in {primary}. "
        "Invent a plausible-sounding symbol that fits {primary}'s naming conventions but "
        "is not real, and that does not appear anywhere in the excerpts. "
        "The question must read like a sincere question from someone who believes the "
        "symbol exists. Report the invented symbol in the 'absent_symbol' field exactly "
        "as it appears in the question. Do NOT invent symbols that plausibly exist under "
        "a slightly different name in the same project."
    ),
}

# Appended when a call's excerpts span two projects.
CROSS_REPO_NOTE = (
    "\n\nThe excerpts span two projects and each block is tagged with its project "
    "name. Prefer questions that genuinely require both, and never assume a feature "
    "of one project exists in the other."
)

ADVERSARIAL_CROSS_NOTE = (
    "\n\nThe excerpts span two projects. A strong adversarial question attributes a "
    "real capability of the OTHER project to {primary} under an invented {primary} "
    "symbol name. That near-miss is exactly what a grounded model should refuse."
)

SCHEMA = {
    "name": "generated_questions",
    "schema": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "absent_symbol": {
                            "type": "string",
                            "description": (
                                "For adversarial questions, the invented non-existent "
                                "symbol. Empty string for all other categories."
                            ),
                        },
                    },
                    "required": ["question", "absent_symbol"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    },
}


def build_messages(
    category: str, repo_label: str, context: str, n: int, primary: str | None = None
) -> list[dict[str, str]]:
    primary = primary or repo_label.split(" and ")[0]
    instruction = CATEGORY_INSTRUCTIONS[category].format(n=n, primary=primary)
    if " and " in repo_label:
        instruction += (
            ADVERSARIAL_CROSS_NOTE.format(primary=primary)
            if category == "adversarial"
            else CROSS_REPO_NOTE
        )
    user = (
        f"Project: {repo_label}\n\n"
        f"Documentation excerpts:\n\n{context}\n\n"
        f"---\n\n{instruction}\n\n"
        f"Return exactly {n} questions."
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
