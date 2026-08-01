"""The single retrieval path.

P1.1 calls this to attach context to every question. P1.4's harness calls the same
functions at eval time, so what the student model sees in training and what it sees
under evaluation come from identical code. Changing retrieval here changes it in
both places by construction.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from openai import OpenAI

from .. import config
from ..budget import Ledger


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    repo: str
    heading_path: str
    source_url: str
    content: str
    distance: float

    @property
    def citation_id(self) -> str:
        return self.chunk_id


def embed_query(client: OpenAI, text: str, ledger: Ledger | None = None) -> list[float]:
    resp = client.embeddings.create(model=config.EMBED_MODEL, input=text)
    if ledger is not None:
        ledger.record(
            "retrieval.embed_query",
            config.EMBED_MODEL,
            resp.usage.prompt_tokens,
            0,
            batch=False,
        )
    return resp.data[0].embedding


def retrieve(
    conn: psycopg.Connection,
    query_embedding: list[float],
    k: int = config.RETRIEVAL_K,
    repo: str | None = None,
) -> list[RetrievedChunk]:
    """Top-k by cosine distance. `repo` scopes the search when a question is known
    to belong to one project; leave it None for the realistic cross-corpus case."""
    sql = """
        SELECT chunk_id, repo, heading_path, source_url, content,
               embedding <=> %s::vector AS distance
        FROM chunks
        WHERE embedding IS NOT NULL
    """
    params: list[object] = [str(query_embedding)]
    if repo:
        sql += " AND repo = %s"
        params.append(repo)
    sql += " ORDER BY distance ASC LIMIT %s"
    params.append(k)

    rows = conn.execute(sql, params).fetchall()
    return [
        RetrievedChunk(
            chunk_id=r[0],
            repo=r[1],
            heading_path=r[2],
            source_url=r[3],
            content=r[4],
            distance=float(r[5]),
        )
        for r in rows
    ]


def format_context(chunks: list[RetrievedChunk]) -> str:
    """Render chunks as the labelled block both teacher and student receive.

    Labels are C1..Ck and are what citation markers refer to.
    """
    blocks = []
    for i, c in enumerate(chunks, start=1):
        blocks.append(
            f"[C{i}] repo={c.repo} | {c.heading_path}\nsource: {c.source_url}\n\n{c.content}"
        )
    return "\n\n---\n\n".join(blocks)
