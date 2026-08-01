"""Retrieval. One entry point, shared by dataset construction and eval-time serving."""

from .search import RetrievedChunk, embed_query, format_context, retrieve

__all__ = ["RetrievedChunk", "embed_query", "format_context", "retrieve"]
