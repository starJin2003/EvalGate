"""P1.1 data pipeline for EvalGate.

Builds the grounded-documentation-QA dataset: parse markdown docs into chunks,
embed them into pgvector, generate categorized questions, attach retrieved context
through the same path the served model uses at eval time, and distill teacher
answers through the OpenAI Batch API.
"""

__version__ = "0.0.1"

__all__ = ["__version__"]
